from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from azure_functions_agents._function_tool import WorkflowTool
from azure_functions_agents.config.schema import (
    BuiltinEndpointsConfig,
    ResolvedAgent,
    ToolsFilter,
)
from azure_functions_agents.registration.capabilities import AgentCapabilities
from azure_functions_agents.registration.catalog import CatalogEntry, build_catalog
from azure_functions_agents.workflows import engine, integration
from azure_functions_agents.workflows.schema import (
    SUB_AGENT_TASK_TYPE,
    TOOL_TASK_TYPE,
    WorkflowPlanPolicy,
)


class _FakeApp:
    def __init__(self) -> None:
        self.blueprints: list[Any] = []

    def register_blueprint(self, blueprint: Any) -> None:
        self.blueprints.append(blueprint)


def _make_resolved(slug: str, *, timeout: float = 12.0) -> ResolvedAgent:
    return ResolvedAgent(
        name=slug,
        slug=slug,
        description=f"{slug} description",
        trigger=None,
        instructions=f"{slug} instructions",
        is_main=False,
        builtin_endpoints=BuiltinEndpointsConfig(),
        model="test-model",
        timeout=timeout,
        enabled_mcp_names=[],
        enabled_skills_names=[],
        tool_filter=ToolsFilter(),
        sandbox_config=None,
        input_schema=None,
        response_schema=None,
        response_example=None,
        metadata={},
        source_file=f"{slug}.agent.md",
    )


def _catalog(*slugs: str):
    return build_catalog(
        {
            slug: CatalogEntry(_make_resolved(slug), AgentCapabilities())
            for slug in slugs
        }
    )


def _registered_function(
    name: str,
    *,
    catalog=None,
    workflow_agent_policies=None,
    handler_catalog=None,
) -> Callable[..., Any]:
    app = _FakeApp()
    engine.register_workflows(
        app,
        catalog=catalog,
        workflow_agent_policies=workflow_agent_policies,
        handler_catalog=handler_catalog,
    )
    [blueprint] = app.blueprints
    for builder in blueprint._function_builders:
        function = builder._function
        if function._name == name:
            registered = function._func
            if name == engine.ORCHESTRATOR_NAME:
                assert registered.__closure__ is not None
                return registered.__closure__[0].cell_contents
            return registered
    raise AssertionError(f"workflow function {name!r} was not registered")


@pytest.mark.asyncio
async def test_sub_agent_activity_uses_catalog_timeout_and_result_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, float, str]] = []

    async def run_leaf(
        resolved: ResolvedAgent,
        capabilities: AgentCapabilities,
        task: str,
        *,
        timeout: float,
        execution_role: str,
    ) -> str:
        calls.append((resolved.slug, task, timeout, execution_role))
        return "PR is ready to merge."

    monkeypatch.setattr(engine, "run_leaf_agent_task", run_leaf)
    activity = _registered_function(
        engine.SUB_AGENT_ACTIVITY_NAME,
        catalog=_catalog("pr_status_analyst"),
        workflow_agent_policies={
            "coordinator": WorkflowPlanPolicy(
                allowed_tools=frozenset(),
                allowed_subagents=frozenset({"pr_status_analyst"}),
            )
        },
    )

    result = await activity(
        {
            "id": "analyze_pr",
            "agent": "pr_status_analyst",
            "task": "Analyze PR 117.",
            "workflow_id": "workflow-1",
            "workflow_agent_slug": "coordinator",
        }
    )

    assert result == {
        "id": "analyze_pr",
        "result": {
            "agent": "pr_status_analyst",
            "text": "PR is ready to merge.",
        },
    }
    assert calls == [
        (
            "pr_status_analyst",
            "Analyze PR 117.",
            12.0,
            "workflow_subagent",
        )
    ]


@pytest.mark.asyncio
async def test_sub_agent_activity_fails_closed_on_catalog_miss() -> None:
    activity = _registered_function(
        engine.SUB_AGENT_ACTIVITY_NAME,
        catalog=_catalog("known"),
        workflow_agent_policies={
            "coordinator": WorkflowPlanPolicy(
                allowed_tools=frozenset(),
                allowed_subagents=frozenset({"missing"}),
            )
        },
    )

    with pytest.raises(RuntimeError, match="not available"):
        await activity(
            {
                "id": "analyze_pr",
                "agent": "missing",
                "task": "Analyze PR 117.",
                "workflow_id": "workflow-1",
                "workflow_agent_slug": "coordinator",
            }
        )


@pytest.mark.asyncio
async def test_sub_agent_activity_rejects_revoked_owner_grant() -> None:
    activity = _registered_function(
        engine.SUB_AGENT_ACTIVITY_NAME,
        catalog=_catalog("pr_status_analyst"),
        workflow_agent_policies={
            "coordinator": WorkflowPlanPolicy(
                allowed_tools=frozenset(),
                allowed_subagents=frozenset(),
            )
        },
    )

    with pytest.raises(RuntimeError, match="not authorized"):
        await activity(
            {
                "id": "analyze_pr",
                "agent": "pr_status_analyst",
                "task": "Analyze PR 117.",
                "workflow_id": "workflow-1",
                "workflow_agent_slug": "coordinator",
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("workflow_agent_policies", [None, {}])
async def test_sub_agent_activity_missing_agent_policy_fails_closed(
    workflow_agent_policies,
) -> None:
    activity = _registered_function(
        engine.SUB_AGENT_ACTIVITY_NAME,
        catalog=_catalog("pr_status_analyst"),
        workflow_agent_policies=workflow_agent_policies,
    )

    with pytest.raises(RuntimeError, match="agent policy"):
        await activity(
            {
                "id": "analyze_pr",
                "agent": "pr_status_analyst",
                "task": "Analyze PR 117.",
                "workflow_id": "workflow-1",
                "workflow_agent_slug": "missing",
            }
        )


@pytest.mark.asyncio
async def test_sub_agent_activity_sanitizes_leaf_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "provider credential secret"

    async def fail(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError(secret)

    monkeypatch.setattr(engine, "run_leaf_agent_task", fail)
    activity = _registered_function(
        engine.SUB_AGENT_ACTIVITY_NAME,
        catalog=_catalog("pr_status_analyst"),
        workflow_agent_policies={
            "coordinator": WorkflowPlanPolicy(
                allowed_tools=frozenset(),
                allowed_subagents=frozenset({"pr_status_analyst"}),
            )
        },
    )

    with pytest.raises(RuntimeError) as exc_info:
        await activity(
            {
                "id": "analyze_pr",
                "agent": "pr_status_analyst",
                "task": "Analyze PR 117.",
                "workflow_id": "workflow-1",
                "workflow_agent_slug": "coordinator",
            }
        )

    assert str(exc_info.value) == (
        "task 'analyze_pr': Workflow Sub Agent 'pr_status_analyst' failed "
        "(error_code=workflow_subagent_execution_failed)"
    )
    assert secret not in str(exc_info.value)


class _Task:
    def __init__(self, result: Any = None) -> None:
        self.result = result
        self.is_completed = True
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class _FakeOrchestrationContext:
    def __init__(
        self,
        tasks: list[dict[str, Any]],
        result_for: Callable[[str, dict[str, Any]], dict[str, Any]],
    ) -> None:
        self.instance_id = "workflow-parent"
        self._input = {"workflow_agent_slug": "coordinator", "tasks": tasks}
        self._result_for = result_for
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.last_wave = _Task([])
        self.cancel_task = _Task()
        self.statuses: list[str] = []

    def get_input(self) -> dict[str, Any]:
        return self._input

    def wait_for_external_event(self, name: str) -> _Task:
        assert name == engine.CANCEL_EVENT_NAME
        return self.cancel_task

    def call_activity(self, name: str, payload: dict[str, Any]) -> _Task:
        self.calls.append((name, payload))
        return _Task(self._result_for(name, payload))

    def task_all(self, tasks: list[_Task]) -> _Task:
        self.last_wave = _Task([task.result for task in tasks])
        return self.last_wave

    def task_any(self, tasks: list[_Task]) -> _Task:
        return _Task()

    def set_custom_status(self, status: str) -> None:
        self.statuses.append(status)


def _run_orchestrator(
    orchestrator: Callable[[Any], Any],
    context: _FakeOrchestrationContext,
) -> dict[str, Any]:
    generator = orchestrator(context)
    try:
        next(generator)
        while True:
            generator.send(context.last_wave)
    except StopIteration as stop:
        return stop.value


def test_orchestrator_preserves_activity_failure() -> None:
    class _FailedWaveContext(_FakeOrchestrationContext):
        def task_all(self, tasks: list[_Task]) -> _Task:
            self.last_wave = _Task(RuntimeError("activity authorization failed"))
            return self.last_wave

    context = _FailedWaveContext(
        [
            {
                "id": "publish",
                "type": TOOL_TASK_TYPE,
                "tool": "publish",
                "args": {},
                "depends_on": [],
            }
        ],
        lambda name, payload: {"id": payload["id"], "result": {"ok": True}},
    )
    orchestrator = _registered_function(engine.ORCHESTRATOR_NAME)

    with pytest.raises(RuntimeError, match="activity authorization failed"):
        _run_orchestrator(orchestrator, context)


def test_orchestrator_fans_out_sub_agents_and_reduces_templated_results() -> None:
    tasks = [
        {
            "id": "analyze_117",
            "type": SUB_AGENT_TASK_TYPE,
            "agent": "pr_status_analyst",
            "task": "Analyze PR 117.",
            "depends_on": [],
        },
        {
            "id": "analyze_118",
            "type": SUB_AGENT_TASK_TYPE,
            "agent": "pr_status_analyst",
            "task": "Analyze PR 118.",
            "depends_on": [],
        },
        {
            "id": "report",
            "type": SUB_AGENT_TASK_TYPE,
            "agent": "report_writer",
            "task": (
                "Reduce 117=${analyze_117.result.text}; "
                "118=${analyze_118.result.text}."
            ),
            "depends_on": ["analyze_117", "analyze_118"],
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert name == engine.SUB_AGENT_ACTIVITY_NAME
        if payload["id"].startswith("analyze"):
            return {
                "id": payload["id"],
                "result": {
                    "agent": payload["agent"],
                    "text": f"summary-{payload['id']}",
                },
            }
        assert payload["task"] == (
            "Reduce 117=summary-analyze_117; 118=summary-analyze_118."
        )
        return {
            "id": "report",
            "result": {"agent": "report_writer", "text": "<html>report</html>"},
        }

    context = _FakeOrchestrationContext(tasks, result_for)
    orchestrator = _registered_function(
        engine.ORCHESTRATOR_NAME,
        catalog=_catalog("pr_status_analyst", "report_writer"),
    )

    result = _run_orchestrator(orchestrator, context)

    assert result["results"]["report"] == {
        "agent": "report_writer",
        "text": "<html>report</html>",
    }
    assert [payload["id"] for _, payload in context.calls[:2]] == [
        "analyze_117",
        "analyze_118",
    ]
    assert all(
        payload["workflow_id"] == "workflow-parent"
        for _, payload in context.calls
    )
    assert all(
        payload["workflow_agent_slug"] == "coordinator"
        for _, payload in context.calls
    )
    assert context.statuses == [
        "0/3 tasks done, running=analyze_117,analyze_118",
        "2/3 tasks done, next=report",
        "2/3 tasks done, running=report",
        "3/3 tasks done",
    ]


def test_orchestrator_threads_workflow_agent_slug_to_tool_activity() -> None:
    tasks = [
        {
            "id": "publish",
            "type": TOOL_TASK_TYPE,
            "tool": "publish",
            "args": {},
            "depends_on": [],
        }
    ]
    context = _FakeOrchestrationContext(
        tasks,
        lambda name, payload: {"id": payload["id"], "result": {"ok": True}},
    )
    orchestrator = _registered_function(engine.ORCHESTRATOR_NAME)

    _run_orchestrator(orchestrator, context)

    assert context.calls == [
        (
            "agents_workflow_run_tool",
            {
                "id": "publish",
                "tool": "publish",
                "args": {},
                "workflow_agent_slug": "coordinator",
                "workflow_id": "workflow-parent",
            },
        )
    ]


def test_tool_activity_reauthorizes_current_agent_policy() -> None:
    handler_catalog = integration.build_workflow_handler_catalog(
        [WorkflowTool("publish", "Publish", lambda args: {"published": args})]
    )
    allowed = _registered_function(
        "agents_workflow_run_tool",
        handler_catalog=handler_catalog,
        workflow_agent_policies={
            "workflow-agent": WorkflowPlanPolicy(
                allowed_tools=frozenset({"publish"}),
                allowed_subagents=frozenset(),
            )
        },
    )
    revoked = _registered_function(
        "agents_workflow_run_tool",
        handler_catalog=handler_catalog,
        workflow_agent_policies={
            "workflow-agent": WorkflowPlanPolicy(
                allowed_tools=frozenset(),
                allowed_subagents=frozenset(),
            )
        },
    )
    payload = {
        "id": "publish",
        "tool": "publish",
        "args": {"value": 1},
        "workflow_agent_slug": "workflow-agent",
        "workflow_id": "workflow-1",
    }

    assert allowed(payload) == {
        "id": "publish",
        "result": {"published": {"value": 1}},
    }
    with pytest.raises(RuntimeError, match="not authorized"):
        revoked(payload)


@pytest.mark.parametrize("workflow_agent_policies", [None, {}])
def test_tool_activity_missing_agent_policy_fails_closed(
    workflow_agent_policies,
) -> None:
    handler_catalog = integration.build_workflow_handler_catalog(
        [WorkflowTool("publish", "Publish", lambda args: args)]
    )
    activity = _registered_function(
        "agents_workflow_run_tool",
        handler_catalog=handler_catalog,
        workflow_agent_policies=workflow_agent_policies,
    )

    with pytest.raises(RuntimeError, match="agent policy"):
        activity(
            {
                "id": "publish",
                "tool": "publish",
                "args": {},
                "workflow_agent_slug": "missing",
                "workflow_id": "workflow-1",
            }
        )
