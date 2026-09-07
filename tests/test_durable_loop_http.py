from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import azure.durable_functions as df
import pytest

from azure_functions_agents import app as app_module
from azure_functions_agents.experimental.durable_loop_activities import (
    DeterministicContextCompactor,
    InMemoryDurableContentStore,
    ScriptedOneStepModelProvider,
    get_protocol_model,
    put_protocol_model,
)
from azure_functions_agents.experimental.durable_loop_config import (
    DURABLE_LOOP_ENABLED_ENV,
)
from azure_functions_agents.experimental.durable_loop_protocol import (
    DurableOrchestrationInputV1,
    DurableRunDocumentV1,
    HumanInputRequestV1,
    canonical_hash,
)
from azure_functions_agents.experimental.durable_loop_registration import (
    DURABLE_LOOP_ADMISSION_ORCHESTRATOR_NAME,
    DURABLE_LOOP_CANCEL_DELIVERY_ORCHESTRATOR_NAME,
    DURABLE_LOOP_CANCEL_EVENT_NAME,
    DURABLE_LOOP_CONTROL_ORCHESTRATOR_NAME,
    DURABLE_LOOP_HUMAN_DELIVERY_ORCHESTRATOR_NAME,
    DURABLE_LOOP_HUMAN_OUTBOX_ORCHESTRATOR_NAME,
    DURABLE_LOOP_ORCHESTRATOR_NAME,
    DurableLoopActivityRuntime,
    apply_session_entity_operation,
    get_durable_loop_activity_runtime,
    reset_durable_loop_activity_runtime_factory,
    set_durable_loop_activity_runtime_factory,
)
from azure_functions_agents.experimental.durable_loop_tools import (
    DurableToolRegistry,
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


def _registered_function(app: df.DFApp, name: str) -> Any:
    for builder in app._function_builders:
        function = builder._function
        if function._name == name:
            return getattr(function._func, "__wrapped__", function._func)
    raise AssertionError(f"function {name!r} was not registered")


def _bindings(app: df.DFApp, name: str) -> list[dict[str, Any]]:
    for builder in app._function_builders:
        function = builder._function
        if function._name == name:
            return [binding.get_dict_repr() for binding in function._bindings]
    raise AssertionError(f"function {name!r} was not registered")


class _Request:
    def __init__(
        self,
        *,
        body: object | None = None,
        headers: dict[str, str] | None = None,
        path_params: dict[str, str] | None = None,
    ) -> None:
        self._body = body
        self.headers = headers or {}
        self.path_params = path_params or {}

    async def json(self) -> object:
        return self._body


class _EntityState:
    entity_exists = False
    entity_state = None


class _Client:
    def __init__(self) -> None:
        self.statuses: dict[str, Any] = {}
        self.starts: list[tuple[str, str, object]] = []
        self.events: list[tuple[str, str, object]] = []
        self.entity_state: dict[str, object] | None = None
        self.fail_events = 0
        self.fail_run_starts = 0

    async def get_status(
        self,
        instance_id: str,
        *,
        show_input: bool = False,
    ) -> Any:
        del show_input
        return self.statuses.get(instance_id)

    async def read_entity_state(self, entity_id: Any) -> _EntityState:
        del entity_id
        return _EntityState()

    async def start_new(
        self,
        name: str,
        *,
        instance_id: str,
        client_input: object,
    ) -> str:
        self.starts.append((name, instance_id, client_input))
        if name in {
            DURABLE_LOOP_ADMISSION_ORCHESTRATOR_NAME,
            DURABLE_LOOP_CONTROL_ORCHESTRATOR_NAME,
            DURABLE_LOOP_HUMAN_OUTBOX_ORCHESTRATOR_NAME,
        }:
            assert isinstance(client_input, dict)
            if name == DURABLE_LOOP_ADMISSION_ORCHESTRATOR_NAME:
                operation = "admit"
            elif name == DURABLE_LOOP_HUMAN_OUTBOX_ORCHESTRATOR_NAME:
                operation = "accept_human"
            else:
                operation = str(client_input["operation"])
            self.entity_state, output = apply_session_entity_operation(
                self.entity_state,
                operation,
                {
                    key: value
                    for key, value in client_input.items()
                    if key not in {"operation", "session_entity_key"}
                },
            )
            self.statuses[instance_id] = SimpleNamespace(
                custom_status=None,
                input=client_input,
                instance_id=instance_id,
                output=output,
                runtime_status=SimpleNamespace(name="Completed"),
            )
            return instance_id
        if name == DURABLE_LOOP_ORCHESTRATOR_NAME and self.fail_run_starts:
            self.fail_run_starts -= 1
            raise RuntimeError("injected start acknowledgement loss")
        self.statuses[instance_id] = SimpleNamespace(
            custom_status=None,
            input=client_input,
            instance_id=instance_id,
            output=None,
            runtime_status=SimpleNamespace(name="Pending"),
        )
        return instance_id

    async def raise_event(
        self,
        instance_id: str,
        event_name: str,
        event_data: object,
    ) -> None:
        if self.fail_events:
            self.fail_events -= 1
            raise RuntimeError("injected event delivery failure")
        self.events.append((instance_id, event_name, event_data))


@pytest.fixture
def durable_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[df.DFApp]:
    monkeypatch.setenv(DURABLE_LOOP_ENABLED_ENV, "true")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "openai")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_MODEL", "resolved-model")
    runtime = DurableLoopActivityRuntime(
        model=ScriptedOneStepModelProvider([]),
        tools=DurableToolRegistry().build_dispatcher(),
        compactor=DeterministicContextCompactor(),
        content=InMemoryDurableContentStore(),
    )
    set_durable_loop_activity_runtime_factory(lambda: runtime)
    _write_agent(tmp_path)
    app = app_module.create_function_app(tmp_path)
    assert isinstance(app, df.DFApp)
    try:
        yield app
    finally:
        reset_durable_loop_activity_runtime_factory()


def test_private_http_routes_register_auth_and_durable_client(
    durable_app: df.DFApp,
) -> None:
    expected = {
        "durable_agent_run_start_v1": ("POST", "experimental/durable-agent-runs"),
        "durable_agent_run_status_v1": (
            "GET",
            "experimental/durable-agent-runs/{run_id}",
        ),
        "durable_agent_run_result_v1": (
            "GET",
            "experimental/durable-agent-runs/{run_id}/result",
        ),
        "durable_agent_run_cancel_v1": (
            "POST",
            "experimental/durable-agent-runs/{run_id}/cancel",
        ),
        "durable_agent_run_human_input_v1": (
            "POST",
            "experimental/durable-agent-runs/{run_id}/input/{request_id}",
        ),
    }
    for name, (method, route) in expected.items():
        bindings = _bindings(durable_app, name)
        assert [binding["type"] for binding in bindings] == [
            "durableClient",
            "httpTrigger",
            "http",
        ]
        assert bindings[1]["route"] == route
        assert method in [str(item) for item in bindings[1]["methods"]]
        assert str(bindings[1]["authLevel"]).lower() == "function"


@pytest.mark.asyncio
async def test_private_gate_blocks_legacy_chat_stream_and_mcp_execution(
    durable_app: df.DFApp,
) -> None:
    chat = _registered_function(durable_app, "chat")
    stream = _registered_function(durable_app, "chat_stream")
    mcp = _registered_function(durable_app, "mcp_agent_chat")
    request = _Request(body={"prompt": "hello"})

    chat_response = await chat(request)
    stream_response = await stream(request)
    mcp_response = await mcp(
        json.dumps({"arguments": {"prompt": "hello"}})
    )

    assert chat_response.status_code == 409
    assert stream_response.status_code == 409
    assert "Legacy MCP agent execution is disabled" in mcp_response


@pytest.mark.asyncio
async def test_start_route_returns_202_urls_and_deduplicates(
    durable_app: df.DFApp,
) -> None:
    start = _registered_function(durable_app, "durable_agent_run_start_v1")
    client = _Client()
    request = _Request(
        body={"prompt": "hello", "request_id": "request-1"},
        headers={"x-ms-session-id": "session-1"},
    )

    first = await start(request, client)
    second = await start(request, client)
    body = json.loads(first.body)

    assert first.status_code == 202
    assert second.status_code == 202
    assert body["session_id"] == "session-1"
    assert body["status"] == "Pending"
    assert body["status_url"].endswith(body["run_id"])
    assert body["result_url"].endswith(f"{body['run_id']}/result")
    assert body["cancel_url"].endswith(f"{body['run_id']}/cancel")
    assert [item[0] for item in client.starts] == [
        DURABLE_LOOP_ADMISSION_ORCHESTRATOR_NAME,
        DURABLE_LOOP_ORCHESTRATOR_NAME,
        DURABLE_LOOP_ADMISSION_ORCHESTRATOR_NAME,
    ]
    assert "hello" not in json.dumps(client.starts)
    assert "Test." not in json.dumps(client.starts)
    durable_input = DurableOrchestrationInputV1.model_validate_json(
        json.dumps(client.statuses[body["run_id"]].input)
    )
    document = await get_protocol_model(
        get_durable_loop_activity_runtime().content,
        durable_input.run_document_ref,
        DurableRunDocumentV1,
    )
    assert document.plan.model == "resolved-model"
    assert document.plan.provider == "openai"
    assert durable_input.identity.deployment_hash == canonical_hash(
        {
            "api_version": "responses-v1",
            "endpoint": "https://api.openai.com/v1",
            "model": "resolved-model",
            "provider": "openai",
        }
    )


@pytest.mark.asyncio
async def test_start_route_rejects_conflicting_request_body(
    durable_app: df.DFApp,
) -> None:
    start = _registered_function(durable_app, "durable_agent_run_start_v1")
    client = _Client()
    headers = {"x-ms-session-id": "session-1"}
    first = await start(
        _Request(
            body={"prompt": "hello", "request_id": "request-1"},
            headers=headers,
        ),
        client,
    )
    assert first.status_code == 202

    conflict = await start(
        _Request(
            body={"prompt": "different", "request_id": "request-1"},
            headers=headers,
        ),
        client,
    )

    assert conflict.status_code == 409
    assert json.loads(conflict.body) == {"error": "idempotency_conflict"}


@pytest.mark.asyncio
async def test_start_retry_recovers_entity_admission_after_lost_start_ack(
    durable_app: df.DFApp,
) -> None:
    start = _registered_function(durable_app, "durable_agent_run_start_v1")
    client = _Client()
    client.fail_run_starts = 1
    request = _Request(
        body={"prompt": "hello", "request_id": "request-1"},
        headers={"x-ms-session-id": "session-1"},
    )

    ambiguous = await start(request, client)
    recovered = await start(request, client)

    ambiguous_body = json.loads(ambiguous.body)
    recovered_body = json.loads(recovered.body)
    assert ambiguous.status_code == 202
    assert ambiguous_body["possibly_committed"] is True
    assert recovered.status_code == 202
    assert recovered_body["run_id"] == ambiguous_body["run_id"]
    assert recovered_body["run_id"] in client.statuses


@pytest.mark.asyncio
async def test_purged_completed_duplicate_returns_terminal_receipt_without_relaunch(
    durable_app: df.DFApp,
) -> None:
    start = _registered_function(durable_app, "durable_agent_run_start_v1")
    client = _Client()
    request = _Request(
        body={"prompt": "hello", "request_id": "request-1"},
        headers={"x-ms-session-id": "session-1"},
    )
    accepted = await start(request, client)
    run_id = json.loads(accepted.body)["run_id"]
    durable_input = DurableOrchestrationInputV1.model_validate_json(
        json.dumps(client.statuses[run_id].input)
    )
    response_ref = await get_durable_loop_activity_runtime().content.put_bytes(
        kind="final-response",
        payload=b"done",
        media_type="text/plain",
        retention_class="result",
    )
    commit_payload = {
        "context_ref": durable_input.run_document_ref.model_dump(mode="json"),
        "expected_generation": 0,
        "request_hash": durable_input.identity.request_hash,
        "response_ref": response_ref.model_dump(mode="json"),
        "run_id": run_id,
    }
    commit_payload["commit_key"] = canonical_hash(commit_payload)
    client.entity_state, completed = apply_session_entity_operation(
        client.entity_state,
        "complete",
        commit_payload,
    )
    assert completed["disposition"] == "completed"
    client.statuses.pop(run_id)
    starts_before = len(
        [item for item in client.starts if item[0] == DURABLE_LOOP_ORCHESTRATOR_NAME]
    )

    replay = await start(request, client)

    assert replay.status_code == 200
    assert json.loads(replay.body)["response"] == "done"
    assert (
        len(
            [
                item
                for item in client.starts
                if item[0] == DURABLE_LOOP_ORCHESTRATOR_NAME
            ]
        )
        == starts_before
    )


@pytest.mark.asyncio
async def test_cancel_route_raises_reserved_cancel_event(
    durable_app: df.DFApp,
) -> None:
    start = _registered_function(durable_app, "durable_agent_run_start_v1")
    cancel = _registered_function(durable_app, "durable_agent_run_cancel_v1")
    client = _Client()
    response = await start(
        _Request(
            body={"prompt": "hello", "request_id": "request-1"},
            headers={"x-ms-session-id": "session-1"},
        ),
        client,
    )
    run_id = json.loads(response.body)["run_id"]

    cancelled = await cancel(
        _Request(path_params={"run_id": run_id}),
        client,
    )

    assert cancelled.status_code == 202
    assert json.loads(cancelled.body)["delivery"] == "delivered"
    assert DURABLE_LOOP_CANCEL_DELIVERY_ORCHESTRATOR_NAME in [
        item[0] for item in client.starts
    ]
    assert client.events[-1][0:2] == (run_id, DURABLE_LOOP_CANCEL_EVENT_NAME)


@pytest.mark.asyncio
async def test_cancel_route_starts_durable_delivery_when_direct_event_fails(
    durable_app: df.DFApp,
) -> None:
    start = _registered_function(durable_app, "durable_agent_run_start_v1")
    cancel = _registered_function(durable_app, "durable_agent_run_cancel_v1")
    client = _Client()
    response = await start(
        _Request(
            body={"prompt": "hello", "request_id": "request-1"},
            headers={"x-ms-session-id": "session-1"},
        ),
        client,
    )
    run_id = json.loads(response.body)["run_id"]
    client.fail_events = 1

    cancelled = await cancel(
        _Request(path_params={"run_id": run_id}),
        client,
    )

    assert cancelled.status_code == 202
    assert json.loads(cancelled.body)["delivery"] == "retry_pending"
    assert DURABLE_LOOP_CANCEL_DELIVERY_ORCHESTRATOR_NAME in [
        item[0] for item in client.starts
    ]


@pytest.mark.asyncio
async def test_human_input_route_uses_durable_outbox_and_server_event_name(
    durable_app: df.DFApp,
) -> None:
    submit = _registered_function(
        durable_app,
        "durable_agent_run_human_input_v1",
    )
    client = _Client()
    start = _registered_function(durable_app, "durable_agent_run_start_v1")
    accepted = await start(
        _Request(
            body={"prompt": "hello", "request_id": "request-1"},
            headers={"x-ms-session-id": "session-1"},
        ),
        client,
    )
    accepted_run_id = json.loads(accepted.body)["run_id"]
    accepted_status = client.statuses[accepted_run_id]
    durable_input = DurableOrchestrationInputV1.model_validate_json(
        json.dumps(accepted_status.input)
    )
    runtime = get_durable_loop_activity_runtime()
    question_ref = await runtime.content.put_bytes(
        kind="human-question",
        payload=b"Which region?",
        media_type="text/plain",
        retention_class="run",
    )
    human = HumanInputRequestV1(
        request_id="human-1",
        run_id=accepted_run_id,
        session_id=durable_input.identity.session_id,
        generation=1,
        turn_index=0,
        step_index=0,
        call_id="call-1",
        call_key="a" * 64,
        request_hash="b" * 64,
        question_ref=question_ref,
        choices=("eastus2", "westus3"),
        allow_free_text=False,
        actor_policy_hash=durable_input.identity.owner_hash,
        event_name="answer:1:server-generated",
        issued_at=datetime(2026, 9, 4, tzinfo=UTC),
        expires_at=datetime(2026, 9, 4, tzinfo=UTC) + timedelta(hours=1),
        record_version=1,
    )
    request_ref = await put_protocol_model(
        runtime.content,
        kind="human-request",
        model=human,
    )
    client.entity_state, _ = apply_session_entity_operation(
        client.entity_state,
        "open_human",
        {
            "call_key": human.call_key,
            "generation": human.generation,
            "owner_hash": durable_input.identity.owner_hash,
            "request_id": human.request_id,
            "request_ref": request_ref.model_dump(mode="json"),
            "run_id": human.run_id,
        },
    )
    accepted_status.custom_status = {
        "phase": "human_wait",
        "question_ref": question_ref.model_dump(mode="json"),
        "request_id": human.request_id,
        "request_ref": request_ref.model_dump(mode="json"),
        "status": "Waiting",
    }
    status_handler = _registered_function(
        durable_app,
        "durable_agent_run_status_v1",
    )
    pending = await status_handler(
        _Request(path_params={"run_id": accepted_run_id}),
        client,
    )
    pending_body = json.loads(pending.body)
    assert pending_body["human_input"]["question"] == "Which region?"
    assert pending_body["human_input"]["choices"] == ["eastus2", "westus3"]

    response = await submit(
        _Request(
            body={"answer": "westus3"},
            headers={"Idempotency-Key": "submission-1"},
            path_params={
                "request_id": "human-1",
                "run_id": accepted_run_id,
            },
        ),
        client,
    )

    assert response.status_code == 202
    start_names = [item[0] for item in client.starts]
    assert DURABLE_LOOP_HUMAN_OUTBOX_ORCHESTRATOR_NAME in start_names
    assert DURABLE_LOOP_HUMAN_DELIVERY_ORCHESTRATOR_NAME in start_names
    assert client.events[-1][0:2] == (
        accepted_run_id,
        "answer:1:server-generated",
    )
    assert "westus3" not in json.dumps(client.starts)
    assert "westus3" not in json.dumps(client.events)
    assert "call-1" not in json.dumps(client.entity_state)
    stored_objects = runtime.content.object_count

    conflict = await submit(
        _Request(
            body={"answer": "eastus2"},
            headers={"Idempotency-Key": "submission-2"},
            path_params={
                "request_id": "human-1",
                "run_id": accepted_run_id,
            },
        ),
        client,
    )
    assert conflict.status_code == 409
    assert json.loads(conflict.body) == {"error": "human_input_conflict"}
    assert runtime.content.object_count == stored_objects
