from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from azure_functions_agents.session_state import AppIdentity, EntraUserOwnerContext, owner_partition
from tests.live import aca_deployed_agent_support as agent_support
from tests.live import aca_deployed_loss_support as support


def test_loss_partition_derives_from_the_easy_auth_owner() -> None:
    identity = AppIdentity.create(
        subscription_id="11111111-2222-3333-4444-555555555555",
        site_name="agent-app",
    )
    config = SimpleNamespace(app_identity=identity)
    authorization = SimpleNamespace(
        tenant_id="22222222-3333-4444-5555-666666666666",
        object_id="33333333-4444-5555-6666-777777777777",
    )

    actual = support.deployed_partition_key(config, authorization, agent_slug="deployed_load")

    assert actual == owner_partition(
        EntraUserOwnerContext.create(
            identity,
            "deployed_load",
            authorization.tenant_id,
            authorization.object_id,
        )
    ).partition_key


def test_loss_state_helpers_require_active_then_controller_terminal_projection() -> None:
    session = SimpleNamespace(
        session_id="session-1",
        status="creating",
        active_run_id="run-1",
        active_operation_id="operation-1",
        sandbox_id="sandbox-1",
        tombstone_reason=None,
    )
    run = SimpleNamespace(
        session_id="session-1",
        run_id="run-1",
        status="accepted",
        status_reason=None,
    )
    sandbox = SimpleNamespace(sandbox_id="sandbox-1")
    operation = SimpleNamespace(
        target=SimpleNamespace(run_id="run-1"),
        state="active",
    )

    assert not support.has_active_owned_backing(
        session,
        run,
        sandbox,
        expected_session_id="session-1",
        expected_run_id="run-1",
    )
    session.status = "running"
    assert support.has_active_owned_backing(
        session,
        run,
        sandbox,
        expected_session_id="session-1",
        expected_run_id="run-1",
    )
    assert not support.has_lost_backing_projection(
        session,
        run,
        (operation,),
        expected_session_id="session-1",
        expected_run_id="run-1",
    )
    session.status = "tombstoned"
    session.tombstone_reason = "sandbox_backing_lost"
    session.active_run_id = None
    session.active_operation_id = None
    run.status = "abandoned"
    run.status_reason = "sandbox_backing_lost"
    operation.state = "completed"
    assert support.has_lost_backing_projection(
        session,
        run,
        (operation,),
        expected_session_id="session-1",
        expected_run_id="run-1",
    )


def test_loss_public_contract_maps_terminal_status_to_200_and_result_to_410() -> None:
    support.assert_public_backing_loss_contract(
        status_code=200,
        status={
            "state": "abandoned",
            "error": {"code": "session_tombstoned"},
        },
        result_code=410,
        result={"error": "result_unavailable"},
    )
    with pytest.raises(AssertionError):
        support.assert_public_backing_loss_contract(
            status_code=410,
            status={"error": "session_gone"},
            result_code=410,
            result={"error": "session_gone"},
        )


def test_loss_live_module_has_no_table_mutation_boundary() -> None:
    source = (Path(__file__).parent / "live" / "test_aca_deployed_loss.py").read_text()

    assert "create_entity" not in source
    assert "upsert_entity" not in source
    assert "update_entity" not in source
    assert "delete_entity" not in source
    assert "TableServiceClient" not in source
    assert "adapter.delete_sandbox" in source
    assert "owned_sandbox" in source


def test_loss_module_compiles_and_skips_when_deployed_opt_in_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_RUN_DEPLOYED_ACA_SMOKE", raising=False)
    source_path = Path(__file__).parent / "live" / "test_aca_deployed_loss.py"
    compile(source_path.read_text(), str(source_path), "exec")
    sys.modules.pop("tests.live.test_aca_deployed_loss", None)
    with pytest.raises(pytest.skip.Exception):
        importlib.import_module("tests.live.test_aca_deployed_loss")


@pytest.fixture
def loss_module(monkeypatch: pytest.MonkeyPatch) -> object:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_RUN_DEPLOYED_ACA_SMOKE", "1")
    sys.modules.pop("tests.live.test_aca_deployed_loss", None)
    return importlib.import_module("tests.live.test_aca_deployed_loss")


@pytest.mark.asyncio
async def test_loss_submission_retries_the_same_key_after_setup_deadline(
    monkeypatch: pytest.MonkeyPatch,
    loss_module: object,
) -> None:
    module = loss_module
    headers_seen: list[dict[str, str]] = []
    responses = iter(
        [(504, {"error": "setup_deadline_exceeded"}, {"Retry-After": "60"})] * 5
        + [(202, {"session_id": "session-1", "run_id": "run-1"}, {})]
    )

    async def request(*_: object, **kwargs: object) -> tuple[int, dict[str, str], dict[str, str]]:
        headers_seen.append(dict(kwargs["headers"]))  # type: ignore[arg-type,index]
        return next(responses)

    retry_delays: list[float] = []

    async def no_sleep(delay: float) -> None:
        retry_delays.append(delay)

    accepted = SimpleNamespace(session_id="session-1", run_id="run-1")
    monkeypatch.setattr(module, "json_request", request)
    monkeypatch.setattr(module, "parse_accepted_run", lambda *_: accepted)
    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)

    actual = await module._submit_held_run(  # type: ignore[attr-defined]
        object(),
        object(),
        SimpleNamespace(deployed=SimpleNamespace(chat_url="https://example.test/chat")),
        {"Authorization": "redacted", "Content-Type": "application/json"},
        "partition",
        "fixed-key",
    )

    assert actual is accepted
    assert len(headers_seen) == 6
    assert {headers["Idempotency-Key"] for headers in headers_seen} == {"fixed-key"}
    assert retry_delays == [60.0] * 5


@pytest.mark.asyncio
async def test_loss_submission_recovers_a_candidate_after_transport_error(
    monkeypatch: pytest.MonkeyPatch,
    loss_module: object,
) -> None:
    module = loss_module
    accepted = SimpleNamespace(session_id="session-1", run_id="run-1")
    recovered_keys: list[str] = []

    async def transport_error(*_: object, **__: object) -> tuple[int, dict[str, str], dict[str, str]]:
        raise module.AcaSmokeEnvironmentError("transport unavailable")

    async def recover(*args: object) -> object:
        recovered_keys.append(args[-1])
        return accepted

    monkeypatch.setattr(module, "json_request", transport_error)
    monkeypatch.setattr(module, "_recover_candidate", recover)

    actual = await module._submit_held_run(  # type: ignore[attr-defined]
        object(),
        object(),
        SimpleNamespace(deployed=SimpleNamespace(chat_url="https://example.test/chat")),
        {"Authorization": "redacted", "Content-Type": "application/json"},
        "partition",
        "recoverable-key",
    )

    assert actual is accepted
    assert recovered_keys == ["recoverable-key"]


@pytest.mark.asyncio
async def test_loss_submission_propagates_unresolved_transport_error_after_recovery(
    monkeypatch: pytest.MonkeyPatch,
    loss_module: object,
) -> None:
    module = loss_module
    recovered_keys: list[str] = []

    async def transport_error(*_: object, **__: object) -> tuple[int, dict[str, str], dict[str, str]]:
        raise module.AcaSmokeEnvironmentError("transport unavailable")

    async def recover(*args: object) -> None:
        recovered_keys.append(args[-1])
        return None

    monkeypatch.setattr(module, "json_request", transport_error)
    monkeypatch.setattr(module, "_recover_candidate", recover)

    with pytest.raises(module.AcaSmokeEnvironmentError, match="transport unavailable"):
        await module._submit_held_run(  # type: ignore[attr-defined]
            object(),
            object(),
            SimpleNamespace(deployed=SimpleNamespace(chat_url="https://example.test/chat")),
            {"Authorization": "redacted", "Content-Type": "application/json"},
            "partition",
            "unresolved-key",
        )

    assert recovered_keys == ["unresolved-key"]


@pytest.mark.asyncio
async def test_loss_sse_reader_stops_after_a_contiguous_hold_start_across_chunks() -> None:
    class Content:
        def __init__(self, chunks: list[bytes]) -> None:
            self._chunks = iter(chunks)

        def __aiter__(self) -> Content:
            return self

        async def __anext__(self) -> bytes:
            try:
                return next(self._chunks)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

    class Response:
        status = 200

        def __init__(self) -> None:
            self.headers: dict[str, str] = {}
            self.content = Content(
                [
                    b'id: 1\ndata: {"type":"session"}\n\nid: 2\ndata: {"type":"tool_',
                    b'start","tool_name":"qualification_hold"}\n\n',
                ]
            )
            self.closed = False

        async def __aenter__(self) -> Response:
            return self

        async def __aexit__(self, *_: object) -> None:
            self.closed = True

    class Session:
        def __init__(self, response: Response) -> None:
            self.response = response

        def get(self, _: str, *, headers: dict[str, str]) -> Response:
            assert headers == {"Authorization": "redacted"}
            return self.response

    response = Response()
    status, event, _ = await agent_support.read_sse_until_matching_event(
        Session(response),  # type: ignore[arg-type]
        "https://example.test/events",
        headers={"Authorization": "redacted"},
        matches=lambda candidate: candidate.payload.get("tool_name") == "qualification_hold",
    )

    assert status == 200
    assert event is not None
    assert event.sequence == 2
    assert event.payload == {"type": "tool_start", "tool_name": "qualification_hold"}
    assert response.closed


def test_loss_deletes_only_after_public_hold_start_evidence() -> None:
    source = (Path(__file__).parent / "live" / "test_aca_deployed_loss.py").read_text()

    assert source.index("await _wait_for_qualification_hold_start(") < source.index(
        "await resources.adapter.delete_sandbox(active.sandbox.sandbox_id)"
    )


@pytest.mark.asyncio
async def test_loss_last_resort_cleanup_deletes_exact_snapshots_before_sandbox(
    monkeypatch: pytest.MonkeyPatch,
    loss_module: object,
) -> None:
    module = loss_module
    calls: list[str] = []
    owned = SimpleNamespace(snapshot_id="owned-snapshot", sandbox_id="owned-sandbox")
    foreign = SimpleNamespace(snapshot_id="foreign-snapshot", sandbox_id="foreign-sandbox")

    class Adapter:
        async def delete_snapshot(self, snapshot_id: str) -> None:
            calls.append(f"snapshot:{snapshot_id}")

        async def delete_sandbox(self, sandbox_id: str) -> None:
            calls.append(f"sandbox:{sandbox_id}")

    async def sandbox(*_: object) -> object:
        return SimpleNamespace(sandbox_id="owned-sandbox")

    async def snapshots(*_: object) -> tuple[object, ...]:
        return (owned,)

    monkeypatch.setattr(module, "owned_sandbox", sandbox)
    monkeypatch.setattr(module, "owned_snapshots", snapshots)

    await module._delete_exact_provider_backing(  # type: ignore[attr-defined]
        SimpleNamespace(adapter=Adapter()),
        SimpleNamespace(session_id="session-1"),
    )

    assert calls == ["snapshot:owned-snapshot", "sandbox:owned-sandbox"]
    assert f"snapshot:{foreign.snapshot_id}" not in calls


@pytest.mark.asyncio
async def test_loss_last_resort_cleanup_rejects_foreign_snapshot_before_any_delete(
    monkeypatch: pytest.MonkeyPatch,
    loss_module: object,
) -> None:
    module = loss_module
    calls: list[str] = []

    class Adapter:
        async def delete_snapshot(self, snapshot_id: str) -> None:
            calls.append(f"snapshot:{snapshot_id}")

        async def delete_sandbox(self, sandbox_id: str) -> None:
            calls.append(f"sandbox:{sandbox_id}")

    async def sandbox(*_: object) -> object:
        return SimpleNamespace(sandbox_id="owned-sandbox")

    async def snapshots(*_: object) -> tuple[object, ...]:
        return (
            SimpleNamespace(snapshot_id="owned-snapshot", sandbox_id="owned-sandbox"),
            SimpleNamespace(snapshot_id="foreign-snapshot", sandbox_id="foreign-sandbox"),
        )

    monkeypatch.setattr(module, "owned_sandbox", sandbox)
    monkeypatch.setattr(module, "owned_snapshots", snapshots)

    with pytest.raises(AssertionError, match="did not belong"):
        await module._delete_exact_provider_backing(  # type: ignore[attr-defined]
            SimpleNamespace(adapter=Adapter()),
            SimpleNamespace(session_id="session-1"),
        )

    assert calls == []
