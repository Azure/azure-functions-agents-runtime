from __future__ import annotations

import importlib
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections import deque
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, BinaryIO, Literal, cast
from urllib import error as urllib_error
from urllib import request as urllib_request

LOGGER = logging.getLogger(__name__)
AZURITE_PORTS = (10000, 10001, 10002)
RESPONSE_EXCERPT_LIMIT = 2048
LOG_CONTEXT_LINES = 4
PROCESS_LOG_HANDLES: dict[int, BinaryIO] = {}

type HeaderMap = dict[str, str]
type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass
class FuncProcess:
    """Handle to a running `func start` subprocess."""

    process: subprocess.Popen[bytes]
    port: int
    log_path: Path
    json_log_path: Path | None
    sample_path: Path


@dataclass
class InvocationResult:
    """Outcome of exercising one invocation."""

    function_name: str
    kind: str
    method: str
    path: str
    request_body: Any
    status_code: int | None
    response_headers: dict[str, str]
    response_excerpt: str
    duration_seconds: float
    success: bool
    error: str | None = None
    log_completion_lines: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class LogCompletionResult:
    """Outcome of waiting for an agent completion marker in the host log."""

    status: Literal["success", "failure"]
    matched_lines: list[str]


def start_azurite(workdir: Path, log_path: Path) -> subprocess.Popen[bytes]:
    """Start azurite as a background process bound to 127.0.0.1."""

    workdir.mkdir(parents=True, exist_ok=True)
    executable = _resolve_executable("azurite", "azurite.cmd")
    command = [
        executable,
        "--blobHost",
        "127.0.0.1",
        "--queueHost",
        "127.0.0.1",
        "--tableHost",
        "127.0.0.1",
        "--location",
        str(workdir),
    ]
    LOGGER.info("Starting Azurite: %s", command)
    return _launch_process(command=command, cwd=workdir, log_path=log_path, env=None)


def wait_for_azurite(timeout_seconds: int = 60) -> None:
    """Block until Azurite blob/queue/table ports are accepting connections."""

    deadline = time.monotonic() + timeout_seconds
    pending_ports = set(AZURITE_PORTS)
    while pending_ports and time.monotonic() < deadline:
        pending_ports = {port for port in pending_ports if not _is_port_open(port)}
        if not pending_ports:
            LOGGER.info("Azurite is ready on ports %s", sorted(AZURITE_PORTS))
            return
        time.sleep(0.5)
    raise TimeoutError(
        f"Timed out waiting for Azurite ports {sorted(pending_ports)} after {timeout_seconds}s."
    )


def start_func(
    *,
    sample_path: Path,
    port: int,
    log_path: Path,
    json_log_path: Path | None,
    extra_env: dict[str, str] | None = None,
) -> FuncProcess:
    """Launch `func start` in sample_path and redirect stdout/stderr to log_path."""

    executable = _resolve_executable("func", "func.cmd")
    command = [executable, "start", "--port", str(port)]
    if json_log_path is not None:
        json_log_path.parent.mkdir(parents=True, exist_ok=True)
        command.extend(
            ["--enable-json-output", "--json-output-file", str(json_log_path)]
        )

    env = os.environ.copy()
    if extra_env is not None:
        env.update(extra_env)

    LOGGER.info("Starting Functions host on port %s: %s", port, command)
    process = _launch_process(
        command=command,
        cwd=sample_path,
        log_path=log_path,
        env=env,
    )
    return FuncProcess(
        process=process,
        port=port,
        log_path=log_path,
        json_log_path=json_log_path,
        sample_path=sample_path,
    )


def wait_for_host_ready(
    *,
    port: int,
    expected_function_names: frozenset[str],
    timeout_seconds: int = 180,
    poll_interval: float = 2.0,
) -> dict[str, Any]:
    """Wait for the Functions host to reach Running state and register all functions."""

    deadline = time.monotonic() + timeout_seconds
    status_url = _build_url(port, "/admin/host/status")
    functions_url = _build_url(port, "/admin/functions")
    last_status_observation: str = "<none>"
    last_functions_observation: str = "<none>"

    while time.monotonic() < deadline:
        try:
            status_code, _, response_text, payload = _http_request_json(
                method="GET",
                url=status_url,
                body=None,
                headers=None,
                timeout_seconds=min(poll_interval, _remaining_seconds(deadline)),
            )
            last_status_observation = _describe_response(status_code, response_text, payload)
            if status_code < 500 and isinstance(payload, dict) and payload.get("state") == "Running":
                break
        except _retryable_http_exceptions() as exc:
            last_status_observation = f"{type(exc).__name__}: {exc}"
        time.sleep(poll_interval)
    else:
        raise TimeoutError(
            "Timed out waiting for host Running state. "
            f"Last /admin/host/status observation: {last_status_observation}"
        )

    while time.monotonic() < deadline:
        try:
            status_code, _, response_text, payload = _http_request_json(
                method="GET",
                url=functions_url,
                body=None,
                headers=None,
                timeout_seconds=min(poll_interval, _remaining_seconds(deadline)),
            )
            last_functions_observation = _describe_response(
                status_code, response_text, payload
            )
            if status_code >= 500:
                time.sleep(poll_interval)
                continue

            function_entries = _extract_function_entries(payload)
            observed_names = {
                entry_name
                for entry in function_entries
                if (entry_name := _get_function_entry_name(entry)) is not None
            }
            if expected_function_names.issubset(observed_names):
                return _normalize_functions_payload(payload)
        except _retryable_http_exceptions() as exc:
            last_functions_observation = f"{type(exc).__name__}: {exc}"
        time.sleep(poll_interval)

    raise TimeoutError(
        "Timed out waiting for expected functions to register. "
        f"Expected: {sorted(expected_function_names)}. "
        f"Last /admin/functions observation: {last_functions_observation}"
    )


def stop_process(
    process: subprocess.Popen[bytes],
    *,
    name: str,
    term_timeout: float = 20.0,
) -> None:
    """Terminate a process, escalating to kill if it does not exit in time."""

    if process.poll() is not None:
        LOGGER.info("%s already exited with code %s", name, process.returncode)
        _close_process_log_handle(process)
        return

    LOGGER.info("Terminating %s (pid=%s)", name, process.pid)
    try:
        _terminate_process(process)
        process.wait(timeout=term_timeout)
        LOGGER.info("%s exited after terminate with code %s", name, process.returncode)
    except subprocess.TimeoutExpired:
        LOGGER.warning("%s did not exit within %.1fs; killing", name, term_timeout)
        if sys.platform == "win32":
            _kill_process_tree_windows(process)
        else:
            _kill_process(process)
        process.wait(timeout=term_timeout)
        LOGGER.info("%s exited after kill with code %s", name, process.returncode)
    finally:
        _close_process_log_handle(process)


def invoke_http(
    *,
    port: int,
    method: str,
    path: str,
    body: Any | None,
    headers: dict[str, str] | None,
    expected_status: tuple[int, ...],
    timeout_seconds: float = 600.0,
    function_name: str = "",
) -> InvocationResult:
    """Invoke an HTTP endpoint and return its outcome."""

    normalized_method = method.upper()
    normalized_path = _normalize_path(path)
    url = _build_url(port, normalized_path)
    start_time = time.monotonic()
    status_code, response_headers, response_text, _ = _http_request_json(
        method=normalized_method,
        url=url,
        body=body,
        headers=headers,
        timeout_seconds=timeout_seconds,
    )
    duration_seconds = time.monotonic() - start_time
    success = status_code in expected_status
    error = None
    if not success:
        error = f"Expected status {expected_status}, got {status_code}."
    return InvocationResult(
        function_name=function_name,
        kind="http",
        method=normalized_method,
        path=normalized_path,
        request_body=body,
        status_code=status_code,
        response_headers=response_headers,
        response_excerpt=_truncate_text(response_text),
        duration_seconds=duration_seconds,
        success=success,
        error=error,
    )


def invoke_http_sse(
    *,
    port: int,
    path: str,
    body: Any,
    headers: dict[str, str] | None,
    expected_status: tuple[int, ...],
    first_event_timeout: float = 60.0,
    function_name: str = "",
) -> InvocationResult:
    """Invoke an SSE endpoint and capture the first non-empty data frame."""

    normalized_path = _normalize_path(path)
    url = _build_url(port, normalized_path)
    start_time = time.monotonic()
    deadline = start_time + first_event_timeout
    response_headers: HeaderMap = {}
    status_code: int | None = None
    excerpt = ""
    success = False
    error: str | None = None

    requests_module = _get_requests_module()
    payload_bytes, request_headers = _prepare_request_body(body=body, headers=headers)

    if requests_module is not None:
        requests_api = cast(Any, requests_module)
        with requests_api.post(
            url,
            data=payload_bytes,
            headers=request_headers,
            stream=True,
            timeout=min(first_event_timeout, _remaining_seconds(deadline)),
        ) as response:
            status_code = int(response.status_code)
            response_headers = _coerce_headers(dict(response.headers.items()))
            if status_code in expected_status:
                for line in response.iter_lines(decode_unicode=True):
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            "Timed out waiting for the first non-empty SSE data frame."
                        )
                    if not line:
                        continue
                    if line.startswith("data:"):
                        payload = line[5:].strip()
                        if payload:
                            excerpt = _truncate_text(payload)
                            success = True
                            break
                if not success:
                    error = "SSE stream ended before the first non-empty data frame."
            else:
                excerpt = _truncate_text(response.text)
                error = f"Expected status {expected_status}, got {status_code}."
    else:
        request = urllib_request.Request(
            url,
            data=payload_bytes,
            headers=request_headers,
            method="POST",
        )
        try:
            with urllib_request.urlopen(
                request, timeout=min(first_event_timeout, _remaining_seconds(deadline))
            ) as response:
                status_code = int(response.getcode())
                response_headers = _coerce_headers(dict(response.headers.items()))
                if status_code in expected_status:
                    while True:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(
                                "Timed out waiting for the first non-empty SSE data frame."
                            )
                        raw_line = response.readline()
                        if not raw_line:
                            break
                        decoded_line = raw_line.decode("utf-8", errors="replace").strip()
                        if not decoded_line or not decoded_line.startswith("data:"):
                            continue
                        payload = decoded_line[5:].strip()
                        if payload:
                            excerpt = _truncate_text(payload)
                            success = True
                            break
                    if not success:
                        error = "SSE stream ended before the first non-empty data frame."
                else:
                    excerpt = _truncate_text(
                        response.read().decode("utf-8", errors="replace")
                    )
                    error = f"Expected status {expected_status}, got {status_code}."
        except urllib_error.HTTPError as exc:
            status_code = exc.code
            response_headers = _coerce_headers(dict(exc.headers.items()))
            excerpt = _truncate_text(exc.read().decode("utf-8", errors="replace"))
            error = f"Expected status {expected_status}, got {status_code}."

    duration_seconds = time.monotonic() - start_time
    return InvocationResult(
        function_name=function_name,
        kind="http_sse",
        method="POST",
        path=normalized_path,
        request_body=body,
        status_code=status_code,
        response_headers=response_headers,
        response_excerpt=excerpt,
        duration_seconds=duration_seconds,
        success=success,
        error=error,
    )


def invoke_admin_function(
    *,
    port: int,
    function_name: str,
    input_value: str = "",
    expected_status: tuple[int, ...] = (202,),
    timeout_seconds: float = 30.0,
) -> InvocationResult:
    """Invoke a function via the admin API."""

    result = invoke_http(
        port=port,
        method="POST",
        path=f"/admin/functions/{function_name}",
        body={"input": input_value},
        headers=None,
        expected_status=expected_status,
        timeout_seconds=timeout_seconds,
        function_name=function_name,
    )
    result.kind = "admin_function"
    return result


def invoke_mcp_webhook(
    *,
    port: int,
    method: str = "tools/list",
    request_id: str = "1",
    params: dict[str, Any] | None = None,
    expected_status: tuple[int, ...] = (200, 202),
    timeout_seconds: float = 60.0,
    function_name: str = "main_debug_mcp",
) -> InvocationResult:
    """Invoke the MCP webhook using a JSON-RPC envelope."""

    request_body = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params or {},
    }
    start_time = time.monotonic()
    status_code, response_headers, response_text, payload = _http_request_json(
        method="POST",
        url=_build_url(port, "/runtime/webhooks/mcp"),
        body=request_body,
        headers=None,
        timeout_seconds=timeout_seconds,
    )
    duration_seconds = time.monotonic() - start_time

    success = status_code in expected_status
    error: str | None = None
    result_excerpt_source: object | None = payload

    if not success:
        error = f"Expected status {expected_status}, got {status_code}."
    elif isinstance(payload, dict):
        json_rpc_error = payload.get("error")
        if json_rpc_error is not None:
            success = False
            error = _format_json_rpc_error(json_rpc_error)
        elif method == "tools/list":
            result_payload = payload.get("result")
            tools = result_payload.get("tools") if isinstance(result_payload, dict) else None
            if not isinstance(tools, list) or not tools:
                success = False
                error = "MCP tools/list returned no tools in result.tools."
            result_excerpt_source = result_payload
        else:
            result_excerpt_source = payload.get("result", payload)
    elif method == "tools/list":
        success = False
        error = "MCP tools/list did not return a JSON object with result.tools."
    else:
        result_excerpt_source = response_text

    result = InvocationResult(
        function_name=function_name,
        kind="mcp_webhook",
        method="POST",
        path="/runtime/webhooks/mcp",
        request_body=request_body,
        status_code=status_code,
        response_headers=response_headers,
        response_excerpt=_truncate_text(_serialize_excerpt(result_excerpt_source, response_text)),
        duration_seconds=duration_seconds,
        success=success,
        error=error,
    )
    return result


def wait_for_log_completion(
    *,
    log_path: Path,
    function_display_name: str,
    timeout_seconds: float = 300.0,
    poll_interval: float = 1.0,
) -> LogCompletionResult:
    """Tail *log_path* and return the completion status plus nearby context lines."""

    success_marker = f"Agent '{function_display_name}' response:"
    failure_marker = f"Agent '{function_display_name}' failed:"
    deadline = time.monotonic() + timeout_seconds
    recent_lines: deque[str] = deque(maxlen=LOG_CONTEXT_LINES)
    tail_lines: deque[str] = deque(maxlen=LOG_CONTEXT_LINES * 3)

    with log_path.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(0, os.SEEK_END)
        while time.monotonic() < deadline:
            saw_new_line = False
            while True:
                position = handle.tell()
                line = handle.readline()
                if not line:
                    handle.seek(position)
                    break

                saw_new_line = True
                stripped = line.rstrip("\r\n")
                recent_lines.append(stripped)
                tail_lines.append(stripped)

                if success_marker in stripped or failure_marker in stripped:
                    matched_lines = list(recent_lines)
                    matched_lines.extend(
                        _read_following_log_lines(handle, deadline=deadline, limit=LOG_CONTEXT_LINES)
                    )
                    return LogCompletionResult(
                        status="success" if success_marker in stripped else "failure",
                        matched_lines=matched_lines,
                    )

            if not saw_new_line:
                time.sleep(poll_interval)

    tail_excerpt = "\n".join(tail_lines) if tail_lines else "<no new log lines observed>"
    raise TimeoutError(
        "Timed out waiting for runtime completion log line. "
        f"Searched for {success_marker!r} or {failure_marker!r}. "
        f"Tail:\n{tail_excerpt}"
    )


def introspect_expected_functions(sample_path: Path) -> frozenset[str]:
    """Enumerate registered function names without starting the Functions host."""

    from azure_functions_agents import create_function_app

    app = create_function_app(sample_path)
    return frozenset(
        function_name
        for function in app.get_functions()
        if (function_name := function.get_function_name()) is not None
    )


def _build_url(port: int, path: str) -> str:
    return f"http://127.0.0.1:{port}{_normalize_path(path)}"


def _normalize_path(path: str) -> str:
    return path if path.startswith("/") else f"/{path}"


def _resolve_executable(primary: str, windows_fallback: str) -> str:
    resolved = shutil.which(primary) or shutil.which(windows_fallback)
    if resolved is None:
        raise FileNotFoundError(f"Could not find executable {primary!r} or {windows_fallback!r}.")
    return resolved


def _launch_process(
    *,
    command: list[str],
    cwd: Path,
    log_path: Path,
    env: dict[str, str] | None,
) -> subprocess.Popen[bytes]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("ab", buffering=0)
    creationflags = 0
    preexec_fn: Any | None = None
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        preexec_fn = os.setsid  # type: ignore[attr-defined]

    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
        preexec_fn=preexec_fn,
    )
    PROCESS_LOG_HANDLES[process.pid] = log_handle
    return process


def _is_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(1.0)
        return client.connect_ex(("127.0.0.1", port)) == 0


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if sys.platform == "win32":
        ctrl_break_event = getattr(signal, "CTRL_BREAK_EVENT", None)
        if ctrl_break_event is not None:
            with suppress(OSError):
                process.send_signal(ctrl_break_event)
        process.terminate()
        return
    os.killpg(process.pid, signal.SIGTERM)


def _kill_process(process: subprocess.Popen[bytes]) -> None:
    if sys.platform == "win32":
        process.kill()
        return
    os.killpg(process.pid, signal.SIGKILL)


def _kill_process_tree_windows(process: subprocess.Popen[bytes]) -> None:
    subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(process.pid)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _close_process_log_handle(process: subprocess.Popen[bytes]) -> None:
    log_handle = PROCESS_LOG_HANDLES.pop(process.pid, None)
    if log_handle is None:
        return
    log_handle.close()


def _retryable_http_exceptions() -> tuple[type[BaseException], ...]:
    exception_types: list[type[BaseException]] = [OSError, urllib_error.URLError]
    requests_module = _get_requests_module()
    if requests_module is not None:
        request_exception = getattr(requests_module, "RequestException", None)
        if isinstance(request_exception, type) and issubclass(request_exception, BaseException):
            exception_types.append(request_exception)
    return tuple(exception_types)


def _get_requests_module() -> ModuleType | None:
    try:
        return importlib.import_module("requests")
    except ImportError:
        return None


def _prepare_request_body(
    *,
    body: Any | None,
    headers: dict[str, str] | None,
) -> tuple[bytes | None, HeaderMap]:
    prepared_headers = _coerce_headers(headers or {})
    if body is None:
        return None, prepared_headers
    if isinstance(body, bytes):
        return body, prepared_headers
    if isinstance(body, str):
        return body.encode("utf-8"), prepared_headers
    if isinstance(body, (Mapping, list, tuple)):
        prepared_headers.setdefault("Content-Type", "application/json")
        return json.dumps(body).encode("utf-8"), prepared_headers
    return str(body).encode("utf-8"), prepared_headers


def _http_request_json(
    *,
    method: str,
    url: str,
    body: Any | None,
    headers: dict[str, str] | None,
    timeout_seconds: float,
) -> tuple[int, HeaderMap, str, object | None]:
    status_code, response_headers, response_bytes = _http_request_bytes(
        method=method,
        url=url,
        body=body,
        headers=headers,
        timeout_seconds=timeout_seconds,
    )
    response_text = response_bytes.decode("utf-8", errors="replace")
    payload = _parse_json_body(response_text)
    return status_code, response_headers, response_text, payload


def _http_request_bytes(
    *,
    method: str,
    url: str,
    body: Any | None,
    headers: dict[str, str] | None,
    timeout_seconds: float,
) -> tuple[int, HeaderMap, bytes]:
    payload_bytes, request_headers = _prepare_request_body(body=body, headers=headers)
    requests_module = _get_requests_module()
    if requests_module is not None:
        requests_api = cast(Any, requests_module)
        response = requests_api.request(
            method=method,
            url=url,
            data=payload_bytes,
            headers=request_headers,
            timeout=timeout_seconds,
        )
        try:
            return (
                int(response.status_code),
                _coerce_headers(dict(response.headers.items())),
                bytes(response.content),
            )
        finally:
            response.close()

    request = urllib_request.Request(
        url,
        data=payload_bytes,
        headers=request_headers,
        method=method,
    )
    try:
        with urllib_request.urlopen(request, timeout=timeout_seconds) as response:
            return (
                int(response.getcode()),
                _coerce_headers(dict(response.headers.items())),
                response.read(),
            )
    except urllib_error.HTTPError as exc:
        return (
            int(exc.code),
            _coerce_headers(dict(exc.headers.items())),
            exc.read(),
        )


def _parse_json_body(response_text: str) -> object | None:
    stripped = response_text.strip()
    if not stripped:
        return None
    try:
        return cast(object, json.loads(stripped))
    except json.JSONDecodeError:
        return None


def _describe_response(status_code: int, response_text: str, payload: object | None) -> str:
    if payload is not None:
        return f"{status_code}: {_truncate_text(json.dumps(payload, ensure_ascii=False, default=str))}"
    return f"{status_code}: {_truncate_text(response_text)}"


def _extract_function_entries(payload: object | None) -> list[object]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("functions", "value", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _get_function_entry_name(entry: object) -> str | None:
    if isinstance(entry, dict):
        name = entry.get("name")
        if isinstance(name, str):
            return name
    return None


def _normalize_functions_payload(payload: object | None) -> dict[str, Any]:
    if isinstance(payload, dict):
        return cast(dict[str, Any], payload)
    if isinstance(payload, list):
        return {"functions": payload}
    return {"payload": payload}


def _coerce_headers(headers: Mapping[str, object]) -> HeaderMap:
    return {str(key): str(value) for key, value in headers.items()}


def _truncate_text(text: str, limit: int = RESPONSE_EXCERPT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...(truncated)"


def _serialize_excerpt(payload: object | None, fallback_text: str) -> str:
    if payload is None:
        return fallback_text
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False, default=str)


def _format_json_rpc_error(error_payload: object) -> str:
    if isinstance(error_payload, dict):
        code = error_payload.get("code")
        message = error_payload.get("message")
        data = error_payload.get("data")
        details: list[str] = []
        if code is not None:
            details.append(f"code={code}")
        if isinstance(message, str) and message.strip():
            details.append(f"message={message}")
        if data is not None:
            details.append(f"data={json.dumps(data, ensure_ascii=False, default=str)}")
        return f"JSON-RPC error: {', '.join(details)}" if details else "JSON-RPC error"
    return f"JSON-RPC error: {error_payload}"


def _remaining_seconds(deadline: float) -> float:
    return max(deadline - time.monotonic(), 0.1)


def _read_following_log_lines(handle: Any, *, deadline: float, limit: int) -> list[str]:
    collected: list[str] = []
    while len(collected) < limit and time.monotonic() < deadline:
        position = handle.tell()
        line = handle.readline()
        if not line:
            handle.seek(position)
            if collected:
                break
            time.sleep(0.1)
            continue
        collected.append(line.rstrip("\r\n"))
    return collected


__all__ = [
    "FuncProcess",
    "InvocationResult",
    "LogCompletionResult",
    "introspect_expected_functions",
    "invoke_admin_function",
    "invoke_http",
    "invoke_http_sse",
    "invoke_mcp_webhook",
    "start_azurite",
    "start_func",
    "stop_process",
    "wait_for_azurite",
    "wait_for_host_ready",
    "wait_for_log_completion",
]
