"""SDK create/resume worker with a synthetic provider and local SessionFs."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from functools import partial
from pathlib import Path
from unittest.mock import patch

import httpx


async def _run(
    phase: int, session_dir: Path, storage_root: Path, out: Path, probe_path: str,
) -> int:
    from copilot import CopilotClient
    from copilot.copilot_request_handler import CopilotRequestHandler

    from azure_functions_agents.harness import _harness_lifecycle
    from azure_functions_agents.harness._harness_binding import AppHarness, HarnessKind
    from azure_functions_agents.harness.copilot_sdk import (
        _copilot_execution as _copilot,
    )
    from azure_functions_agents.harness.copilot_sdk import (
        _copilot_session_fs as fs,
    )
    from azure_functions_agents.harness.copilot_sdk._copilot_providers import OpenAIProvider
    from azure_functions_agents.harness.copilot_sdk._copilot_runtime import get_runtime
    from azure_functions_agents.harness.copilot_sdk._copilot_session_identity import resolve_route

    paths: list[str] = []
    denied_paths: list[str] = []
    original = fs.CopilotSessionFs._path

    def record(self: fs.CopilotSessionFs, path: str) -> str:
        paths.append(path)
        try:
            return original(self, path)
        except OSError:
            denied_paths.append(path)
            raise

    fs.CopilotSessionFs._path = record

    app_root = session_dir / "app"
    app_root.mkdir(parents=True, exist_ok=True)
    harness = AppHarness(
        HarnessKind.COPILOT, app_root, storage_root, "offline-model", OpenAIProvider("offline"),
        session_storage=resolve_route(app_root),
    )
    owner = get_runtime(harness)
    native_id = _copilot._copilot_session_id("agent", "restore")
    result: dict[str, object] = {
        "phase": phase,
        "pid": os.getpid(),
        "cwd": os.getcwd(),
        "workspace": str(owner.workspace),
    }
    storage = None
    session = None

    class SyntheticProvider(CopilotRequestHandler):
        async def send_request(self, request, options):
            assert request.url.path == "/v1/chat/completions"
            return httpx.Response(200, request=request, json={
                "id": "offline-restore", "object": "chat.completion", "created": 0,
                "model": "offline-model",
                "choices": [{
                    "index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "OFFLINE_RESTORE_MARKER"},
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })

    try:
        storage = await fs.open_session_fs(
            harness.session_storage, "agent", "restore",
            workspace_path=str(owner.workspace),
            deadline=asyncio.get_running_loop().time() + 120,
        )
        with patch("copilot.CopilotClient", partial(
            CopilotClient, request_handler=SyntheticProvider(),
        )):
            client = await owner.client()
        session = await _open_session(client, native_id, phase, storage)
        if phase == 1:
            response = await session.send_and_wait("Return the offline marker.", timeout=30)
            assert response is not None
            assert response.data.content == "OFFLINE_RESTORE_MARKER"
        result["event_count"] = len(await session.get_events())
        if phase == 1:
            await storage.write_file("/session-state/opaque.sdk", "\x00opaque\r\nfixture")
        else:
            assert await storage.read_file("/session-state/opaque.sdk") == "\x00opaque\r\nfixture"
        if probe_path:
            await storage.stat(probe_path)
        result["outcome"] = "ok"
    except Exception as error:
        result["outcome"] = "error"
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        try:
            if session is not None:
                await session.disconnect()
        finally:
            try:
                if storage is not None:
                    await storage.close()
            finally:
                await _harness_lifecycle._shutdown_harnesses()
    root = session_dir / "copilot-native" / "local" / "agent" / "restore"
    result["files"] = sorted(
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    )
    result["paths"] = paths
    result["denied_paths"] = denied_paths
    out.write_text(json.dumps(result), encoding="utf-8")
    return 0 if result["outcome"] == "ok" else 1


async def _open_session(client, native_id, phase, storage):
    from copilot.rpc import PermissionDecisionDeniedByRules
    from copilot.session import (
        PermissionInvocation,
        ProviderConfig,
        SystemMessageReplaceConfig,
        ToolSearchConfig,
    )
    from copilot.session_events import PermissionRequest

    def deny_permission(
        _request: PermissionRequest, _invocation: PermissionInvocation
    ) -> PermissionDecisionDeniedByRules:
        return PermissionDecisionDeniedByRules(rules=[])

    options = {
        "model": "offline-model",
        "tools": [],
        "available_tools": [],
        "system_message": SystemMessageReplaceConfig(mode="replace", content="offline"),
        "provider": ProviderConfig(
            type="openai", wire_api="completions", base_url="http://127.0.0.1:9/v1",
            bearer_token_provider=lambda _args: "offline",
        ),
        "streaming": False,
        "enable_config_discovery": False,
        "enable_session_telemetry": False,
        "request_extensions": False,
        "tool_search": ToolSearchConfig(enabled=False),
        "create_session_fs_handler": lambda _session: storage,
    }
    if phase == 1:
        return await client.create_session(
            session_id=native_id, on_permission_request=deny_permission, **options
        )
    return await client.resume_session(
        native_id, on_permission_request=deny_permission, **options,
    )


def main() -> None:
    phase, session_dir, storage_root, out, probe_path = sys.argv[1:6]
    os.environ["COPILOT_SKIP_CLI_DOWNLOAD"] = "1"
    os.environ["COPILOT_CLI_EXTRACT_DIR"] = str(
        Path(__file__).resolve().parents[1] / ".tmp-validation" / "runtime-1.0.93-4"
    )
    from copilot._cli_version import get_runtime_platform

    executable = "copilot-runtime.exe" if os.name == "nt" else "copilot-runtime"
    os.environ["COPILOT_CLI_PATH"] = str(
        Path(os.environ["COPILOT_CLI_EXTRACT_DIR"]) / "prebuilds" / get_runtime_platform() / executable
    )
    os.environ.pop("COPILOT_SDK_DEFAULT_CONNECTION", None)
    os.environ["OPENAI_API_KEY"] = "offline"
    os.environ["AZURE_FUNCTIONS_AGENTS_SESSION_DIR"] = session_dir
    for name in (
        "AzureWebJobsStorage", "AzureWebJobsStorage__blobServiceUri", "WEBSITE_INSTANCE_ID",
        "WEBSITE_OWNER_NAME", "WEBSITE_DEPLOYMENT_ID", "WEBSITE_SITE_NAME",
    ):
        os.environ.pop(name, None)
    raise SystemExit(asyncio.run(
        _run(int(phase), Path(session_dir), Path(storage_root), Path(out), probe_path)
    ))


if __name__ == "__main__":
    main()
