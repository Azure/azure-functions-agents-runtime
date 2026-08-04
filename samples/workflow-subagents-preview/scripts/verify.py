"""Run the PR status sample end to end with local emulators.

The functions in this module intentionally keep process, storage, and assertion
logic separate so the same harness can move into ``tests/endtoend`` later.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import queue
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
from typing import NamedTuple

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient
from azure.storage.queue import QueueClient, TextBase64EncodePolicy

SAMPLE_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_SRC = SAMPLE_ROOT / "src"
QUEUE_NAME = "pr-status-requests"
CONTAINER_NAME = "workflow-reports"
BLOB_NAME = "reports/functions-pr-status.html"
TASK_HUB = "prstatusreports"
AZURITE_ACCOUNT = "devstoreaccount1"
AZURITE_KEY = (
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
    "APPLICATIONINSIGHTS_CONNECTION_STRING",
    "ENABLE_SENSITIVE_DATA",
)
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


class EmulatorCommands(NamedTuple):
    azurite: list[str]
    dts: list[str]


def build_request() -> dict[str, object]:
    """Return the deterministic request used by local and future CI verification."""
    return {
        "report_title": "Functions team PR status",
        "report_blob": BLOB_NAME,
        "pull_requests": [
            {
                "url": "https://github.com/Azure/azure-functions-host/pull/123",
                "last_checked_at": "2026-07-22T17:00:00Z",
            },
            {
                "url": "https://github.com/Azure/azure-functions-python-worker/pull/456",
                "last_checked_at": "2026-07-22T17:00:00Z",
            },
        ],
    }


def build_emulator_commands(run_id: str) -> EmulatorCommands:
    """Build isolated Docker commands with ephemeral host ports."""
    azurite_name = f"workflow-subagents-azurite-{run_id}"
    dts_name = f"workflow-subagents-dts-{run_id}"
    return EmulatorCommands(
        azurite=[
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
        ],
        dts=[
            "docker",
            "run",
            "--detach",
            "--rm",
            "--name",
            dts_name,
            "--env",
            f"DTS_TASK_HUB_NAMES={TASK_HUB}",
            "--publish",
            "127.0.0.1::8080",
            "--publish",
            "127.0.0.1::8082",
            "mcr.microsoft.com/dts/dts-emulator:latest",
        ],
    )


def validate_report(content: bytes, request: Mapping[str, object]) -> None:
    """Require an HTML report containing every requested pull-request URL."""
    html = content.decode("utf-8")
    if "<html" not in html.lower() or "</html>" not in html.lower():
        raise RuntimeError("generated report is not a complete HTML document")
    pull_requests = request.get("pull_requests")
    if not isinstance(pull_requests, list):
        raise RuntimeError("verification request has no pull_requests list")
    for item in pull_requests:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            raise RuntimeError("verification request contains an invalid pull request")
        if item["url"] not in html:
            raise RuntimeError(f"generated report is missing pull-request URL {item['url']!r}")


def _run(
    command: Sequence[str],
    *,
    timeout: float = 120.0,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=capture,
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
        raise RuntimeError(f"{settings_path} must contain a Values object")
    values = {str(key): str(value) for key, value in raw_values.items()}
    for key in PROVIDER_KEYS:
        env_value = (os.environ.get(key) or "").strip()
        if env_value:
            values[key] = env_value

    explicit = values.get("AZURE_FUNCTIONS_AGENTS_PROVIDER", "").strip().lower()
    if values.get("AZURE_OPENAI_ENDPOINT", "").strip():
        values["AZURE_FUNCTIONS_AGENTS_PROVIDER"] = explicit or "azure_openai"
        if not values.get("AZURE_OPENAI_DEPLOYMENT", "").strip():
            raise RuntimeError(
                "AZURE_OPENAI_DEPLOYMENT must name an existing deployment when "
                "AZURE_OPENAI_ENDPOINT is configured"
            )
    elif values.get("FOUNDRY_PROJECT_ENDPOINT", "").strip():
        values["AZURE_FUNCTIONS_AGENTS_PROVIDER"] = explicit or "foundry"
    elif values.get("OPENAI_API_KEY", "").strip():
        values["AZURE_FUNCTIONS_AGENTS_PROVIDER"] = explicit or "openai"
    else:
        raise RuntimeError(
            "no model provider is configured; set provider values in "
            "src/local.settings.json or the current environment"
        )
    return values


@contextlib.contextmanager
def _temporary_app(
    *,
    storage_connection: str,
    dts_port: int,
) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="workflow-subagents-") as temp:
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
                "PR_STATUS_REPORT_CONTAINER": CONTAINER_NAME,
            }
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
        self._lines: deque[str] = deque(maxlen=200)
        self._queue: queue.Queue[str | None] = queue.Queue()
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
        raise RuntimeError(f"Functions host was not ready within {timeout:.0f}s:\n{self.output_tail()}")

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
                        _run(
                            ["taskkill", "/PID", str(self._process.pid), "/T", "/F"],
                            timeout=15,
                        )
                    else:
                        os.killpg(self._process.pid, signal.SIGKILL)
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


def _prepare_storage(connection: str) -> tuple[QueueClient, object]:
    queue_client = QueueClient.from_connection_string(
        connection,
        QUEUE_NAME,
        message_encode_policy=TextBase64EncodePolicy(),
    )
    blob_service = BlobServiceClient.from_connection_string(connection)
    container = blob_service.get_container_client(CONTAINER_NAME)
    with contextlib.suppress(ResourceExistsError):
        queue_client.create_queue()
    queue_client.clear_messages()
    with contextlib.suppress(ResourceExistsError):
        container.create_container()
    with contextlib.suppress(ResourceNotFoundError):
        container.delete_blob(BLOB_NAME)
    return queue_client, container


def _wait_for_report(
    container: object,
    request: Mapping[str, object],
    *,
    timeout: float,
    previous_etag: str | None = None,
) -> tuple[str, bytes]:
    deadline = time.monotonic() + timeout
    blob = container.get_blob_client(BLOB_NAME)
    while time.monotonic() < deadline:
        try:
            properties = blob.get_blob_properties()
            etag = str(properties.etag)
            if previous_etag is None or etag != previous_etag:
                content = blob.download_blob().readall()
                validate_report(content, request)
                return etag, content
        except ResourceNotFoundError:
            pass
        time.sleep(2)
    raise RuntimeError(f"report Blob {BLOB_NAME!r} was not updated within {timeout:.0f}s")


def verify(*, timeout: float, keep_services: bool) -> None:
    """Run two requests and verify the stable report Blob is overwritten."""
    if shutil.which("docker") is None:
        raise RuntimeError("required executable 'docker' was not found on PATH")
    _run(["docker", "info"], timeout=30)

    run_id = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    commands = build_emulator_commands(run_id)
    created: list[str] = []
    request = build_request()
    try:
        for label, command in (("Azurite", commands.azurite), ("DTS", commands.dts)):
            print(f"Starting {label}...")
            _run(command, timeout=180)
            created.append(_container_name(command))

        azurite_name = _container_name(commands.azurite)
        dts_name = _container_name(commands.dts)
        blob_port = _mapped_port(azurite_name, 10000)
        queue_port = _mapped_port(azurite_name, 10001)
        table_port = _mapped_port(azurite_name, 10002)
        dts_port = _mapped_port(dts_name, 8080)
        dashboard_port = _mapped_port(dts_name, 8082)
        for port in (blob_port, queue_port, table_port, dts_port):
            _wait_for_port(port, timeout=timeout)

        connection = _azurite_connection(blob_port, queue_port, table_port)
        queue_client, container = _prepare_storage(connection)
        try:
            with _temporary_app(storage_connection=connection, dts_port=dts_port) as app_dir:
                print("Starting Functions host...")
                with _running_host(app_dir, timeout=timeout) as host:
                    payload = json.dumps(request, separators=(",", ":"))
                    print("Submitting first workflow request...")
                    queue_client.send_message(payload)
                    first_etag, first_content = _wait_for_report(
                        container, request, timeout=timeout
                    )
                    print(f"First report validated ({len(first_content):,} bytes).")

                    print("Submitting the same request again...")
                    queue_client.send_message(payload)
                    second_etag, second_content = _wait_for_report(
                        container,
                        request,
                        timeout=timeout,
                        previous_etag=first_etag,
                    )
                    names = [
                        item.name
                        for item in container.list_blobs(
                            name_starts_with="reports/functions-pr-status"
                        )
                    ]
                    if names != [BLOB_NAME]:
                        raise RuntimeError(
                            f"expected one stable report Blob {BLOB_NAME!r}; found {names!r}"
                        )
                    if first_etag == second_etag:
                        raise RuntimeError("report Blob ETag did not change after resubmission")
                    if any(marker in host.output_tail().lower() for marker in FAILURE_MARKERS):
                        raise RuntimeError(f"Functions host reported a failure:\n{host.output_tail()}")
                    print(
                        "PASS: two workflows produced one stable report Blob "
                        f"({len(second_content):,} bytes); ETag changed; DTS dashboard "
                        f"was available at http://127.0.0.1:{dashboard_port}."
                    )
        finally:
            queue_client.close()
            container.close()
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
        "--timeout",
        type=float,
        default=300,
        help="Seconds to wait for each service or report transition (default: 300).",
    )
    parser.add_argument(
        "--keep-services",
        action="store_true",
        help="Keep the uniquely named emulator containers after verification.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        verify(timeout=args.timeout, keep_services=args.keep_services)
    except (KeyboardInterrupt, RuntimeError) as exc:
        print(f"FAIL: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
