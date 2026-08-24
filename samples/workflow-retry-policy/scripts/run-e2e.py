"""Opt-in real-model E2E for the workflow retry-policy sample."""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

ENDPOINT_ENV = "AZURE_FUNCTIONS_AGENTS_SAMPLE_E2E_FOUNDRY_PROJECT_ENDPOINT"
MODEL_ENV = "AZURE_FUNCTIONS_AGENTS_SAMPLE_E2E_FOUNDRY_MODEL"
PROMPT = "Recover delayed order ORD-1001 and complete it safely."
TERMINAL_STATES = {"Completed", "Failed", "Canceled", "Terminated"}


def read_opt_in() -> tuple[str, str] | None:
    """Return Foundry E2E settings, or None when the test is not opted in."""
    endpoint = os.environ.get(ENDPOINT_ENV, "").strip()
    model = os.environ.get(MODEL_ENV, "").strip()
    if not endpoint and not model:
        return None
    if not endpoint or not model:
        missing = MODEL_ENV if endpoint else ENDPOINT_ENV
        raise RuntimeError(f"Both E2E variables are required; {missing} is missing.")
    return endpoint, model


def _command(executable: str, *args: str) -> list[str]:
    if os.name == "nt" and Path(executable).suffix.lower() in {".bat", ".cmd"}:
        return [os.environ["COMSPEC"], "/d", "/c", executable, *args]
    return [executable, *args]


def _is_port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _process_options() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _stop_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _request_json(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10,
) -> dict[str, Any]:
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    request_headers = {"Content-Type": "application/json"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(
        url,
        data=payload,
        headers=request_headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        decoded = json.loads(response.read().decode("utf-8"))
    if not isinstance(decoded, dict):
        raise RuntimeError(f"Expected a JSON object from {url}.")
    return decoded


def _wait_for_host(base_url: str, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Functions host exited with code {process.returncode}.")
        try:
            _request_json(f"{base_url}/agents/main/workflows", timeout=2)
            return
        except (OSError, ValueError, urllib.error.URLError):
            time.sleep(1)
    raise RuntimeError("Functions host did not become ready within 90 seconds.")


def _workflow_id(chat: dict[str, Any]) -> str:
    tool_calls = chat.get("tool_calls")
    if not isinstance(tool_calls, list):
        raise RuntimeError("Chat response did not include tool calls.")
    names = {
        call.get("tool_name")
        for call in tool_calls
        if isinstance(call, dict) and call.get("type") == "tool_start"
    }
    missing = {"load_skill", "read_skill_resource", "start_workflow"} - names
    if missing:
        raise RuntimeError(f"Model skipped required Skill workflow calls: {sorted(missing)}")

    start = next(
        call
        for call in tool_calls
        if isinstance(call, dict)
        and call.get("type") == "tool_start"
        and call.get("tool_name") == "start_workflow"
    )
    result = start.get("result")
    if isinstance(result, str):
        if not result.strip():
            raise RuntimeError("start_workflow returned an empty result.")
        parsed = json.loads(result)
    else:
        parsed = result
    if not isinstance(parsed, dict) or not isinstance(parsed.get("workflow_id"), str):
        raise RuntimeError("start_workflow did not return a workflow id.")
    return parsed["workflow_id"]


def _wait_for_workflow(
    base_url: str,
    session_id: str,
    workflow_id: str,
) -> dict[str, Any]:
    deadline = time.monotonic() + 120
    headers = {"x-ms-session-id": session_id}
    while time.monotonic() < deadline:
        response = _request_json(
            f"{base_url}/agents/main/workflows",
            headers=headers,
        )
        workflows = response.get("workflows")
        if isinstance(workflows, list):
            workflow = next(
                (
                    item
                    for item in workflows
                    if isinstance(item, dict) and item.get("workflow_id") == workflow_id
                ),
                None,
            )
            if workflow is not None and workflow.get("runtime_status") in TERMINAL_STATES:
                return workflow
        time.sleep(1)
    raise RuntimeError(f"Workflow {workflow_id} did not finish within 120 seconds.")


def _assert_expected_status(workflow: dict[str, Any]) -> dict[str, Any]:
    if workflow.get("runtime_status") != "Completed":
        raise RuntimeError(f"Workflow ended as {workflow.get('runtime_status')!r}.")
    custom = workflow.get("custom_status")
    nodes = custom.get("nodes") if isinstance(custom, dict) else None
    reserve = nodes.get("reserve_inventory") if isinstance(nodes, dict) else None
    confirm = nodes.get("confirm_order") if isinstance(nodes, dict) else None
    if not isinstance(reserve, dict) or not isinstance(confirm, dict):
        raise RuntimeError("Workflow status omitted the expected order-recovery nodes.")
    if reserve.get("state") != "completed":
        raise RuntimeError(f"Inventory reservation ended as {reserve.get('state')!r}.")
    if reserve.get("attempt") != 3 or reserve.get("max_attempts") != 3:
        raise RuntimeError(f"Decorator retry precedence was not observed: {reserve!r}")
    if confirm.get("state") != "completed":
        raise RuntimeError(f"Order confirmation ended as {confirm.get('state')!r}.")
    return {
        "runtime_status": workflow["runtime_status"],
        "reserve_inventory": {
            "state": reserve["state"],
            "attempt": reserve["attempt"],
            "max_attempts": reserve["max_attempts"],
        },
        "confirm_order": {"state": confirm["state"]},
    }


def main() -> int:
    try:
        opt_in = read_opt_in()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if opt_in is None:
        print(f"SKIPPED: set both {ENDPOINT_ENV} and {MODEL_ENV} to run the live E2E.")
        return 0

    endpoint, model = opt_in
    sample_root = Path(__file__).resolve().parents[1]
    source_dir = sample_root / "src"
    settings_path = source_dir / "local.settings.json"
    if settings_path.exists():
        print(
            f"ERROR: refusing to overwrite existing {settings_path}. "
            "Move it aside before running the E2E script.",
            file=sys.stderr,
        )
        return 2

    func = shutil.which("func")
    azurite = shutil.which("azurite")
    if func is None:
        print("ERROR: Azure Functions Core Tools (`func`) is required.", file=sys.stderr)
        return 2
    if not _is_port_open(10000) and azurite is None:
        print("ERROR: Azurite must be reachable or installed on PATH.", file=sys.stderr)
        return 2

    settings = {
        "IsEncrypted": False,
        "Values": {
            "FUNCTIONS_WORKER_RUNTIME": "python",
            "AzureWebJobsStorage": "UseDevelopmentStorage=true",
            "AZURE_FUNCTIONS_AGENTS_PROVIDER": "foundry",
            "FOUNDRY_PROJECT_ENDPOINT": endpoint,
            "FOUNDRY_MODEL": model,
        },
    }

    temp_dir = Path(tempfile.mkdtemp(prefix="workflow-retry-policy-e2e-"))
    host_log_path = temp_dir / "functions-host.log"
    azurite_log_path = temp_dir / "azurite.log"
    host_process: subprocess.Popen[bytes] | None = None
    azurite_process: subprocess.Popen[bytes] | None = None
    host_log = None
    azurite_log = None
    created_settings = False
    try:
        try:
            settings_fd = os.open(
                settings_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            )
        except FileExistsError as exc:
            raise RuntimeError(
                f"refusing to overwrite concurrently created {settings_path}"
            ) from exc
        created_settings = True
        with os.fdopen(settings_fd, "w", encoding="utf-8") as settings_file:
            settings_file.write(json.dumps(settings, indent=2) + "\n")

        if not _is_port_open(10000):
            assert azurite is not None
            azurite_log = azurite_log_path.open("wb")
            azurite_process = subprocess.Popen(
                _command(
                    azurite,
                    "--silent",
                    "--location",
                    str(temp_dir / "azurite"),
                    "--blobPort",
                    "10000",
                    "--queuePort",
                    "10001",
                    "--tablePort",
                    "10002",
                ),
                cwd=temp_dir,
                stdout=azurite_log,
                stderr=subprocess.STDOUT,
                **_process_options(),
            )
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not _is_port_open(10000):
                if azurite_process.poll() is not None:
                    raise RuntimeError(
                        f"Azurite exited with code {azurite_process.returncode}."
                    )
                time.sleep(0.5)
            if not _is_port_open(10000):
                raise RuntimeError("Azurite did not become ready within 30 seconds.")

        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        repository_src = sample_root.parents[1] / "src"
        host_environment = os.environ.copy()
        existing_pythonpath = host_environment.get("PYTHONPATH")
        host_environment["PYTHONPATH"] = os.pathsep.join(
            part
            for part in (str(repository_src), existing_pythonpath)
            if part
        )
        host_log = host_log_path.open("wb")
        host_process = subprocess.Popen(
            _command(func, "start", "--port", str(port)),
            cwd=source_dir,
            env=host_environment,
            stdout=host_log,
            stderr=subprocess.STDOUT,
            **_process_options(),
        )
        _wait_for_host(base_url, host_process)

        session_id = f"workflow-retry-policy-e2e-{uuid.uuid4()}"
        chat = _request_json(
            f"{base_url}/agents/main/chat",
            method="POST",
            body={"prompt": PROMPT},
            headers={"x-ms-session-id": session_id},
            timeout=180,
        )
        workflow_id = _workflow_id(chat)
        workflow = _wait_for_workflow(base_url, session_id, workflow_id)
        evidence = _assert_expected_status(workflow)
        evidence["workflow_id"] = workflow_id
        evidence["skill_resource_read"] = True
        print(json.dumps(evidence, indent=2))
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if host_log is not None:
            host_log.flush()
        if host_log_path.exists():
            lines = host_log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            print("\nFunctions host log (last 80 lines):", file=sys.stderr)
            print("\n".join(lines[-80:]), file=sys.stderr)
        return 1
    finally:
        _stop_process(host_process)
        _stop_process(azurite_process)
        if host_log is not None:
            host_log.close()
        if azurite_log is not None:
            azurite_log.close()
        if created_settings and settings_path.exists():
            settings_path.unlink()
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
