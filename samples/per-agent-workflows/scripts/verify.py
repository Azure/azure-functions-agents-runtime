"""Verify both Engineering Operations Hub workflow owners end to end."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, NamedTuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SAMPLE_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_SRC = SAMPLE_ROOT / "src"
REPO_ROOT = Path(__file__).resolve().parents[3]
TASK_HUB = "engineeringopshub"
SESSION_ID = "engineering-ops-shared-session"
AZURITE_ACCOUNT = "devstoreaccount1"
AZURITE_KEY = (
    # Azurite's documented public emulator key, not a real credential.
    "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/"
    "K1SZFPTOtr/KBHBeksoGMGw=="
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
READY_MARKERS = (
    "worker process started and initialized",
    "host started",
    "application started. press ctrl+c to shut down",
)
FAILURE_MARKERS = (
    "worker failed to index functions",
    "failed to index functions",
    "no job functions found",
    "a host error has occurred",
    "traceback (most recent call last)",
    "unhandled exception",
)
TERMINAL_STATUSES = frozenset({"Completed", "Failed", "Terminated", "Canceled"})
WORKFLOW_ID_RE = re.compile(
    r"\b[0-9a-f]{32}-[0-9a-f]{32}\b",
    re.IGNORECASE,
)

INCIDENT_PROMPT = (
    "Start exactly one incident workflow now for incident INC-4821 on checkout-api. "
    "Use parallel task IDs incident_logs, incident_metrics, and incident_deployments "
    "for the three incident evidence tools. Then use a sub_agent task named "
    "incident_analysis with incident_evidence_analyst and include all three whole "
    "results. Finish with incident_report using compile_incident_report and pass "
    "the incident ID, service, all whole evidence results, and the whole specialist "
    "result. Return the workflow ID without polling."
)
RELEASE_PROMPT = (
    "Start exactly one release-readiness workflow now for release REL-2026.08.11 "
    "on checkout-api. Use parallel task IDs release_prs, release_tests, "
    "release_vulnerabilities, and release_window for the four release evidence "
    "tools. Then use a sub_agent task named release_review with release_risk_reviewer "
    "and include all four whole results. Finish with release_dossier using "
    "compile_release_dossier and pass the release ID, service, all whole evidence "
    "results, and the whole specialist result. Return the workflow ID without polling."
)

Owner = Literal["incident_commander", "release_manager"]


class EmulatorCommands(NamedTuple):
    azurite: list[str]
    dts: list[str] | None


OWNER_EXPECTATIONS: dict[str, dict[str, object]] = {
    "incident_commander": {
        "marker": "INCIDENT_REPORT_READY",
        "report_type": "incident",
        "identity_key": "incident_id",
        "identity": "INC-4821",
        "decision": "ROLLBACK",
        "evidence": frozenset({
            "get_incident_logs",
            "get_incident_metrics",
            "get_incident_deployments",
        }),
        "required": frozenset({
            "get_incident_logs",
            "get_incident_metrics",
            "get_incident_deployments",
            "incident_evidence_analyst",
            "compile_incident_report",
        }),
        "allowed": frozenset({
            "get_incident_logs",
            "get_incident_metrics",
            "get_incident_deployments",
            "incident_evidence_analyst",
            "compile_incident_report",
        }),
    },
    "release_manager": {
        "marker": "RELEASE_DOSSIER_READY",
        "report_type": "release_readiness",
        "identity_key": "release_id",
        "identity": "REL-2026.08.11",
        "decision": "NO_GO",
        "evidence": frozenset({
            "get_release_pull_requests",
            "get_release_test_results",
            "get_release_vulnerabilities",
            "get_release_change_window",
        }),
        "required": frozenset({
            "get_release_pull_requests",
            "get_release_test_results",
            "get_release_vulnerabilities",
            "get_release_change_window",
            "release_risk_reviewer",
            "compile_release_dossier",
        }),
        "allowed": frozenset({
            "get_release_pull_requests",
            "get_release_test_results",
            "get_release_vulnerabilities",
            "get_release_change_window",
            "release_risk_reviewer",
            "compile_release_dossier",
        }),
    },
}


def build_emulator_commands(run_id: str, backend: str) -> EmulatorCommands:
    """Build uniquely named containers with Docker-assigned host ports."""
    if backend not in {"storage", "dts"}:
        raise ValueError(f"unsupported backend {backend!r}")
    azurite_name = f"engineering-ops-azurite-{run_id}"
    azurite = [
        "docker",
        "run",
        "--detach",
        "--rm",
        "--name",
        azurite_name,
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
    ]
    dts = None
    if backend == "dts":
        dts = [
            "docker",
            "run",
            "--detach",
            "--rm",
            "--name",
            f"engineering-ops-dts-{run_id}",
            "--env",
            f"DTS_TASK_HUB_NAMES={TASK_HUB}",
            "--publish",
            "127.0.0.1::8080",
            "--publish",
            "127.0.0.1::8082",
            "mcr.microsoft.com/dts/dts-emulator:latest",
        ]
    return EmulatorCommands(azurite=azurite, dts=dts)


def extract_workflow_id(payload: object) -> str:
    """Find a workflow ID in nested JSON, tool-result JSON strings, or prose."""
    if isinstance(payload, Mapping):
        direct = payload.get("workflow_id")
        if isinstance(direct, str) and WORKFLOW_ID_RE.fullmatch(direct):
            return direct
        for value in payload.values():
            with contextlib.suppress(RuntimeError):
                return extract_workflow_id(value)
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        for value in payload:
            with contextlib.suppress(RuntimeError):
                return extract_workflow_id(value)
    elif isinstance(payload, str):
        with contextlib.suppress(json.JSONDecodeError):
            parsed = json.loads(payload)
            if parsed != payload:
                return extract_workflow_id(parsed)
        match = WORKFLOW_ID_RE.search(payload)
        if match:
            return match.group(0)
    raise RuntimeError("chat response did not contain a valid workflow ID")


def _walk_values(value: object) -> Iterator[tuple[str, object]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key), item
            yield from _walk_values(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            yield from _walk_values(item)


def validate_terminal_result(owner: Owner, envelope: Mapping[str, object]) -> None:
    """Validate terminal success, deterministic output, and owner capabilities."""
    if envelope.get("runtime_status") != "Completed":
        raise RuntimeError(
            f"{owner} workflow ended as {envelope.get('runtime_status')!r}: "
            f"{envelope.get('output')!r}"
        )
    output = envelope.get("output")
    results = output.get("results") if isinstance(output, Mapping) else None
    if not isinstance(results, Mapping):
        raise RuntimeError(f"{owner} workflow output has no results object")

    expected = OWNER_EXPECTATIONS[owner]
    marker = expected["marker"]
    report = next(
        (
            value
            for value in results.values()
            if isinstance(value, Mapping) and value.get("marker") == marker
        ),
        None,
    )
    if not isinstance(report, Mapping):
        raise RuntimeError(f"{owner} output is missing terminal marker {marker}")
    for key, value in (
        ("report_type", expected["report_type"]),
        (str(expected["identity_key"]), expected["identity"]),
        ("service", "checkout-api"),
        ("decision", expected["decision"]),
    ):
        if report.get(key) != value:
            raise RuntimeError(f"{owner} terminal report has invalid {key!r}")

    known = set().union(
        *(set(item["allowed"]) for item in OWNER_EXPECTATIONS.values())  # type: ignore[arg-type]
    )
    used = {
        value
        for key, value in _walk_values(results)
        if key in {"capability", "agent"} and isinstance(value, str) and value in known
    }
    allowed = set(expected["allowed"])  # type: ignore[arg-type]
    unauthorized = used - allowed
    if unauthorized:
        raise RuntimeError(
            f"{owner} used unauthorized capabilities: {sorted(unauthorized)!r}"
        )
    missing = set(expected["required"]) - used  # type: ignore[arg-type]
    if missing:
        raise RuntimeError(f"{owner} did not use required capabilities: {sorted(missing)!r}")

    identity_key = str(expected["identity_key"])
    expected_identity = expected["identity"]
    evidence_capabilities = set(expected["evidence"])  # type: ignore[arg-type]
    for result in results.values():
        if not isinstance(result, Mapping):
            continue
        capability = result.get("capability")
        if capability not in evidence_capabilities:
            continue
        if (
            result.get(identity_key) != expected_identity
            or result.get("service") != "checkout-api"
        ):
            raise RuntimeError(
                f"{owner} evidence {capability!r} has an invalid identity or service"
            )


def validate_owner_list(
    payload: Mapping[str, object],
    own_workflow_id: str,
    other_workflow_id: str,
) -> None:
    workflows = payload.get("workflows")
    if not isinstance(workflows, list):
        raise RuntimeError("workflow list response has no workflows array")
    ids = {
        item.get("workflow_id")
        for item in workflows
        if isinstance(item, Mapping) and isinstance(item.get("workflow_id"), str)
    }
    if other_workflow_id in ids:
        raise RuntimeError("owner list exposed the other owner's workflow")
    if own_workflow_id not in ids:
        raise RuntimeError("owner list did not include its own workflow")


def _run(
    command: Sequence[str],
    *,
    timeout: float = 120,
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
        raise RuntimeError(f"required executable {command[0]!r} was not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError(f"{command[0]} command failed: {detail[-2000:]}") from exc


def _container_name(command: Sequence[str]) -> str:
    return command[command.index("--name") + 1]


def _mapped_port(container: str, container_port: int) -> int:
    output = _run(
        ["docker", "port", container, f"{container_port}/tcp"],
        timeout=30,
    ).stdout.strip()
    try:
        return int(output.rsplit(":", 1)[1])
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
    raise RuntimeError(f"service on localhost:{port} was not ready within {timeout:.0f}s")


def _azurite_connection(blob_port: int, queue_port: int, table_port: int) -> str:
    return (
        "DefaultEndpointsProtocol=http;"
        f"AccountName={AZURITE_ACCOUNT};"
        f"AccountKey={AZURITE_KEY};"
        f"BlobEndpoint=http://127.0.0.1:{blob_port}/{AZURITE_ACCOUNT};"
        f"QueueEndpoint=http://127.0.0.1:{queue_port}/{AZURITE_ACCOUNT};"
        f"TableEndpoint=http://127.0.0.1:{table_port}/{AZURITE_ACCOUNT};"
    )


def _provider_values() -> dict[str, str]:
    settings_path = SAMPLE_SRC / "local.settings.json"
    if not settings_path.exists():
        settings_path = SAMPLE_SRC / "local.settings.template.json"
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    raw_values = data.get("Values")
    if not isinstance(raw_values, dict):
        raise RuntimeError(f"{settings_path.name} must contain a Values object")
    values = {str(key): str(value) for key, value in raw_values.items()}
    for key in PROVIDER_KEYS:
        env_value = (os.environ.get(key) or "").strip()
        if env_value:
            values[key] = env_value

    configured = {
        "foundry": bool(values.get("FOUNDRY_PROJECT_ENDPOINT", "").strip()),
        "azure_openai": bool(values.get("AZURE_OPENAI_ENDPOINT", "").strip()),
        "openai": bool(values.get("OPENAI_API_KEY", "").strip()),
    }
    selected = [provider for provider, is_configured in configured.items() if is_configured]
    if not selected:
        raise RuntimeError(
            "no model provider is configured; set Foundry, Azure OpenAI, or OpenAI "
            "values in src/local.settings.json or the current environment"
        )
    if len(selected) > 1:
        raise RuntimeError(
            "multiple model providers are configured; populate settings for exactly "
            "one of Foundry, Azure OpenAI, or OpenAI"
        )

    provider = selected[0]
    required = {
        "foundry": ("FOUNDRY_MODEL",),
        "azure_openai": (
            "AZURE_OPENAI_DEPLOYMENT",
            "AZURE_OPENAI_API_VERSION",
        ),
        "openai": ("OPENAI_CHAT_MODEL_ID",),
    }
    missing = [key for key in required[provider] if not values.get(key, "").strip()]
    if missing:
        raise RuntimeError(
            f"{', '.join(missing)} must be configured for provider {provider}"
        )
    values["AZURE_FUNCTIONS_AGENTS_PROVIDER"] = provider
    return values


def build_host_environment() -> dict[str, str]:
    """Pin the Functions worker to this checkout while preserving caller paths."""
    environment = os.environ.copy()
    checkout_src = str((REPO_ROOT / "src").resolve())
    existing = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = (
        f"{checkout_src}{os.pathsep}{existing}" if existing else checkout_src
    )
    environment["AZURE_FUNCTIONS_AGENTS_EXPECTED_ROOT"] = checkout_src
    return environment


@contextlib.contextmanager
def _temporary_app(
    *,
    backend: str,
    storage_connection: str,
    dts_port: int | None,
) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix=".verify-work-", dir=SAMPLE_ROOT) as temp:
        app_dir = Path(temp) / "src"
        shutil.copytree(
            SAMPLE_SRC,
            app_dir,
            ignore=shutil.ignore_patterns(".venv", "local.settings.json", "__pycache__"),
        )
        if backend == "dts":
            shutil.copyfile(app_dir / "host.dts.json", app_dir / "host.json")
        values = _provider_values()
        values.update({
            "FUNCTIONS_WORKER_RUNTIME": "python",
            "AzureWebJobsStorage": storage_connection,
            "TASKHUB_NAME": TASK_HUB,
        })
        if dts_port is not None:
            values["DURABLE_TASK_SCHEDULER_CONNECTION_STRING"] = (
                f"Endpoint=http://127.0.0.1:{dts_port};Authentication=None"
            )
        (app_dir / "local.settings.json").write_text(
            json.dumps({"IsEncrypted": False, "Values": values}, indent=2) + "\n",
            encoding="utf-8",
        )
        yield app_dir


class _FunctionHost:
    def __init__(self, app_dir: Path) -> None:
        func = shutil.which("func")
        if func is None:
            raise RuntimeError("required executable 'func' was not found on PATH")
        self.port = _free_port()
        self._lines: deque[str] = deque(maxlen=300)
        self._queue: queue.Queue[str | None] = queue.Queue()
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self._process = subprocess.Popen(
            [func, "start", "--port", str(self.port)],
            cwd=app_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=build_host_environment(),
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _read(self) -> None:
        assert self._process.stdout is not None
        for line in self._process.stdout:
            self._queue.put(line)
        self._queue.put(None)

    def wait_ready(self, *, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        ready_at: float | None = None
        while time.monotonic() < deadline:
            if ready_at is not None and time.monotonic() - ready_at >= 2:
                return
            try:
                line = self._queue.get(timeout=0.5)
            except queue.Empty:
                if self._process.poll() is not None:
                    break
                continue
            if line is None:
                break
            self._lines.append(line)
            lowered = line.lower()
            if any(marker in lowered for marker in FAILURE_MARKERS):
                raise RuntimeError(f"Functions host startup failed:\n{self.output_tail()}")
            if ready_at is None and any(marker in lowered for marker in READY_MARKERS):
                ready_at = time.monotonic()
        raise RuntimeError(
            f"Functions host was not ready within {timeout:.0f}s:\n{self.output_tail()}"
        )

    def output_tail(self) -> str:
        while True:
            try:
                line = self._queue.get_nowait()
            except queue.Empty:
                break
            if line is not None:
                self._lines.append(line)
        return "".join(self._lines)

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
                    if os.name == "nt":
                        self._process.kill()
                    else:
                        os.killpg(self._process.pid, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self._process.wait(timeout=5)
        self._reader.join(timeout=5)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def _running_host(app_dir: Path, *, timeout: float) -> Iterator[_FunctionHost]:
    host = _FunctionHost(app_dir)
    try:
        host.wait_ready(timeout=timeout)
        yield host
    finally:
        host.stop()


def _request_json(
    method: str,
    url: str,
    *,
    timeout: float,
    payload: Mapping[str, object] | None = None,
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(
        url,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "x-ms-session-id": SESSION_ID,
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            status = response.status
            body = response.read()
    except HTTPError as exc:
        status = exc.code
        body = exc.read()
    except (TimeoutError, URLError) as exc:
        raise RuntimeError(f"{method} {url} failed: {exc}") from exc
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{method} {url} returned non-JSON status {status}") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError(f"{method} {url} returned a non-object JSON response")
    return status, decoded


def _start_owner(host: _FunctionHost, owner: Owner, prompt: str, *, timeout: float) -> str:
    status, payload = _request_json(
        "POST",
        f"{host.base_url}/agents/{owner}/chat",
        payload={"prompt": prompt},
        timeout=timeout,
    )
    if status != 200:
        raise RuntimeError(f"{owner} chat returned HTTP {status}: {payload!r}")
    return extract_workflow_id(payload)


def _poll_owner(
    host: _FunctionHost,
    owner: Owner,
    workflow_id: str,
    *,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    url = (
        f"{host.base_url}/agents/{owner}/workflow-status?"
        f"{urlencode({'workflow_id': workflow_id})}"
    )
    last_status = "not observed"
    while time.monotonic() < deadline:
        status, payload = _request_json("GET", url, timeout=min(30, timeout))
        if status == 200:
            last_status = str(payload.get("runtime_status"))
            if last_status in TERMINAL_STATUSES:
                return payload
        elif status != 404:
            raise RuntimeError(f"{owner} status route returned HTTP {status}: {payload!r}")
        time.sleep(2)
    raise RuntimeError(
        f"{owner} workflow {workflow_id} did not finish within {timeout:.0f}s "
        f"(last status: {last_status})"
    )


def _assert_http_isolation(
    host: _FunctionHost,
    owner: Owner,
    own_id: str,
    other_id: str,
    *,
    timeout: float,
) -> None:
    status_url = (
        f"{host.base_url}/agents/{owner}/workflow-status?"
        f"{urlencode({'workflow_id': other_id})}"
    )
    status, _ = _request_json("GET", status_url, timeout=timeout)
    if status != 404:
        raise RuntimeError(
            f"{owner} status route exposed the other owner with HTTP {status}"
        )
    status, payload = _request_json(
        "GET",
        f"{host.base_url}/agents/{owner}/workflows",
        timeout=timeout,
    )
    if status != 200:
        raise RuntimeError(f"{owner} list route returned HTTP {status}: {payload!r}")
    validate_owner_list(payload, own_id, other_id)


def verify(*, backend: str, timeout: float, keep_services: bool) -> None:
    """Run both owners with one session and prove result and route isolation."""
    if shutil.which("docker") is None:
        raise RuntimeError("required executable 'docker' was not found on PATH")
    if shutil.which("func") is None:
        raise RuntimeError("required executable 'func' was not found on PATH")
    _provider_values()
    _run(["docker", "info"], timeout=30)

    run_id = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    commands = build_emulator_commands(run_id, backend)
    created: list[str] = []
    dashboard_port: int | None = None
    try:
        services = [("Azurite", commands.azurite)]
        if commands.dts is not None:
            services.append(("DTS", commands.dts))
        for label, command in services:
            print(f"Starting isolated {label}...")
            _run(command, timeout=180)
            created.append(_container_name(command))

        azurite_name = _container_name(commands.azurite)
        blob_port = _mapped_port(azurite_name, 10000)
        queue_port = _mapped_port(azurite_name, 10001)
        table_port = _mapped_port(azurite_name, 10002)
        for port in (blob_port, queue_port, table_port):
            _wait_for_port(port, timeout=timeout)

        dts_port = None
        if commands.dts is not None:
            dts_name = _container_name(commands.dts)
            dts_port = _mapped_port(dts_name, 8080)
            dashboard_port = _mapped_port(dts_name, 8082)
            _wait_for_port(dts_port, timeout=timeout)

        storage = _azurite_connection(blob_port, queue_port, table_port)
        with _temporary_app(
            backend=backend,
            storage_connection=storage,
            dts_port=dts_port,
        ) as app_dir:
            print("Starting Functions host...")
            with _running_host(app_dir, timeout=timeout) as host:
                print("Starting incident and release workflows with one shared session...")
                incident_id = _start_owner(
                    host, "incident_commander", INCIDENT_PROMPT, timeout=timeout
                )
                release_id = _start_owner(
                    host, "release_manager", RELEASE_PROMPT, timeout=timeout
                )

                incident = _poll_owner(
                    host, "incident_commander", incident_id, timeout=timeout
                )
                release = _poll_owner(
                    host, "release_manager", release_id, timeout=timeout
                )
                validate_terminal_result("incident_commander", incident)
                validate_terminal_result("release_manager", release)
                _assert_http_isolation(
                    host,
                    "incident_commander",
                    incident_id,
                    release_id,
                    timeout=30,
                )
                _assert_http_isolation(
                    host,
                    "release_manager",
                    release_id,
                    incident_id,
                    timeout=30,
                )
                lowered = host.output_tail().lower()
                if any(marker in lowered for marker in FAILURE_MARKERS):
                    raise RuntimeError(
                        f"Functions host reported a failure:\n{host.output_tail()}"
                    )

        dashboard = (
            f" DTS dashboard: http://127.0.0.1:{dashboard_port}."
            if dashboard_port is not None
            else ""
        )
        print(
            "PASS: both owner workflows completed with isolated capabilities, "
            "cross-owner status returned 404, and lists remained private."
            f"{dashboard}"
        )
    finally:
        if keep_services and created:
            print(f"Keeping emulator containers: {', '.join(created)}")
        else:
            for name in reversed(created):
                with contextlib.suppress(RuntimeError):
                    _run(["docker", "rm", "--force", name], timeout=30)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backend",
        choices=("storage", "dts"),
        default="storage",
        help="Durable backend to verify (default: storage).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300,
        help="Seconds allowed for startup and each workflow (default: 300).",
    )
    parser.add_argument(
        "--keep-services",
        action="store_true",
        help="Keep uniquely named emulator containers for debugging.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        verify(
            backend=args.backend,
            timeout=args.timeout,
            keep_services=args.keep_services,
        )
    except (KeyboardInterrupt, RuntimeError, ValueError) as exc:
        print(f"FAIL: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
