from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import azure.durable_functions as df
import pytest

from azure_functions_agents import app as app_module
from azure_functions_agents.experimental.durable_loop import create_run_identity
from azure_functions_agents.experimental.durable_loop_activities import (
    DeterministicContextCompactor,
    DurableLoopProviderTerminalError,
    InMemoryDurableContentStore,
    ScriptedModelStep,
    ScriptedOneStepModelProvider,
    get_protocol_model,
    put_protocol_model,
)
from azure_functions_agents.experimental.durable_loop_config import (
    DURABLE_LOOP_ENABLED_ENV,
    DurableLoopSettings,
)
from azure_functions_agents.experimental.durable_loop_protocol import (
    CheckpointStateV1,
    ContentRefV1,
    DurableFaultProfile,
    DurableLoopPlanDocumentV1,
    DurableLoopRunStatus,
    DurableOrchestrationInputV1,
    DurableRunDocumentV1,
    FrozenToolDescriptorV1,
    HumanEventDeliveryResultV1,
    HumanEventDeliveryStatus,
    MAFMessageBundleV1,
    ModelOperationStatus,
    ModelOperationV1,
    ModelStepActivityResultV1,
    SandboxExecutionProfile,
    ToolBehavior,
    ToolProvenance,
    ToolRequestV1,
    ToolResultRefV1,
    ToolResultStatus,
    ToolResultV1,
    UsageV1,
    WorkingContextV1,
    canonical_hash,
    tool_request_hash,
)
from azure_functions_agents.experimental.durable_loop_receipts import (
    DurableOneShotFaults,
    InMemoryDurableKeyedDocumentStore,
)
from azure_functions_agents.experimental.durable_loop_registration import (
    DURABLE_LOOP_APPEND_ACTIVITY_NAME,
    DURABLE_LOOP_CANCEL_DELIVERY_ORCHESTRATOR_NAME,
    DURABLE_LOOP_CLEANUP_ACTIVITY_NAME,
    DURABLE_LOOP_COMPACTION_ACTIVITY_NAME,
    DURABLE_LOOP_FAULT_ACTIVITY_NAME,
    DURABLE_LOOP_HUMAN_ACTIVITY_NAME,
    DURABLE_LOOP_HUMAN_DELIVERY_ACTIVITY_NAME,
    DURABLE_LOOP_HUMAN_DELIVERY_ORCHESTRATOR_NAME,
    DURABLE_LOOP_MODEL_ACTIVITY_NAME,
    DURABLE_LOOP_MODEL_POLL_ACTIVITY_NAME,
    DURABLE_LOOP_ORCHESTRATOR_NAME,
    DURABLE_LOOP_TOOL_ACTIVITY_NAME,
    DurableLoopActivityRuntime,
    apply_session_entity_operation,
    reset_durable_loop_activity_runtime_factory,
    set_durable_loop_activity_runtime_factory,
)
from azure_functions_agents.experimental.durable_loop_tools import (
    DurableToolRegistry,
)
from azure_functions_agents.strict_json import canonical_json_bytes

_HASH = "a" * 64


class _Task:
    def __init__(self, result: object = None) -> None:
        self.result = result
        self.is_completed = False
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class _OrchestrationContext:
    def __init__(
        self,
        payload: dict[str, object],
        *,
        activity_results: dict[str, object],
        cancelled: bool = False,
        cancellation_sequence: list[bool] | None = None,
    ) -> None:
        self._payload = payload
        self._activity_results = activity_results
        self._cancelled = cancelled
        self._cancellation_sequence = list(cancellation_sequence or [])
        self.current_utc_datetime = datetime(2026, 9, 4, tzinfo=UTC)
        self.activity_calls: list[tuple[str, object]] = []
        self.entity_calls: list[tuple[str, object]] = []
        self.event_names: list[str] = []
        self.timers: list[_Task] = []
        self.custom_statuses: list[object] = []
        self.continued: list[object] = []

    def get_input(self) -> dict[str, object]:
        return self._payload

    def call_entity(
        self,
        _entity: object,
        operation: str,
        payload: object,
    ) -> _Task:
        self.entity_calls.append((operation, payload))
        if operation == "admit":
            return _Task({"disposition": "replayed"})
        if operation == "is_cancelled":
            cancelled = (
                self._cancellation_sequence.pop(0)
                if self._cancellation_sequence
                else self._cancelled
            )
            return _Task({"cancelled": cancelled})
        if operation == "mark_running":
            return _Task({"disposition": "running"})
        if operation == "complete":
            return _Task(
                {"committed_generation": 1, "disposition": "completed"}
            )
        if operation == "abort":
            return _Task({"disposition": "aborted"})
        if operation == "open_human":
            return _Task({"disposition": "opened"})
        return _Task({"disposition": "found"})

    def call_activity(self, name: str, payload: object) -> _Task:
        self.activity_calls.append((name, payload))
        result = self._activity_results[name]
        if isinstance(result, list):
            result = result.pop(0)
        return _Task(result)

    def wait_for_external_event(self, name: str) -> _Task:
        self.event_names.append(name)
        return _Task()

    def create_timer(self, _deadline: datetime) -> _Task:
        task = _Task()
        self.timers.append(task)
        return task

    def task_any(self, tasks: list[_Task]) -> _Task:
        return _Task(tasks[0])

    def task_all(self, tasks: list[_Task]) -> _Task:
        return _Task([task.result for task in tasks])

    def set_custom_status(self, status: object) -> None:
        self.custom_statuses.append(status)

    def continue_as_new(self, payload: object) -> None:
        self.continued.append(payload)


def _write_agent(root: Path) -> None:
    (root / "main.agent.md").write_text(
        (
            "---\n"
            "name: Main\n"
            "description: Durable test\n"
            "builtin_endpoints: true\n"
            "model: model\n"
            "---\n"
            "Test."
        ),
        encoding="utf-8",
    )


def _registered(app: df.DFApp, name: str) -> Callable[..., Any]:
    for builder in app._function_builders:
        function = builder._function
        if function._name != name:
            continue
        registered = function._func
        if name in {
            DURABLE_LOOP_ORCHESTRATOR_NAME,
            DURABLE_LOOP_HUMAN_DELIVERY_ORCHESTRATOR_NAME,
            DURABLE_LOOP_CANCEL_DELIVERY_ORCHESTRATOR_NAME,
        }:
            assert registered.__closure__ is not None
            return registered.__closure__[0].cell_contents
        return registered
    raise AssertionError(f"function {name!r} was not registered")


def _settings() -> DurableLoopSettings:
    settings = DurableLoopSettings.from_environment(
        {DURABLE_LOOP_ENABLED_ENV: "true"}
    )
    assert settings is not None
    return settings


async def _run_input(
    content: InMemoryDurableContentStore,
    catalog,
) -> DurableOrchestrationInputV1:
    settings = _settings()
    identity = create_run_identity(
        run_id="run-1",
        session_id="session-1",
        request_id="request-1",
        request_body={"prompt": "sensitive prompt"},
        owner_hash="b" * 64,
        agent_slug="main",
        agent_hash="c" * 64,
        catalog_hash=catalog.catalog_hash,
        deployment_hash="d" * 64,
        tool_package_hash=catalog.package_hash,
        policy_hash=catalog.policy_hash,
        settings=settings,
        now=datetime(2026, 9, 4, tzinfo=UTC),
    )
    messages: tuple[dict[str, object], ...] = (
        {
            "contents": [{"text": "sensitive prompt", "type": "text"}],
            "role": "user",
        },
    )
    bundle = MAFMessageBundleV1.create(
        messages=messages,
        maf_core_version="1.17.0",
        provider="fake",
        model="model",
        api_version="responses-v1",
    )
    working = WorkingContextV1(
        bundle=bundle,
        compaction_generation=0,
        source_audit_hash=bundle.bundle_hash,
        source_start=0,
        source_end=1,
        estimated_tokens=4,
    )
    checkpoint = CheckpointStateV1(
        identity=identity,
        status=DurableLoopRunStatus.PENDING,
        audit_bundle=bundle,
        working_context=working,
        audit_head_hash=bundle.bundle_hash,
        completed_model_steps=0,
        completed_tool_calls=0,
        human_wait_count=0,
        next_model_step=0,
        committed_session_generation=0,
        continue_as_new_generation=0,
        checkpoints_in_generation=0,
    )
    document = DurableRunDocumentV1(
        plan=DurableLoopPlanDocumentV1(
            instructions="sensitive instructions",
            catalog=catalog,
            model_settings={},
            maf_core_version="1.17.0",
            provider="fake",
            model="model",
            api_version="responses-v1",
            settings=asdict(settings),
        ),
        checkpoint=checkpoint,
    )
    reference = await put_protocol_model(
        content,
        kind="run-document",
        model=document,
    )
    return DurableOrchestrationInputV1(
        identity=identity,
        run_document_ref=reference,
        session_entity_key="e" * 64,
        working_context_bytes=len(canonical_json_bytes(messages)),
    )


def _drive_to_completion(generator: Any) -> object:
    try:
        task = next(generator)
        while True:
            task.is_completed = True
            task = generator.send(task.result)
    except StopIteration as stopped:
        return stopped.value


def test_session_entity_cancellation_fences_final_commit() -> None:
    state, admitted = apply_session_entity_operation(
        None,
        "admit",
        {
            "request_hash": "a" * 64,
            "request_id_hash": "b" * 64,
            "run_id": "run-1",
        },
    )
    assert admitted["disposition"] == "admitted"
    state, running = apply_session_entity_operation(
        state,
        "mark_running",
        {"run_id": "run-1"},
    )
    assert running["disposition"] == "running"
    state, cancelled = apply_session_entity_operation(
        state,
        "cancel",
        {"run_id": "run-1"},
    )
    assert cancelled["disposition"] == "accepted"
    completion_data = {
        "context_ref": {"object_id": "content/context"},
        "expected_generation": 0,
        "request_hash": "a" * 64,
        "response_ref": {"object_id": "content/response"},
        "run_id": "run-1",
    }
    completion_data["commit_key"] = canonical_hash(completion_data)

    state, completion = apply_session_entity_operation(
        state,
        "complete",
        completion_data,
    )

    assert completion == {"disposition": "cancelled"}
    assert state["active_run_id"] == "run-1"


@pytest.mark.asyncio
async def test_model_activity_returns_only_refs_and_preserves_full_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DURABLE_LOOP_ENABLED_ENV, "true")
    registry = DurableToolRegistry()
    catalog = registry.catalog(policy_hash=_HASH, package_hash="f" * 64)
    content = InMemoryDurableContentStore()
    runtime = DurableLoopActivityRuntime(
        model=ScriptedOneStepModelProvider(
            [ScriptedModelStep(final_text="sensitive final answer")]
        ),
        tools=registry.build_dispatcher(),
        compactor=DeterministicContextCompactor(),
        content=content,
    )
    set_durable_loop_activity_runtime_factory(lambda: runtime)
    _write_agent(tmp_path)
    app = app_module.create_function_app(tmp_path)
    assert isinstance(app, df.DFApp)
    run_input = await _run_input(content, catalog)

    try:
        activity = _registered(app, DURABLE_LOOP_MODEL_ACTIVITY_NAME)
        raw_result = await activity(
            {
                "run_document_ref": run_input.run_document_ref.model_dump(
                    mode="json"
                )
            }
        )
    finally:
        reset_durable_loop_activity_runtime_factory()

    assert "sensitive prompt" not in json.dumps(raw_result)
    assert "sensitive final answer" not in json.dumps(raw_result)
    result = ModelStepActivityResultV1.model_validate_json(
        json.dumps(raw_result)
    )
    assert result.final_response_ref is not None
    assert result.written_bytes > result.final_response_ref.byte_length
    assert await content.get_bytes(result.final_response_ref) == b"sensitive final answer"
    document = await get_protocol_model(
        content,
        result.run_document_ref,
        DurableRunDocumentV1,
    )
    assert len(document.checkpoint.audit_bundle.messages) == 2
    assert document.checkpoint.audit_head_hash == document.checkpoint.audit_bundle.bundle_hash


@pytest.mark.asyncio
async def test_model_activity_returns_typed_incomplete_without_final_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class IncompleteProvider:
        async def run_one_step(self, _request):
            raise DurableLoopProviderTerminalError(
                code="model_response_incomplete",
                finish_reason="length",
                usage=UsageV1(
                    input_tokens=70,
                    output_tokens=12000,
                    reasoning_tokens=12000,
                ),
            )

    monkeypatch.setenv(DURABLE_LOOP_ENABLED_ENV, "true")
    registry = DurableToolRegistry()
    catalog = registry.catalog(policy_hash=_HASH, package_hash="f" * 64)
    content = InMemoryDurableContentStore()
    runtime = DurableLoopActivityRuntime(
        model=IncompleteProvider(),
        tools=registry.build_dispatcher(),
        compactor=DeterministicContextCompactor(),
        content=content,
    )
    set_durable_loop_activity_runtime_factory(lambda: runtime)
    _write_agent(tmp_path)
    app = app_module.create_function_app(tmp_path)
    assert isinstance(app, df.DFApp)
    run_input = await _run_input(content, catalog)
    try:
        activity = _registered(app, DURABLE_LOOP_MODEL_ACTIVITY_NAME)
        raw_result = await activity(
            {
                "run_document_ref": run_input.run_document_ref.model_dump(
                    mode="json"
                )
            }
        )
    finally:
        reset_durable_loop_activity_runtime_factory()

    result = ModelStepActivityResultV1.model_validate_json(
        canonical_json_bytes(raw_result)
    )
    assert result.error is not None
    assert result.error.code == "model_response_incomplete"
    assert result.final_response_ref is None
    assert result.tool_calls == ()
    assert result.usage.reasoning_tokens == 12000


@pytest.mark.asyncio
async def test_model_and_tool_activities_keep_arguments_and_results_out_of_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DURABLE_LOOP_ENABLED_ENV, "true")
    registry = DurableToolRegistry()
    descriptor = FrozenToolDescriptorV1(
        name="lookup",
        description="Lookup.",
        parameters={"additionalProperties": True, "type": "object"},
        provenance=ToolProvenance.REMOTE,
        behavior=ToolBehavior.READ_ONLY,
        parallel_safe=True,
    )

    def lookup(_arguments, _call_key):
        return {"value": "result-secret"}

    registry.register(descriptor, lookup)
    catalog = registry.catalog(policy_hash=_HASH, package_hash="f" * 64)
    content = InMemoryDurableContentStore()
    runtime = DurableLoopActivityRuntime(
        model=ScriptedOneStepModelProvider(
            [
                ScriptedModelStep(
                    calls=(
                        (
                            "call-1",
                            "lookup",
                            {"value": "argument-secret"},
                        ),
                    )
                )
            ]
        ),
        tools=registry.build_dispatcher(),
        compactor=DeterministicContextCompactor(),
        content=content,
    )
    set_durable_loop_activity_runtime_factory(lambda: runtime)
    _write_agent(tmp_path)
    app = app_module.create_function_app(tmp_path)
    assert isinstance(app, df.DFApp)
    run_input = await _run_input(content, catalog)
    try:
        model_activity = _registered(app, DURABLE_LOOP_MODEL_ACTIVITY_NAME)
        model_raw = await model_activity(
            {
                "run_document_ref": run_input.run_document_ref.model_dump(
                    mode="json"
                )
            }
        )
        model_result = ModelStepActivityResultV1.model_validate_json(
            json.dumps(model_raw)
        )
        tool_activity = _registered(app, DURABLE_LOOP_TOOL_ACTIVITY_NAME)
        tool_raw = await tool_activity(
            {
                "request_ref": model_result.tool_calls[
                    0
                ].request_ref.model_dump(mode="json")
            }
        )
        append_activity = _registered(app, DURABLE_LOOP_APPEND_ACTIVITY_NAME)
        append_raw = await append_activity(
            {
                "result_refs": [tool_raw],
                "run_document_ref": model_result.run_document_ref.model_dump(
                    mode="json"
                ),
            }
        )
    finally:
        reset_durable_loop_activity_runtime_factory()

    assert "argument-secret" not in json.dumps(model_raw)
    assert "result-secret" not in json.dumps(tool_raw)
    assert "argument-secret" not in json.dumps(append_raw)
    assert "result-secret" not in json.dumps(append_raw)
    assert "call-1" not in json.dumps(model_raw)
    assert "call-1" not in json.dumps(tool_raw)
    assert "call-1" not in json.dumps(append_raw)
    tool_ref = ToolResultRefV1.model_validate_json(json.dumps(tool_raw))
    assert tool_ref.written_bytes == tool_ref.result_ref.byte_length
    result = await get_protocol_model(
        content,
        tool_ref.result_ref,
        ToolResultV1,
    )
    assert result.value == {"value": "result-secret"}
    appended = await get_protocol_model(
        content,
        ContentRefV1.model_validate(append_raw["run_document_ref"]),
        DurableRunDocumentV1,
    )
    assert appended.checkpoint.completed_tool_calls == 1
    assert append_raw["written_bytes"] > 0


@pytest.mark.asyncio
async def test_registered_tool_activity_enforces_configured_payload_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecordingDispatcher:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def dispatch(self, request: ToolRequestV1) -> ToolResultV1:
            self.calls.append(request.tool_name)
            return ToolResultV1(
                run_id=request.run_id,
                step_index=request.step_index,
                call_ordinal=request.call_ordinal,
                provider_call_id=request.provider_call_id,
                call_key=request.call_key,
                request_hash=request.request_hash,
                tool_name=request.tool_name,
                status=ToolResultStatus.SUCCEEDED,
                value={"value": "x" * 1100},
                elapsed_ms=1.0,
            )

    monkeypatch.setenv(DURABLE_LOOP_ENABLED_ENV, "true")
    registry = DurableToolRegistry()
    catalog = registry.catalog(policy_hash=_HASH, package_hash="f" * 64)
    content = InMemoryDurableContentStore()
    dispatcher = RecordingDispatcher()
    runtime = DurableLoopActivityRuntime(
        model=ScriptedOneStepModelProvider([]),
        tools=dispatcher,
        compactor=DeterministicContextCompactor(),
        content=content,
    )
    set_durable_loop_activity_runtime_factory(lambda: runtime)
    _write_agent(tmp_path)
    app = app_module.create_function_app(tmp_path)
    assert isinstance(app, df.DFApp)
    run_input = await _run_input(content, catalog)
    document = await get_protocol_model(
        content,
        run_input.run_document_ref,
        DurableRunDocumentV1,
    )

    def request_for(
        *,
        name: str,
        arguments: dict[str, object],
        argument_limit: int,
        result_limit: int,
    ) -> ToolRequestV1:
        request_hash = tool_request_hash(
            tool_name=name,
            arguments=arguments,
            behavior=ToolBehavior.READ_ONLY,
            provenance=ToolProvenance.REMOTE,
            argument_byte_limit=argument_limit,
            result_byte_limit=result_limit,
            policy_hash=catalog.policy_hash,
            catalog_hash=catalog.catalog_hash,
            package_hash=catalog.package_hash,
        )
        return ToolRequestV1(
            run_id=document.checkpoint.identity.run_id,
            step_index=0,
            call_ordinal=0,
            provider_call_id=f"{name}-call",
            call_key=canonical_hash({"name": name}),
            tool_name=name,
            provenance=ToolProvenance.REMOTE,
            behavior=ToolBehavior.READ_ONLY,
            arguments=arguments,
            argument_byte_limit=argument_limit,
            result_byte_limit=result_limit,
            request_hash=request_hash,
            policy_hash=catalog.policy_hash,
            catalog_hash=catalog.catalog_hash,
            package_hash=catalog.package_hash,
            deadline=datetime(2026, 9, 4, tzinfo=UTC) + timedelta(minutes=1),
        )

    try:
        activity = _registered(app, DURABLE_LOOP_TOOL_ACTIVITY_NAME)
        argument_request = request_for(
            name="argument-limit",
            arguments={"value": "x" * 1100},
            argument_limit=1024,
            result_limit=2048,
        )
        argument_ref = await put_protocol_model(
            content,
            kind="tool-request",
            model=argument_request,
        )
        argument_raw = await activity(
            {"request_ref": argument_ref.model_dump(mode="json")}
        )
        result_request = request_for(
            name="result-limit",
            arguments={"value": "small"},
            argument_limit=2048,
            result_limit=1024,
        )
        result_request_ref = await put_protocol_model(
            content,
            kind="tool-request",
            model=result_request,
        )
        result_raw = await activity(
            {"request_ref": result_request_ref.model_dump(mode="json")}
        )
    finally:
        reset_durable_loop_activity_runtime_factory()

    argument_result_ref = ToolResultRefV1.model_validate_json(
        canonical_json_bytes(argument_raw)
    )
    argument_result = await get_protocol_model(
        content,
        argument_result_ref.result_ref,
        ToolResultV1,
    )
    result_result_ref = ToolResultRefV1.model_validate_json(
        canonical_json_bytes(result_raw)
    )
    result_result = await get_protocol_model(
        content,
        result_result_ref.result_ref,
        ToolResultV1,
    )
    assert argument_result.error is not None
    assert argument_result.error.code == "tool_arguments_too_large"
    assert result_result.error is not None
    assert result_result.error.code == "tool_result_too_large"
    assert dispatcher.calls == ["result-limit"]


@pytest.mark.asyncio
async def test_tool_activity_ack_loss_replays_external_receipt_without_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DURABLE_LOOP_ENABLED_ENV, "true")
    registry = DurableToolRegistry()
    effects = 0

    def write(_arguments, _call_key):
        nonlocal effects
        effects += 1
        return {"effects": effects}

    descriptor = FrozenToolDescriptorV1(
        name="write",
        description="Idempotent write.",
        parameters={"additionalProperties": True, "type": "object"},
        provenance=ToolProvenance.REMOTE,
        behavior=ToolBehavior.IDEMPOTENT_WRITE,
    )
    registry.register(descriptor, write)
    catalog = registry.catalog(policy_hash=_HASH, package_hash="f" * 64)
    arguments = {"value": 1}
    request_hash = tool_request_hash(
        tool_name="write",
        arguments=arguments,
        behavior=descriptor.behavior,
        provenance=descriptor.provenance,
        policy_hash=catalog.policy_hash,
        catalog_hash=catalog.catalog_hash,
        package_hash=catalog.package_hash,
        fault_profile=DurableFaultProfile.TOOL_ACTIVITY_ACK_LOSS_ONCE,
    )
    request = ToolRequestV1(
        run_id="run-1",
        session_id="session-1",
        step_index=0,
        call_ordinal=0,
        provider_call_id="provider-call",
        call_key="1" * 64,
        tool_name="write",
        provenance=descriptor.provenance,
        behavior=descriptor.behavior,
        arguments=arguments,
        request_hash=request_hash,
        policy_hash=catalog.policy_hash,
        catalog_hash=catalog.catalog_hash,
        package_hash=catalog.package_hash,
        fault_profile=DurableFaultProfile.TOOL_ACTIVITY_ACK_LOSS_ONCE,
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )
    content = InMemoryDurableContentStore()
    request_ref = await put_protocol_model(
        content,
        kind="tool-request",
        model=request,
    )
    receipt_store = InMemoryDurableKeyedDocumentStore()
    runtime = DurableLoopActivityRuntime(
        model=ScriptedOneStepModelProvider([]),
        tools=registry.build_dispatcher(),
        compactor=DeterministicContextCompactor(),
        content=content,
        faults=DurableOneShotFaults(receipt_store, enabled=True),
    )
    set_durable_loop_activity_runtime_factory(lambda: runtime)
    _write_agent(tmp_path)
    app = app_module.create_function_app(tmp_path)
    assert isinstance(app, df.DFApp)
    try:
        activity = _registered(app, DURABLE_LOOP_TOOL_ACTIVITY_NAME)
        raw = await activity(
            {"request_ref": request_ref.model_dump(mode="json")}
        )
        replay_raw = await activity(
            {"request_ref": request_ref.model_dump(mode="json")}
        )
    finally:
        reset_durable_loop_activity_runtime_factory()

    result_ref = ToolResultRefV1.model_validate_json(
        canonical_json_bytes(raw)
    )
    result = await get_protocol_model(
        content,
        result_ref.result_ref,
        ToolResultV1,
    )
    assert effects == 1
    assert result.deduplicated is True
    replay_ref = ToolResultRefV1.model_validate_json(
        canonical_json_bytes(replay_raw)
    )
    replay = await get_protocol_model(
        content,
        replay_ref.result_ref,
        ToolResultV1,
    )
    assert replay.deduplicated is True


@pytest.mark.asyncio
async def test_mutating_tool_ack_loss_returns_ambiguous_after_one_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DURABLE_LOOP_ENABLED_ENV, "true")
    registry = DurableToolRegistry()
    effects = 0

    def write(_arguments, _call_key):
        nonlocal effects
        effects += 1
        return {"effects": effects}

    descriptor = FrozenToolDescriptorV1(
        name="unsafe_write",
        description="Unsafe write.",
        parameters={"additionalProperties": True, "type": "object"},
        provenance=ToolProvenance.REMOTE,
        behavior=ToolBehavior.MUTATING,
    )
    registry.register(descriptor, write)
    catalog = registry.catalog(policy_hash=_HASH, package_hash="f" * 64)
    arguments = {"value": 1}
    request_hash = tool_request_hash(
        tool_name=descriptor.name,
        arguments=arguments,
        behavior=descriptor.behavior,
        provenance=descriptor.provenance,
        policy_hash=catalog.policy_hash,
        catalog_hash=catalog.catalog_hash,
        package_hash=catalog.package_hash,
        fault_profile=DurableFaultProfile.TOOL_ACTIVITY_ACK_LOSS_ONCE,
    )
    request = ToolRequestV1(
        run_id="run-1",
        session_id="session-1",
        step_index=0,
        call_ordinal=0,
        provider_call_id="provider-call",
        call_key="1" * 64,
        tool_name=descriptor.name,
        provenance=descriptor.provenance,
        behavior=descriptor.behavior,
        arguments=arguments,
        request_hash=request_hash,
        policy_hash=catalog.policy_hash,
        catalog_hash=catalog.catalog_hash,
        package_hash=catalog.package_hash,
        fault_profile=DurableFaultProfile.TOOL_ACTIVITY_ACK_LOSS_ONCE,
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )
    content = InMemoryDurableContentStore()
    request_ref = await put_protocol_model(
        content,
        kind="tool-request",
        model=request,
    )
    runtime = DurableLoopActivityRuntime(
        model=ScriptedOneStepModelProvider([]),
        tools=registry.build_dispatcher(),
        compactor=DeterministicContextCompactor(),
        content=content,
        faults=DurableOneShotFaults(
            InMemoryDurableKeyedDocumentStore(),
            enabled=True,
        ),
    )
    set_durable_loop_activity_runtime_factory(lambda: runtime)
    _write_agent(tmp_path)
    app = app_module.create_function_app(tmp_path)
    assert isinstance(app, df.DFApp)
    try:
        raw = await _registered(app, DURABLE_LOOP_TOOL_ACTIVITY_NAME)(
            {"request_ref": request_ref.model_dump(mode="json")}
        )
    finally:
        reset_durable_loop_activity_runtime_factory()

    result_ref = ToolResultRefV1.model_validate_json(canonical_json_bytes(raw))
    result = await get_protocol_model(content, result_ref.result_ref, ToolResultV1)
    assert effects == 1
    assert result.status is ToolResultStatus.AMBIGUOUS
    assert result.error is not None
    assert result.error.possibly_committed is True


@pytest.mark.asyncio
async def test_registered_orchestrator_completes_with_refs_only_output() -> None:
    registry = DurableToolRegistry()
    catalog = registry.catalog(policy_hash=_HASH, package_hash="f" * 64)
    content = InMemoryDurableContentStore()
    run_input = await _run_input(content, catalog)
    final_ref = await content.put_text(
        kind="final",
        value="sensitive final answer",
        retention_class="result",
    )
    model_result = ModelStepActivityResultV1(
        run_document_ref=run_input.run_document_ref,
        step_index=0,
        final_response_ref=final_ref,
    )
    context = _OrchestrationContext(
        run_input.model_dump(mode="json"),
        activity_results={
            DURABLE_LOOP_MODEL_ACTIVITY_NAME: model_result.model_dump(mode="json")
        },
    )
    from azure_functions_agents.experimental.durable_loop_registration import (
        _run_durable_loop,
    )

    output = _drive_to_completion(
        _run_durable_loop(
            context,
            df.EntityId("durable_agent_session_entity_v1", "e" * 64),
            run_input,
        )
    )

    assert "sensitive final answer" not in json.dumps(output)
    assert output["status"] == "Completed"  # type: ignore[index]
    assert output["phase"] == "completed"  # type: ignore[index]
    assert output["model_steps"] == 1  # type: ignore[index]
    assert output["step_index"] == 1  # type: ignore[index]
    assert [operation for operation, _ in context.entity_calls] == [
        "admit",
        "mark_running",
        "is_cancelled",
        "is_cancelled",
        "complete",
    ]


@pytest.mark.asyncio
async def test_successful_retained_session_keeps_sandbox_for_next_turn() -> None:
    registry = DurableToolRegistry()
    catalog = registry.catalog(policy_hash=_HASH, package_hash="f" * 64)
    content = InMemoryDurableContentStore()
    run_input = (await _run_input(content, catalog)).model_copy(
        update={"sandbox_profile": SandboxExecutionProfile.RETAINED_SESSION}
    )
    final_ref = await content.put_text(
        kind="final",
        value="done",
        retention_class="result",
    )
    model_result = ModelStepActivityResultV1(
        run_document_ref=run_input.run_document_ref,
        step_index=0,
        final_response_ref=final_ref,
    )
    context = _OrchestrationContext(
        run_input.model_dump(mode="json"),
        activity_results={
            DURABLE_LOOP_MODEL_ACTIVITY_NAME: model_result.model_dump(mode="json")
        },
    )
    from azure_functions_agents.experimental.durable_loop_registration import (
        _run_durable_loop,
    )

    output = _drive_to_completion(
        _run_durable_loop(
            context,
            df.EntityId("durable_agent_session_entity_v1", "e" * 64),
            run_input,
        )
    )

    assert output["status"] == "Completed"  # type: ignore[index]
    assert all(
        name != DURABLE_LOOP_CLEANUP_ACTIVITY_NAME
        for name, _ in context.activity_calls
    )


@pytest.mark.asyncio
async def test_registered_orchestrator_parks_and_polls_background_response() -> None:
    registry = DurableToolRegistry()
    catalog = registry.catalog(policy_hash=_HASH, package_hash="f" * 64)
    content = InMemoryDurableContentStore()
    run_input = await _run_input(content, catalog)
    operation = ModelOperationV1(
        operation_key=canonical_hash({"operation": "model"}),
        run_id=run_input.identity.run_id,
        step_index=0,
        status=ModelOperationStatus.QUEUED,
        backend_binding_hash="b" * 64,
        deployment_hash=run_input.identity.deployment_hash,
        deadline=run_input.identity.active_deadline,
    )
    operation_ref = await put_protocol_model(
        content,
        kind="model-operation",
        model=operation,
    )
    final_ref = await content.put_text(
        kind="final",
        value="sensitive final answer",
        retention_class="result",
    )
    pending = ModelStepActivityResultV1(
        run_document_ref=run_input.run_document_ref,
        step_index=0,
        background_operation_ref=operation_ref,
        poll_after_seconds=2,
    )
    terminal = ModelStepActivityResultV1(
        run_document_ref=run_input.run_document_ref,
        step_index=0,
        final_response_ref=final_ref,
    )
    context = _OrchestrationContext(
        run_input.model_dump(mode="json"),
        activity_results={
            DURABLE_LOOP_MODEL_ACTIVITY_NAME: pending.model_dump(mode="json"),
            DURABLE_LOOP_MODEL_POLL_ACTIVITY_NAME: terminal.model_dump(mode="json"),
        },
    )
    from azure_functions_agents.experimental.durable_loop_registration import (
        _run_durable_loop,
    )

    output = _drive_to_completion(
        _run_durable_loop(
            context,
            df.EntityId("durable_agent_session_entity_v1", "e" * 64),
            run_input,
        )
    )

    assert output["status"] == DurableLoopRunStatus.COMPLETED.value  # type: ignore[index]
    assert len(context.timers) == 1
    assert [name for name, _payload in context.activity_calls] == [
        DURABLE_LOOP_MODEL_ACTIVITY_NAME,
        DURABLE_LOOP_MODEL_POLL_ACTIVITY_NAME,
    ]
    assert any(
        status["phase"] == "background_poll"  # type: ignore[index]
        for status in context.custom_statuses
    )


@pytest.mark.asyncio
async def test_commit_ack_loss_replays_same_entity_commit_receipt() -> None:
    registry = DurableToolRegistry()
    catalog = registry.catalog(policy_hash=_HASH, package_hash="f" * 64)
    content = InMemoryDurableContentStore()
    run_input = (await _run_input(content, catalog)).model_copy(
        update={"fault_profile": DurableFaultProfile.COMMIT_ACK_LOSS_ONCE}
    )
    final_ref = await content.put_text(
        kind="final",
        value="done",
        retention_class="result",
    )
    model_result = ModelStepActivityResultV1(
        run_document_ref=run_input.run_document_ref,
        step_index=0,
        final_response_ref=final_ref,
    )
    context = _OrchestrationContext(
        run_input.model_dump(mode="json"),
        activity_results={
            DURABLE_LOOP_MODEL_ACTIVITY_NAME: model_result.model_dump(mode="json"),
            DURABLE_LOOP_FAULT_ACTIVITY_NAME: {"injected": True},
        },
    )
    from azure_functions_agents.experimental.durable_loop_registration import (
        _run_durable_loop,
    )

    output = _drive_to_completion(
        _run_durable_loop(
            context,
            df.EntityId("durable_agent_session_entity_v1", "e" * 64),
            run_input,
        )
    )

    assert output["status"] == DurableLoopRunStatus.COMPLETED.value  # type: ignore[index]
    completions = [
        payload
        for operation, payload in context.entity_calls
        if operation == "complete"
    ]
    assert len(completions) == 2
    assert completions[0] == completions[1]


@pytest.mark.asyncio
async def test_registered_orchestrator_does_not_commit_after_cancellation_wins() -> None:
    registry = DurableToolRegistry()
    catalog = registry.catalog(policy_hash=_HASH, package_hash="f" * 64)
    content = InMemoryDurableContentStore()
    run_input = await _run_input(content, catalog)
    final_ref = await content.put_text(
        kind="final",
        value="must not commit",
        retention_class="result",
    )
    model_result = ModelStepActivityResultV1(
        run_document_ref=run_input.run_document_ref,
        step_index=0,
        final_response_ref=final_ref,
    )
    context = _OrchestrationContext(
        run_input.model_dump(mode="json"),
        activity_results={
            DURABLE_LOOP_MODEL_ACTIVITY_NAME: model_result.model_dump(mode="json")
        },
        cancellation_sequence=[False, True],
    )
    from azure_functions_agents.experimental.durable_loop_registration import (
        _run_durable_loop,
    )

    output = _drive_to_completion(
        _run_durable_loop(
            context,
            df.EntityId("durable_agent_session_entity_v1", "e" * 64),
            run_input,
        )
    )

    assert output == {"status": DurableLoopRunStatus.CANCELLED.value}
    assert [operation for operation, _ in context.entity_calls] == [
        "admit",
        "mark_running",
        "is_cancelled",
        "is_cancelled",
        "abort",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("budget_updates", "cursor_updates", "usage", "written_bytes", "error"),
    [
        (
            {"max_total_tokens": 3},
            {},
            UsageV1(input_tokens=2, output_tokens=2, reasoning_tokens=2),
            0,
            "model_token_budget_exceeded",
        ),
        (
            {"max_external_content_bytes": 1024},
            {"external_content_bytes": 1020},
            UsageV1(),
            5,
            "external_content_budget_exceeded",
        ),
    ],
)
async def test_registered_orchestrator_stops_after_aggregate_budget_exhaustion(
    budget_updates: dict[str, int],
    cursor_updates: dict[str, int],
    usage: UsageV1,
    written_bytes: int,
    error: str,
) -> None:
    registry = DurableToolRegistry()
    catalog = registry.catalog(policy_hash=_HASH, package_hash="f" * 64)
    content = InMemoryDurableContentStore()
    run_input = await _run_input(content, catalog)
    run_input = run_input.model_copy(
        update={
            **cursor_updates,
            "identity": run_input.identity.model_copy(
                update={
                    "budget": run_input.identity.budget.model_copy(
                        update=budget_updates
                    )
                }
            ),
        }
    )
    final_ref = await content.put_text(
        kind="final",
        value="must not commit",
        retention_class="result",
    )
    model_result = ModelStepActivityResultV1(
        run_document_ref=run_input.run_document_ref,
        step_index=0,
        final_response_ref=final_ref,
        usage=usage,
        written_bytes=written_bytes,
    )
    context = _OrchestrationContext(
        run_input.model_dump(mode="json"),
        activity_results={
            DURABLE_LOOP_MODEL_ACTIVITY_NAME: model_result.model_dump(mode="json")
        },
    )
    from azure_functions_agents.experimental.durable_loop_registration import (
        _run_durable_loop,
    )

    output = _drive_to_completion(
        _run_durable_loop(
            context,
            df.EntityId("durable_agent_session_entity_v1", "e" * 64),
            run_input,
        )
    )

    assert output == {"error": error, "status": "Failed"}
    assert all(operation != "complete" for operation, _ in context.entity_calls)


@pytest.mark.asyncio
async def test_registered_orchestrator_terminalizes_ambiguous_tool_outcome() -> None:
    registry = DurableToolRegistry()
    catalog = registry.catalog(policy_hash=_HASH, package_hash="f" * 64)
    content = InMemoryDurableContentStore()
    run_input = await _run_input(content, catalog)
    request_ref = await content.put_text(
        kind="tool-request",
        value="request",
        retention_class="run",
    )
    result_ref = await content.put_text(
        kind="tool-result",
        value="ambiguous",
        retention_class="run",
    )
    appended_ref = await content.put_text(
        kind="run-document",
        value="appended",
        retention_class="run",
    )
    call = {
        "behavior": ToolBehavior.MUTATING,
        "call_key": "1" * 64,
        "call_ordinal": 0,
        "parallel_safe": False,
        "provenance": ToolProvenance.REMOTE,
        "request_hash": "2" * 64,
        "request_ref": request_ref.model_dump(mode="json"),
        "schema_version": "1",
        "tool_name": "write",
    }
    model_result = ModelStepActivityResultV1.model_validate(
        {
            "run_document_ref": run_input.run_document_ref,
            "step_index": 0,
            "tool_calls": (call,),
        }
    )
    tool_result = ToolResultRefV1(
        result_ref=result_ref,
        call_ordinal=0,
        call_key="1" * 64,
        request_hash="2" * 64,
        tool_name="write",
        status=ToolResultStatus.AMBIGUOUS,
    )
    context = _OrchestrationContext(
        run_input.model_dump(mode="json"),
        activity_results={
            DURABLE_LOOP_MODEL_ACTIVITY_NAME: model_result.model_dump(mode="json"),
            DURABLE_LOOP_TOOL_ACTIVITY_NAME: tool_result.model_dump(mode="json"),
            DURABLE_LOOP_APPEND_ACTIVITY_NAME: {
                "run_document_ref": appended_ref.model_dump(mode="json"),
                "working_context_bytes": 1,
            },
        },
    )
    from azure_functions_agents.experimental.durable_loop_registration import (
        _run_durable_loop,
    )

    output = _drive_to_completion(
        _run_durable_loop(
            context,
            df.EntityId("durable_agent_session_entity_v1", "e" * 64),
            run_input,
        )
    )

    assert output == {
        "disposition": "Ambiguous",
        "error": "tool_outcome_ambiguous",
        "possibly_committed": True,
        "status": "Failed",
    }
    assert [operation for operation, _ in context.entity_calls] == [
        "admit",
        "mark_running",
        "is_cancelled",
        "is_cancelled",
        "is_cancelled",
        "abort",
    ]
    assert [name for name, _ in context.activity_calls] == [
        DURABLE_LOOP_MODEL_ACTIVITY_NAME,
        DURABLE_LOOP_TOOL_ACTIVITY_NAME,
        DURABLE_LOOP_APPEND_ACTIVITY_NAME,
    ]


@pytest.mark.asyncio
async def test_registered_orchestrator_checkpoints_first_write_before_mid_batch_cancel() -> None:
    registry = DurableToolRegistry()
    catalog = registry.catalog(policy_hash=_HASH, package_hash="f" * 64)
    content = InMemoryDurableContentStore()
    run_input = await _run_input(content, catalog)
    request_refs = [
        await content.put_text(
            kind=f"tool-request-{index}",
            value=f"request-{index}",
            retention_class="run",
        )
        for index in range(2)
    ]
    result_ref = await content.put_text(
        kind="tool-result",
        value="first-result",
        retention_class="run",
    )
    appended_ref = await content.put_text(
        kind="run-document",
        value="appended",
        retention_class="run",
    )
    calls = tuple(
        {
            "behavior": ToolBehavior.MUTATING,
            "call_key": str(index + 1) * 64,
            "call_ordinal": index,
            "parallel_safe": False,
            "provenance": ToolProvenance.REMOTE,
            "request_hash": str(index + 3) * 64,
            "request_ref": request_refs[index].model_dump(mode="json"),
            "schema_version": "1",
            "tool_name": f"write-{index}",
        }
        for index in range(2)
    )
    model_result = ModelStepActivityResultV1.model_validate(
        {
            "run_document_ref": run_input.run_document_ref,
            "step_index": 0,
            "tool_calls": calls,
        }
    )
    tool_result = ToolResultRefV1(
        result_ref=result_ref,
        call_ordinal=0,
        call_key="1" * 64,
        request_hash="3" * 64,
        tool_name="write-0",
        status=ToolResultStatus.SUCCEEDED,
    )
    context = _OrchestrationContext(
        run_input.model_dump(mode="json"),
        activity_results={
            DURABLE_LOOP_MODEL_ACTIVITY_NAME: model_result.model_dump(mode="json"),
            DURABLE_LOOP_TOOL_ACTIVITY_NAME: tool_result.model_dump(mode="json"),
            DURABLE_LOOP_APPEND_ACTIVITY_NAME: {
                "run_document_ref": appended_ref.model_dump(mode="json"),
                "working_context_bytes": 1,
            },
        },
        cancellation_sequence=[False, False, False, True],
    )
    from azure_functions_agents.experimental.durable_loop_registration import (
        _run_durable_loop,
    )

    output = _drive_to_completion(
        _run_durable_loop(
            context,
            df.EntityId("durable_agent_session_entity_v1", "e" * 64),
            run_input,
        )
    )

    assert output == {"status": DurableLoopRunStatus.CANCELLED.value}
    assert [
        name for name, _ in context.activity_calls
    ] == [
        DURABLE_LOOP_MODEL_ACTIVITY_NAME,
        DURABLE_LOOP_TOOL_ACTIVITY_NAME,
        DURABLE_LOOP_APPEND_ACTIVITY_NAME,
    ]
    append_payload = context.activity_calls[-1][1]
    assert len(append_payload["result_refs"]) == 1  # type: ignore[index]
    assert len(append_payload["cancelled_request_refs"]) == 1  # type: ignore[index]


@pytest.mark.asyncio
async def test_registered_orchestrator_continue_as_new_preserves_completed_work() -> None:
    registry = DurableToolRegistry()
    catalog = registry.catalog(policy_hash=_HASH, package_hash="f" * 64)
    content = InMemoryDurableContentStore()
    run_input = await _run_input(content, catalog)
    run_input = run_input.model_copy(
        update={
            "identity": run_input.identity.model_copy(
                update={
                    "budget": run_input.identity.budget.model_copy(
                        update={"continue_as_new_checkpoints": 1}
                    )
                }
            )
        }
    )
    request_ref = await content.put_text(
        kind="tool-request",
        value="request",
        retention_class="run",
    )
    result_ref = await content.put_text(
        kind="tool-result",
        value="result",
        retention_class="run",
    )
    appended_ref = await content.put_text(
        kind="run-document",
        value="appended",
        retention_class="run",
    )
    call = {
        "behavior": ToolBehavior.MUTATING,
        "call_key": "1" * 64,
        "call_ordinal": 0,
        "parallel_safe": False,
        "provenance": ToolProvenance.REMOTE,
        "request_hash": "2" * 64,
        "request_ref": request_ref.model_dump(mode="json"),
        "schema_version": "1",
        "tool_name": "write",
    }
    first_model = ModelStepActivityResultV1.model_validate(
        {
            "run_document_ref": run_input.run_document_ref,
            "step_index": 0,
            "tool_calls": (call,),
        }
    )
    tool_result = ToolResultRefV1(
        result_ref=result_ref,
        call_ordinal=0,
        call_key="1" * 64,
        request_hash="2" * 64,
        tool_name="write",
        status=ToolResultStatus.SUCCEEDED,
    )
    first_context = _OrchestrationContext(
        run_input.model_dump(mode="json"),
        activity_results={
            DURABLE_LOOP_MODEL_ACTIVITY_NAME: first_model.model_dump(mode="json"),
            DURABLE_LOOP_TOOL_ACTIVITY_NAME: tool_result.model_dump(mode="json"),
            DURABLE_LOOP_APPEND_ACTIVITY_NAME: {
                "run_document_ref": appended_ref.model_dump(mode="json"),
                "working_context_bytes": 10,
                "written_bytes": 10,
            },
        },
    )
    from azure_functions_agents.experimental.durable_loop_registration import (
        _run_durable_loop,
    )

    first_output = _drive_to_completion(
        _run_durable_loop(
            first_context,
            df.EntityId("durable_agent_session_entity_v1", "e" * 64),
            run_input,
        )
    )

    assert first_output is None
    assert len(first_context.continued) == 1
    continued = DurableOrchestrationInputV1.model_validate_json(
        canonical_json_bytes(first_context.continued[0])
    )
    assert continued.completed_model_steps == 1
    assert continued.completed_tool_calls == 1
    assert continued.continue_as_new_generation == 1
    assert continued.run_document_ref == appended_ref

    final_ref = await content.put_text(
        kind="final",
        value="done",
        retention_class="result",
    )
    second_model = ModelStepActivityResultV1(
        run_document_ref=appended_ref,
        step_index=1,
        final_response_ref=final_ref,
    )
    second_context = _OrchestrationContext(
        continued.model_dump(mode="json"),
        activity_results={
            DURABLE_LOOP_MODEL_ACTIVITY_NAME: second_model.model_dump(mode="json")
        },
    )
    second_output = _drive_to_completion(
        _run_durable_loop(
            second_context,
            df.EntityId("durable_agent_session_entity_v1", "e" * 64),
            continued,
        )
    )

    assert second_output["status"] == "Completed"  # type: ignore[index]
    assert [
        name
        for name, _ in (*first_context.activity_calls, *second_context.activity_calls)
    ].count(DURABLE_LOOP_TOOL_ACTIVITY_NAME) == 1


@pytest.mark.asyncio
async def test_registered_orchestrator_opens_exact_human_wait_topology() -> None:
    registry = DurableToolRegistry()
    catalog = registry.catalog(policy_hash=_HASH, package_hash="f" * 64)
    content = InMemoryDurableContentStore()
    run_input = await _run_input(content, catalog)
    request_ref = await content.put_text(
        kind="request",
        value="request",
        retention_class="run",
    )
    call = {
        "behavior": "read_only",
        "call_key": "1" * 64,
        "call_ordinal": 0,
        "parallel_safe": False,
        "provenance": "runtime",
        "request_hash": "2" * 64,
        "request_ref": request_ref.model_dump(mode="json"),
        "schema_version": "1",
        "tool_name": "request_human_input",
    }
    model_result = ModelStepActivityResultV1.model_validate_json(
        json.dumps(
            {
                "run_document_ref": run_input.run_document_ref.model_dump(mode="json"),
                "schema_version": "1",
                "step_index": 0,
                "tool_calls": [call],
            }
        )
    )
    question_ref = await content.put_text(
        kind="question",
        value="Which region?",
        retention_class="run",
    )
    human_result = {
        "call_key": "1" * 64,
        "event_name": "answer:1:server",
        "expires_at": datetime(2026, 9, 5, tzinfo=UTC).isoformat(),
        "generation": 1,
        "question_ref": question_ref.model_dump(mode="json"),
        "request_id": "human-1",
        "request_ref": request_ref.model_dump(mode="json"),
        "run_id": "run-1",
        "schema_version": "1",
    }
    context = _OrchestrationContext(
        run_input.model_dump(mode="json"),
        activity_results={
            DURABLE_LOOP_HUMAN_ACTIVITY_NAME: human_result,
            DURABLE_LOOP_MODEL_ACTIVITY_NAME: model_result.model_dump(mode="json"),
        },
    )
    from azure_functions_agents.experimental.durable_loop_registration import (
        _run_durable_loop,
    )

    generator = _run_durable_loop(
        context,
        df.EntityId("durable_agent_session_entity_v1", "e" * 64),
        run_input,
    )
    task = next(generator)
    for _ in range(7):
        task.is_completed = True
        task = generator.send(task.result)

    assert context.event_names == [
        "answer:1:server",
        "durable_agent_cancel_v1",
    ]
    assert len(context.timers) == 1
    assert len(context.custom_statuses) == 2
    assert context.custom_statuses[-1]["phase"] == "human_wait"  # type: ignore[index]
    assert "Which region?" not in json.dumps(context.custom_statuses)
    assert context.continued == []
    generator.close()


@pytest.mark.asyncio
async def test_registered_orchestrator_compacts_only_at_quiescent_boundary() -> None:
    registry = DurableToolRegistry()
    catalog = registry.catalog(policy_hash=_HASH, package_hash="f" * 64)
    content = InMemoryDurableContentStore()
    run_input = (await _run_input(content, catalog)).model_copy(
        update={"working_context_bytes": 4 * 1024 * 1024}
    )
    compacted_ref = await content.put_text(
        kind="compacted",
        value="compacted",
        retention_class="run",
    )
    context = _OrchestrationContext(
        run_input.model_dump(mode="json"),
        activity_results={
            DURABLE_LOOP_COMPACTION_ACTIVITY_NAME: {
                "run_document_ref": compacted_ref.model_dump(mode="json"),
                "working_context_bytes": 1024,
            }
        },
    )
    from azure_functions_agents.experimental.durable_loop_registration import (
        _run_durable_loop,
    )

    generator = _run_durable_loop(
        context,
        df.EntityId("durable_agent_session_entity_v1", "e" * 64),
        run_input,
    )
    task = next(generator)
    task.is_completed = True
    task = generator.send(task.result)
    task.is_completed = True
    task = generator.send(task.result)
    task.is_completed = True
    task = generator.send(task.result)

    assert context.activity_calls[0][0] == DURABLE_LOOP_COMPACTION_ACTIVITY_NAME
    generator.close()


@pytest.mark.asyncio
async def test_registered_orchestrator_observes_persisted_cancel_before_new_work() -> None:
    registry = DurableToolRegistry()
    catalog = registry.catalog(policy_hash=_HASH, package_hash="f" * 64)
    content = InMemoryDurableContentStore()
    run_input = (await _run_input(content, catalog)).model_copy(
        update={"working_context_bytes": 4 * 1024 * 1024}
    )
    context = _OrchestrationContext(
        run_input.model_dump(mode="json"),
        activity_results={},
        cancelled=True,
    )
    from azure_functions_agents.experimental.durable_loop_registration import (
        _run_durable_loop,
    )

    output = _drive_to_completion(
        _run_durable_loop(
            context,
            df.EntityId("durable_agent_session_entity_v1", "e" * 64),
            run_input,
        )
    )

    assert output == {"status": "Cancelled"}
    assert context.activity_calls == []
    assert context.continued == []
    assert [operation for operation, _ in context.entity_calls] == [
        "admit",
        "mark_running",
        "is_cancelled",
        "abort",
    ]


@pytest.mark.asyncio
async def test_registered_orchestrator_releases_session_after_activity_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DURABLE_LOOP_ENABLED_ENV, "true")
    _write_agent(tmp_path)
    app = app_module.create_function_app(tmp_path)
    assert isinstance(app, df.DFApp)
    registry = DurableToolRegistry()
    content = InMemoryDurableContentStore()
    run_input = await _run_input(
        content,
        registry.catalog(policy_hash=_HASH, package_hash="f" * 64),
    )
    context = _OrchestrationContext(
        run_input.model_dump(mode="json"),
        activity_results={},
    )
    orchestrator = _registered(app, DURABLE_LOOP_ORCHESTRATOR_NAME)
    generator = orchestrator(context)

    task = next(generator)
    task.is_completed = True
    task = generator.send(task.result)
    task.is_completed = True
    task = generator.send(task.result)
    task.is_completed = True
    abort_task = generator.send(task.result)

    assert context.entity_calls[-1][0] == "abort"
    with pytest.raises(KeyError):
        generator.send(abort_task.result)


@pytest.mark.asyncio
async def test_registered_orchestrator_allows_one_mixed_clarification_repair() -> None:
    registry = DurableToolRegistry()
    catalog = registry.catalog(policy_hash=_HASH, package_hash="f" * 64)
    content = InMemoryDurableContentStore()
    run_input = await _run_input(content, catalog)
    first_ref = await content.put_text(
        kind="request",
        value="first",
        retention_class="run",
    )
    second_ref = await content.put_text(
        kind="request",
        value="second",
        retention_class="run",
    )
    model_result = ModelStepActivityResultV1.model_validate_json(
        json.dumps(
            {
                "run_document_ref": run_input.run_document_ref.model_dump(mode="json"),
                "schema_version": "1",
                "step_index": 0,
                "tool_calls": [
                    {
                        "behavior": "read_only",
                        "call_key": "1" * 64,
                        "call_ordinal": 0,
                        "parallel_safe": False,
                        "provenance": "runtime",
                        "request_hash": "2" * 64,
                        "request_ref": first_ref.model_dump(mode="json"),
                        "schema_version": "1",
                        "tool_name": "request_human_input",
                    },
                    {
                        "behavior": "idempotent_write",
                        "call_key": "3" * 64,
                        "call_ordinal": 1,
                        "parallel_safe": False,
                        "provenance": "remote",
                        "request_hash": "4" * 64,
                        "request_ref": second_ref.model_dump(mode="json"),
                        "schema_version": "1",
                        "tool_name": "write_value",
                    },
                ],
            }
        )
    )
    context = _OrchestrationContext(
        run_input.model_dump(mode="json"),
        activity_results={
            DURABLE_LOOP_APPEND_ACTIVITY_NAME: {
                "run_document_ref": run_input.run_document_ref.model_dump(mode="json"),
                "working_context_bytes": 1024,
            },
            DURABLE_LOOP_MODEL_ACTIVITY_NAME: model_result.model_dump(mode="json"),
        },
    )
    from azure_functions_agents.experimental.durable_loop_registration import (
        _run_durable_loop,
    )

    output = _drive_to_completion(
        _run_durable_loop(
            context,
            df.EntityId("durable_agent_session_entity_v1", "e" * 64),
            run_input,
        )
    )

    assert output == {
        "error": "invalid_clarification_batch",
        "status": "Failed",
    }
    assert [
        name for name, _ in context.activity_calls
    ] == [
        DURABLE_LOOP_MODEL_ACTIVITY_NAME,
        DURABLE_LOOP_APPEND_ACTIVITY_NAME,
        DURABLE_LOOP_MODEL_ACTIVITY_NAME,
    ]
    repair_payload = context.activity_calls[1][1]
    assert repair_payload["protocol_error"] is True  # type: ignore[index]
    assert len(repair_payload["request_refs"]) == 2  # type: ignore[index]


@pytest.mark.asyncio
async def test_human_delivery_outbox_retries_transient_failure_before_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DURABLE_LOOP_ENABLED_ENV, "true")
    _write_agent(tmp_path)
    app = app_module.create_function_app(tmp_path)
    assert isinstance(app, df.DFApp)
    context = _OrchestrationContext(
        {
            "event_data": {
                "body_hash": "a" * 64,
                "request_id": "human-1",
                "submission_id_hash": "b" * 64,
            },
            "event_name": "answer:1:server",
            "expires_at": datetime(2026, 9, 5, tzinfo=UTC).isoformat(),
            "request_id": "human-1",
            "run_id": "run-1",
            "session_entity_key": "c" * 64,
        },
        activity_results={
            DURABLE_LOOP_HUMAN_DELIVERY_ACTIVITY_NAME: [
                HumanEventDeliveryResultV1(
                    status=HumanEventDeliveryStatus.RETRY,
                    retry_after_seconds=1.0,
                ).model_dump(mode="json"),
                HumanEventDeliveryResultV1(
                    status=HumanEventDeliveryStatus.RETRY,
                    retry_after_seconds=1.0,
                ).model_dump(mode="json"),
                HumanEventDeliveryResultV1(
                    status=HumanEventDeliveryStatus.RETRY,
                    retry_after_seconds=1.0,
                ).model_dump(mode="json"),
                HumanEventDeliveryResultV1(
                    status=HumanEventDeliveryStatus.RETRY,
                    retry_after_seconds=1.0,
                ).model_dump(mode="json"),
                HumanEventDeliveryResultV1(
                    status=HumanEventDeliveryStatus.DELIVERED,
                ).model_dump(mode="json"),
            ]
        },
    )
    orchestrator = _registered(
        app,
        DURABLE_LOOP_HUMAN_DELIVERY_ORCHESTRATOR_NAME,
    )

    output = _drive_to_completion(orchestrator(context))

    assert len(context.timers) == 4
    assert [name for name, _ in context.activity_calls] == [
        DURABLE_LOOP_HUMAN_DELIVERY_ACTIVITY_NAME,
        DURABLE_LOOP_HUMAN_DELIVERY_ACTIVITY_NAME,
        DURABLE_LOOP_HUMAN_DELIVERY_ACTIVITY_NAME,
        DURABLE_LOOP_HUMAN_DELIVERY_ACTIVITY_NAME,
        DURABLE_LOOP_HUMAN_DELIVERY_ACTIVITY_NAME,
    ]
    assert context.entity_calls[-1][0] == "mark_human_delivery"
    assert output["disposition"] == "found"  # type: ignore[index]


@pytest.mark.asyncio
async def test_human_delivery_outbox_rolls_history_after_twenty_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DURABLE_LOOP_ENABLED_ENV, "true")
    _write_agent(tmp_path)
    app = app_module.create_function_app(tmp_path)
    assert isinstance(app, df.DFApp)
    retry = HumanEventDeliveryResultV1(
        status=HumanEventDeliveryStatus.RETRY,
        retry_after_seconds=1.0,
    ).model_dump(mode="json")
    context = _OrchestrationContext(
        {
            "event_data": {"request_id": "human-1"},
            "event_name": "answer:1:server",
            "expires_at": datetime(2026, 9, 5, tzinfo=UTC).isoformat(),
            "request_id": "human-1",
            "run_id": "run-1",
            "session_entity_key": "c" * 64,
        },
        activity_results={
            DURABLE_LOOP_HUMAN_DELIVERY_ACTIVITY_NAME: [
                dict(retry) for _ in range(20)
            ]
        },
    )
    orchestrator = _registered(
        app,
        DURABLE_LOOP_HUMAN_DELIVERY_ORCHESTRATOR_NAME,
    )

    output = _drive_to_completion(orchestrator(context))

    assert output is None
    assert len(context.continued) == 1
    assert context.continued[0]["attempt"] == 20  # type: ignore[index]


@pytest.mark.asyncio
async def test_cancel_delivery_outbox_retries_transient_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DURABLE_LOOP_ENABLED_ENV, "true")
    _write_agent(tmp_path)
    app = app_module.create_function_app(tmp_path)
    assert isinstance(app, df.DFApp)
    context = _OrchestrationContext(
        {
            "event_data": {"run_id": "run-1"},
            "event_name": "durable_agent_cancel_v1",
            "expires_at": datetime(2026, 9, 5, tzinfo=UTC).isoformat(),
            "run_id": "run-1",
        },
        activity_results={
            DURABLE_LOOP_HUMAN_DELIVERY_ACTIVITY_NAME: [
                HumanEventDeliveryResultV1(
                    status=HumanEventDeliveryStatus.RETRY,
                    retry_after_seconds=1.0,
                ).model_dump(mode="json"),
                HumanEventDeliveryResultV1(
                    status=HumanEventDeliveryStatus.DELIVERED,
                ).model_dump(mode="json"),
            ]
        },
    )
    orchestrator = _registered(
        app,
        DURABLE_LOOP_CANCEL_DELIVERY_ORCHESTRATOR_NAME,
    )

    output = _drive_to_completion(orchestrator(context))

    assert output["status"] == HumanEventDeliveryStatus.DELIVERED.value  # type: ignore[index]
    assert len(context.timers) == 1
    assert [name for name, _ in context.activity_calls] == [
        DURABLE_LOOP_HUMAN_DELIVERY_ACTIVITY_NAME,
        DURABLE_LOOP_HUMAN_DELIVERY_ACTIVITY_NAME,
    ]
