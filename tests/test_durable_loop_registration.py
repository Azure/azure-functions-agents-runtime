from __future__ import annotations

import json
from importlib.metadata import version
from pathlib import Path
from typing import get_type_hints

import azure.durable_functions as df
import pytest

from azure_functions_agents import app as app_module
from azure_functions_agents.experimental.durable_loop_config import (
    DURABLE_LOOP_ENABLED_ENV,
)
from azure_functions_agents.experimental.durable_loop_protocol import (
    HumanEventDeliveryResultV1,
    HumanEventDeliveryStatus,
    canonical_hash,
)
from azure_functions_agents.experimental.durable_loop_registration import (
    DURABLE_LOOP_ADMISSION_ORCHESTRATOR_NAME,
    DURABLE_LOOP_APPEND_ACTIVITY_NAME,
    DURABLE_LOOP_CANCEL_DELIVERY_ORCHESTRATOR_NAME,
    DURABLE_LOOP_CLEANUP_ACTIVITY_NAME,
    DURABLE_LOOP_COMPACTION_ACTIVITY_NAME,
    DURABLE_LOOP_CONTROL_ORCHESTRATOR_NAME,
    DURABLE_LOOP_FAULT_ACTIVITY_NAME,
    DURABLE_LOOP_HUMAN_ACTIVITY_NAME,
    DURABLE_LOOP_HUMAN_DELIVERY_ACTIVITY_NAME,
    DURABLE_LOOP_HUMAN_DELIVERY_ORCHESTRATOR_NAME,
    DURABLE_LOOP_HUMAN_OUTBOX_ORCHESTRATOR_NAME,
    DURABLE_LOOP_HUMAN_RESULT_ACTIVITY_NAME,
    DURABLE_LOOP_MODEL_ACTIVITY_NAME,
    DURABLE_LOOP_MODEL_CANCEL_ACTIVITY_NAME,
    DURABLE_LOOP_MODEL_POLL_ACTIVITY_NAME,
    DURABLE_LOOP_ORCHESTRATOR_NAME,
    DURABLE_LOOP_SESSION_ENTITY_NAME,
    DURABLE_LOOP_TOOL_ACTIVITY_NAME,
    _deliver_event_with_durable_client,
    apply_session_entity_operation,
)
from azure_functions_agents.experimental.hybrid_config import (
    HYBRID_SANDBOX_GROUP_ENV,
    HYBRID_SANDBOX_REGION_ENV,
)


def _write_agent(root: Path) -> None:
    (root / "main.agent.md").write_text(
        (
            "---\n"
            "name: Main\n"
            "description: Durable test\n"
            "builtin_endpoints: true\n"
            "---\n"
            "Test."
        ),
        encoding="utf-8",
    )


def _registered_functions(app: df.DFApp) -> dict[str, list[str]]:
    functions: dict[str, list[str]] = {}
    for builder in app._function_builders:
        function = builder._function
        functions[function._name] = [
            binding.get_dict_repr()["type"] for binding in function._bindings
        ]
    return functions


def _registered_handler(app: df.DFApp, name: str):
    for builder in app._function_builders:
        function = builder._function
        if function._name == name:
            return function._func
    raise AssertionError(f"function {name!r} was not registered")


def test_private_gate_registers_one_versioned_durable_blueprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DURABLE_LOOP_ENABLED_ENV, "true")
    _write_agent(tmp_path)

    app = app_module.create_function_app(tmp_path)

    assert version("azure-functions-durable") == "1.6.0"
    assert isinstance(app, df.DFApp)
    functions = _registered_functions(app)
    assert functions[DURABLE_LOOP_SESSION_ENTITY_NAME] == ["entityTrigger"]
    assert functions[DURABLE_LOOP_ADMISSION_ORCHESTRATOR_NAME] == [
        "orchestrationTrigger"
    ]
    assert functions[DURABLE_LOOP_ORCHESTRATOR_NAME] == ["orchestrationTrigger"]
    assert functions[DURABLE_LOOP_HUMAN_OUTBOX_ORCHESTRATOR_NAME] == [
        "orchestrationTrigger"
    ]
    assert functions[DURABLE_LOOP_HUMAN_DELIVERY_ORCHESTRATOR_NAME] == [
        "orchestrationTrigger"
    ]
    assert functions[DURABLE_LOOP_CANCEL_DELIVERY_ORCHESTRATOR_NAME] == [
        "orchestrationTrigger"
    ]
    assert functions[DURABLE_LOOP_CONTROL_ORCHESTRATOR_NAME] == [
        "orchestrationTrigger"
    ]
    assert functions[DURABLE_LOOP_MODEL_ACTIVITY_NAME] == ["activityTrigger"]
    assert functions[DURABLE_LOOP_MODEL_POLL_ACTIVITY_NAME] == ["activityTrigger"]
    assert functions[DURABLE_LOOP_MODEL_CANCEL_ACTIVITY_NAME] == ["activityTrigger"]
    assert functions[DURABLE_LOOP_CLEANUP_ACTIVITY_NAME] == ["activityTrigger"]
    assert functions[DURABLE_LOOP_FAULT_ACTIVITY_NAME] == ["activityTrigger"]
    assert functions[DURABLE_LOOP_TOOL_ACTIVITY_NAME] == ["activityTrigger"]
    assert functions[DURABLE_LOOP_APPEND_ACTIVITY_NAME] == ["activityTrigger"]
    assert functions[DURABLE_LOOP_HUMAN_ACTIVITY_NAME] == ["activityTrigger"]
    assert functions[DURABLE_LOOP_HUMAN_RESULT_ACTIVITY_NAME] == [
        "activityTrigger"
    ]
    assert functions[DURABLE_LOOP_HUMAN_DELIVERY_ACTIVITY_NAME] == [
        "durableClient",
        "activityTrigger"
    ]
    assert functions[DURABLE_LOOP_COMPACTION_ACTIVITY_NAME] == ["activityTrigger"]
    for name in (
        DURABLE_LOOP_SESSION_ENTITY_NAME,
        DURABLE_LOOP_ADMISSION_ORCHESTRATOR_NAME,
        DURABLE_LOOP_ORCHESTRATOR_NAME,
        DURABLE_LOOP_HUMAN_OUTBOX_ORCHESTRATOR_NAME,
        DURABLE_LOOP_HUMAN_DELIVERY_ORCHESTRATOR_NAME,
        DURABLE_LOOP_CANCEL_DELIVERY_ORCHESTRATOR_NAME,
        DURABLE_LOOP_CONTROL_ORCHESTRATOR_NAME,
        DURABLE_LOOP_MODEL_ACTIVITY_NAME,
        DURABLE_LOOP_MODEL_POLL_ACTIVITY_NAME,
        DURABLE_LOOP_MODEL_CANCEL_ACTIVITY_NAME,
        DURABLE_LOOP_CLEANUP_ACTIVITY_NAME,
        DURABLE_LOOP_FAULT_ACTIVITY_NAME,
        DURABLE_LOOP_TOOL_ACTIVITY_NAME,
        DURABLE_LOOP_APPEND_ACTIVITY_NAME,
        DURABLE_LOOP_HUMAN_ACTIVITY_NAME,
        DURABLE_LOOP_HUMAN_RESULT_ACTIVITY_NAME,
        DURABLE_LOOP_HUMAN_DELIVERY_ACTIVITY_NAME,
        DURABLE_LOOP_COMPACTION_ACTIVITY_NAME,
    ):
        assert list(functions).count(name) == 1


def test_registered_durable_binding_annotations_are_worker_compatible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DURABLE_LOOP_ENABLED_ENV, "true")
    _write_agent(tmp_path)
    app = app_module.create_function_app(tmp_path)
    assert isinstance(app, df.DFApp)

    activity_names = (
        DURABLE_LOOP_MODEL_ACTIVITY_NAME,
        DURABLE_LOOP_MODEL_POLL_ACTIVITY_NAME,
        DURABLE_LOOP_MODEL_CANCEL_ACTIVITY_NAME,
        DURABLE_LOOP_CLEANUP_ACTIVITY_NAME,
        DURABLE_LOOP_FAULT_ACTIVITY_NAME,
        DURABLE_LOOP_TOOL_ACTIVITY_NAME,
        DURABLE_LOOP_APPEND_ACTIVITY_NAME,
        DURABLE_LOOP_HUMAN_ACTIVITY_NAME,
        DURABLE_LOOP_HUMAN_RESULT_ACTIVITY_NAME,
        DURABLE_LOOP_HUMAN_DELIVERY_ACTIVITY_NAME,
        DURABLE_LOOP_COMPACTION_ACTIVITY_NAME,
    )
    for name in activity_names:
        handler = _registered_handler(app, name)
        assert get_type_hints(handler)["payload"] is dict

    delivery = _registered_handler(app, DURABLE_LOOP_HUMAN_DELIVERY_ACTIVITY_NAME)
    assert get_type_hints(delivery)["client"] is str


@pytest.mark.asyncio
async def test_registered_delivery_activity_uses_durable_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Client:
        def __init__(self) -> None:
            self.events: list[tuple[str, str, object]] = []

        async def raise_event(
            self,
            run_id: str,
            event_name: str,
            event_data: object,
        ) -> None:
            self.events.append((run_id, event_name, event_data))

    monkeypatch.setenv(DURABLE_LOOP_ENABLED_ENV, "true")
    _write_agent(tmp_path)
    app = app_module.create_function_app(tmp_path)
    assert isinstance(app, df.DFApp)
    client = Client()
    activity = _registered_handler(
        app,
        DURABLE_LOOP_HUMAN_DELIVERY_ACTIVITY_NAME,
    )
    activity = getattr(activity, "__wrapped__", activity)

    raw = await activity(
        {
            "event_data": {"request_id": "human-1"},
            "event_name": "answer:1:server",
            "run_id": "run-1",
        },
        client=client,
    )

    result = HumanEventDeliveryResultV1.model_validate_json(
        json.dumps(raw)
    )
    assert result.status is HumanEventDeliveryStatus.DELIVERED
    assert client.events == [
        ("run-1", "answer:1:server", {"request_id": "human-1"})
    ]


@pytest.mark.asyncio
async def test_durable_delivery_adapter_maps_terminal_status() -> None:
    class TerminalError(RuntimeError):
        status_code = 410

    class Client:
        async def raise_event(
            self,
            _run_id: str,
            _event_name: str,
            _event_data: object,
        ) -> None:
            raise TerminalError("run is gone")

    result = await _deliver_event_with_durable_client(
        Client(),
        run_id="run-1",
        event_name="answer:1:server",
        event_data={"request_id": "human-1"},
    )

    assert result.status is HumanEventDeliveryStatus.TERMINAL
    assert result.terminal_status_code == 410


def test_gate_absent_registers_no_durable_loop_functions(tmp_path: Path) -> None:
    _write_agent(tmp_path)

    app = app_module.create_function_app(tmp_path)

    assert not isinstance(app, df.DFApp)


def test_durable_gate_with_aca_registers_owned_inventory_reaper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DURABLE_LOOP_ENABLED_ENV, "true")
    monkeypatch.setenv(
        HYBRID_SANDBOX_GROUP_ENV,
        (
            "/subscriptions/s/resourceGroups/r/providers/"
            "Microsoft.App/sandboxGroups/g"
        ),
    )
    monkeypatch.setenv(HYBRID_SANDBOX_REGION_ENV, "eastus2")
    (tmp_path / "agents.config.yaml").write_text(
        "system_tools:\n  web_request: false\n",
        encoding="utf-8",
    )
    _write_agent(tmp_path)

    app = app_module.create_function_app(tmp_path)

    assert isinstance(app, df.DFApp)
    functions = _registered_functions(app)
    assert functions["azure_functions_agents_durable_loop_reaper"] == [
        "timerTrigger"
    ]


def test_exact_durable_entity_wrapper_serializes_state_and_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DURABLE_LOOP_ENABLED_ENV, "true")
    _write_agent(tmp_path)
    app = app_module.create_function_app(tmp_path)
    assert isinstance(app, df.DFApp)
    entity = _registered_handler(app, DURABLE_LOOP_SESSION_ENTITY_NAME)
    result = json.loads(
        entity(
            json.dumps(
                {
                    "batch": [
                        {
                            "input": json.dumps(
                                json.dumps(
                                    {
                                        "request_hash": "b" * 64,
                                        "request_id_hash": "a" * 64,
                                        "run_id": "run-1",
                                    }
                                )
                            ),
                            "name": "admit",
                        },
                        {"input": json.dumps("{}"), "name": "get"},
                    ],
                    "exists": False,
                    "self": {
                        "key": "session-key",
                        "name": DURABLE_LOOP_SESSION_ENTITY_NAME,
                    },
                    "state": None,
                }
            )
        )
    )

    assert result["entityExists"] is True
    state = json.loads(result["entityState"])
    first = json.loads(result["results"][0]["result"])
    second = json.loads(result["results"][1]["result"])
    assert state["active_run_id"] == "run-1"
    assert first["disposition"] == "admitted"
    assert second["active_run_id"] == "run-1"


def test_session_entity_admission_fences_one_active_run_and_deduplicates() -> None:
    state, admitted = apply_session_entity_operation(
        None,
        "admit",
        {
            "request_hash": "b" * 64,
            "request_id_hash": "a" * 64,
            "run_id": "run-1",
        },
    )
    replay_state, replayed = apply_session_entity_operation(
        state,
        "admit",
        {
            "request_hash": "b" * 64,
            "request_id_hash": "a" * 64,
            "run_id": "run-2",
        },
    )
    _busy_state, busy = apply_session_entity_operation(
        state,
        "admit",
        {
            "request_hash": "c" * 64,
            "request_id_hash": "d" * 64,
            "run_id": "run-2",
        },
    )
    _conflict_state, conflict = apply_session_entity_operation(
        state,
        "admit",
        {
            "request_hash": "e" * 64,
            "request_id_hash": "a" * 64,
            "run_id": "run-3",
        },
    )

    assert admitted["disposition"] == "admitted"
    assert replay_state == state
    assert replayed["disposition"] == "replayed"
    assert replayed["run_id"] == "run-1"
    assert busy == {"active_run_id": "run-1", "disposition": "busy"}
    assert conflict == {"disposition": "conflict"}


def test_session_entity_completion_releases_slot_and_advances_generation() -> None:
    state, _ = apply_session_entity_operation(
        None,
        "admit",
        {
            "request_hash": "b" * 64,
            "request_id_hash": "a" * 64,
            "run_id": "run-1",
        },
    )

    commit_payload = {
        "context_ref": {"object_id": "context"},
        "expected_generation": 0,
        "request_hash": "b" * 64,
        "response_ref": {"object_id": "response"},
        "run_id": "run-1",
    }
    commit_payload["commit_key"] = canonical_hash(commit_payload)
    completed_state, result = apply_session_entity_operation(
        state,
        "complete",
        commit_payload,
    )

    assert completed_state["active_run_id"] is None
    assert completed_state["committed_generation"] == 1
    assert completed_state["committed_context_ref"] == {"object_id": "context"}
    assert result["committed_generation"] == 1
    assert result["disposition"] == "completed"

    replay_state, replayed = apply_session_entity_operation(
        completed_state,
        "complete",
        commit_payload,
    )
    assert replay_state == completed_state
    assert replayed == result


def test_session_entity_human_input_cas_accepts_first_and_rejects_conflict() -> None:
    state, _ = apply_session_entity_operation(
        None,
        "admit",
        {
            "request_hash": "b" * 64,
            "request_id_hash": "a" * 64,
            "run_id": "run-1",
        },
    )
    state, opened = apply_session_entity_operation(
        state,
        "open_human",
        {
            "call_key": "c" * 64,
            "generation": 1,
            "owner_hash": "d" * 64,
            "request_id": "human-1",
            "request_ref": {"object_id": "request"},
            "run_id": "run-1",
        },
    )
    reservation = {
        "accepted_at": "2026-09-04T00:00:00Z",
        "body_hash": "e" * 64,
        "call_key": "c" * 64,
        "expires_at": "2026-09-05T00:00:00Z",
        "generation": 1,
        "now": "2026-09-04T00:00:00Z",
        "owner_hash": "d" * 64,
        "request_id": "human-1",
        "run_id": "run-1",
        "submission_id_hash": "f" * 64,
    }
    state, reserved = apply_session_entity_operation(
        state,
        "reserve_human",
        reservation,
    )
    response = {
        **reservation,
        "response_ref": {"object_id": "response"},
    }
    state, accepted = apply_session_entity_operation(state, "accept_human", response)
    replay_state, replayed = apply_session_entity_operation(
        state,
        "accept_human",
        response,
    )
    conflict_state, conflict = apply_session_entity_operation(
        state,
        "accept_human",
        {**response, "body_hash": "0" * 64},
    )

    assert opened == {"disposition": "opened"}
    assert reserved["disposition"] == "reserved"
    assert accepted["disposition"] == "accepted"
    assert replay_state == state
    assert replayed == accepted
    assert conflict_state == state
    assert conflict == {"disposition": "conflict"}
    closed_state, closed = apply_session_entity_operation(
        state,
        "close_human",
        {"request_id": "human-1", "run_id": "run-1"},
    )
    assert closed_state == state
    assert closed == accepted
    cancel_state, cancel = apply_session_entity_operation(
        state,
        "cancel",
        {"run_id": "run-1"},
    )
    assert cancel_state == state
    assert cancel == {"disposition": "answer_won"}


def test_session_entity_abort_releases_slot_without_advancing_generation() -> None:
    state, _ = apply_session_entity_operation(
        None,
        "admit",
        {
            "request_hash": "b" * 64,
            "request_id_hash": "a" * 64,
            "run_id": "run-1",
        },
    )

    aborted_state, result = apply_session_entity_operation(
        state,
        "abort",
        {"run_id": "run-1"},
    )

    assert aborted_state["active_run_id"] is None
    assert aborted_state["committed_generation"] == 0
    assert result == {
        "committed_generation": 0,
        "disposition": "aborted",
        "error": None,
        "possibly_committed": False,
        "status": "Failed",
    }


def test_abort_receipt_saturation_still_releases_active_slot() -> None:
    state, _ = apply_session_entity_operation(
        None,
        "admit",
        {
            "request_hash": "b" * 64,
            "request_id_hash": "a" * 64,
            "run_id": "run-1",
        },
    )
    state["aborted_runs"] = {f"old-{index}": True for index in range(64)}

    aborted_state, result = apply_session_entity_operation(
        state,
        "abort",
        {"run_id": "run-1"},
    )

    assert aborted_state["active_run_id"] is None
    assert result["disposition"] == "aborted"
    assert len(aborted_state["aborted_runs"]) == 64
    assert "old-0" not in aborted_state["aborted_runs"]
    idempotency = aborted_state["idempotency"]
    assert isinstance(idempotency, dict)
    assert idempotency["a" * 64]["lifecycle"] == "aborted"


def test_session_entity_evicts_oldest_terminal_receipts_at_capacity() -> None:
    state, _ = apply_session_entity_operation(None, "get", {})
    state["idempotency"] = {
        f"{index:064x}": {
            "expires_at": "2026-09-03T00:00:00Z",
            "lifecycle": "completed",
            "request_hash": f"{index + 1:064x}",
            "run_id": f"old-{index}",
        }
        for index in range(128)
    }

    state, admitted = apply_session_entity_operation(
        state,
        "admit",
        {
            "expires_at": "2026-09-05T00:00:00Z",
            "now": "2026-09-04T00:00:00Z",
            "request_hash": "e" * 64,
            "request_id_hash": "f" * 64,
            "run_id": "run-new",
        },
    )

    assert admitted["disposition"] == "admitted"
    assert len(state["idempotency"]) == 128
    assert f"{0:064x}" not in state["idempotency"]
    assert "f" * 64 in state["idempotency"]


def test_session_entity_does_not_evict_unexpired_idempotency_receipt() -> None:
    retained_request_id = f"{0:064x}"
    state, _ = apply_session_entity_operation(None, "get", {})
    state["idempotency"] = {
        f"{index:064x}": {
            "expires_at": "2026-09-05T00:00:00Z",
            "lifecycle": "completed",
            "request_hash": f"{index + 1:064x}",
            "run_id": f"old-{index}",
        }
        for index in range(128)
    }

    state, rejected = apply_session_entity_operation(
        state,
        "admit",
        {
            "expires_at": "2026-09-06T00:00:00Z",
            "now": "2026-09-04T00:00:00Z",
            "request_hash": "e" * 64,
            "request_id_hash": "f" * 64,
            "run_id": "run-new",
        },
    )
    _, replayed = apply_session_entity_operation(
        state,
        "admit",
        {
            "expires_at": "2026-09-05T00:00:00Z",
            "now": "2026-09-04T00:00:00Z",
            "request_hash": f"{1:064x}",
            "request_id_hash": retained_request_id,
            "run_id": "duplicate-run",
        },
    )

    assert rejected["disposition"] == "idempotency_capacity_exceeded"
    assert replayed["disposition"] == "replayed"
    assert replayed["run_id"] == "old-0"


def test_session_entity_evicts_consumed_human_receipts_at_capacity() -> None:
    state, _ = apply_session_entity_operation(
        None,
        "admit",
        {
            "request_hash": "b" * 64,
            "request_id_hash": "a" * 64,
            "run_id": "run-1",
        },
    )
    state, _ = apply_session_entity_operation(
        state,
        "open_human",
        {
            "call_key": "c" * 64,
            "generation": 1,
            "owner_hash": "d" * 64,
            "request_id": "human-new",
            "request_ref": {"object_id": "request"},
            "run_id": "run-1",
        },
    )
    state["human_inputs"] = {
        f"old-{index}": {
            "disposition": "consumed",
            "expires_at": "2026-09-03T00:00:00Z",
        }
        for index in range(64)
    }

    reservation = {
        "accepted_at": "2026-09-04T00:00:00Z",
        "body_hash": "e" * 64,
        "call_key": "c" * 64,
        "expires_at": "2026-09-05T00:00:00Z",
        "generation": 1,
        "now": "2026-09-04T00:00:00Z",
        "owner_hash": "d" * 64,
        "request_id": "human-new",
        "run_id": "run-1",
        "submission_id_hash": "f" * 64,
    }
    state, _ = apply_session_entity_operation(
        state,
        "reserve_human",
        reservation,
    )
    state, accepted = apply_session_entity_operation(
        state,
        "accept_human",
        {**reservation, "response_ref": {"object_id": "response"}},
    )

    assert accepted["disposition"] == "accepted"
    assert len(state["human_inputs"]) == 64
    assert "old-0" not in state["human_inputs"]
    assert "human-new" in state["human_inputs"]


def test_session_entity_preserves_ambiguous_terminal_replay_metadata() -> None:
    state, _ = apply_session_entity_operation(
        None,
        "admit",
        {
            "request_hash": "b" * 64,
            "request_id_hash": "a" * 64,
            "run_id": "run-1",
        },
    )
    state, _ = apply_session_entity_operation(
        state,
        "abort",
        {
            "context_ref": {"object_id": "context"},
            "disposition": "Ambiguous",
            "error": "tool_outcome_ambiguous",
            "possibly_committed": True,
            "run_id": "run-1",
            "status": "Failed",
        },
    )

    _, replayed = apply_session_entity_operation(
        state,
        "admit",
        {
            "request_hash": "b" * 64,
            "request_id_hash": "a" * 64,
            "run_id": "different-run",
        },
    )

    assert replayed["terminal_context_ref"] == {"object_id": "context"}
    assert replayed["terminal_disposition"] == "Ambiguous"
    assert replayed["terminal_error"] == "tool_outcome_ambiguous"
    assert replayed["terminal_possibly_committed"] is True
