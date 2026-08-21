from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent_framework import Message
from azure.ai.agentserver.responses import ResponseEventStream

from azure_functions_agents.config.schema import (
    BuiltinEndpointsConfig,
    ResolvedAgent,
    SubagentRef,
    ToolsFilter,
    TriggerSpec,
)
from azure_functions_agents.execution.result import AgentResult
from azure_functions_agents.foundry_responses import fha_resilient_responses_entrypoint
from azure_functions_agents.foundry_responses.fha_private_history import (
    FhaHistoryFactory,
    FhaResponsesRequestEnvelope,
)
from azure_functions_agents.foundry_responses.fha_resilient_responses_entrypoint import (
    FhaAgentServerResponsesApis,
    FhaResilientResponsesError,
    create_fha_resilient_responses_host,
    execute_fha_v0_stage,
    execute_fha_v0_stage_stream,
    render_fha_hosted_responses_entrypoint,
    stream_fha_resilient_v0_stage,
)
from azure_functions_agents.foundry_responses.fha_run_idempotent_history import (
    FhaRunIdempotentHistoryProvider,
)
from azure_functions_agents.registration.capabilities import AgentCapabilities
from azure_functions_agents.registration.catalog import CatalogEntry, build_catalog


@dataclass
class _Context:
    is_recovery: bool
    persisted_response: object | None
    response_id: str = "response-private"


class _Text:
    def emit_added(self) -> str:
        return "text.added"

    def emit_delta(self, text: str) -> str:
        return f"text.delta:{text}"

    def emit_text_done(self, text: str) -> str:
        return f"text.done:{text}"

    def emit_done(self) -> str:
        return "text.closed"


class _Message:
    def emit_added(self) -> str:
        return "message.added"

    def add_text_content(self) -> _Text:
        return _Text()

    def emit_done(self) -> str:
        return "message.closed"


class _Stream:
    def __init__(
        self, history_factory: FhaHistoryFactory, envelope: FhaResponsesRequestEnvelope
    ) -> None:
        self._history_factory = history_factory
        self._envelope = envelope

    def emit_created(self) -> str:
        return "created"

    def emit_in_progress(self) -> str:
        return "in_progress"

    def add_output_item_message(self) -> _Message:
        return _Message()

    def checkpoint(self) -> str:
        assert self._history_factory.read_committed_stage(self._envelope) is not None
        return "checkpoint"

    def emit_completed(self) -> str:
        return "completed"


class _Host:
    def __init__(self, options: object) -> None:
        self.options = options
        self.handler: Callable[..., object] | None = None

    def response_handler(self, handler: Callable[..., object]) -> Callable[..., object]:
        self.handler = handler
        return handler


class _Options:
    def __init__(self, *, resilient_background: bool) -> None:
        self.resilient_background = resilient_background


def _envelope() -> FhaResponsesRequestEnvelope:
    return FhaResponsesRequestEnvelope(
        agent_slug="model_only",
        history_scope="o1-" + ("a" * 52),
        runtime_session_id="opaque-session-1",
        runtime_run_id="a" * 32,
        prompt="Hello",
    )


def _catalog():
    resolved = ResolvedAgent(
        name="Model only",
        slug="model_only",
        description="Model-only agent.",
        trigger=TriggerSpec(type="http_trigger"),
        instructions="Answer the request.",
        is_main=False,
        builtin_endpoints=BuiltinEndpointsConfig(),
        model=None,
        timeout=30.0,
        enabled_mcp_names=[],
        enabled_skills_names=[],
        tool_filter=ToolsFilter(),
        tools_disabled=True,
        skills_disabled=True,
        mcp_disabled=True,
        sandbox_config=None,
        web_request_config=None,
        input_schema=None,
        response_schema=None,
        response_example=None,
    )
    return build_catalog({"model_only": CatalogEntry(resolved, AgentCapabilities())})


async def _collect(
    envelope: FhaResponsesRequestEnvelope,
    context: _Context,
    history_factory: FhaHistoryFactory,
    stage_runner,
    cancellation_signal: asyncio.Event | None = None,
    stream_inputs: list[object | None] | None = None,
    stage_stream_runner=None,
) -> list[object]:
    def stream_factory(_persisted_response: object | None) -> _Stream:
        if stream_inputs is not None:
            stream_inputs.append(_persisted_response)
        return _Stream(history_factory, envelope)

    return [
        event
        async for event in stream_fha_resilient_v0_stage(
            envelope,
            context=context,
            history_factory=history_factory,
            stage_runner=stage_runner,
            stage_stream_runner=stage_stream_runner,
            stream_factory=stream_factory,
            persisted_output_check=lambda snapshot: bool(snapshot["output"]),
            cancellation_signal=cancellation_signal or asyncio.Event(),
        )
    ]


@pytest.mark.asyncio
async def test_fresh_stage_creates_once_commits_before_checkpoint_and_completes(tmp_path) -> None:
    envelope = _envelope()
    history_factory = FhaHistoryFactory(home_directory=tmp_path)
    calls = 0
    stream_inputs: list[object | None] = []

    async def run_model(_envelope: FhaResponsesRequestEnvelope) -> str:
        nonlocal calls
        calls += 1
        return "answer"

    events = await _collect(
        envelope,
        _Context(False, None),
        history_factory,
        run_model,
        stream_inputs=stream_inputs,
    )

    assert calls == 1
    assert stream_inputs == [None]
    assert events == [
        "created",
        "in_progress",
        "message.added",
        "text.added",
        "text.done:answer",
        "text.closed",
        "message.closed",
        "checkpoint",
        "completed",
    ]
    assert history_factory.read_committed_stage(envelope).output == "answer"


@pytest.mark.asyncio
async def test_fresh_streaming_stage_emits_deltas_before_checkpoint(tmp_path) -> None:
    envelope = _envelope()
    history_factory = FhaHistoryFactory(home_directory=tmp_path)

    async def unexpected_stage(_envelope: FhaResponsesRequestEnvelope) -> str:
        raise AssertionError("streaming stage must not use the non-streaming runner")

    async def stream_stage(_envelope: FhaResponsesRequestEnvelope):
        yield "capable "
        yield "answer"

    events = await _collect(
        envelope,
        _Context(False, None),
        history_factory,
        unexpected_stage,
        stage_stream_runner=stream_stage,
    )

    assert events == [
        "created",
        "in_progress",
        "message.added",
        "text.added",
        "text.delta:capable ",
        "text.delta:answer",
        "text.done:capable answer",
        "text.closed",
        "message.closed",
        "checkpoint",
        "completed",
    ]
    assert history_factory.read_committed_stage(envelope).output == "capable answer"


@pytest.mark.asyncio
async def test_fresh_streaming_stage_emits_real_agentserver_delta_events(tmp_path) -> None:
    envelope = _envelope()
    history_factory = FhaHistoryFactory(home_directory=tmp_path)

    async def unexpected_stage(_envelope: FhaResponsesRequestEnvelope) -> str:
        raise AssertionError("streaming stage must not use the non-streaming runner")

    async def stream_stage(_envelope: FhaResponsesRequestEnvelope):
        yield "capable "
        yield "answer"

    events = [
        event
        async for event in stream_fha_resilient_v0_stage(
            envelope,
            context=_Context(False, None),
            history_factory=history_factory,
            stage_runner=unexpected_stage,
            stage_stream_runner=stream_stage,
            stream_factory=lambda _persisted: ResponseEventStream(
                response_id="response-private"
            ),
            persisted_output_check=lambda snapshot: bool(snapshot["output"]),
            cancellation_signal=asyncio.Event(),
        )
    ]
    event_types = [
        (
            event["type"]
            if isinstance(event, dict)
            else "response.checkpoint"
        )
        for event in events
    ]
    deltas = [
        event["delta"]
        for event in events
        if isinstance(event, dict) and event.get("type") == "response.output_text.delta"
    ]

    assert event_types == [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.checkpoint",
        "response.completed",
    ]
    assert deltas == ["capable ", "answer"]


@pytest.mark.asyncio
async def test_recovery_appends_in_progress_without_a_second_created(tmp_path) -> None:
    envelope = _envelope()
    history_factory = FhaHistoryFactory(home_directory=tmp_path)
    history_factory.commit_model_stage(envelope, "answer")
    persisted_response = {"output": []}
    stream_inputs: list[object | None] = []

    async def unexpected_model_run(_envelope: FhaResponsesRequestEnvelope) -> str:
        raise AssertionError("recovery must not repeat a committed model stage")

    events = await _collect(
        envelope,
        _Context(True, persisted_response),
        history_factory,
        unexpected_model_run,
        stream_inputs=stream_inputs,
    )

    assert stream_inputs == [persisted_response]
    assert events[0] == "in_progress"
    assert "created" not in events
    assert events[-2:] == ["checkpoint", "completed"]


@pytest.mark.asyncio
async def test_recovery_skips_duplicate_history_and_output_after_a_checkpoint(tmp_path) -> None:
    envelope = _envelope()
    history_factory = FhaHistoryFactory(home_directory=tmp_path)
    history_factory.commit_model_stage(envelope, "answer")

    async def unexpected_model_run(_envelope: FhaResponsesRequestEnvelope) -> str:
        raise AssertionError("checkpointed recovery must not rerun the model")

    events = await _collect(
        envelope,
        _Context(True, {"output": [{"id": "original-item"}]}),
        history_factory,
        unexpected_model_run,
    )

    assert events == ["in_progress", "completed"]


@pytest.mark.asyncio
async def test_recovery_rejects_persisted_output_without_committed_history(tmp_path) -> None:
    calls = 0

    async def unexpected_stage(_envelope: FhaResponsesRequestEnvelope) -> str:
        nonlocal calls
        calls += 1
        return "unexpected"

    with pytest.raises(FhaResilientResponsesError, match="no matching history commit"):
        await _collect(
            _envelope(),
            _Context(True, {"output": [{"id": "orphaned-item"}]}),
            FhaHistoryFactory(home_directory=tmp_path),
            unexpected_stage,
        )

    assert calls == 0


@pytest.mark.asyncio
async def test_post_history_commit_crash_reuses_stored_output_without_rerunning(
    tmp_path,
) -> None:
    envelope = _envelope()
    history_factory = FhaHistoryFactory(home_directory=tmp_path)
    calls = 0

    async def run_stage(stage_envelope: FhaResponsesRequestEnvelope) -> str:
        nonlocal calls
        calls += 1
        history_provider = history_factory.create_maf_history_provider(stage_envelope)
        await history_provider.save_messages(
            stage_envelope.runtime_session_id,
            [
                Message("user", [stage_envelope.effective_prompt]),
                Message("assistant", ["answer"]),
            ],
        )
        return "answer"

    fresh_stream = stream_fha_resilient_v0_stage(
        envelope,
        context=_Context(False, None),
        history_factory=history_factory,
        stage_runner=run_stage,
        stream_factory=lambda _persisted_response: _Stream(history_factory, envelope),
        persisted_output_check=lambda snapshot: bool(snapshot["output"]),
        cancellation_signal=asyncio.Event(),
    )
    fresh_events = [await anext(fresh_stream) for _ in range(3)]

    assert fresh_events == ["created", "in_progress", "message.added"]
    assert history_factory.read_committed_stage(envelope).output == "answer"
    await fresh_stream.aclose()

    recovery_events = await _collect(
        envelope,
        _Context(True, {"output": []}),
        history_factory,
        run_stage,
    )

    assert calls == 1
    assert recovery_events == [
        "in_progress",
        "message.added",
        "text.added",
        "text.done:answer",
        "text.closed",
        "message.closed",
        "checkpoint",
        "completed",
    ]
    history_provider = history_factory.create_maf_history_provider(envelope)
    history_messages = await history_provider.get_messages(envelope.runtime_session_id)
    assert [message.text for message in history_messages] == ["Hello", "answer"]
    assert history_factory.read_committed_stage(envelope).output == "answer"


@pytest.mark.asyncio
async def test_precommit_crash_replays_an_at_least_once_stage_without_a_second_created(
    tmp_path,
) -> None:
    envelope = _envelope()
    history_factory = FhaHistoryFactory(home_directory=tmp_path)
    side_effect_calls = 0

    async def run_stage(_envelope: FhaResponsesRequestEnvelope) -> str:
        nonlocal side_effect_calls
        side_effect_calls += 1
        if side_effect_calls == 1:
            raise RuntimeError("crashed before history commit")
        return "answer"

    fresh_events: list[object] = []
    fresh_stream = stream_fha_resilient_v0_stage(
        envelope,
        context=_Context(False, None),
        history_factory=history_factory,
        stage_runner=run_stage,
        stream_factory=lambda _persisted_response: _Stream(history_factory, envelope),
        persisted_output_check=lambda snapshot: bool(snapshot["output"]),
        cancellation_signal=asyncio.Event(),
    )
    with pytest.raises(RuntimeError, match="before history commit"):
        async for event in fresh_stream:
            fresh_events.append(event)

    recovery_events = await _collect(
        envelope,
        _Context(True, {"output": []}),
        history_factory,
        run_stage,
    )

    assert side_effect_calls == 2
    assert fresh_events == ["created", "in_progress"]
    assert recovery_events[0] == "in_progress"
    assert "created" not in recovery_events
    assert [*fresh_events, *recovery_events].count("created") == 1
    assert history_factory.read_committed_stage(envelope).output == "answer"


@pytest.mark.asyncio
async def test_midrun_cancellation_stops_before_history_checkpoint_and_completion(tmp_path) -> None:
    envelope = _envelope()
    history_factory = FhaHistoryFactory(home_directory=tmp_path)
    cancellation_signal = asyncio.Event()
    started = asyncio.Event()

    async def run_model(_envelope: FhaResponsesRequestEnvelope) -> str:
        started.set()
        await asyncio.Event().wait()
        return "unexpected"

    collect_task = asyncio.create_task(
        _collect(
            envelope,
            _Context(False, None),
            history_factory,
            run_model,
            cancellation_signal,
        )
    )
    await started.wait()
    cancellation_signal.set()
    events = await collect_task

    assert events == ["created", "in_progress"]
    assert history_factory.read_committed_stage(envelope) is None


@pytest.mark.asyncio
async def test_recovery_fails_closed_without_a_persisted_snapshot(tmp_path) -> None:
    envelope = _envelope()

    async def run_model(_envelope: FhaResponsesRequestEnvelope) -> str:
        return "answer"

    with pytest.raises(FhaResilientResponsesError, match="persisted response"):
        await _collect(
            envelope,
            _Context(True, None),
            FhaHistoryFactory(home_directory=tmp_path),
            run_model,
        )


@pytest.mark.asyncio
async def test_fha_v0_stage_uses_catalog_capabilities_and_private_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    custom_tool = object()
    remote_mcp_tool = object()
    forbidden_web_request_tool = object()
    skill_path = Path("skills/summarize")
    coordinator = ResolvedAgent(
        name="Coordinator",
        slug="model_only",
        description="Coordinates capabilities.",
        trigger=TriggerSpec(type="http_trigger"),
        instructions="Coordinate the request.",
        is_main=False,
        builtin_endpoints=BuiltinEndpointsConfig(),
        model="coordinator-model",
        timeout=42.0,
        enabled_mcp_names=["remote"],
        enabled_skills_names=["summarize"],
        tool_filter=ToolsFilter(),
        subagents=[SubagentRef(agent="specialist")],
        tools_disabled=False,
        skills_disabled=False,
        mcp_disabled=False,
        sandbox_config=None,
        web_request_config=None,
        input_schema=None,
        response_schema=None,
        response_example=None,
    )
    specialist = ResolvedAgent(
        name="Specialist",
        slug="specialist",
        description="Handles delegated work.",
        trigger=TriggerSpec(type="http_trigger"),
        instructions="Handle delegated work.",
        is_main=False,
        builtin_endpoints=BuiltinEndpointsConfig(),
        model="specialist-model",
        timeout=24.0,
        enabled_mcp_names=[],
        enabled_skills_names=[],
        tool_filter=ToolsFilter(),
        tools_disabled=True,
        skills_disabled=True,
        mcp_disabled=True,
        sandbox_config=None,
        web_request_config=None,
        input_schema=None,
        response_schema=None,
        response_example=None,
    )
    catalog = build_catalog(
        {
            "model_only": CatalogEntry(
                coordinator,
                AgentCapabilities(
                    filtered_user_tools=[custom_tool],
                    filtered_mcp_tools=[remote_mcp_tool],
                    enabled_skill_paths=[skill_path],
                    web_request_tools=[forbidden_web_request_tool],
                ),
            ),
            "specialist": CatalogEntry(specialist, AgentCapabilities()),
        }
    )
    captured: dict[str, object] = {}

    async def fake_run_agent(prompt: str, **kwargs: object) -> AgentResult:
        captured["prompt"] = prompt
        captured["kwargs"] = kwargs
        return AgentResult(session_id="opaque-session-1", content="capable answer")

    monkeypatch.setattr(fha_resilient_responses_entrypoint, "run_agent", fake_run_agent)
    monkeypatch.setattr(
        fha_resilient_responses_entrypoint,
        "validate_fha_v0_catalog",
        lambda _catalog: None,
    )
    history_factory = FhaHistoryFactory(home_directory=tmp_path)

    result = await execute_fha_v0_stage(
        _envelope(),
        catalog=catalog,
        history_factory=history_factory,
    )

    assert result == "capable answer"
    assert captured["prompt"] == "Hello"
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["instructions"] == "Coordinate the request."
    assert kwargs["timeout"] == 42.0
    assert kwargs["model"] == "coordinator-model"
    assert kwargs["session_id"] == "opaque-session-1"
    assert kwargs["agent_name"] == "model_only"
    assert kwargs["tools"] == [custom_tool]
    assert kwargs["mcp_tools"] == [remote_mcp_tool]
    assert kwargs["skill_paths"] == [skill_path]
    assert kwargs["subagents"] == [SubagentRef(agent="specialist")]
    assert kwargs["catalog"] is catalog
    assert kwargs["sandbox_tools"] is None
    assert kwargs["web_request_tools"] is None
    assert kwargs["workflow_enabled"] is False
    assert kwargs["workflow_durable_client"] is None
    assert isinstance(kwargs["history_provider"], FhaRunIdempotentHistoryProvider)


@pytest.mark.asyncio
async def test_fha_v0_stage_stream_uses_private_history_and_yields_text_deltas(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    async def fake_run_agent_events(prompt: str, **kwargs: object):
        captured["prompt"] = prompt
        captured["kwargs"] = kwargs
        yield {"type": "session", "session_id": "opaque-session-1"}
        yield {"type": "delta", "content": "capable "}
        yield {"type": "delta", "content": "answer"}
        yield {"type": "done"}

    monkeypatch.setattr(
        fha_resilient_responses_entrypoint,
        "run_agent_events",
        fake_run_agent_events,
    )
    monkeypatch.setattr(
        fha_resilient_responses_entrypoint,
        "validate_fha_v0_catalog",
        lambda _catalog: None,
    )
    catalog = _catalog()

    chunks = [
        chunk
        async for chunk in execute_fha_v0_stage_stream(
            _envelope(),
            catalog=catalog,
            history_factory=FhaHistoryFactory(home_directory=tmp_path),
        )
    ]

    assert chunks == ["capable ", "answer"]
    assert captured["prompt"] == "Hello"
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["display_name"] == "Model only"
    assert isinstance(kwargs["history_provider"], FhaRunIdempotentHistoryProvider)


@pytest.mark.asyncio
async def test_fha_v0_stage_accepts_compiled_remote_mcp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    existing_entry = _catalog().get("model_only")
    assert existing_entry is not None
    remote_mcp_tool = SimpleNamespace(name="remote")
    catalog = build_catalog(
        {
            "model_only": CatalogEntry(
                existing_entry.resolved.model_copy(
                    update={
                        "enabled_mcp_names": ["remote"],
                        "mcp_disabled": False,
                    }
                ),
                AgentCapabilities(filtered_mcp_tools=[remote_mcp_tool]),
            )
        }
    )

    async def fake_run_agent(_prompt: str, **_kwargs: object) -> AgentResult:
        return AgentResult(session_id="opaque-session-1", content="answer")

    monkeypatch.setattr(fha_resilient_responses_entrypoint, "run_agent", fake_run_agent)

    result = await execute_fha_v0_stage(
        _envelope(),
        catalog=catalog,
        history_factory=FhaHistoryFactory(home_directory=tmp_path),
    )

    assert result == "answer"


def test_rendered_fha_entrypoint_uses_the_staged_projection_and_shared_compiler() -> None:
    entrypoint = render_fha_hosted_responses_entrypoint()

    compile(entrypoint, "fha_hosted_responses_entrypoint.py", "exec")
    assert "Path(__file__).resolve().parent" in entrypoint
    assert "set_app_root(application_root)" in entrypoint
    assert "FHA_RUNTIME_PROJECTION_FILENAME" in entrypoint
    assert "load_fha_runtime_projection" in entrypoint
    assert "compile_fha_v0_project" in entrypoint
    assert "expected_projection=projection" in entrypoint
    assert "create_fha_resilient_responses_host(compilation.catalog)" in entrypoint
    assert 'os.environ["FOUNDRY_PROJECT_ENDPOINT"] = projection.project_endpoint' in entrypoint
    assert 'os.environ["FOUNDRY_MODEL"] = projection.default_model' in entrypoint
    assert 'os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"] = projection.default_model' in entrypoint
    assert "get_app_root" not in entrypoint
    assert "AZURE_FUNCTIONS_AGENTS_FHA_" not in entrypoint


def test_host_enables_resilient_background_before_registering_handler(monkeypatch) -> None:
    enabled: list[bool] = []
    options: list[_Options] = []
    lifecycle: list[str] = []
    validated_catalogs: list[object] = []

    def create_options(*, resilient_background: bool) -> _Options:
        option = _Options(resilient_background=resilient_background)
        options.append(option)
        return option

    class OrderedHost(_Host):
        def response_handler(self, handler: Callable[..., object]) -> Callable[..., object]:
            lifecycle.append("handler")
            return super().response_handler(handler)

    def create_host(*, options: object) -> OrderedHost:
        lifecycle.append("host")
        return OrderedHost(options)

    def enable_resilient_tasks(value: bool) -> None:
        enabled.append(value)
        lifecycle.append("enabled")

    apis = FhaAgentServerResponsesApis(
        responses_server_options=create_options,
        responses_agent_server_host=create_host,
        response_event_stream=lambda **_kwargs: None,
        set_resilient_tasks_enabled=enable_resilient_tasks,
    )
    monkeypatch.setattr(
        fha_resilient_responses_entrypoint,
        "_load_fha_agentserver_apis",
        lambda: apis,
    )
    monkeypatch.setattr(
        fha_resilient_responses_entrypoint,
        "validate_fha_v0_catalog",
        lambda catalog: validated_catalogs.append(catalog),
    )

    catalog = _catalog()
    host = create_fha_resilient_responses_host(catalog)

    assert isinstance(host, _Host)
    assert validated_catalogs == [catalog]
    assert options[0].resilient_background is True
    assert enabled == [True]
    assert host.handler is not None
    assert lifecycle == ["host", "enabled", "handler"]
