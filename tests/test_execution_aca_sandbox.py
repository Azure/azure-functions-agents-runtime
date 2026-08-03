from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from azure_functions_agents.controller.readiness import SessionRuntimeBinding, StateStoreBinding
from azure_functions_agents.execution.aca_sandbox import AcaSandboxExecutionBackend
from azure_functions_agents.execution.backend import (
    AgentExecutionBackend,
    EventCursorExpiredError,
    RunContext,
    StartRunRequest,
)
from azure_functions_agents.execution.binding import AgentBinding
from azure_functions_agents.session_state import (
    AppIdentity,
    DurableRunRecord,
    DurableSessionRecord,
    FunctionAppOwnerContext,
    owner_partition,
)
from azure_functions_agents.transport.transport_models import DiskSource
from tests.doubles.content_package import content_package
from tests.doubles.fake_session_runtime import (
    DEFAULT_GROUP_RESOURCE_ID,
    FakeSandboxSessionHandle,
    FakeSandboxSessionProvider,
    FakeSessionStateStore,
)
from tests.test_execution_backend import assert_event_cursor_conformance

_FINGERPRINT = "s1-" + ("a" * 52)
pytestmark = pytest.mark.usefixtures("deterministic_content_package")


def _owner() -> FunctionAppOwnerContext:
    app = AppIdentity.create(
        subscription_id="11111111-2222-3333-4444-555555555555",
        site_name="agent-app",
    )
    return FunctionAppOwnerContext.create(app, "main")


def _binding() -> AgentBinding:
    return AgentBinding(agent_name="main")


def _script_root(tmp_path: Path) -> Path:
    (tmp_path / "function_app.py").write_text("app = object()\n", encoding="utf-8")
    return tmp_path


def _session(script_root: Path, *, status: str = "ready") -> DurableSessionRecord:
    owner = _owner()
    package = content_package()
    now = datetime.now(UTC)
    return DurableSessionRecord.create(
        owner_partition=owner_partition(owner),
        session_id="session-1",
        sandbox_id="sandbox-1",
        generation=1,
        digest_kind=package.digest_kind,
        digest=package.digest,
        protocol="1",
        status=status,  # type: ignore[arg-type]
        last_activity_at=now,
        expires_at=now + timedelta(hours=24),
        idle_policy_armed=True,
        active_run_id=None,
        snapshot_ids=(),
        region="westus2",
        state_store_fingerprint=_FINGERPRINT,
        quarantine_reason=None,
        tombstone_reason=None,
        created_at=now,
        updated_at=now,
    )


def _run(session: DurableSessionRecord, *, state: str = "accepted") -> DurableRunRecord:
    now = datetime.now(UTC)
    return DurableRunRecord.create(
        owner_partition=session.owner_partition,
        session_id=session.session_id,
        run_id="run-1",
        generation=session.generation,
        status=state,  # type: ignore[arg-type]
        result_available=False,
        status_reason=None,
        expires_at=now + timedelta(minutes=15),
        created_at=now,
        updated_at=now,
    )


def _runtime(
    script_root: Path,
    provider: FakeSandboxSessionProvider,
    store: FakeSessionStateStore,
) -> SessionRuntimeBinding:
    async def provider_factory() -> FakeSandboxSessionProvider:
        return provider

    async def store_factory() -> StateStoreBinding:
        return StateStoreBinding.create(
            store=store,
            state_store_fingerprint=_FINGERPRINT,
        )

    return SessionRuntimeBinding.create(
        app_identity=_owner().app_identity,
        sandbox_group_resource_id=DEFAULT_GROUP_RESOURCE_ID,
        script_root=script_root,
        provider_factory=provider_factory,
        state_store_factory=store_factory,
        creation_source=DiskSource.create("test-harness"),
    )


def _status(
    *,
    state: str,
    last_sequence: int = 0,
    result_available: bool = False,
    run_id: str = "run-1",
    session_id: str = "session-1",
) -> bytes:
    return json.dumps(
        {
            "run_id": run_id,
            "session_id": session_id,
            "state": state,
            "last_sequence": last_sequence,
            "result_available": result_available,
            "error": None,
        }
    ).encode("utf-8")


@pytest.mark.asyncio
async def test_backend_satisfies_the_lifecycle_seam_and_submits_after_admission(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    handle = FakeSandboxSessionHandle("sandbox-1")
    provider = FakeSandboxSessionProvider(handle)
    store = FakeSessionStateStore()

    async def accept(command: str) -> None:
        run_id = command.split("--run-id ", 1)[1].split(" ", 1)[0]
        inbox = json.loads(
            await handle.read_file(f"/var/lib/azure-functions-agents/inbox/{run_id}.json")
        )
        handle.seed_file(
            f"/var/lib/azure-functions-agents/runs/{run_id}/status.json",
            _status(
                state="accepted",
                run_id=run_id,
                session_id=inbox["session_id"],
            ),
        )

    handle.exec_hook = accept
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
    )
    assert isinstance(backend, AgentExecutionBackend)

    run_handle = await backend.start_run(StartRunRequest(prompt="hello"))

    assert run_handle.state == "accepted"
    assert store.session is not None
    assert store.session.status == "running"
    assert store.session.active_run_id == run_handle.run_id
    assert provider.create_calls
    assert handle.closed


@pytest.mark.asyncio
async def test_backend_reads_replayable_events_and_adopts_terminal_result(tmp_path: Path) -> None:
    script_root = _script_root(tmp_path)
    session = _session(script_root)
    store = FakeSessionStateStore(session)
    store.runs["run-1"] = _run(session)
    handle = FakeSandboxSessionHandle()
    provider = FakeSandboxSessionProvider(handle)
    handle.seed_file(
        "/var/lib/azure-functions-agents/runs/run-1/status.json",
        _status(state="succeeded", last_sequence=5, result_available=True),
    )
    handle.seed_file(
        "/var/lib/azure-functions-agents/runs/run-1/events.jsonl",
        (
            "\n".join(
                [
                    json.dumps(
                        {
                            "sequence": 3,
                            "type": "delta",
                            "data": {"content": "a"},
                            "timestamp": "2026-08-03T00:00:00+00:00",
                        }
                    ),
                    json.dumps(
                        {
                            "sequence": 4,
                            "type": "delta",
                            "data": {"content": "b"},
                            "timestamp": "2026-08-03T00:00:00+00:00",
                        }
                    ),
                    json.dumps(
                        {
                            "sequence": 5,
                            "type": "done",
                            "data": {},
                            "timestamp": "2026-08-03T00:00:00+00:00",
                        }
                    ),
                ]
            )
            + "\n"
        ).encode("utf-8"),
    )
    handle.seed_file(
        "/var/lib/azure-functions-agents/runs/run-1/result.json",
        json.dumps(
            {
                "content": "answer",
                "content_intermediate": [],
                "tool_calls": [],
                "reasoning": None,
                "delegate_error_count": 0,
            }
        ).encode("utf-8"),
    )
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
    )
    context = RunContext(run_id="run-1", session_id="session-1")

    await assert_event_cursor_conformance(
        backend,
        context,
        retained_sequences=(3, 4, 5),
        earliest_available_sequence=3,
        too_old_cursor=1,
    )
    status = await backend.get_run(context)

    assert status.state == "succeeded"
    assert status.result is not None
    assert status.result.content == "answer"
    assert store.adopted[-1].status == "succeeded"
    with pytest.raises(EventCursorExpiredError):
        _ = [event async for event in backend.read_events(context, 1)]


@pytest.mark.asyncio
async def test_backend_cancels_through_the_live_handle_and_adopts_the_terminal_row(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    session = _session(script_root)
    store = FakeSessionStateStore(session)
    store.runs["run-1"] = _run(session, state="running")
    handle = FakeSandboxSessionHandle()
    provider = FakeSandboxSessionProvider(handle)
    handle.seed_file(
        "/var/lib/azure-functions-agents/runs/run-1/status.json",
        _status(state="running"),
    )
    handle.seed_file(
        "/var/lib/azure-functions-agents/runs/run-1/process.json",
        b'{"process_group_id":42}',
    )

    async def journal_canceled(_command: str) -> None:
        handle.seed_file(
            "/var/lib/azure-functions-agents/runs/run-1/status.json",
            _status(state="canceled"),
        )

    handle.exec_hook = journal_canceled
    backend = AcaSandboxExecutionBackend(
        _binding(),
        runtime=_runtime(script_root, provider, store),
        owner=_owner(),
    )

    status = await backend.cancel_run(RunContext(run_id="run-1", session_id="session-1"))

    assert status.state == "canceled"
    assert store.adopted[-1].status == "canceled"
