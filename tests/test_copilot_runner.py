from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from azure_functions_agents import runner
from azure_functions_agents._function_tool import tool
from azure_functions_agents._runtime_tool_names import (
    FHS_CANCEL_WORKFLOW_TOOL_NAME,
    FHS_GET_WORKFLOW_STATUS_TOOL_NAME,
    FHS_LIST_WORKFLOWS_TOOL_NAME,
    FHS_START_WORKFLOW_TOOL_NAME,
    FHS_TERMINATE_WORKFLOW_TOOL_NAME,
)
from azure_functions_agents.config.schema import (
    BuiltinEndpointsConfig,
    ResolvedAgent,
    SubagentRef,
    ToolsFilter,
)
from azure_functions_agents.discovery.skills import SkillDescriptor
from azure_functions_agents.harness import _harness_execution, _harness_lifecycle
from azure_functions_agents.harness.copilot_sdk import _copilot_execution, _copilot_runner
from azure_functions_agents.harness.copilot_sdk._copilot_runtime import CopilotPreviewError
from azure_functions_agents.registration.capabilities import (
    AgentCapabilities,
    with_runtime_skill_paths,
)
from azure_functions_agents.registration.catalog import CatalogEntry, build_catalog
from azure_functions_agents.workflows.integration import data_driven_workflows_skill_path
from azure_functions_agents.workflows.schema import WorkflowPlanPolicy
from tests._harness_helpers import copilot_state, make_copilot_harness
from tests.test_copilot_execution import _fake_client, _request, _seed_session_state
from tests.test_copilot_execution import invocation_spans as invocation_spans
from tests.test_copilot_execution import preview as preview


def _specialist():
    return ResolvedAgent(
        name="analyst", slug="analyst", description="Analyze.", trigger=None,
        instructions="Specialist instructions.", is_main=False,
        builtin_endpoints=BuiltinEndpointsConfig(), model="specialist-model", timeout=4,
        enabled_mcp_names=[], enabled_skills_names=[], tool_filter=ToolsFilter(),
        sandbox_config=None, input_schema=None, response_schema=None,
        response_example=None, metadata={},
    )


@pytest.fixture
def native(monkeypatch):
    import copilot

    client = _fake_client()
    client.delete_session = AsyncMock()
    monkeypatch.setattr(copilot, "CopilotClient", Mock(return_value=client))
    return client


def _exception_event(span, *, message: str):
    [event] = [item for item in span.events if item.name == "exception"]
    assert event.attributes["exception.message"] == message
    assert message in event.attributes["exception.stacktrace"]
    return event


@pytest.mark.asyncio
async def test_direct_invocation_summary_includes_recovered_delegate_errors(
    preview, native, monkeypatch, invocation_spans,
):
    from copilot.tools import ToolInvocation

    from azure_functions_agents._agent_run_diagnostics import _total_tool_error_count

    _, exporter = invocation_spans
    monkeypatch.setattr(
        runner, "run_leaf_agent_task", AsyncMock(side_effect=RuntimeError("leaf failed")),
    )
    specialist = _specialist()
    catalog = build_catalog({
        specialist.slug: CatalogEntry(specialist, AgentCapabilities()),
    })
    session = native.create_session.return_value

    async def create(**options):
        [delegate] = options["tools"]
        session.rpc.tools.get_current_metadata.return_value = SimpleNamespace(
            tools=[SimpleNamespace(name=delegate.name)],
        )

        async def send(**_kwargs):
            await delegate.handler(ToolInvocation(
                session_id="example", tool_call_id="delegate-call",
                tool_name=delegate.name, arguments={"task": "analyze"},
            ))
            return session.send_and_wait.return_value

        session.send_and_wait.side_effect = send
        return session

    native.create_session.side_effect = create
    try:
        result = await copilot_state(preview).run_agent(
            "hello", session_id="example", session_is_new=True, agent_name="main",
            deadline=asyncio.get_running_loop().time() + 10,
            subagents=[SubagentRef(agent=specialist.slug)], catalog=catalog,
        )
    finally:
        await _harness_lifecycle._shutdown_harnesses()

    invocation = next(
        span for span in exporter.get_finished_spans() if span.name.startswith("invoke_agent ")
    )
    assert result.delegate_error_count == 1
    assert invocation.attributes["af.agent.tool_error_count"] == _total_tool_error_count(result)
    assert invocation.attributes["af.agent.outcome"] == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "timeout", "disconnect"])
async def test_sse_semantic_invocation_tracks_actual_execution_lifecycle(
    preview, native, invocation_spans, outcome,
):
    _, exporter = invocation_spans
    session = native.create_session.return_value
    if outcome != "success":
        async def wait(**_kwargs):
            await asyncio.Event().wait()

        session.send_and_wait.side_effect = wait
    stream = copilot_state(preview).run_agent_stream(
        "hello", session_is_new=True, agent_name="main",
        deadline=asyncio.get_running_loop().time() + (0.05 if outcome == "timeout" else 5),
    )
    events = []
    try:
        if outcome == "disconnect":
            events.append(json.loads((await anext(stream)).removeprefix("data: ").strip()))
            await stream.aclose()
        else:
            events = [
                json.loads(chunk.removeprefix("data: ").strip()) async for chunk in stream
            ]
    finally:
        await stream.aclose()
        await _harness_lifecycle._shutdown_harnesses()

    spans = exporter.get_finished_spans()
    [invocation] = [span for span in spans if span.name.startswith("invoke_agent ")]
    [outer] = [span for span in spans if span.name == "agent.run main"]
    assert invocation.parent.span_id == outer.context.span_id
    assert invocation.context.trace_id == outer.context.trace_id
    assert events[0]["type"] == "session"
    assert invocation.attributes["gen_ai.conversation.id"] == events[0]["session_id"]
    assert native.create_session.call_args.kwargs["session_id"] != events[0]["session_id"]
    assert invocation.attributes["gen_ai.request.model"] == "gpt-4.1-mini"
    assert "gen_ai.operation.name" not in outer.attributes
    assert invocation.attributes["af.agent.outcome"] == (
        "cancelled" if outcome == "disconnect" else "error" if outcome == "timeout" else "success"
    )
    assert invocation.status.status_code.name == ("ERROR" if outcome == "timeout" else "UNSET")
    if outcome != "disconnect":
        assert events[-1]["type"] == ("error" if outcome == "timeout" else "done")
    session.on.assert_called_once()
    session.unsubscribe.assert_called_once()
    session.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_leaves_are_isolated_local_and_deleted(preview, native, monkeypatch):
    from azure_functions_agents.harness.copilot_sdk._copilot_session_identity import (
        StorageMode,
        resolve_route,
    )

    entered = asyncio.Event()
    active = 0
    providers = []
    sessions = [_fake_client().create_session.return_value for _ in range(2)]

    async def create(**options):
        nonlocal active
        session = sessions[active]
        active += 1
        provider = options["create_session_fs_handler"](session)
        await provider.write_file("/session-state/opaque", "private specialist state")
        providers.append(provider)
        if active == 2:
            entered.set()
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert options["infinite_sessions"]["enabled"] is False
        assert options["model"] == "specialist-model"
        return session

    native.create_session.side_effect = create
    locks = dict(_harness_execution._SESSION_LOCKS)
    # Ephemeral leaves must not open the primary route, even when it is configured for Blob.
    monkeypatch.setenv("AzureWebJobsStorage", "UseDevelopmentStorage=true")
    primary = make_copilot_harness(
        preview.app_root,
        storage_root=copilot_state(preview).storage_root,
        session_storage=resolve_route(preview.app_root),
    )
    assert copilot_state(primary).session_storage.mode is StorageMode.BLOB
    try:
        results = await asyncio.gather(*[
            runner.run_leaf_agent_task(
                _specialist(), AgentCapabilities(_harness=primary), f"task-{index}",
                timeout=3, execution_role="workflow_subagent",
            ) for index in range(2)
        ])
        assert results == ["synthetic reply"] * 2
        ids = [call.kwargs["session_id"] for call in native.create_session.call_args_list]
        assert ids[0] != ids[1]
        assert {call.args[0] for call in native.delete_session.call_args_list} == set(ids)
        assert locks == _harness_execution._SESSION_LOCKS
        assert not (copilot_state(primary).session_storage.local_dir / "copilot-native").exists()
        for provider in providers:
            assert not provider.backend.root.exists()
        native.resume_session.assert_not_awaited()
    finally:
        await _harness_lifecycle._shutdown_harnesses()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "timeout", "success"])
@pytest.mark.parametrize("delete_fails", [False, True])
async def test_leaf_delete_failure_preserves_body_outcome(preview, native, failure, delete_fails):
    session = native.create_session.return_value
    if delete_fails:
        native.delete_session.side_effect = RuntimeError("private deletion detail")
    started = asyncio.Event()

    async def stall(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    if failure != "success":
        session.send_and_wait.side_effect = stall
    task = asyncio.create_task(runner.run_leaf_agent_task(
        _specialist(), AgentCapabilities(_harness=preview), "task",
        timeout=0.03 if failure == "timeout" else 3,
        execution_role="delegate",
    ))
    try:
        if failure == "cancel":
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif failure == "timeout":
            with pytest.raises(TimeoutError, match="specialist timed out"):
                await task
        elif delete_fails:
            with pytest.raises(RuntimeError, match="private deletion detail") as caught:
                await task
            assert caught.value is native.delete_session.side_effect
        else:
            assert await task == "synthetic reply"
        native.delete_session.assert_awaited_once()
        session.disconnect.assert_awaited_once()
        if failure != "success":
            session.abort.assert_awaited_once()
    finally:
        await _harness_lifecycle._shutdown_harnesses()


@pytest.mark.asyncio
async def test_delegate_tool_retains_parent_binding_and_counts_sanitized_failure(preview, monkeypatch):
    resolved = _specialist()
    catalog = build_catalog({
        resolved.slug: CatalogEntry(resolved, AgentCapabilities()),
    })
    leaf = AsyncMock(side_effect=RuntimeError("private failure"))
    monkeypatch.setattr(runner, "run_leaf_agent_task", leaf)
    [function], tracker = await runner.build_subagent_tools(
        [SubagentRef(agent=resolved.slug)], catalog,
        coordinator_deadline=asyncio.get_running_loop().time() + 5,
        harness=preview,
    )
    content = await function.invoke(arguments={"task": "check"}, tool_call_id="call")
    assert "private failure" not in content
    assert tracker.count == 1
    assert leaf.call_args.args[1]._harness is preview


@pytest.mark.asyncio
@pytest.mark.parametrize("include_authored_skill", [False, True])
async def test_workflow_management_uses_native_skill_loading_without_prompt_injection(
    preview, monkeypatch, tmp_path, include_authored_skill,
):
    invoke = AsyncMock(return_value=runner.AgentResult("public-session", "ready"))
    monkeypatch.setattr(_copilot_execution, "run", invoke)
    policy = WorkflowPlanPolicy(
        allowed_tools=frozenset({"inspect"}), allowed_subagents=frozenset({"analyst"}),
    )
    runtime_skill = data_driven_workflows_skill_path()
    capabilities = AgentCapabilities.create()
    expected_skill_names = ["data-driven-workflows"]
    if include_authored_skill:
        authored = tmp_path / "skills" / "authored"
        authored.mkdir(parents=True)
        (authored / "SKILL.md").write_text(
            "---\nname: authored\ndescription: Authored skill.\n---\n",
            encoding="utf-8",
        )
        capabilities = AgentCapabilities.create(
            skills=(SkillDescriptor.create(name="authored", path=authored),),
        )
        expected_skill_names = ["authored", "data-driven-workflows"]
    direct_capabilities = with_runtime_skill_paths(capabilities, [runtime_skill])
    await runner.run_agent(
        "Create workflow",
        _harness=preview,
        tools=[],
        mcp_tools=[],
        instructions="Base instructions.",
        system_addendum="\n\nShort workflow guidance.",
        skill_paths=direct_capabilities.enabled_skill_paths,
        skills=direct_capabilities.skills,
        skill_catalog=direct_capabilities.skill_catalog,
        workflow_enabled=True, workflow_policy=policy,
        workflow_agent_slug="coordinator", workflow_durable_client=Mock(),
    )
    harness, request = invoke.call_args.args
    assert harness is preview
    assert request.agent_slug == "coordinator"
    assert [skill.name for skill in request.skills] == expected_skill_names
    assert [skill.name for skill in request.skill_catalog] == expected_skill_names
    assert {function.name for function in request.tools} == {
        FHS_START_WORKFLOW_TOOL_NAME,
        FHS_GET_WORKFLOW_STATUS_TOOL_NAME,
        FHS_LIST_WORKFLOWS_TOOL_NAME,
        FHS_CANCEL_WORKFLOW_TOOL_NAME,
        FHS_TERMINATE_WORKFLOW_TOOL_NAME,
    }
    assert request.instructions == "Base instructions.\n\nShort workflow guidance."
    assert "Collection fan-out" not in request.instructions
    assert "name: data-driven-workflows" not in request.instructions


@pytest.mark.asyncio
async def test_leaf_roles_keep_project_skills_without_runtime_workflow_skill(preview, monkeypatch):
    invoke = AsyncMock(return_value=runner.AgentResult("leaf-session", "ready"))
    monkeypatch.setattr(_copilot_execution, "run", invoke)
    authored = preview.app_root / "skills" / "authored"
    authored.mkdir(parents=True)
    (authored / "SKILL.md").write_text(
        "---\nname: authored\ndescription: Project skill.\n---\n",
        encoding="utf-8",
    )
    capabilities = AgentCapabilities.create(
        skills=(SkillDescriptor.create(name="authored", path=authored),),
    )

    result = await runner.run_leaf_agent_task(
        _specialist(),
        capabilities,
        "Investigate",
        timeout=3,
        execution_role="workflow_subagent",
        _harness=preview,
    )

    assert result == "ready"
    _, request = invoke.call_args.args
    assert request.execution_role == "workflow_subagent"
    assert [skill.name for skill in request.skills] == ["authored"]
    assert "data-driven-workflows" not in [skill.name for skill in request.skill_catalog]


@pytest.mark.asyncio
async def test_copilot_delegate_trace_nests_wrapper_and_leaf_agent_run(preview, monkeypatch):
    from copilot.tools import ToolInvocation
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    import azure_functions_agents._observability as observability
    from azure_functions_agents._tool_descriptor import ToolDescriptor
    from azure_functions_agents.harness.copilot_sdk._copilot_execution import _tool
    from azure_functions_agents.harness.copilot_sdk._copilot_tool_calls import CopilotToolCalls

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("copilot-delegate-trace-test")
    monkeypatch.setattr(observability, "_enabled", True)
    monkeypatch.setattr(observability, "get_tracer", lambda: tracer)
    leaf_result = runner.AgentResult("leaf-session", "leaf answer")

    leaf_request = None

    async def fake_leaf_execution(_harness, request):
        nonlocal leaf_request
        leaf_request = request
        observability.current_span().add_event(
            "af.agent.model.usage",
            {"gen_ai.usage.output_tokens": 4},
        )
        return leaf_result

    monkeypatch.setattr(_copilot_execution, "run", AsyncMock(side_effect=fake_leaf_execution))
    leaf_runner = copilot_state(preview)

    async def delegate(task: str) -> str:
        return await leaf_runner.run_leaf_agent_task(
            _specialist(),
            AgentCapabilities(_harness=preview),
            task,
            timeout=3,
            execution_role="delegate",
        )

    descriptor = ToolDescriptor.create(
        name="delegate_analyst",
        description="Delegate to analyst.",
        func=delegate,
    )
    parent = tracer.start_span("agent.run coordinator")
    with trace.use_span(parent, end_on_exit=True):
        calls = CopilotToolCalls(
            parent_context=observability.capture_current_context(),
            run_span=observability.current_span(),
            custom_tool_names=(descriptor.name,),
        )
        wrapped = _tool(descriptor, calls)
        callback_span = tracer.start_span("sdk.native.callback")
        with trace.use_span(callback_span, end_on_exit=True):
            result = await wrapped.handler(ToolInvocation(
                session_id="main-session",
                tool_call_id="delegate-call",
                tool_name=descriptor.name,
                arguments={"task": "analyze this"},
            ))

    spans = {span.name: span for span in exporter.get_finished_spans()}
    root = spans["agent.run coordinator"]
    tool_span = spans["execute_tool delegate_analyst"]
    leaf_span = spans["agent.run analyst"]
    assert tool_span.parent.span_id == root.context.span_id
    assert leaf_span.parent.span_id == tool_span.context.span_id
    assert leaf_span.attributes["af.agent.execution_role"] == "delegate"
    assert leaf_span.attributes["af.agent.outcome"] == "success"
    assert leaf_span.attributes["af.agent.harness"] == "copilot"
    assert "af.harness" not in leaf_span.attributes
    assert "gen_ai.operation.name" not in leaf_span.attributes
    assert leaf_span.attributes["af.agent.session_id"] == leaf_request.session_id
    [usage_event] = [
        event for event in leaf_span.events if event.name == "af.agent.model.usage"
    ]
    assert usage_event.attributes["gen_ai.usage.output_tokens"] == 4
    assert result.text_result_for_llm == "leaf answer"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_name", "workflow_agent_slug", "outer_name"),
    [
        ("sse_agent", None, "agent.run sse_agent"),
        (None, None, "agent.run agent"),
        (None, "workflow", "agent.run agent"),
    ],
)
async def test_sse_execution_attaches_usage_event_to_stream_run_span(
    preview,
    monkeypatch,
    agent_name,
    workflow_agent_slug,
    outer_name,
):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    import azure_functions_agents._observability as observability

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("copilot-sse-telemetry-test")
    monkeypatch.setattr(observability, "_enabled", True)
    monkeypatch.setattr(observability, "get_tracer", lambda: tracer)

    async def fake_execution(harness, request):
        from azure_functions_agents.harness.copilot_sdk._copilot_observability import (
            invocation_span,
        )

        with invocation_span(request) as observation_span:
            observation_span.add_event(
                "af.agent.model.usage",
                {"gen_ai.usage.output_tokens": 5},
            )
            observation_span.add_event("af.skill.invoked", {"gen_ai.tool.name": "review"})
            observation_span.add_event("af.tool.incomplete", {"gen_ai.tool.name": "search"})
            return runner.AgentResult(
                session_id=request.session_id,
                content="stream reply",
                tool_calls=[],
                model=request.model,
            )

    monkeypatch.setattr(_copilot_execution, "run", fake_execution)
    harness_runner = copilot_state(preview)
    events = [
        json.loads(chunk.removeprefix("data: ").strip())
        async for chunk in harness_runner.run_agent_stream(
            "hello",
            deadline=asyncio.get_running_loop().time() + 5,
            model="test-model",
            session_id="sse-session",
            agent_name=agent_name,
            workflow_agent_slug=workflow_agent_slug,
            tools=[],
            mcp_tools=[],
        )
    ]

    assert [event["type"] for event in events] == ["delta", "done"], events
    finished_spans = exporter.get_finished_spans()
    span = next(item for item in finished_spans if item.name == outer_name)
    semantic = next(
        item
        for item in finished_spans
        if item.attributes.get("gen_ai.operation.name") == "invoke_agent"
    )
    assert semantic.parent.span_id == span.context.span_id
    assert span.attributes["af.agent.harness"] == "copilot"
    assert "af.harness" not in span.attributes
    assert span.attributes["af.agent.session_id"] == "sse-session"
    assert "gen_ai.operation.name" not in span.attributes
    assert [event.name for event in span.events] == [
        "af.agent.model.usage",
        "af.skill.invoked",
        "af.tool.incomplete",
    ]
    usage_event = span.events[0]
    assert usage_event.attributes["gen_ai.usage.output_tokens"] == 5
    assert semantic.events == ()


@pytest.mark.asyncio
async def test_custom_tool_cancellation_closes_wrapper_span_without_exception_content(
    preview, monkeypatch,
):
    from copilot.tools import ToolInvocation
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    import azure_functions_agents._observability as observability
    from azure_functions_agents._tool_descriptor import ToolDescriptor
    from azure_functions_agents.harness.copilot_sdk._copilot_execution import _tool
    from azure_functions_agents.harness.copilot_sdk._copilot_tool_calls import CopilotToolCalls

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("copilot-tool-cancellation-test")
    monkeypatch.setattr(observability, "_enabled", True)
    monkeypatch.setattr(observability, "get_tracer", lambda: tracer)

    async def cancelled(task: str) -> str:
        raise asyncio.CancelledError(task)

    descriptor = ToolDescriptor.create(
        name="cancel_tool",
        description="Cancel the active call.",
        func=cancelled,
    )
    parent = tracer.start_span("agent.run primary")
    with trace.use_span(parent, end_on_exit=True):
        calls = CopilotToolCalls(
            parent_context=observability.capture_current_context(),
            run_span=observability.current_span(),
            custom_tool_names=(descriptor.name,),
        )
        wrapped = _tool(descriptor, calls)
        with pytest.raises(asyncio.CancelledError):
            await wrapped.handler(ToolInvocation(
                session_id="main-session",
                tool_call_id="cancel-call",
                tool_name=descriptor.name,
                arguments={"task": "cancel"},
            ))
        calls.finish_telemetry()

    spans = {span.name: span for span in exporter.get_finished_spans()}
    cancelled_span = spans["execute_tool cancel_tool"]
    assert cancelled_span.attributes["af.tool.outcome"] == "cancelled"
    assert cancelled_span.status.status_code.name == "UNSET"
    assert "af.fault_domain" not in cancelled_span.attributes
    assert all(event.name != "exception" for event in cancelled_span.events)
    assert "cancel" not in repr(cancelled_span.events)


@pytest.mark.asyncio
@pytest.mark.parametrize("capture_sensitive_data", [False, True])
async def test_custom_tool_error_records_exception_once_without_changing_model_visible_failure(
    preview, monkeypatch, capture_sensitive_data,
):
    from copilot.tools import ToolInvocation
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    import azure_functions_agents._observability as observability
    from azure_functions_agents._tool_descriptor import ToolDescriptor
    from azure_functions_agents.harness.copilot_sdk._copilot_execution import _tool
    from azure_functions_agents.harness.copilot_sdk._copilot_tool_calls import CopilotToolCalls

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("copilot-tool-error-test")
    monkeypatch.setattr(observability, "_enabled", True)
    monkeypatch.setattr(observability, "_capture_sensitive_data", capture_sensitive_data)
    monkeypatch.setattr(observability, "get_tracer", lambda: tracer)

    def fail() -> str:
        raise RuntimeError("private tool failure")

    descriptor = ToolDescriptor.create(
        name="failing_tool",
        description="Return a controlled failure.",
        func=fail,
    )
    parent = tracer.start_span("agent.run primary")
    with trace.use_span(parent, end_on_exit=True):
        calls = CopilotToolCalls(
            parent_context=observability.capture_current_context(),
            run_span=observability.current_span(),
            custom_tool_names=(descriptor.name,),
        )
        wrapped = _tool(descriptor, calls)
        result = await wrapped.handler(ToolInvocation(
            session_id="main-session",
            tool_call_id="failure-call",
            tool_name=descriptor.name,
            arguments={},
        ))
        calls.finish_telemetry()

    spans = {span.name: span for span in exporter.get_finished_spans()}
    failed_span = spans["execute_tool failing_tool"]
    assert result.result_type == "failure"
    assert "private tool failure" not in result.text_result_for_llm
    assert failed_span.attributes["af.tool.outcome"] == "error"
    assert failed_span.status.status_code.name == "ERROR"
    assert failed_span.status.description == "RuntimeError('private tool failure')"
    assert failed_span.attributes["af.fault_domain"] == "app"
    assert failed_span.attributes["error.type"] == "RuntimeError"
    _exception_event(failed_span, message="private tool failure")


@pytest.mark.asyncio
@pytest.mark.parametrize("capture_sensitive_data", [False, True])
async def test_delegate_tool_error_records_exception_once_and_keeps_model_visible_failure_sanitized(
    preview, monkeypatch, capture_sensitive_data,
):
    from copilot.tools import ToolInvocation
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    import azure_functions_agents._observability as observability
    from azure_functions_agents.harness.copilot_sdk._copilot_execution import _tool
    from azure_functions_agents.harness.copilot_sdk._copilot_tool_calls import CopilotToolCalls
    from azure_functions_agents.registration.catalog import CatalogEntry, build_catalog

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("copilot-delegate-error-test")
    monkeypatch.setattr(observability, "_enabled", True)
    monkeypatch.setattr(observability, "_capture_sensitive_data", capture_sensitive_data)
    monkeypatch.setattr(observability, "get_tracer", lambda: tracer)
    monkeypatch.setattr(
        runner,
        "run_leaf_agent_task",
        AsyncMock(side_effect=RuntimeError("private leaf failure")),
    )

    specialist = _specialist()
    catalog = build_catalog({
        specialist.slug: CatalogEntry(specialist, AgentCapabilities()),
    })
    [descriptor], tracker = await runner.build_subagent_tools(
        [SubagentRef(agent=specialist.slug)],
        catalog,
        coordinator_deadline=asyncio.get_running_loop().time() + 5,
        harness=preview,
    )

    parent = tracer.start_span("agent.run coordinator")
    with trace.use_span(parent, end_on_exit=True):
        calls = CopilotToolCalls(
            parent_context=observability.capture_current_context(),
            run_span=observability.current_span(),
            custom_tool_names=(descriptor.name,),
        )
        wrapped = _tool(descriptor, calls)
        result = await wrapped.handler(ToolInvocation(
            session_id="main-session",
            tool_call_id="delegate-call",
            tool_name=descriptor.name,
            arguments={"task": "analyze this"},
        ))

    spans = {span.name: span for span in exporter.get_finished_spans()}
    delegate_span = spans["execute_tool delegate_analyst"]
    assert tracker.count == 1
    assert result.result_type == "success"
    assert "private leaf failure" not in result.text_result_for_llm
    assert delegate_span.attributes["af.delegate.outcome"] == "error"
    assert delegate_span.attributes["af.fault_domain"] == "delegate"
    assert delegate_span.status.status_code.name == "ERROR"
    assert delegate_span.status.description == "RuntimeError('private leaf failure')"
    assert "error.type" not in delegate_span.attributes
    _exception_event(delegate_span, message="private leaf failure")


@pytest.mark.asyncio
@pytest.mark.parametrize("capture_sensitive_data", [False, True])
async def test_workflow_leaf_failure_records_exception_once_without_changing_usage_events(
    preview, monkeypatch, capture_sensitive_data,
):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    import azure_functions_agents._observability as observability

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("copilot-workflow-leaf-failure-test")
    monkeypatch.setattr(observability, "_enabled", True)
    monkeypatch.setattr(observability, "_capture_sensitive_data", capture_sensitive_data)
    monkeypatch.setattr(observability, "get_tracer", lambda: tracer)

    async def fail_with_usage(*_args, **_kwargs):
        observability.current_span().add_event(
            "af.agent.model.usage",
            {"gen_ai.usage.output_tokens": 6},
        )
        raise RuntimeError("private session-open detail")

    monkeypatch.setattr(_copilot_execution, "run", AsyncMock(side_effect=fail_with_usage))
    leaf_runner = copilot_state(preview)
    parent = tracer.start_span("agent.run workflow")

    with (
        trace.use_span(parent, end_on_exit=True),
        pytest.raises(RuntimeError, match="private session-open detail"),
    ):
        await leaf_runner.run_leaf_agent_task(
            _specialist(),
            AgentCapabilities(_harness=preview),
            "Analyze",
            timeout=3,
            execution_role="workflow_subagent",
        )

    spans = {span.name: span for span in exporter.get_finished_spans()}
    workflow_span = spans["agent.run analyst"]
    assert workflow_span.parent.span_id == spans["agent.run workflow"].context.span_id
    assert workflow_span.attributes["af.agent.execution_role"] == "workflow_subagent"
    assert workflow_span.attributes["af.agent.outcome"] == "error"
    assert workflow_span.attributes["af.fault_domain"] == "runtime"
    assert workflow_span.attributes["error.type"] == "RuntimeError"
    assert workflow_span.status.description == "RuntimeError('private session-open detail')"
    _exception_event(workflow_span, message="private session-open detail")
    [usage_event] = [
        event for event in workflow_span.events if event.name == "af.agent.model.usage"
    ]
    assert usage_event.attributes["gen_ai.usage.output_tokens"] == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("capture_sensitive_data", [False, True])
async def test_stream_failure_records_exception_once_without_changing_sse_error_semantics(
    preview, monkeypatch, capture_sensitive_data,
):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    import azure_functions_agents._observability as observability

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("copilot-stream-failure-test")
    monkeypatch.setattr(observability, "_enabled", True)
    monkeypatch.setattr(observability, "_capture_sensitive_data", capture_sensitive_data)
    monkeypatch.setattr(observability, "get_tracer", lambda: tracer)

    async def fail_with_usage(*_args, **_kwargs):
        observability.current_span().add_event(
            "af.agent.model.usage",
            {"gen_ai.usage.output_tokens": 4},
        )
        raise RuntimeError("private stream failure")

    monkeypatch.setattr(_copilot_execution, "run", AsyncMock(side_effect=fail_with_usage))
    leaf_runner = copilot_state(preview)

    chunks = [
        chunk
        async for chunk in leaf_runner.run_agent_stream(
            "hello",
            deadline=asyncio.get_running_loop().time() + 5,
            model="test-model",
            session_id="stream-session",
            agent_name="stream_agent",
            display_name="Stream Agent",
        )
    ]

    assert chunks == ['data: {"type": "error", "content": "private stream failure"}\n\n']
    spans = {span.name: span for span in exporter.get_finished_spans()}
    stream_span = spans["agent.run stream_agent"]
    assert stream_span.attributes["af.agent.outcome"] == "error"
    assert stream_span.attributes["af.fault_domain"] == "runtime"
    assert stream_span.attributes["error.type"] == "RuntimeError"
    assert stream_span.status.description == "RuntimeError('private stream failure')"
    _exception_event(stream_span, message="private stream failure")
    [usage_event] = [
        event for event in stream_span.events if event.name == "af.agent.model.usage"
    ]
    assert usage_event.attributes["gen_ai.usage.output_tokens"] == 4


@pytest.mark.asyncio
async def test_delegate_leaf_cancellation_closes_run_span_without_error_status(
    preview, monkeypatch,
):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    import azure_functions_agents._observability as observability

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("copilot-delegate-leaf-cancellation-test")
    monkeypatch.setattr(observability, "_enabled", True)
    monkeypatch.setattr(observability, "get_tracer", lambda: tracer)

    async def cancel_with_usage(*_args, **_kwargs):
        observability.current_span().add_event(
            "af.agent.model.usage",
            {"gen_ai.usage.output_tokens": 6},
        )
        raise asyncio.CancelledError("private cancellation detail")

    monkeypatch.setattr(_copilot_execution, "run", AsyncMock(side_effect=cancel_with_usage))
    leaf_runner = copilot_state(preview)
    parent = tracer.start_span("agent.run coordinator")

    with trace.use_span(parent, end_on_exit=True), pytest.raises(asyncio.CancelledError):
        await leaf_runner.run_leaf_agent_task(
            _specialist(),
            AgentCapabilities(_harness=preview),
            "Analyze",
            timeout=3,
            execution_role="delegate",
        )

    spans = {span.name: span for span in exporter.get_finished_spans()}
    leaf_span = spans["agent.run analyst"]
    assert leaf_span.parent.span_id == spans["agent.run coordinator"].context.span_id
    assert leaf_span.attributes["af.agent.execution_role"] == "delegate"
    assert leaf_span.attributes["af.agent.outcome"] == "cancelled"
    assert leaf_span.status.status_code.name == "UNSET"
    assert "af.fault_domain" not in leaf_span.attributes
    assert all(event.name != "exception" for event in leaf_span.events)
    [usage_event] = [
        event for event in leaf_span.events if event.name == "af.agent.model.usage"
    ]
    assert usage_event.attributes["gen_ai.usage.output_tokens"] == 6


@pytest.mark.asyncio
async def test_sse_orders_tools_and_disconnects_after_success(preview, native):
    from copilot.session_events import (
        AssistantMessageDeltaData,
        SessionEvent,
        SessionEventType,
    )
    from copilot.tools import ToolInvocation

    session = native.create_session.return_value
    function = tool(name="inspect")(lambda: "inspection")
    session.rpc.tools.get_current_metadata.return_value = SimpleNamespace(
        tools=[SimpleNamespace(name="inspect")]
    )

    async def send(*args, **kwargs):
        for parent, text in (("child-call", "secret"), (None, "answer")):
            event = SessionEvent(
                data=AssistantMessageDeltaData(
                    delta_content=text, message_id="message", parent_tool_call_id=parent,
                ),
                type=SessionEventType.ASSISTANT_MESSAGE_DELTA,
                id=uuid4(), timestamp=datetime.now(UTC),
            )
            for callback in session.handlers:
                callback(event)
        [native_tool] = native.create_session.call_args.kwargs["tools"]
        await native_tool.handler(ToolInvocation(
            session_id="native", tool_call_id="call-1", tool_name="inspect", arguments={},
        ))
        return session.send_and_wait.return_value

    session.send_and_wait.side_effect = send
    events = []
    try:
        async for chunk in runner.run_agent_stream(
            "hello", _harness=preview, tools=[function], mcp_tools=[],
        ):
            event = json.loads(chunk.removeprefix("data: "))
            if event["type"] == "session":
                native.create_session.assert_awaited_once()
                session.rpc.tools.get_current_metadata.assert_awaited_once()
            if event["type"] == "done":
                session.disconnect.assert_awaited_once()
            events.append(event)
        assert [event["type"] for event in events] == [
            "session", "delta", "tool_start", "tool_end", "done",
        ]
        assert events[1]["content"] == "answer"
        assert events[2]["tool_call_id"] == events[3]["tool_call_id"]
    finally:
        await _harness_lifecycle._shutdown_harnesses()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["create", "catalog", "interrupt"])
async def test_sse_failure_boundaries_never_advertise_success(preview, native, boundary):
    from copilot.session_events import (
        AbortData,
        AbortReason,
        AssistantMessageData,
        SessionEvent,
        SessionEventType,
    )

    session = native.create_session.return_value
    if boundary == "create":
        native.create_session.side_effect = RuntimeError("private failure")
    elif boundary == "catalog":
        session.rpc.tools.get_current_metadata.return_value = SimpleNamespace(tools=None)
    else:
        async def interrupted(*_args, **_kwargs):
            session.handlers[0](SessionEvent(
                data=AbortData(reason=AbortReason.USER_ABORT),
                type=SessionEventType.ABORT,
                id=uuid4(),
                timestamp=datetime.now(UTC),
            ))
            return SessionEvent(
                data=AssistantMessageData(content="final reply", message_id="final"),
                type=SessionEventType.ASSISTANT_MESSAGE,
                id=uuid4(),
                timestamp=datetime.now(UTC),
            )

        session.send_and_wait.side_effect = interrupted
    try:
        events = [
            json.loads(chunk.removeprefix("data: "))
            async for chunk in runner.run_agent_stream(
                "hello", _harness=preview, tools=[], mcp_tools=[],
            )
        ]
        assert [event["type"] for event in events] == (
            ["error"] if boundary == "create" else ["session", "error"]
        )
        if boundary == "interrupt":
            session.abort.assert_awaited_once()
    finally:
        await _harness_lifecycle._shutdown_harnesses()


@pytest.mark.asyncio
async def test_stream_invalid_session_id_emits_sse_error_without_native_execution(preview, native):
    events = [
        json.loads(chunk.removeprefix("data: "))
        async for chunk in runner.run_agent_stream(
            "hello",
            _harness=preview,
            tools=[],
            mcp_tools=[],
            session_id="bad id!",
        )
    ]

    assert [event["type"] for event in events] == ["error"]
    assert "Invalid session_id" in events[0]["content"]
    native.create_session.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_resume", [False, True])
async def test_sse_supplied_session_id_is_emitted_once_even_when_resume_fails(
    preview, native, fail_resume,
):
    await _seed_session_state(preview, session_id="caller-id")
    if fail_resume:
        native.resume_session.side_effect = RuntimeError("private resume failure")
    try:
        events = [
            json.loads(chunk.removeprefix("data: "))
            async for chunk in runner.run_agent_stream(
                "hello", _harness=preview, tools=[], mcp_tools=[], session_id="caller-id",
            )
        ]
        assert [event for event in events if event["type"] == "session"] == [
            {"type": "session", "session_id": "caller-id"},
        ]
        assert events[-1]["type"] == ("error" if fail_resume else "done")
        native.create_session.assert_not_awaited()
        native.resume_session.assert_awaited_once()
    finally:
        await _harness_lifecycle._shutdown_harnesses()


@pytest.mark.asyncio
async def test_sse_fresh_supplied_session_id_creates_requested_native_session(preview, native):
    try:
        events = [
            json.loads(chunk.removeprefix("data: "))
            async for chunk in runner.run_agent_stream(
                "hello", _harness=preview, tools=[], mcp_tools=[], session_id="caller-id",
            )
        ]
        assert [event for event in events if event["type"] == "session"] == [
            {"type": "session", "session_id": "caller-id"},
        ]
        assert events[-1]["type"] == "done"
        assert native.create_session.call_args.kwargs["session_id"] == "main-caller-id"
        native.resume_session.assert_not_awaited()
    finally:
        await _harness_lifecycle._shutdown_harnesses()


@pytest.mark.asyncio
async def test_sse_slow_consumer_drops_old_events_without_cancelling_run(preview, native):
    from copilot.session_events import (
        AssistantMessageDeltaData,
        SessionEvent,
        SessionEventType,
    )

    session = native.create_session.return_value
    produced = 0
    total_events = _copilot_runner._STREAM_EVENT_QUEUE_CAPACITY + 32

    async def flood(*_args, **_kwargs):
        nonlocal produced
        for index in range(total_events):
            event = SessionEvent(
                data=AssistantMessageDeltaData(
                    delta_content=f"chunk-{index}",
                    message_id=f"message-{index}",
                    parent_tool_call_id=None,
                ),
                type=SessionEventType.ASSISTANT_MESSAGE_DELTA,
                id=uuid4(),
                timestamp=datetime.now(UTC),
            )
            for callback in list(session.handlers):
                callback(event)
            produced += 1
        return session.send_and_wait.return_value

    session.send_and_wait.side_effect = flood
    stream = runner.run_agent_stream("hello", _harness=preview, tools=[], mcp_tools=[])
    try:
        first = json.loads((await anext(stream)).removeprefix("data: "))
        assert first["type"] == "session"
        events = [first]
        async for chunk in stream:
            events.append(json.loads(chunk.removeprefix("data: ")))
        deltas = [event for event in events if event["type"] == "delta"]
        truncation = [event for event in events if event["type"] == "stream_truncated"]
        messages = [event for event in events if event["type"] == "message"]
        errors = [event for event in events if event["type"] == "error"]
        assert len(deltas) <= _copilot_runner._STREAM_EVENT_QUEUE_CAPACITY
        first_chunk = int(deltas[0]["content"].removeprefix("chunk-"))
        assert first_chunk == total_events - len(deltas)
        assert deltas[-1]["content"] == f"chunk-{total_events - 1}"
        assert truncation == [
            {
                "type": "stream_truncated",
                "dropped_events": total_events - len(deltas),
            }
        ]
        assert messages == [{"type": "message", "content": "synthetic reply"}]
        assert errors == []
        assert events[-1] == {"type": "done"}
        assert produced == total_events
        session.abort.assert_not_awaited()
        session.disconnect.assert_awaited_once()
    finally:
        await _harness_lifecycle._shutdown_harnesses()


@pytest.mark.asyncio
async def test_closing_stream_cancels_and_aborts_without_stopping_shared_client(preview, native):
    session = native.create_session.return_value
    started = asyncio.Event()

    async def stall(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    session.send_and_wait.side_effect = stall
    stream = runner.run_agent_stream("hello", _harness=preview, tools=[], mcp_tools=[])
    try:
        assert json.loads((await anext(stream)).removeprefix("data: "))["type"] == "session"
        await started.wait()
        await stream.aclose()
        session.abort.assert_awaited_once()
        session.disconnect.assert_awaited_once()
        native.stop.assert_not_awaited()
    finally:
        await _harness_lifecycle._shutdown_harnesses()


@pytest.mark.asyncio
async def test_leaf_resume_is_rejected_without_creating_storage(preview, native):
    with pytest.raises(CopilotPreviewError, match="cannot be resumed"):
        await _copilot_execution.run(
            preview, replace(_request(new_session=False), execution_role="delegate"),
        )
    native.start.assert_not_awaited()
    assert not copilot_state(preview).storage_root.exists()
