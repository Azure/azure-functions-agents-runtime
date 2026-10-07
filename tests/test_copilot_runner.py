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
from azure_functions_agents.config.schema import (
    BuiltinEndpointsConfig,
    ResolvedAgent,
    SubagentRef,
    ToolsFilter,
)
from azure_functions_agents.discovery.skills import SkillDescriptor
from azure_functions_agents.harness import _harness_execution, _harness_lifecycle
from azure_functions_agents.harness.copilot_sdk import _copilot_execution, _copilot_runner
from azure_functions_agents.harness.copilot_sdk._copilot_preview import CopilotPreviewError
from azure_functions_agents.registration.capabilities import (
    AgentCapabilities,
    with_runtime_skill_paths,
)
from azure_functions_agents.registration.catalog import CatalogEntry, build_catalog
from azure_functions_agents.workflows.integration import data_driven_workflows_skill_path
from azure_functions_agents.workflows.schema import WorkflowPlanPolicy
from tests.test_copilot_execution import _fake_client, _request
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
    primary = replace(preview, session_storage=resolve_route(preview.app_root))
    assert primary.session_storage.mode is StorageMode.BLOB
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
        assert not (primary.session_storage.local_dir / "copilot-native").exists()
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
            with pytest.raises(CopilotPreviewError, match="ephemeral specialist"):
                await task
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
        "start_workflow", "get_workflow_status", "list_workflows",
        "cancel_workflow", "terminate_workflow",
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
    assert not preview.storage_root.exists()
