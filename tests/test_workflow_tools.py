"""Workflow start results and operation diagnostics."""

from __future__ import annotations

import asyncio
import json
import re
from unittest.mock import AsyncMock, Mock

import pytest
from opentelemetry.trace import StatusCode

from azure_functions_agents import _observability as obs
from azure_functions_agents._agent_run_diagnostics import _total_tool_error_count
from azure_functions_agents.runner import AgentResult
from azure_functions_agents.workflows import tools
from azure_functions_agents.workflows.context import WorkflowSessionContext
from azure_functions_agents.workflows.integration import _SHARED_ADDENDUM
from azure_functions_agents.workflows.schema import WorkflowPlanPolicy
from tests.test_observability import _install_test_tracer

SECRET = "private-storage-token-and-endpoint"
POLICY = WorkflowPlanPolicy(allowed_tools=frozenset(), allowed_subagents=frozenset())
TASKS = [{"id": "pause", "type": "wait", "duration": "PT1S"}]


@pytest.fixture
def workflow_spans(monkeypatch):
    provider, exporter = _install_test_tracer(monkeypatch)
    monkeypatch.setattr(obs, "_configured", True)
    monkeypatch.setattr(obs, "_capture_sensitive_data", False)
    yield provider, exporter
    provider.shutdown()


def _client(*, failure=None):
    client = Mock()
    client.get_status_all = AsyncMock(return_value=[])
    client.get_status = AsyncMock()
    client.accepted = []

    async def schedule(*_args, **kwargs):
        client.accepted.append(kwargs["instance_id"])
        await asyncio.sleep(0)
        if failure == "schedule":
            raise RuntimeError(SECRET)
        if failure == "cancel_schedule":
            raise asyncio.CancelledError(SECRET)
        return kwargs["instance_id"]

    client.schedule_new_orchestration = AsyncMock(side_effect=schedule)
    if failure == "check":
        client.get_status_all.side_effect = RuntimeError(SECRET)
    if failure == "cancel_check":
        client.get_status_all.side_effect = asyncio.CancelledError(SECRET)
    return client


async def _start(client, *, session_id="session-1"):
    return await tools.start_workflow(
        tools.StartWorkflowParams(tasks=TASKS),
        WorkflowSessionContext("agent", session_id, "Agent", client),
        policy=POLICY,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["check", "schedule"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_start_client_failure_has_safe_result_and_operation_span(
    monkeypatch, caplog, workflow_spans, failure, enabled,
):
    provider, exporter = workflow_spans
    monkeypatch.setattr(obs, "_enabled", enabled)
    client = _client(failure=failure)
    with provider.get_tracer("test").start_as_current_span("parent") as parent:
        result = json.loads(await _start(client))

    diagnostic_id = result["diagnostic_id"]
    assert re.fullmatch("[0-9a-f]{32}", diagnostic_id)
    code = (
        "workflow_admission_check_failed"
        if failure == "check" else "workflow_schedule_outcome_unknown"
    )
    operation = "check_active_workflows" if failure == "check" else "schedule_workflow"
    start_status = "not_started" if failure == "check" else "unknown"
    expected = {
        "error": "failed to start workflow",
        "error_code": code,
        "operation": operation,
        "start_status": start_status,
        "diagnostic_id": diagnostic_id,
    }
    if failure == "schedule":
        expected["attempted_workflow_id"] = (
            client.schedule_new_orchestration.call_args.kwargs["instance_id"]
        )
        assert client.accepted == [expected["attempted_workflow_id"]]
    assert result == expected
    assert SECRET not in json.dumps(result)
    assert client.get_status_all.await_count == 1
    assert client.schedule_new_orchestration.await_count == (failure == "schedule")
    client.get_status.assert_not_called()
    [log] = [record for record in caplog.records if record.exc_info]
    for value in (diagnostic_id, code, operation, start_status):
        assert value in log.message
    assert SECRET in str(log.exc_info[1])
    if failure == "schedule":
        assert expected["attempted_workflow_id"] in log.message

    spans = [
        span for span in exporter.get_finished_spans()
        if span.name.startswith("workflow.start.")
    ]
    if not enabled:
        assert spans == []
        return
    assert [span.name for span in spans] == (
        ["workflow.start.check_active"] if failure == "check"
        else ["workflow.start.check_active", "workflow.start.schedule"]
    )
    failed = spans[-1]
    assert failed.status.status_code is StatusCode.ERROR
    assert failed.attributes["error.type"] == code
    assert failed.attributes["af.fault_domain"] == "unknown"
    assert failed.attributes["af.workflow.operation"] == operation
    assert failed.attributes["af.workflow.start_status"] == start_status
    assert failed.attributes["af.workflow.error_code"] == code
    for span in spans:
        assert span.parent.span_id == parent.get_span_context().span_id
        assert span.attributes["af.lifecycle_stage"] == "tool_execution"
        assert span.attributes["af.workflow.agent_slug"] == "agent"
        assert span.attributes["af.workflow.diagnostic_id"] == diagnostic_id
        assert SECRET not in str(span.attributes)
        assert SECRET not in (span.status.description or "")
        assert not span.events
    if failure == "schedule":
        assert failed.attributes["af.workflow.attempted_workflow_id"] == (
            expected["attempted_workflow_id"]
        )
        assert spans[0].status.status_code is StatusCode.UNSET


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel_check", "cancel_schedule"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_start_cancellation_propagates_without_dependency_error(
    monkeypatch, workflow_spans, failure, enabled,
):
    _, exporter = workflow_spans
    monkeypatch.setattr(obs, "_enabled", enabled)
    client = _client(failure=failure)
    with pytest.raises(asyncio.CancelledError):
        await _start(client)
    assert client.schedule_new_orchestration.await_count == (failure == "cancel_schedule")
    spans = exporter.get_finished_spans()
    if not enabled:
        assert spans == ()
        return
    assert len(spans) == (1 if failure == "cancel_check" else 2)
    cancelled = spans[-1]
    assert cancelled.attributes["af.tool.outcome"] == "cancelled"
    assert cancelled.attributes["af.workflow.start_status"] == (
        "not_started" if failure == "cancel_check" else "unknown"
    )
    assert cancelled.status.status_code is StatusCode.UNSET
    assert "error.type" not in cancelled.attributes
    assert "af.workflow.error_code" not in cancelled.attributes
    assert not cancelled.events
    assert SECRET not in str(cancelled.attributes)


@pytest.mark.asyncio
async def test_active_limit_is_successful_check_without_schedule(monkeypatch, workflow_spans):
    _, exporter = workflow_spans
    client = _client()
    monkeypatch.setattr(
        tools, "count_active_session_workflows",
        AsyncMock(return_value=tools.MAX_ACTIVE_WORKFLOWS_PER_SESSION),
    )
    result = json.loads(await _start(client))
    assert result == {
        "error": "too many active workflows for this session",
        "active": tools.MAX_ACTIVE_WORKFLOWS_PER_SESSION,
        "limit": tools.MAX_ACTIVE_WORKFLOWS_PER_SESSION,
    }
    client.schedule_new_orchestration.assert_not_called()
    [span] = exporter.get_finished_spans()
    assert span.name == "workflow.start.check_active"
    assert span.status.status_code is StatusCode.UNSET
    assert span.attributes["af.workflow.start_status"] == "not_started"
    assert "error.type" not in span.attributes


@pytest.mark.asyncio
async def test_success_and_mismatched_sdk_id_keep_submitted_id(monkeypatch, caplog, workflow_spans):
    _, exporter = workflow_spans
    client = _client()
    client.schedule_new_orchestration.side_effect = None
    client.schedule_new_orchestration.return_value = "different-id"
    with caplog.at_level("INFO", logger="azure.functions.AgentRuntime"):
        result = json.loads(await _start(client))
    assert result == {
        "workflow_id": client.schedule_new_orchestration.call_args.kwargs["instance_id"],
    }
    spans = exporter.get_finished_spans()
    assert [span.attributes["af.workflow.start_status"] for span in spans] == [
        "not_started", "scheduled",
    ]
    assert [span.attributes["af.workflow.operation"] for span in spans] == [
        "check_active_workflows", "schedule_workflow",
    ]
    assert all(span.status.status_code is StatusCode.UNSET for span in spans)
    assert all(span.parent is None for span in spans)
    diagnostic_id = spans[0].attributes["af.workflow.diagnostic_id"]
    assert spans[1].attributes["af.workflow.diagnostic_id"] == diagnostic_id
    assert any(diagnostic_id in record.message for record in caplog.records)
    assert any("different-id" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_concurrent_start_failures_have_distinct_correlation(workflow_spans):
    _, exporter = workflow_spans
    results = await asyncio.gather(*[
        _start(_client(failure="schedule"), session_id=f"session-{index}")
        for index in range(4)
    ])
    payloads = [json.loads(result) for result in results]
    ids = {result["diagnostic_id"] for result in payloads}
    assert len(ids) == 4
    for result in payloads:
        spans = [
            span for span in exporter.get_finished_spans()
            if span.attributes["af.workflow.diagnostic_id"] == result["diagnostic_id"]
        ]
        assert len(spans) == 2
        assert spans[1].attributes["af.workflow.attempted_workflow_id"] == (
            result["attempted_workflow_id"]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["maf", "copilot"])
async def test_harness_tool_preserves_safe_error_and_completion(workflow_spans, harness):
    from copilot.tools import ToolInvocation

    from azure_functions_agents.harness.agent_framework._maf_tools import build_maf_tools
    from azure_functions_agents.harness.copilot_sdk._copilot_execution import _tool
    from azure_functions_agents.harness.copilot_sdk._copilot_tool_calls import CopilotToolCalls

    provider, exporter = workflow_spans
    client = _client(failure="schedule")
    [descriptor, *_] = tools.build_workflow_tools(
        session_id="session-1", workflow_agent_slug="agent", agent_name="Agent",
        durable_client=client, policy=POLICY,
    )
    events = []
    with provider.get_tracer("test").start_as_current_span("execute_tool fhs_start_workflow"):
        if harness == "maf":
            [content] = await build_maf_tools([descriptor])[0].invoke(arguments={"tasks": TASKS})
            text = content.text
        else:
            calls = CopilotToolCalls(custom_tool_names=(descriptor.name,))
            result = await _tool(descriptor, calls, events.append).handler(ToolInvocation(
                session_id="session-1", tool_call_id="start-1",
                tool_name=descriptor.name, arguments={"tasks": TASKS},
            ))
            assert result.result_type == "success"
            text = result.text_result_for_llm
            assert calls.calls[0]["success"] is True
            assert [event["type"] for event in events] == ["tool_start", "tool_end"]
            assert events[-1]["result"] == text
    payload = json.loads(text)
    assert payload["error_code"] == "workflow_schedule_outcome_unknown"
    assert SECRET not in text
    evidence = AgentResult(content="Start acceptance is unknown.", session_id="session-1", tool_calls=[{
        "type": "tool_start", "tool_call_id": "start-1", "tool_name": descriptor.name,
        "arguments": {}, "result": text,
    }])
    assert _total_tool_error_count(evidence) == 1
    spans = exporter.get_finished_spans()
    failed = next(span for span in spans if span.name == "workflow.start.schedule")
    assert failed.status.status_code is StatusCode.ERROR
    assert failed.parent is not None
    parents = {span.context.span_id: span for span in spans}
    assert parents[failed.parent.span_id].name == "execute_tool fhs_start_workflow"


def test_workflow_start_guidance_does_not_confirm_unknown_acceptance():
    for guidance in (tools.START_WORKFLOW_DESCRIPTION, _SHARED_ADDENDUM):
        assert "attempted_workflow_id" in guidance
        assert "unknown" in guidance
        assert "Do not repeat" in guidance


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["maf", "copilot"])
@pytest.mark.parametrize("surface", ["queue", "http", "chat", "mcp"])
@pytest.mark.parametrize("recover", [False, True])
async def test_workflow_operation_failure_preserves_transport_completion(
    monkeypatch, tmp_path, workflow_spans, harness, surface, recover,
):
    from azure_functions_agents.config.schema import BuiltinEndpointsConfig
    from azure_functions_agents.harness._harness_binding import AppHarness, HarnessKind
    from azure_functions_agents.registration import _handlers, endpoints
    from azure_functions_agents.registration.capabilities import AgentCapabilities
    from tests._harness_helpers import make_copilot_harness
    from tests.test_registration_endpoints import (
        DummyRequest,
        FakeFunctionApp,
        _resolved_agent,
    )

    _, exporter = workflow_spans
    client = _client(failure="schedule")
    binding = (
        make_copilot_harness(tmp_path) if harness == "copilot"
        else AppHarness(HarnessKind.MAF, tmp_path)
    )
    capabilities = AgentCapabilities(_harness=binding)
    resolved = _resolved_agent(
        name="Agent", slug="agent", is_main=False,
        builtin_endpoints=BuiltinEndpointsConfig(debug_chat_ui=True, mcp=True),
    )
    tool_results = []

    async def run(_prompt, **kwargs):
        assert kwargs["_harness"] is binding
        assert kwargs["workflow_durable_client"] is client
        [start, *_] = tools.build_workflow_tools(
            session_id=kwargs["session_id"],
            workflow_agent_slug=kwargs["workflow_agent_slug"],
            agent_name=kwargs["agent_name"], durable_client=client, policy=POLICY,
        )
        tool_results.append(await start.invoke(arguments={"tasks": TASKS}))
        if recover:
            client.schedule_new_orchestration.side_effect = (
                lambda *_args, **kw: kw["instance_id"]
            )
            tool_results.append(await start.invoke(arguments={"tasks": TASKS}))
        return AgentResult(
            session_id=kwargs["session_id"],
            content="Workflow started." if recover else "Start acceptance is unknown.",
            tool_calls=[{
                "type": "tool_start", "tool_call_id": f"start-{index}",
                "tool_name": start.name, "arguments": {}, "result": text,
            } for index, text in enumerate(tool_results)],
        )

    monkeypatch.setattr(_handlers, "_run_agent", run)
    monkeypatch.setattr(endpoints, "_run_agent", run)
    if surface == "queue":
        handler = _handlers.make_agent_handler(
            resolved, "queue_trigger", capabilities, workflows_enabled=True, workflow_policy=POLICY,
        )
        assert await handler({"message": "start"}, client) is None
    elif surface == "http":
        handler = _handlers.make_http_agent_handler(
            resolved, capabilities, workflows_enabled=True, workflow_policy=POLICY,
        )
        response = await handler(
            DummyRequest({"prompt": "start"}, headers={"x-ms-session-id": "caller"}), client,
        )
        assert response.status_code == 200
        assert response.headers["x-ms-session-id"] == "caller"
        assert SECRET not in response.body.decode()
    else:
        app = FakeFunctionApp()
        endpoints.register_builtin_endpoints(
            app, resolved, capabilities, workflows_enabled=True, workflow_policy=POLICY,
        )
        if surface == "chat":
            handler = next(
                route["handler"] for route in app.routes if route.get("route") == "agents/agent/chat"
            )
            response = await handler(
                DummyRequest({"prompt": "start"}, headers={"x-ms-session-id": "caller"}), client,
            )
            assert response.status_code == 200
            payload = json.loads(response.body)
            assert response.headers["x-ms-session-id"] == "caller"
        else:
            handler = next(route["handler"] for route in app.routes if route.get("mcp_tool_trigger"))
            payload = json.loads(await handler(
                json.dumps({"arguments": {"prompt": "start"}, "sessionId": "caller"}), client,
            ))
        assert payload["session_id"] == "caller"
        assert json.loads(payload["tool_calls"][0]["result"])["start_status"] == "unknown"
        assert SECRET not in json.dumps(payload)

    assert client.schedule_new_orchestration.await_count == (2 if recover else 1)
    spans = exporter.get_finished_spans()
    [run_span] = [span for span in spans if span.name == "agent.run agent"]
    assert run_span.attributes["af.agent.outcome"] == "success"
    assert run_span.attributes["af.agent.tool_error_count"] == 1
    assert run_span.status.status_code is StatusCode.UNSET
    operations = [span for span in spans if span.name == "workflow.start.schedule"]
    assert len(operations) == (2 if recover else 1)
    assert operations[0].status.status_code is StatusCode.ERROR
    assert operations[0].parent.span_id == run_span.context.span_id
    if recover:
        assert json.loads(tool_results[1])["workflow_id"]
        assert operations[1].status.status_code is StatusCode.UNSET
        assert operations[0].attributes["af.workflow.diagnostic_id"] != (
            operations[1].attributes["af.workflow.diagnostic_id"]
        )


@pytest.mark.asyncio
async def test_non_http_runner_exception_still_escapes(monkeypatch, workflow_spans):
    from azure_functions_agents.registration import _handlers
    from azure_functions_agents.registration.capabilities import AgentCapabilities
    from tests.test_registration_handlers import _resolved_agent

    async def fail(*_args, **_kwargs):
        raise RuntimeError("runner failed")

    monkeypatch.setattr(_handlers, "_run_agent", fail)
    handler = _handlers.make_agent_handler(
        _resolved_agent(response_schema=None), "queue_trigger", AgentCapabilities(),
    )
    with pytest.raises(RuntimeError, match="runner failed"):
        await handler({"message": "start"})


@pytest.mark.asyncio
async def test_plan_serialization_error_is_not_a_schedule_result(monkeypatch, workflow_spans):
    def fail(*_args):
        raise ValueError("serialization failed")

    monkeypatch.setattr(tools, "plan_to_activity_inputs", fail)
    client = _client()
    with pytest.raises(ValueError, match="serialization failed"):
        await _start(client)
    client.schedule_new_orchestration.assert_not_called()
    [span] = workflow_spans[1].get_finished_spans()
    assert span.name == "workflow.start.check_active"


@pytest.mark.asyncio
async def test_maf_stream_keeps_safe_tool_error_and_normal_completion(
    monkeypatch, tmp_path, workflow_spans,
):
    from agent_framework import (
        BaseChatClient,
        ChatResponse,
        ChatResponseUpdate,
        Content,
        FunctionInvocationLayer,
        ResponseStream,
    )

    from azure_functions_agents import runner
    from azure_functions_agents.harness._harness_binding import AppHarness, HarnessKind
    from azure_functions_agents.harness._provider_config import InferenceTarget
    from azure_functions_agents.harness.agent_framework import _maf_execution as maf

    class StartClient(FunctionInvocationLayer, BaseChatClient):
        def __init__(self):
            super().__init__()
            self.call_count = 0

        def _inner_get_response(self, *, messages, stream, options, **kwargs):
            assert stream
            self.call_count += 1
            tool_turn = self.call_count == 1

            async def updates():
                yield ChatResponseUpdate(
                    contents=[
                        Content.from_function_call(
                            "start-1", "fhs_start_workflow", arguments={"tasks": TASKS},
                        )
                        if tool_turn else Content.from_text("Start acceptance is unknown.")
                    ],
                    role="assistant", finish_reason="tool_calls" if tool_turn else "stop",
                )

            return ResponseStream(updates(), finalizer=ChatResponse.from_updates)

    chat_client = StartClient()
    monkeypatch.setattr(maf, "build_chat_client", lambda _model: (chat_client, InferenceTarget()))
    monkeypatch.setattr(maf, "_build_history_provider", lambda _slug: None)
    client = _client(failure="schedule")
    chunks = [chunk async for chunk in runner.run_agent_stream(
        "Start work.", _harness=AppHarness(HarnessKind.MAF, tmp_path),
        session_id="session-1", agent_name="agent", mcp_tools=[], model="fixture-model",
        workflow_enabled=True, workflow_agent_slug="agent",
        workflow_durable_client=client, workflow_policy=POLICY,
    )]
    events = [json.loads(chunk.removeprefix("data: ").strip()) for chunk in chunks]
    assert [event["type"] for event in events] == [
        "session", "tool_start", "tool_end", "delta", "done",
    ]
    assert json.loads(events[2]["result"])["start_status"] == "unknown"
    assert SECRET not in "".join(chunks)
    assert chat_client.call_count == 2
    spans = workflow_spans[1].get_finished_spans()
    run_span = next(span for span in spans if span.name == "agent.run agent")
    assert run_span.attributes["af.agent.outcome"] == "success"
    assert run_span.attributes["af.agent.tool_error_count"] == 1
    operation = next(span for span in spans if span.name == "workflow.start.schedule")
    assert operation.status.status_code is StatusCode.ERROR
    assert operation.context.trace_id == run_span.context.trace_id
