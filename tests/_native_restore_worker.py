"""Out-of-process native create/resume worker for the cold-restore regression test.

Run as ``python -m tests._native_restore_worker <phase> <session_dir> <storage_root> <out.json>``.
It mirrors the adapter wiring in :mod:`azure_functions_agents._copilot` but points the
provider at a closed loopback port, so a turn is attempted and its events are flushed to
the SessionFs without any model call.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path


async def _run(phase: int, session_dir: Path, storage_root: Path, out: Path) -> None:
    from azure_functions_agents import _copilot
    from azure_functions_agents import _copilot_session_fs as fs
    from azure_functions_agents._harness import AppHarness, HarnessKind, ProviderKind
    from azure_functions_agents._native_session_identity import resolve_route

    paths: list[str] = []
    original = fs.NativeSessionFs._path

    def record(self: fs.NativeSessionFs, path: str) -> str:
        resolved = original(self, path)
        paths.append(resolved)
        return resolved

    fs.NativeSessionFs._path = record

    app_root = session_dir / "app"
    app_root.mkdir(parents=True, exist_ok=True)
    harness = AppHarness(
        HarnessKind.COPILOT, app_root, storage_root, "offline-model", ProviderKind.OPENAI,
        session_storage=resolve_route(app_root),
    )
    owner = _copilot._runtime(harness)
    native_id = _copilot._native_id("agent", "restore")
    result: dict[str, object] = {
        "phase": phase,
        "pid": os.getpid(),
        "cwd": os.getcwd(),
        "workspace": str(owner.workspace),
    }
    storage = None
    try:
        client = await owner.client()
        storage = await fs.NativeSession.open(
            harness, "agent", "restore", native_id, asyncio.get_running_loop().time() + 300
        )
        await storage.prepare(new_session=(phase == 1), workspace_path=str(owner.workspace))
        session = await _open_session(client, native_id, phase, storage, owner)
        async with session:
            # A closed loopback provider still drives a real turn whose events are persisted.
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(session.send_and_wait("ping"), timeout=90)
        await storage.transition(state=fs.SessionState.ACTIVE)
        await storage.transition(handoff_may_have_started=True)
        await storage.complete()
        result["outcome"] = "ok"
        result["files"] = sorted(storage.envelope.completed.files)
    except BaseException as error:
        result["outcome"] = "error"
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        if storage is not None:
            await storage.close()
        await _copilot.shutdown()
    result["paths"] = paths
    out.write_text(json.dumps(result), encoding="utf-8")


async def _open_session(client, native_id, phase, storage, owner):
    from copilot.session import (
        InfiniteSessionConfig,
        ProviderConfig,
        SystemMessageReplaceConfig,
        ToolSearchConfig,
    )

    from azure_functions_agents import _copilot
    from azure_functions_agents import _copilot_session_fs as fs

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
        "infinite_sessions": InfiniteSessionConfig(enabled=True),
        "tool_search": ToolSearchConfig(enabled=False),
        "on_event": lambda _event: None,
        "create_session_fs_handler": lambda _session: fs.NativeSessionFs(
            storage, fs.HOST_PATH_CONVENTIONS, str(owner.workspace)
        ),
    }
    if phase == 1:
        return await client.create_session(
            session_id=native_id, on_permission_request=_copilot._deny_permission, **options
        )
    return await client.resume_session(
        native_id, on_permission_request=_copilot._deny_permission,
        continue_pending_work=False, **options,
    )


def main() -> None:
    phase, session_dir, storage_root, out = sys.argv[1:5]
    os.environ["COPILOT_SKIP_CLI_DOWNLOAD"] = "1"
    os.environ["OPENAI_API_KEY"] = "offline"
    os.environ["AZURE_FUNCTIONS_AGENTS_SESSION_DIR"] = session_dir
    os.environ["AZURE_FUNCTIONS_AGENTS_COPILOT_SESSION_STORAGE"] = "local"
    for name in ("AzureWebJobsStorage", "AzureWebJobsStorage__blobServiceUri", "WEBSITE_INSTANCE_ID"):
        os.environ.pop(name, None)
    asyncio.run(_run(int(phase), Path(session_dir), Path(storage_root), Path(out)))


if __name__ == "__main__":
    main()
