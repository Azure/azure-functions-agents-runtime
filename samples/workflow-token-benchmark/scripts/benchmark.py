"""Run paired baseline and Dynamic Workflow token benchmark trials locally."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import signal
import socket
import statistics
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient
from azure.storage.queue import QueueClient, TextBase64EncodePolicy

SAMPLE_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_SRC = SAMPLE_ROOT / "src"
RESULTS_DIR = SAMPLE_ROOT / ".benchmark-results"
CONTAINER_NAME = "token-benchmark-reports"
TASK_HUB = "tokenbenchmark"
USAGE_PREFIX = "Agent token usage: "
READY_MARKERS = (
    "worker process started and initialized",
    "host started",
    "application started. press ctrl+c to shut down",
)
FAILURE_MARKERS = (
    "worker failed to index functions",
    "failed to index functions",
    "a host error has occurred",
    "traceback (most recent call last)",
    "unhandled exception",
)
PROVIDER_KEYS = (
    "AZURE_FUNCTIONS_AGENTS_PROVIDER",
    "AZURE_FUNCTIONS_AGENTS_MODEL",
    "FOUNDRY_PROJECT_ENDPOINT",
    "FOUNDRY_MODEL",
    "OPENAI_API_KEY",
    "OPENAI_CHAT_MODEL_ID",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_DEPLOYMENT",
    "AZURE_OPENAI_API_VERSION",
    "AZURE_CLIENT_ID",
)


class EmulatorCommands(NamedTuple):
    azurite: list[str]
    dts: list[str]


@dataclass(frozen=True)
class Usage:
    agent_name: str
    execution_role: str
    input_tokens: int
    output_tokens: int
    provider: str | None
    model: str | None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class ModeResult:
    mode: str
    agent_name: str
    elapsed_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    provider: str | None = None
    model: str | None = None
    report_blob: str | None = None
    report_json: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class PairResult:
    trial_id: str
    service_count: int
    evidence_lines: int
    execution_order: list[str]
    baseline: ModeResult
    workflow: ModeResult
    reports_equal: bool = False
    reduction: float | None = None


def parse_usage_line(line: str) -> Mapping[str, Any] | None:
    marker = line.find(USAGE_PREFIX)
    if marker < 0:
        return None
    raw = line[marker + len(USAGE_PREFIX) :].strip()
    try:
        payload, _ = json.JSONDecoder().raw_decode(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid token usage JSON: {raw!r}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("token usage payload must be a JSON object")
    return payload


def select_trial_usage(
    lines: Sequence[str],
    *,
    expected_agent: str,
    workflow_mode: bool,
) -> Usage:
    payloads = [payload for line in lines if (payload := parse_usage_line(line)) is not None]
    forbidden = [
        payload
        for payload in payloads
        if payload.get("execution_role") == "workflow_subagent"
    ]
    if workflow_mode and forbidden:
        raise RuntimeError(
            f"workflow trial emitted {len(forbidden)} forbidden workflow_subagent usage record(s)"
        )

    primaries = [
        payload
        for payload in payloads
        if payload.get("execution_role") == "primary"
        and payload.get("agent_name") == expected_agent
    ]
    other_primaries = [
        payload
        for payload in payloads
        if payload.get("execution_role") == "primary"
        and payload.get("agent_name") != expected_agent
    ]
    if other_primaries:
        names = sorted({str(payload.get("agent_name")) for payload in other_primaries})
        raise RuntimeError(f"trial interval contains primary usage for other agent(s): {names}")
    if len(primaries) != 1:
        raise RuntimeError(
            f"expected exactly one primary usage record for {expected_agent!r}; "
            f"found {len(primaries)}"
        )

    payload = primaries[0]
    input_tokens = payload.get("input_tokens")
    output_tokens = payload.get("output_tokens")
    if (
        not isinstance(input_tokens, int)
        or isinstance(input_tokens, bool)
        or input_tokens < 0
        or not isinstance(output_tokens, int)
        or isinstance(output_tokens, bool)
        or output_tokens < 0
    ):
        raise RuntimeError("provider did not report valid input and output token counts")
    return Usage(
        agent_name=expected_agent,
        execution_role="primary",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        provider=(
            str(payload["provider"]) if isinstance(payload.get("provider"), str) else None
        ),
        model=str(payload["model"]) if isinstance(payload.get("model"), str) else None,
    )


def build_emulator_commands(run_id: str) -> EmulatorCommands:
    return EmulatorCommands(
        azurite=[
            "docker",
            "run",
            "--detach",
            "--rm",
            "--name",
            f"token-benchmark-azurite-{run_id}",
            "--publish",
            "127.0.0.1::10000",
            "--publish",
            "127.0.0.1::10001",
            "--publish",
            "127.0.0.1::10002",
            "mcr.microsoft.com/azure-storage/azurite:latest",
            "azurite",
            "--silent",
            "--skipApiVersionCheck",
            "--blobHost",
            "0.0.0.0",
            "--queueHost",
            "0.0.0.0",
            "--tableHost",
            "0.0.0.0",
        ],
        dts=[
            "docker",
            "run",
            "--detach",
            "--rm",
            "--name",
            f"token-benchmark-dts-{run_id}",
            "--env",
            f"DTS_TASK_HUB_NAMES={TASK_HUB}",
            "--publish",
            "127.0.0.1::8080",
            "--publish",
            "127.0.0.1::8082",
            "mcr.microsoft.com/dts/dts-emulator:latest",
        ],
    )


def _run(
    command: Sequence[str],
    *,
    timeout: float = 120.0,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"required executable {command[0]!r} was not found") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError(f"{command[0]} failed: {detail[-2000:]}") from exc


def _container_name(command: Sequence[str]) -> str:
    return command[command.index("--name") + 1]


def _mapped_port(container: str, container_port: int) -> int:
    output = _run(["docker", "port", container, f"{container_port}/tcp"], timeout=30)
    try:
        return int(output.stdout.strip().rsplit(":", 1)[1])
    except (IndexError, ValueError) as exc:
        raise RuntimeError(
            f"could not determine mapped port {container_port} for {container}"
        ) from exc


def _wait_for_port(port: int, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.25)
    raise RuntimeError(f"service on localhost:{port} was not ready")


def _azurite_connection(blob_port: int, queue_port: int, table_port: int) -> str:
    account = "devstoreaccount1"
    key = (
        "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/"
        "K1SZFPTOtr/KBHBeksoGMGw=="
    )
    return (
        "DefaultEndpointsProtocol=http;"
        f"AccountName={account};AccountKey={key};"
        f"BlobEndpoint=http://127.0.0.1:{blob_port}/{account};"
        f"QueueEndpoint=http://127.0.0.1:{queue_port}/{account};"
        f"TableEndpoint=http://127.0.0.1:{table_port}/{account};"
    )


def _provider_values() -> dict[str, str]:
    settings_path = SAMPLE_SRC / "local.settings.json"
    if not settings_path.exists():
        raise RuntimeError(
            "src/local.settings.json is required; copy the template and configure a model"
        )
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    raw_values = data.get("Values")
    if not isinstance(raw_values, dict):
        raise RuntimeError("local.settings.json must contain a Values object")
    values = {str(key): str(value) for key, value in raw_values.items()}
    for key in PROVIDER_KEYS:
        if value := (os.environ.get(key) or "").strip():
            values[key] = value
    if not any(
        values.get(key, "").strip()
        for key in ("FOUNDRY_PROJECT_ENDPOINT", "AZURE_OPENAI_ENDPOINT", "OPENAI_API_KEY")
    ):
        raise RuntimeError("local.settings.json has no configured model provider endpoint")
    return values


@contextlib.contextmanager
def _temporary_app(
    *,
    storage_connection: str,
    dts_port: int,
) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="workflow-token-benchmark-") as temp:
        app_dir = Path(temp) / "src"
        shutil.copytree(
            SAMPLE_SRC,
            app_dir,
            ignore=shutil.ignore_patterns(".venv", "local.settings.json", "__pycache__"),
        )
        values = _provider_values()
        values.update(
            {
                "FUNCTIONS_WORKER_RUNTIME": "python",
                "AzureWebJobsStorage": storage_connection,
                "DURABLE_TASK_SCHEDULER_CONNECTION_STRING": (
                    f"Endpoint=http://127.0.0.1:{dts_port};Authentication=None"
                ),
                "TASKHUB_NAME": TASK_HUB,
                "TOKEN_BENCHMARK_CONTAINER": CONTAINER_NAME,
                "ENABLE_SENSITIVE_DATA": "false",
            }
        )
        (app_dir / "local.settings.json").write_text(
            json.dumps({"IsEncrypted": False, "Values": values}, indent=2) + "\n",
            encoding="utf-8",
        )
        yield app_dir


class FunctionHost:
    def __init__(self, app_dir: Path) -> None:
        func = shutil.which("func")
        if func is None:
            raise RuntimeError("required executable 'func' was not found")
        self._lines: list[str] = []
        self._condition = threading.Condition()
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self._process = subprocess.Popen(
            [func, "start", "--port", str(_free_port())],
            cwd=app_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self._process.stdout is not None
        for line in self._process.stdout:
            with self._condition:
                self._lines.append(line)
                self._condition.notify_all()

    def mark(self) -> int:
        with self._condition:
            return len(self._lines)

    def lines_since(self, mark: int) -> list[str]:
        with self._condition:
            return list(self._lines[mark:])

    def wait_ready(self, *, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        ready_at: float | None = None
        cursor = 0
        while time.monotonic() < deadline:
            with self._condition:
                if cursor >= len(self._lines):
                    self._condition.wait(timeout=0.5)
                new_lines = self._lines[cursor:]
                cursor = len(self._lines)
            for line in new_lines:
                lowered = line.lower()
                if any(marker in lowered for marker in FAILURE_MARKERS):
                    raise RuntimeError(f"Functions host startup failed:\n{self.output_tail()}")
                if ready_at is None and any(marker in lowered for marker in READY_MARKERS):
                    ready_at = time.monotonic()
            if ready_at is not None and time.monotonic() - ready_at >= 2:
                return
            if self._process.poll() is not None:
                break
        raise RuntimeError(f"Functions host was not ready:\n{self.output_tail()}")

    def wait_for_usage(
        self,
        mark: int,
        *,
        expected_agent: str,
        workflow_mode: bool,
        timeout: float,
    ) -> Usage:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            lines = self.lines_since(mark)
            usage_count = sum(parse_usage_line(line) is not None for line in lines)
            if usage_count:
                time.sleep(0.5)
                return select_trial_usage(
                    self.lines_since(mark),
                    expected_agent=expected_agent,
                    workflow_mode=workflow_mode,
                )
            if self._process.poll() is not None:
                break
            with self._condition:
                self._condition.wait(timeout=0.25)
        raise RuntimeError(f"token usage record did not arrive for {expected_agent!r}")

    def output_tail(self, lines: int = 200) -> str:
        with self._condition:
            return "".join(self._lines[-lines:])

    def stop(self) -> None:
        if self._process.poll() is None:
            with contextlib.suppress(OSError):
                if os.name == "nt":
                    self._process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    os.killpg(self._process.pid, signal.SIGTERM)
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    self._process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self._process.wait(timeout=5)
        self._reader.join(timeout=5)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def _running_host(app_dir: Path, *, timeout: float) -> Iterator[FunctionHost]:
    host = FunctionHost(app_dir)
    try:
        host.wait_ready(timeout=timeout)
        yield host
    finally:
        host.stop()


def _queue(connection: str, name: str) -> QueueClient:
    client = QueueClient.from_connection_string(
        connection,
        name,
        message_encode_policy=TextBase64EncodePolicy(),
    )
    with contextlib.suppress(ResourceExistsError):
        client.create_queue()
    client.clear_messages()
    return client


def _wait_for_blob(
    container: Any,
    blob_name: str,
    *,
    timeout: float,
) -> tuple[dict[str, Any], bytes]:
    deadline = time.monotonic() + timeout
    blob = container.get_blob_client(blob_name)
    while time.monotonic() < deadline:
        try:
            content = blob.download_blob().readall()
            report = json.loads(content)
            if not isinstance(report, dict):
                raise RuntimeError(f"report {blob_name!r} is not a JSON object")
            return report, content
        except ResourceNotFoundError:
            time.sleep(0.5)
    raise RuntimeError(f"report Blob {blob_name!r} was not created")


def _run_mode(
    *,
    mode: str,
    request: dict[str, Any],
    queue_client: QueueClient,
    container: Any,
    host: FunctionHost,
    timeout: float,
) -> ModeResult:
    workflow_mode = mode == "workflow"
    agent_name = "workflow" if workflow_mode else "baseline"
    result = ModeResult(
        mode=mode,
        agent_name=agent_name,
        report_blob=str(request["report_blob"]),
    )
    queue_client.clear_messages()
    with contextlib.suppress(ResourceNotFoundError):
        container.delete_blob(result.report_blob)
    mark = host.mark()
    started = time.monotonic()
    try:
        queue_client.send_message(json.dumps(request, separators=(",", ":")))
        report, _ = _wait_for_blob(container, result.report_blob, timeout=timeout)
        usage = host.wait_for_usage(
            mark,
            expected_agent=agent_name,
            workflow_mode=workflow_mode,
            timeout=min(timeout, 30),
        )
        result.elapsed_ms = round((time.monotonic() - started) * 1000)
        result.input_tokens = usage.input_tokens
        result.output_tokens = usage.output_tokens
        result.total_tokens = usage.total_tokens
        result.provider = usage.provider
        result.model = usage.model
        result.report_json = report
    except Exception as exc:
        result.elapsed_ms = round((time.monotonic() - started) * 1000)
        result.error = str(exc)
    return result


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile requires at least one value")
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _print_summary(results: Sequence[PairResult]) -> None:
    print()
    print(
        "services  valid/pairs  baseline tokens  workflow tokens  "
        "median reduction  p25..p75"
    )
    for service_count in sorted({result.service_count for result in results}):
        group = [result for result in results if result.service_count == service_count]
        valid = [
            result
            for result in group
            if result.reports_equal and result.reduction is not None
        ]
        if not valid:
            print(f"{service_count:>8}  {0:>5}/{len(group):<5}  no valid pairs")
            continue
        baseline = [
            float(result.baseline.total_tokens)
            for result in valid
            if result.baseline.total_tokens is not None
        ]
        workflow = [
            float(result.workflow.total_tokens)
            for result in valid
            if result.workflow.total_tokens is not None
        ]
        reductions = [
            float(result.reduction) for result in valid if result.reduction is not None
        ]
        print(
            f"{service_count:>8}  {len(valid):>5}/{len(group):<5}  "
            f"{statistics.median(baseline):>15,.0f}  "
            f"{statistics.median(workflow):>15,.0f}  "
            f"{statistics.median(reductions):>15.1%}  "
            f"{_percentile(reductions, 0.25):.1%}.."
            f"{_percentile(reductions, 0.75):.1%}"
        )


def benchmark(
    *,
    service_counts: Sequence[int],
    repeats: int,
    evidence_lines: int,
    timeout: float,
    keep_services: bool,
) -> list[PairResult]:
    if shutil.which("docker") is None:
        raise RuntimeError("required executable 'docker' was not found")
    _run(["docker", "info"], timeout=30)
    run_id = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    commands = build_emulator_commands(run_id)
    created: list[str] = []
    results: list[PairResult] = []
    host_output = ""
    baseline_queue: QueueClient | None = None
    workflow_queue: QueueClient | None = None
    container: Any = None
    try:
        for label, command in (("Azurite", commands.azurite), ("DTS", commands.dts)):
            print(f"Starting {label}...")
            _run(command, timeout=180)
            created.append(_container_name(command))

        azurite = _container_name(commands.azurite)
        dts = _container_name(commands.dts)
        blob_port = _mapped_port(azurite, 10000)
        queue_port = _mapped_port(azurite, 10001)
        table_port = _mapped_port(azurite, 10002)
        dts_port = _mapped_port(dts, 8080)
        for port in (blob_port, queue_port, table_port, dts_port):
            _wait_for_port(port, timeout=timeout)

        connection = _azurite_connection(blob_port, queue_port, table_port)
        baseline_queue = _queue(connection, "token-benchmark-baseline")
        workflow_queue = _queue(connection, "token-benchmark-workflow")
        container = BlobServiceClient.from_connection_string(
            connection
        ).get_container_client(CONTAINER_NAME)
        with contextlib.suppress(ResourceExistsError):
            container.create_container()

        with _temporary_app(storage_connection=connection, dts_port=dts_port) as app_dir:
            print("Starting Functions host...")
            with _running_host(app_dir, timeout=timeout) as host:
                try:
                    pair_index = 0
                    for service_count in service_counts:
                        for repeat in range(1, repeats + 1):
                            trial_id = f"services-{service_count}-repeat-{repeat}"
                            services = [
                                f"checkout-api-{index:02d}"
                                for index in range(service_count)
                            ]
                            order = (
                                ["baseline", "workflow"]
                                if pair_index % 2 == 0
                                else ["workflow", "baseline"]
                            )
                            pair_index += 1
                            mode_results: dict[str, ModeResult] = {}
                            for mode in order:
                                print(f"Running {trial_id} {mode}...")
                                request = {
                                    "trial_id": trial_id,
                                    "services": services,
                                    "evidence_lines": evidence_lines,
                                    "report_blob": f"runs/{trial_id}/{mode}.json",
                                }
                                mode_results[mode] = _run_mode(
                                    mode=mode,
                                    request=request,
                                    queue_client=(
                                        workflow_queue
                                        if mode == "workflow"
                                        else baseline_queue
                                    ),
                                    container=container,
                                    host=host,
                                    timeout=timeout,
                                )
                            baseline = mode_results["baseline"]
                            workflow = mode_results["workflow"]
                            pair = PairResult(
                                trial_id=trial_id,
                                service_count=service_count,
                                evidence_lines=evidence_lines,
                                execution_order=order,
                                baseline=baseline,
                                workflow=workflow,
                            )
                            if (
                                baseline.error is None
                                and workflow.error is None
                                and baseline.report_json is not None
                                and workflow.report_json is not None
                            ):
                                pair.reports_equal = (
                                    json.dumps(
                                        baseline.report_json,
                                        ensure_ascii=True,
                                        separators=(",", ":"),
                                        sort_keys=True,
                                    )
                                    == json.dumps(
                                        workflow.report_json,
                                        ensure_ascii=True,
                                        separators=(",", ":"),
                                        sort_keys=True,
                                    )
                                )
                                if (
                                    pair.reports_equal
                                    and baseline.total_tokens is not None
                                    and baseline.total_tokens > 0
                                    and workflow.total_tokens is not None
                                ):
                                    pair.reduction = (
                                        1
                                        - workflow.total_tokens
                                        / baseline.total_tokens
                                    )
                            results.append(pair)
                            status = (
                                f"reduction={pair.reduction:.1%}"
                                if pair.reduction is not None
                                else "invalid pair"
                            )
                            print(f"Completed {trial_id}: {status}")

                    if any(
                        marker in host.output_tail().lower()
                        for marker in FAILURE_MARKERS
                    ):
                        print(
                            "Warning: host output contains a failure marker; "
                            "inspect the saved host log."
                        )
                finally:
                    host_output = host.output_tail(lines=1_000_000)
    finally:
        for client in (baseline_queue, workflow_queue, container):
            if client is not None:
                with contextlib.suppress(Exception):
                    client.close()
        if keep_services and created:
            print(f"Keeping emulator containers: {', '.join(created)}")
        else:
            for name in reversed(created):
                with contextlib.suppress(RuntimeError):
                    _run(["docker", "rm", "--force", name], timeout=30)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    result_path = RESULTS_DIR / f"benchmark-{timestamp}.json"
    result_path.write_text(
        json.dumps([asdict(result) for result in results], indent=2) + "\n",
        encoding="utf-8",
    )
    host_path = RESULTS_DIR / f"host-{timestamp}.log"
    host_path.write_text(host_output, encoding="utf-8")
    print(f"Raw results: {result_path}")
    print(f"Host log: {host_path}")
    _print_summary(results)
    return results


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--service-counts",
        type=int,
        nargs="+",
        default=[1, 3, 5, 10, 20],
    )
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--evidence-lines", type=int, default=40)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--keep-services", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.repeats < 1:
        print("FAIL: --repeats must be positive")
        return 1
    if any(count < 1 or count > 20 for count in args.service_counts):
        print("FAIL: service counts must be between 1 and 20")
        return 1
    if not 1 <= args.evidence_lines <= 200:
        print("FAIL: --evidence-lines must be between 1 and 200")
        return 1
    try:
        benchmark(
            service_counts=args.service_counts,
            repeats=args.repeats,
            evidence_lines=args.evidence_lines,
            timeout=args.timeout,
            keep_services=args.keep_services,
        )
    except (KeyboardInterrupt, RuntimeError) as exc:
        print(f"FAIL: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
