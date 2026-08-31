from __future__ import annotations

import asyncio
import importlib.util
import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from durabletask.task import TaskFailedError

from azure_functions_agents._function_tool import WorkflowTool
from azure_functions_agents.config.schema import (
    BuiltinEndpointsConfig,
    ResolvedAgent,
    ToolsFilter,
)
from azure_functions_agents.discovery.tools import discover_project_tools
from azure_functions_agents.registration.capabilities import AgentCapabilities
from azure_functions_agents.registration.catalog import CatalogEntry, build_catalog
from azure_functions_agents.workflows import engine, integration, policy
from azure_functions_agents.workflows.context import (
    _workflow_task_idempotency_key,
    current_workflow_task_context,
)
from azure_functions_agents.workflows.native_retry import DurableRetryableActivityError
from azure_functions_agents.workflows.schema import (
    MAX_NODES,
    MAX_PARALLELISM,
    SUB_AGENT_TASK_TYPE,
    TOOL_TASK_TYPE,
    WAIT_TASK_TYPE,
    WorkflowPlanPolicy,
    WorkflowRetryableError,
    WorkflowTerminalError,
    resolve_workflow_task_execution,
    validate_plan,
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


def _policy_activity_payload(
    *,
    tool: str = "run",
    timeout_ms: int = 1_000,
) -> dict[str, Any]:
    workflow_id = "workflow-1"
    node_instance_id = "logical[0]"
    return {
        "id": node_instance_id,
        "task_id": "logical",
        "node_instance_id": node_instance_id,
        "tool": tool,
        "args": {"value": 1},
        "workflow_agent_slug": "coordinator",
        "workflow_id": workflow_id,
        "attempt": 1,
        "max_attempts": 1,
        "idempotency_key": _workflow_task_idempotency_key(
            workflow_id, node_instance_id
        ),
        "execution": {
            "timeout_ms": timeout_ms,
            "max_attempts": 1,
            "retry_delays_ms": [],
            "continue_on_error": False,
            "timeout_source": "task",
            "retry_source": "runtime_default",
        },
    }


def _policy_tool_activity(handler: Callable[[dict[str, Any]], Any]) -> Callable[..., Any]:
    return _registered_function(
        engine._ACTIVITY_NAME,
        handler_catalog=integration.build_workflow_handler_catalog(
            [WorkflowTool("run", "Run", handler)]
        ),
        workflow_agent_policies={
            "coordinator": WorkflowPlanPolicy(
                allowed_tools=frozenset({"run"}),
            )
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_kind", ["sync", "async", "sync_awaitable"])
async def test_policy_tool_invokes_once_with_context_and_thread_propagation(
    handler_kind: str,
) -> None:
    calls = 0
    seen = []
    activity_thread = threading.get_ident()

    async def finish() -> dict[str, bool]:
        seen.append(current_workflow_task_context())
        return {"ok": True}

    async def async_handler(args: dict[str, Any]) -> dict[str, bool]:
        nonlocal calls
        calls += 1
        seen.append(current_workflow_task_context())
        return await finish()

    def sync_handler(args: dict[str, Any]) -> Any:
        nonlocal calls
        calls += 1
        context = current_workflow_task_context()
        seen.append(context)
        assert threading.get_ident() != activity_thread
        return finish() if handler_kind == "sync_awaitable" else {"ok": True}

    handler = async_handler if handler_kind == "async" else sync_handler
    result = await _policy_tool_activity(handler)(_policy_activity_payload())

    assert result == {
        "id": "logical[0]",
        "ok": True,
        "result": {"ok": True},
    }
    assert calls == 1
    assert all(context is not None for context in seen)
    assert seen[0].deadline.tzinfo is UTC
    assert current_workflow_task_context() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "kind", "retryable", "continuable"),
    [
        (
            WorkflowRetryableError("service_busy", " Try\nagain "),
            "handler_transient",
            True,
            True,
        ),
        (
            WorkflowTerminalError("invalid_record", " Bad\trecord "),
            "handler_terminal",
            False,
            True,
        ),
    ],
)
async def test_policy_tool_classifies_declared_handler_failures(
    error: Exception,
    kind: str,
    retryable: bool,
    continuable: bool,
) -> None:
    def fail(args: dict[str, Any]) -> None:
        raise error

    outcome = await _policy_tool_activity(fail)(_policy_activity_payload())

    assert outcome["failure"] == {
        "error_code": error.error_code,
        "error": error.message,
        "kind": kind,
        "retryable": retryable,
        "continuable": continuable,
    }


@pytest.mark.asyncio
async def test_policy_tool_does_not_treat_handler_timeout_error_as_deadline() -> None:
    def fail(args: dict[str, Any]) -> None:
        raise TimeoutError("provider timeout")

    outcome = await _policy_tool_activity(fail)(_policy_activity_payload())

    assert outcome["failure"] == {
        "error_code": "workflow_task_execution_unknown",
        "error": "Task execution failed.",
        "kind": "execution_unknown",
        "retryable": False,
        "continuable": True,
    }


@pytest.mark.asyncio
async def test_policy_tool_sanitizes_unknown_and_rejects_non_json(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "provider-secret"

    def fail(args: dict[str, Any]) -> None:
        raise RuntimeError(secret)

    unknown = await _policy_tool_activity(fail)(_policy_activity_payload())
    contract = await _policy_tool_activity(lambda args: {object()})(
        _policy_activity_payload()
    )

    assert unknown["failure"] == {
        "error_code": "workflow_task_execution_unknown",
        "error": "Task execution failed.",
        "kind": "execution_unknown",
        "retryable": False,
        "continuable": True,
    }
    assert secret not in unknown["failure"]["error"]
    assert secret in caplog.text
    assert contract["failure"]["kind"] == "handler_contract"
    assert contract["failure"]["continuable"] is False


@pytest.mark.asyncio
async def test_policy_tool_timeout_clears_context_and_sync_thread_exits() -> None:
    exited = threading.Event()

    def bounded(args: dict[str, Any]) -> None:
        try:
            threading.Event().wait(1.1)
        finally:
            exited.set()

    outcome = await _policy_tool_activity(bounded)(_policy_activity_payload())

    assert outcome["failure"]["error_code"] == "workflow_task_timeout"
    assert current_workflow_task_context() is None
    assert await asyncio.to_thread(exited.wait, 1.0)


@pytest.mark.asyncio
async def test_policy_async_tool_timeout_cancels_handler() -> None:
    canceled = asyncio.Event()

    async def blocked(args: dict[str, Any]) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            canceled.set()

    outcome = await _policy_tool_activity(blocked)(_policy_activity_payload())

    assert outcome["failure"]["kind"] == "timeout"
    assert canceled.is_set()
    assert current_workflow_task_context() is None


@pytest.mark.asyncio
async def test_policy_tool_cancelled_error_propagates_and_cleans_context() -> None:
    async def cancel(args: dict[str, Any]) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _policy_tool_activity(cancel)(_policy_activity_payload())
    assert current_workflow_task_context() is None


@pytest.mark.asyncio
async def test_policy_tool_reauthorizes_before_handler_invocation() -> None:
    called = False

    def handler(args: dict[str, Any]) -> None:
        nonlocal called
        called = True

    activity = _registered_function(
        engine._ACTIVITY_NAME,
        handler_catalog=integration.build_workflow_handler_catalog(
            [WorkflowTool("run", "Run", handler)]
        ),
        workflow_agent_policies={
            "coordinator": WorkflowPlanPolicy(allowed_tools=frozenset())
        },
    )

    outcome = await activity(_policy_activity_payload())
    assert outcome["failure"] == {
        "error_code": "workflow_task_authorization",
        "error": "Task target is not authorized.",
        "kind": "authorization",
        "retryable": False,
        "continuable": False,
    }
    assert called is False


@pytest.mark.asyncio
async def test_policy_retry_reauthorizes_after_backoff_revocation() -> None:
    calls = 0

    def handler(args: dict[str, Any]) -> None:
        nonlocal calls
        calls += 1
        raise WorkflowRetryableError("temporary", "Try again.")

    catalog = integration.build_workflow_handler_catalog(
        [WorkflowTool("run", "Run", handler)]
    )
    allowed = _registered_function(
        engine._ACTIVITY_NAME,
        handler_catalog=catalog,
        workflow_agent_policies={
            "coordinator": WorkflowPlanPolicy(allowed_tools=frozenset({"run"}))
        },
    )
    revoked = _registered_function(
        engine._ACTIVITY_NAME,
        handler_catalog=catalog,
        workflow_agent_policies={
            "coordinator": WorkflowPlanPolicy(allowed_tools=frozenset())
        },
    )
    first = _policy_activity_payload()
    first["max_attempts"] = 2
    first["execution"]["max_attempts"] = 2
    first["execution"]["retry_delays_ms"] = [1_000]
    second = {**first, "attempt": 2}

    first_outcome = await allowed(first)
    second_outcome = await revoked(second)

    assert first_outcome["failure"]["retryable"] is True
    assert second_outcome["failure"] == {
        "error_code": "workflow_task_authorization",
        "error": "Task target is not authorized.",
        "kind": "authorization",
        "retryable": False,
        "continuable": False,
    }
    assert calls == 1


@pytest.mark.asyncio
async def test_redelivered_attempt_reuses_attempt_and_idempotency_key() -> None:
    observed: list[tuple[int, str]] = []

    def handler(args: dict[str, Any]) -> dict[str, bool]:
        context = current_workflow_task_context()
        assert context is not None
        observed.append((context.attempt, context.idempotency_key))
        return {"ok": True}

    activity = _policy_tool_activity(handler)
    payload = _policy_activity_payload()

    assert (await activity(payload))["ok"] is True
    assert (await activity(payload))["ok"] is True
    assert observed == [
        (payload["attempt"], payload["idempotency_key"]),
        (payload["attempt"], payload["idempotency_key"]),
    ]


@pytest.mark.asyncio
async def test_policy_tool_rejects_malformed_effective_input() -> None:
    payload = _policy_activity_payload()
    payload["attempt"] = True

    outcome = await _policy_tool_activity(lambda args: args)(payload)
    assert outcome["failure"]["kind"] == "handler_contract"
    assert outcome["failure"]["retryable"] is False
    assert outcome["failure"]["continuable"] is False

    payload = _policy_activity_payload()
    payload["max_attempts"] = True
    outcome = await _policy_tool_activity(lambda args: args)(payload)
    assert outcome["failure"]["kind"] == "handler_contract"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "missing_execution_field",
        "extra_execution_field",
        "retry_delay_count",
        "retry_delay_bool",
        "retry_delay_range",
        "elapsed_limit",
        "timeout_source",
        "retry_source",
        "activity_id",
        "empty_task_id",
        "attempt_range",
        "maximum_attempts",
        "idempotency_key",
    ],
)
async def test_policy_tool_rejects_every_malformed_boundary_invariant(case: str) -> None:
    payload = _policy_activity_payload()
    execution = payload["execution"]
    if case == "missing_execution_field":
        execution.pop("retry_source")
    elif case == "extra_execution_field":
        execution["unknown"] = True
    elif case == "retry_delay_count":
        execution["retry_delays_ms"] = [100]
    elif case == "retry_delay_bool":
        execution["max_attempts"] = 2
        payload["max_attempts"] = 2
        execution["retry_delays_ms"] = [True]
    elif case == "retry_delay_range":
        execution["max_attempts"] = 2
        payload["max_attempts"] = 2
        execution["retry_delays_ms"] = [900_001]
    elif case == "elapsed_limit":
        execution["timeout_ms"] = 600_000
        execution["max_attempts"] = 5
        payload["max_attempts"] = 5
        execution["retry_delays_ms"] = [200_000] * 4
    elif case == "timeout_source":
        execution["timeout_source"] = "forged"
    elif case == "retry_source":
        execution["retry_source"] = "forged"
    elif case == "activity_id":
        payload["id"] = "another-node"
    elif case == "empty_task_id":
        payload["task_id"] = ""
    elif case == "attempt_range":
        payload["attempt"] = 2
    elif case == "maximum_attempts":
        payload["max_attempts"] = 2
    elif case == "idempotency_key":
        payload["idempotency_key"] = "forged"

    outcome = await _policy_tool_activity(lambda args: args)(payload)

    assert outcome["failure"] == {
        "error_code": "workflow_task_handler_contract",
        "error": "Task Activity returned an invalid outcome.",
        "kind": "handler_contract",
        "retryable": False,
        "continuable": False,
    }


@pytest.mark.asyncio
async def test_policy_boundary_validation_does_not_log_input_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = _policy_activity_payload()
    payload["id"] = "another-node"
    payload["args"] = {"api_key": "SUPER_SECRET"}

    outcome = await _policy_tool_activity(lambda args: args)(payload)

    assert outcome["failure"]["kind"] == "handler_contract"
    assert "SUPER_SECRET" not in caplog.text
    assert "api_key" not in caplog.text


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


@pytest.mark.asyncio
async def test_policy_sub_agent_uses_effective_timeout_and_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[float, Any]] = []

    async def run_leaf(*args: Any, timeout: float, **kwargs: Any) -> str:
        seen.append((timeout, current_workflow_task_context()))
        return "done"

    monkeypatch.setattr(engine, "run_leaf_agent_task", run_leaf)
    activity = _registered_function(
        engine.SUB_AGENT_ACTIVITY_NAME,
        catalog=_catalog("specialist"),
        workflow_agent_policies={
            "coordinator": WorkflowPlanPolicy(
                allowed_tools=frozenset(),
                allowed_subagents=frozenset({"specialist"}),
            )
        },
    )
    payload = _policy_activity_payload(timeout_ms=2_000)
    payload.pop("tool")
    payload.pop("args")
    payload.update({"agent": "specialist", "task": "work"})

    assert await activity(payload) == {
        "id": "logical[0]",
        "ok": True,
        "result": {"agent": "specialist", "text": "done"},
    }
    assert seen[0][0] == 2.0
    assert seen[0][1].task_id == "logical"
    assert current_workflow_task_context() is None


@pytest.mark.asyncio
async def test_policy_sub_agent_classifies_unknown_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("private provider response")

    monkeypatch.setattr(engine, "run_leaf_agent_task", fail)
    activity = _registered_function(
        engine.SUB_AGENT_ACTIVITY_NAME,
        catalog=_catalog("specialist"),
        workflow_agent_policies={
            "coordinator": WorkflowPlanPolicy(
                allowed_tools=frozenset(),
                allowed_subagents=frozenset({"specialist"}),
            )
        },
    )
    payload = _policy_activity_payload()
    payload.pop("tool")
    payload.pop("args")
    payload.update({"agent": "specialist", "task": "work"})

    outcome = await activity(payload)
    assert outcome["failure"]["kind"] == "execution_unknown"
    assert outcome["failure"]["error"] == "Task execution failed."


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
        result_for: Callable[[str, dict[str, Any]], Any],
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
            selected = context.last_wave
            selected.is_completed = True
            if isinstance(context, _DynamicContext) and selected in context.timers:
                deadline = context.timer_deadlines[context.timers.index(selected)]
                context._now = max(context._now, deadline)
            generator.send(selected)
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


@pytest.mark.asyncio
async def test_tool_activity_reauthorizes_current_agent_policy() -> None:
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

    assert await allowed(payload) == {
        "id": "publish",
        "result": {"published": {"value": 1}},
    }
    with pytest.raises(RuntimeError, match="not authorized"):
        await revoked(payload)


@pytest.mark.parametrize("workflow_agent_policies", [None, {}])
@pytest.mark.asyncio
async def test_tool_activity_missing_agent_policy_fails_closed(
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
        await activity(
            {
                "id": "publish",
                "tool": "publish",
                "args": {},
                "workflow_agent_slug": "missing",
                "workflow_id": "workflow-1",
            }
        )


# ---------------------------------------------------------------------------
# Dynamic (data-driven) orchestration — Issue #1276.
# ---------------------------------------------------------------------------


class _DynamicContext(_FakeOrchestrationContext):
    """Fake context that also supports timers, a clock, and a persisted policy."""

    def __init__(
        self,
        tasks: list[dict[str, Any]],
        result_for: Callable[[str, dict[str, Any]], Any],
        *,
        policy: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        super().__init__(tasks, result_for)
        self._input["policy"] = policy or {}
        self._now = now or datetime(2024, 1, 1, tzinfo=UTC)
        self.timers: list[_Task] = []
        self.timer_deadlines: list[datetime] = []
        self.retry_policies: list[Any] = []

    @property
    def current_utc_datetime(self) -> datetime:
        return self._now

    def create_timer(self, deadline: datetime) -> _Task:
        timer = _Task()
        timer.is_completed = False
        self.timers.append(timer)
        self.timer_deadlines.append(deadline)
        return timer

    def call_activity_with_retry(
        self,
        name: str,
        retry_policy: Any,
        payload: dict[str, Any],
    ) -> _Task:
        self.retry_policies.append(retry_policy)
        return self.call_activity(name, payload)

    def task_any(self, tasks: list[_Task]) -> _Task:
        candidates = [task for task in tasks if task is not self.cancel_task]
        selected = next((task for task in candidates if task.is_completed), None)
        if selected is None:
            timers = [
                (self.timer_deadlines[self.timers.index(task)], task)
                for task in candidates
                if task in self.timers
            ]
            if timers:
                _, selected = min(timers, key=lambda pair: pair[0])
            elif candidates:
                selected = candidates[0]
            else:
                selected = self.cancel_task
        self.last_wave = selected
        return _Task()


def _run_dynamic(
    tasks: list[dict[str, Any]],
    *,
    policy: dict[str, Any],
    result_for: Callable[[str, dict[str, Any]], Any],
    now: datetime | None = None,
) -> tuple[dict[str, Any], _DynamicContext]:
    context = _DynamicContext(tasks, result_for, policy=policy, now=now)
    orchestrator = _registered_function(engine.ORCHESTRATOR_NAME)
    result = _run_orchestrator(orchestrator, context)
    return result, context


def _activity_ids(context: _FakeOrchestrationContext, name: str) -> list[str]:
    return [payload["id"] for called, payload in context.calls if called == name]


def test_dynamic_dispatch_rejects_unsupported_persisted_task_type() -> None:
    tasks = [
        {
            "id": "src",
            "type": TOOL_TASK_TYPE,
            "tool": "collect",
            "args": {},
            "depends_on": [],
        },
        {
            "id": "invalid",
            "type": "unsupported",
            "depends_on": ["src"],
            "when": {
                "ref": "${src.result.run}",
                "operator": "equals",
                "value": True,
            },
        },
    ]

    with pytest.raises(RuntimeError, match="unsupported task type 'unsupported'"):
        _run_dynamic(
            tasks,
            policy={"allowed_tools": ["collect"], "allowed_subagents": []},
            result_for=lambda _name, payload: {
                "id": payload["id"],
                "result": {"run": True},
            },
        )


def test_dynamic_activity_failure_cancels_pending_wave_timer() -> None:
    class _FailedSecondWaveContext(_DynamicContext):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.activity_count = 0

        def call_activity(self, name: str, payload: dict[str, Any]) -> _Task:
            self.activity_count += 1
            if self.activity_count == 2:
                self.calls.append((name, payload))
                return _Task(RuntimeError("dynamic activity failed"))
            return super().call_activity(name, payload)

    tasks = [
        {
            "id": "src",
            "type": TOOL_TASK_TYPE,
            "tool": "collect",
            "args": {},
            "depends_on": [],
        },
        {
            "id": "act",
            "type": TOOL_TASK_TYPE,
            "tool": "inspect",
            "args": {},
            "depends_on": ["src"],
            "when": {
                "ref": "${src.result.run}",
                "operator": "equals",
                "value": True,
            },
        },
        {
            "id": "pause",
            "type": WAIT_TASK_TYPE,
            "duration": "PT1S",
            "depends_on": ["src"],
        },
    ]
    context = _FailedSecondWaveContext(
        tasks,
        lambda _name, payload: {
            "id": payload["id"],
            "result": {"run": True},
        },
        policy={
            "allowed_tools": ["collect", "inspect"],
            "allowed_subagents": [],
        },
    )
    orchestrator = _registered_function(engine.ORCHESTRATOR_NAME)

    with pytest.raises(RuntimeError, match="dynamic activity failed"):
        _run_orchestrator(orchestrator, context)

    assert len(context.timers) == 1
    assert context.timers[0].is_completed is True


# --- Static-path preservation ---------------------------------------------


def test_plan_is_dynamic_detection() -> None:
    static = [{"id": "a", "type": TOOL_TASK_TYPE, "tool": "t", "depends_on": []}]
    with_when = [
        {
            "id": "a",
            "type": TOOL_TASK_TYPE,
            "tool": "t",
            "depends_on": [],
            "when": {"ref": "${b.result.x}", "operator": "equals", "value": 1},
        }
    ]
    with_for_each = [
        {
            "id": "a",
            "type": TOOL_TASK_TYPE,
            "tool": "t",
            "depends_on": [],
            "for_each": "${b.result.items}",
        }
    ]
    assert engine._plan_is_dynamic(static) is False
    assert engine._plan_is_dynamic(with_when) is True
    assert engine._plan_is_dynamic(with_for_each) is True


def test_static_plan_keeps_exact_string_custom_status() -> None:
    tasks = [
        {"id": "a", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "b",
            "type": TOOL_TASK_TYPE,
            "tool": "collect",
            "args": {},
            "depends_on": ["a"],
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"id": payload["id"], "result": {"ok": payload["id"]}}

    context = _FakeOrchestrationContext(tasks, result_for)
    orchestrator = _registered_function(engine.ORCHESTRATOR_NAME)
    result = _run_orchestrator(orchestrator, context)

    assert result == {"results": {"a": {"ok": "a"}, "b": {"ok": "b"}}}
    # Static path publishes plain strings, never structured dict snapshots.
    assert all(isinstance(status, str) for status in context.statuses)
    assert context.statuses == [
        "0/2 tasks done, running=a",
        "1/2 tasks done, next=b",
        "1/2 tasks done, running=b",
        "2/2 tasks done",
    ]


# --- Conditions ------------------------------------------------------------


def test_condition_true_resolves_args_and_runs() -> None:
    tasks = [
        {"id": "src", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "act",
            "type": TOOL_TASK_TYPE,
            "tool": "noop",
            "args": {"echoed": "${src.result.val}"},
            "depends_on": ["src"],
            "when": {"ref": "${src.result.flag}", "operator": "equals", "value": True},
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["id"] == "src":
            return {"id": "src", "result": {"flag": True, "val": "hi"}}
        return {"id": payload["id"], "result": {"ok": True}}

    result, context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "noop"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert result["results"]["act"] == {"ok": True}
    act_call = next(p for _, p in context.calls if p["id"] == "act")
    assert act_call["args"] == {"echoed": "hi"}


def test_condition_false_skips_before_resolving_args() -> None:
    # ``act`` references a missing path in its args; if the predicate were
    # evaluated after args (or not at all) this plan would fail. Predicate
    # runs first, the task is skipped, and the bad args are never resolved.
    tasks = [
        {"id": "src", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "act",
            "type": TOOL_TASK_TYPE,
            "tool": "noop",
            "args": {"x": "${src.result.MISSING}"},
            "depends_on": ["src"],
            "when": {"ref": "${src.result.flag}", "operator": "equals", "value": True},
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"id": "src", "result": {"flag": False}}

    result, context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "noop"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert result["results"]["act"] is None
    assert "noop" not in [p.get("tool") for _, p in context.calls]
    assert result.get("failed") is None
    final = context.statuses[-1]
    assert final["nodes"]["act"]["state"] == "skipped"


def test_normal_skip_unlocks_descendants() -> None:
    tasks = [
        {"id": "src", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "b",
            "type": TOOL_TASK_TYPE,
            "tool": "noop",
            "args": {},
            "depends_on": ["src"],
            "when": {"ref": "${src.result.flag}", "operator": "equals", "value": True},
        },
        {
            "id": "c",
            "type": TOOL_TASK_TYPE,
            "tool": "finish",
            "args": {},
            "depends_on": ["b"],
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["id"] == "src":
            return {"id": "src", "result": {"flag": False}}
        return {"id": payload["id"], "result": {"done": payload["id"]}}

    result, context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "noop", "finish"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert result["results"]["b"] is None
    assert result["results"]["c"] == {"done": "c"}
    # A skipped node satisfies the dependency without dispatching an activity.
    assert "noop" not in [p.get("tool") for _, p in context.calls]


def test_full_reference_to_skipped_result_resolves_to_null() -> None:
    tasks = [
        {"id": "src", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "skipped",
            "type": TOOL_TASK_TYPE,
            "tool": "noop",
            "args": {},
            "depends_on": ["src"],
            "when": {"ref": "${src.result.run}", "operator": "equals", "value": True},
        },
        {
            "id": "sink",
            "type": TOOL_TASK_TYPE,
            "tool": "finish",
            "args": {"value": "${skipped.result}"},
            "depends_on": ["skipped"],
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["id"] == "src":
            return {"id": "src", "result": {"run": False}}
        return {
            "id": payload["id"],
            "result": {"seen": payload["args"]["value"]},
        }

    result, context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "noop", "finish"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert result["results"]["skipped"] is None
    assert result["results"]["sink"] == {"seen": None}
    sink_call = next(payload for _, payload in context.calls if payload["id"] == "sink")
    assert sink_call["args"] == {"value": None}


def test_dotted_reference_below_skipped_result_is_controlled_failure() -> None:
    tasks = [
        {"id": "src", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "skipped",
            "type": TOOL_TASK_TYPE,
            "tool": "noop",
            "args": {},
            "depends_on": ["src"],
            "when": {"ref": "${src.result.run}", "operator": "equals", "value": True},
        },
        {
            "id": "sink",
            "type": TOOL_TASK_TYPE,
            "tool": "finish",
            "args": {"value": "${skipped.result.field}"},
            "depends_on": ["skipped"],
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"id": "src", "result": {"run": False}}

    result, context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "noop", "finish"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert result["failed"] is True
    assert result["error_code"] == "workflow_reference_unresolved"
    assert result["node_id"] == "sink"
    assert result["path"] is None
    assert result["results"] == {"src": {"run": False}, "skipped": None}
    assert context.statuses[-1]["nodes"]["sink"]["state"] == "failed"
    assert "finish" not in [payload.get("tool") for _, payload in context.calls]


def test_non_scalar_condition_is_condition_invalid() -> None:
    tasks = [
        {"id": "disc", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "act",
            "type": TOOL_TASK_TYPE,
            "tool": "noop",
            "args": {},
            "depends_on": ["disc"],
            "when": {"ref": "${disc.result.obj}", "operator": "equals", "value": "x"},
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"id": "disc", "result": {"obj": {"a": 1}}}

    result, _ = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "noop"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert result["failed"] is True
    assert result["error_code"] == "workflow_condition_invalid"
    assert result["node_id"] == "act"


# --- for_each expansion ----------------------------------------------------


def test_expanded_mixed_run_skip_aggregate_source_order() -> None:
    tasks = [
        {"id": "disc", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "analyze",
            "type": TOOL_TASK_TYPE,
            "tool": "at",
            "args": {"i": "${index}"},
            "depends_on": ["disc"],
            "for_each": "${disc.result.items}",
            "when": {"ref": "${item.open}", "operator": "equals", "value": True},
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["id"] == "disc":
            return {
                "id": "disc",
                "result": {"items": [{"open": True}, {"open": False}, {"open": True}]},
            }
        return {"id": payload["id"], "result": {"idx": payload["args"]["i"]}}

    result, context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "at"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert result["results"]["analyze"] == [
        {"index": 0, "status": "completed", "result": {"idx": 0}},
        {"index": 1, "status": "skipped", "result": None},
        {"index": 2, "status": "completed", "result": {"idx": 2}},
    ]
    # The skipped element (index 1) never dispatches an activity.
    assert _activity_ids(context, engine._ACTIVITY_NAME) == [
        "disc",
        "analyze[0]",
        "analyze[2]",
    ]


def test_empty_expansion_aggregates_immediately() -> None:
    tasks = [
        {"id": "disc", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "analyze",
            "type": TOOL_TASK_TYPE,
            "tool": "at",
            "args": {},
            "depends_on": ["disc"],
            "for_each": "${disc.result.items}",
        },
        {
            "id": "sink",
            "type": TOOL_TASK_TYPE,
            "tool": "noop",
            "args": {"all": "${analyze.result}"},
            "depends_on": ["analyze"],
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["id"] == "disc":
            return {"id": "disc", "result": {"items": []}}
        return {"id": payload["id"], "result": {"ok": True}}

    result, context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "at", "noop"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert result["results"]["analyze"] == []
    assert "at" not in [p.get("tool") for _, p in context.calls]
    sink_call = next(p for _, p in context.calls if p["id"] == "sink")
    assert sink_call["args"] == {"all": []}


def test_all_skipped_expansion_aggregates_without_dispatch() -> None:
    tasks = [
        {"id": "disc", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "analyze",
            "type": TOOL_TASK_TYPE,
            "tool": "at",
            "args": {"i": "${index}"},
            "depends_on": ["disc"],
            "for_each": "${disc.result.items}",
            "when": {"ref": "${item.open}", "operator": "equals", "value": True},
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": "disc",
            "result": {"items": [{"open": False}, {"open": False}]},
        }

    result, context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "at"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert result["results"]["analyze"] == [
        {"index": 0, "status": "skipped", "result": None},
        {"index": 1, "status": "skipped", "result": None},
    ]
    assert _activity_ids(context, engine._ACTIVITY_NAME) == ["disc"]
    assert context.statuses[-1]["nodes"]["analyze"]["state"] == "aggregated"


def test_numeric_scheduling_under_parallel_cap() -> None:
    count = 12
    tasks = [
        {"id": "disc", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "analyze",
            "type": TOOL_TASK_TYPE,
            "tool": "at",
            "args": {"i": "${index}"},
            "depends_on": ["disc"],
            "for_each": "${disc.result.items}",
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["id"] == "disc":
            return {"id": "disc", "result": {"items": [{} for _ in range(count)]}}
        return {"id": payload["id"], "result": {"idx": payload["args"]["i"]}}

    result, context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "at"], "allowed_subagents": []},
        result_for=result_for,
    )

    # Numeric (not lexical) order: analyze[10] follows analyze[2], not analyze[1].
    assert _activity_ids(context, engine._ACTIVITY_NAME) == [
        "disc",
        *[f"analyze[{i}]" for i in range(count)],
    ]
    assert len(result["results"]["analyze"]) == count
    # Parallelism cap: a wave never runs more than MAX_PARALLELISM instances,
    # and the first analyze wave saturates the cap (proving a second wave ran).
    running_peaks = [
        s["counts"]["running"] for s in context.statuses if isinstance(s, dict)
    ]
    assert max(running_peaks) <= MAX_PARALLELISM
    assert MAX_PARALLELISM in running_peaks


def test_ready_normal_node_waiting_for_parallel_slot_stays_pending() -> None:
    tasks = [
        {
            "id": "discover",
            "type": TOOL_TASK_TYPE,
            "tool": "collect",
            "args": {},
            "depends_on": [],
        },
        *[
            {
                "id": f"task{i:02}",
                "type": TOOL_TASK_TYPE,
                "tool": "inspect",
                "args": {"index": i},
                "depends_on": ["discover"],
                "when": {
                    "ref": "${discover.result.run}",
                    "operator": "equals",
                    "value": True,
                },
            }
            for i in range(MAX_PARALLELISM + 1)
        ],
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["id"] == "discover":
            return {"id": "discover", "result": {"run": True}}
        return {"id": payload["id"], "result": payload["args"]}

    _, context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "inspect"], "allowed_subagents": []},
        result_for=result_for,
    )

    first_child_wave = next(
        status
        for status in context.statuses
        if status["nodes"]["task00"]["state"] == "running"
    )
    assert first_child_wave["nodes"][f"task{MAX_PARALLELISM:02}"]["state"] == "pending"


def test_incident_sample_plan_runs_through_dynamic_scheduler() -> None:
    sample_src = (
        Path(__file__).resolve().parents[1]
        / "samples"
        / "workflow-incident-triage"
        / "src"
    )
    spec = importlib.util.spec_from_file_location(
        "incident_tools_scheduler_test",
        sample_src / "tools" / "incident_tools.py",
    )
    assert spec is not None and spec.loader is not None
    incident_tools = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(incident_tools)

    incident = "Something is degrading across our platform."
    raw_plan = {
        "version": 1,
        "tasks": [
            {
                "id": "discover",
                "type": TOOL_TASK_TYPE,
                "tool": "discover_services",
                "args": {"incident": incident},
                "depends_on": [],
            },
            {
                "id": "inspect",
                "type": TOOL_TASK_TYPE,
                "tool": "inspect_service",
                "args": {"service": "${item.name}", "index": "${index}"},
                "depends_on": ["discover"],
                "for_each": "${discover.result.services}",
                "when": {
                    "ref": "${item.in_scope}",
                    "operator": "equals",
                    "value": True,
                },
            },
            {
                "id": "summarize",
                "type": TOOL_TASK_TYPE,
                "tool": "summarize_scan",
                "args": {
                    "incident": incident,
                    "findings": "${inspect.result}",
                },
                "depends_on": ["inspect"],
            },
        ],
    }
    allowed_tools = frozenset({
        "discover_services",
        "inspect_service",
        "summarize_scan",
    })
    policy = WorkflowPlanPolicy(
        allowed_tools=allowed_tools,
        allowed_subagents=frozenset(),
    )
    plan = validate_plan(raw_plan, policy=policy)
    handlers = {
        name: getattr(incident_tools, name)
        for name in allowed_tools
    }

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = handlers[payload["tool"]](payload["args"])
        return {"id": payload["id"], "result": result}

    result, context = _run_dynamic(
        [task.model_dump(mode="json") for task in plan.tasks],
        policy={
            "allowed_tools": sorted(allowed_tools),
            "allowed_subagents": [],
        },
        result_for=result_for,
    )

    aggregate = result["results"]["inspect"]
    assert [entry["index"] for entry in aggregate] == list(range(len(aggregate)))
    assert any(entry["status"] == "skipped" for entry in aggregate)
    assert all(
        entry["result"] is None
        for entry in aggregate
        if entry["status"] == "skipped"
    )
    summary = result["results"]["summarize"]
    completed_count = sum(
        entry["status"] == "completed" for entry in aggregate
    )
    skipped_count = sum(
        entry["status"] == "skipped" for entry in aggregate
    )
    assert summary["scanned"] == completed_count
    assert summary["skipped"] == skipped_count
    assert _activity_ids(context, engine._ACTIVITY_NAME)[0] == "discover"
    assert _activity_ids(context, engine._ACTIVITY_NAME)[-1] == "summarize"

    final_status = context.statuses[-1]
    assert final_status["counts"] == {
        "logical_total": 3,
        "materialized_total": len(aggregate) + 2,
        "completed": completed_count + 2,
        "skipped": skipped_count,
        "running": 0,
    }
    assert final_status["nodes"]["discover"] == {"state": "completed"}
    assert final_status["nodes"]["inspect"] == {
        "state": "aggregated",
        "expanded_count": len(aggregate),
        "instances": {
            f"inspect[{entry['index']}]": {"state": entry["status"]}
            for entry in aggregate
        },
    }
    assert final_status["nodes"]["summarize"] == {"state": "completed"}


def test_retry_policy_e2e_plan_exercises_decorator_precedence() -> None:
    sample_src = (
        Path(__file__).resolve().parent
        / "endtoend"
        / "apps"
        / "workflow-retry-policy"
    )
    raw_plan = json.loads(
        (
            sample_src
            / "skills"
            / "retry-policy-e2e"
            / "references"
            / "order-recovery-plan.json"
        ).read_text(encoding="utf-8")
    )
    discovered = discover_project_tools(sample_src)
    tools_by_name = {tool.name: tool for tool in discovered.workflow_tools}
    allowed_tools = frozenset(task["tool"] for task in raw_plan["tasks"])
    policy = WorkflowPlanPolicy(
        allowed_tools=allowed_tools,
        allowed_subagents=frozenset(),
    )
    plan = validate_plan(raw_plan, policy=policy)
    persisted: list[dict[str, Any]] = []
    for task in plan.tasks:
        dumped = task.model_dump(mode="json")
        declaration = tools_by_name[task.tool]
        execution = resolve_workflow_task_execution(
            task,
            decorator_timeout=declaration.timeout,
            decorator_retry=declaration.retry,
        )
        if execution is not None:
            dumped["execution"] = execution
        persisted.append(dumped)

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        task_id = payload["id"]
        tool_name = payload["tool"]
        if tool_name == "reserve_inventory":
            return {
                "id": task_id,
                "ok": True,
                "result": {
                    "order_id": "ORD-1001",
                    "sku": "trail-shoes-blue-42",
                    "reserved": True,
                    "transient_failures_observed": 2,
                },
            }
        handler = tools_by_name[tool_name].handler
        assert handler is not None
        return {"id": task_id, "result": handler(payload["args"])}

    result, context = _run_dynamic(
        persisted,
        policy={
            "allowed_tools": sorted(allowed_tools),
            "allowed_subagents": [],
        },
        result_for=result_for,
    )

    retry_calls = [
        payload
        for _, payload in context.calls
        if payload["id"] == "reserve_inventory"
    ]
    assert len(retry_calls) == 1
    assert "attempt" not in retry_calls[0]
    assert all(payload["max_attempts"] == 3 for payload in retry_calls)
    assert len({payload["idempotency_key"] for payload in retry_calls}) == 1
    assert len(context.retry_policies) == 1
    assert context.retry_policies[0].max_number_of_attempts == 3
    assert result["results"]["reserve_inventory"]["reserved"] is True
    assert result["results"]["confirm_order"] == {
        "order_id": "ORD-1001",
        "status": "confirmed",
        "transient_failures_observed": 2,
    }
    assert context.statuses[-1]["schema_version"] == 4
    assert context.statuses[-1]["retry_driver"] == "durable"


def test_multiple_expansions_run_in_sorted_logical_id_order() -> None:
    tasks = [
        {"id": "disc", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "grp_a",
            "type": TOOL_TASK_TYPE,
            "tool": "at",
            "args": {},
            "depends_on": ["disc"],
            "for_each": "${disc.result.a}",
        },
        {
            "id": "grp_b",
            "type": TOOL_TASK_TYPE,
            "tool": "at",
            "args": {},
            "depends_on": ["disc"],
            "for_each": "${disc.result.b}",
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["id"] == "disc":
            return {"id": "disc", "result": {"a": [{}], "b": [{}]}}
        return {"id": payload["id"], "result": {"ok": payload["id"]}}

    _, context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "at"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert _activity_ids(context, engine._ACTIVITY_NAME) == [
        "disc",
        "grp_a[0]",
        "grp_b[0]",
    ]


def test_node_limit_atomic_rejection_counts_skipped_items() -> None:
    # 50 elements + 1 reserved non-for_each node (disc) exceeds MAX_NODES.
    # Every element would be skipped by the predicate, yet the plan is
    # rejected before any instance is created — skipped items still consume
    # the budget, and the rejection is atomic (no partial materialization).
    over = MAX_NODES  # 50 elements → 1 + 50 > 50
    tasks = [
        {"id": "disc", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "analyze",
            "type": TOOL_TASK_TYPE,
            "tool": "at",
            "args": {},
            "depends_on": ["disc"],
            "for_each": "${disc.result.items}",
            "when": {"ref": "${item.open}", "operator": "equals", "value": True},
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"id": "disc", "result": {"items": [{"open": False} for _ in range(over)]}}

    result, context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "at"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert result["failed"] is True
    assert result["error_code"] == "workflow_node_limit_exceeded"
    assert result["node_id"] == "analyze"
    # Atomic: only ``disc`` ran; no analyze instances were dispatched.
    assert _activity_ids(context, engine._ACTIVITY_NAME) == ["disc"]


def test_node_limit_is_cumulative_across_expansions() -> None:
    tasks = [
        {"id": "disc", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "grp_a",
            "type": TOOL_TASK_TYPE,
            "tool": "at",
            "args": {},
            "depends_on": ["disc"],
            "for_each": "${disc.result.a}",
        },
        {
            "id": "grp_b",
            "type": TOOL_TASK_TYPE,
            "tool": "at",
            "args": {},
            "depends_on": ["disc"],
            "for_each": "${disc.result.b}",
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": "disc",
            "result": {"a": [{} for _ in range(24)], "b": [{} for _ in range(26)]},
        }

    result, context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "at"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert result["failed"] is True
    assert result["error_code"] == "workflow_node_limit_exceeded"
    assert result["node_id"] == "grp_b"
    assert result["results"] == {
        "disc": {"a": [{} for _ in range(24)], "b": [{} for _ in range(26)]}
    }
    assert _activity_ids(context, engine._ACTIVITY_NAME) == ["disc"]


def test_dynamic_replay_produces_identical_calls_statuses_and_results() -> None:
    tasks = [
        {"id": "disc", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "analyze",
            "type": TOOL_TASK_TYPE,
            "tool": "at",
            "args": {"i": "${index}"},
            "depends_on": ["disc"],
            "for_each": "${disc.result.items}",
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["id"] == "disc":
            return {"id": "disc", "result": {"items": [{}, {}, {}]}}
        return {"id": payload["id"], "result": {"idx": payload["args"]["i"]}}

    first_result, first_context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "at"], "allowed_subagents": []},
        result_for=result_for,
    )
    replay_result, replay_context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "at"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert replay_result == first_result
    assert replay_context.calls == first_context.calls
    assert replay_context.statuses == first_context.statuses


def test_for_each_non_array_is_iteration_not_array() -> None:
    tasks = [
        {"id": "disc", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "analyze",
            "type": TOOL_TASK_TYPE,
            "tool": "at",
            "args": {},
            "depends_on": ["disc"],
            "for_each": "${disc.result.items}",
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"id": "disc", "result": {"items": {"not": "a list"}}}

    result, _ = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "at"], "allowed_subagents": []},
        result_for=result_for,
    )

    # Stable, flat controlled-failure envelope shape.
    assert set(result) == {"failed", "error", "error_code", "node_id", "path", "results"}
    assert result["failed"] is True
    assert result["error_code"] == "workflow_iteration_not_array"
    assert result["node_id"] == "analyze"
    assert result["path"] == "${disc.result.items}"
    # Committed logical results are preserved through the failure.
    assert result["results"]["disc"] == {"items": {"not": "a list"}}


def test_for_each_missing_upstream_path_is_reference_unresolved() -> None:
    tasks = [
        {"id": "disc", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "analyze",
            "type": TOOL_TASK_TYPE,
            "tool": "at",
            "args": {},
            "depends_on": ["disc"],
            "for_each": "${disc.result.MISSING}",
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"id": "disc", "result": {"items": [{}]}}

    result, _ = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "at"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert result["failed"] is True
    assert result["error_code"] == "workflow_reference_unresolved"
    assert result["node_id"] == "analyze"


def test_expanded_missing_item_path_uses_instance_node_id() -> None:
    tasks = [
        {"id": "disc", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "analyze",
            "type": TOOL_TASK_TYPE,
            "tool": "at",
            "args": {},
            "depends_on": ["disc"],
            "for_each": "${disc.result.items}",
            "when": {"ref": "${item.missing}", "operator": "equals", "value": True},
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"id": "disc", "result": {"items": [{}]}}

    result, _ = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "at"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert result["failed"] is True
    assert result["error_code"] == "workflow_reference_unresolved"
    assert result["node_id"] == "analyze[0]"
    assert result["path"] == "${item.missing}"


# --- Immutable owner policy (fail closed) ----------------------------------


def test_expanded_tool_outside_policy_raises() -> None:
    tasks = [
        {"id": "disc", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "analyze",
            "type": TOOL_TASK_TYPE,
            "tool": "restricted",
            "args": {},
            "depends_on": ["disc"],
            "for_each": "${disc.result.items}",
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"id": "disc", "result": {"items": [{}]}}

    with pytest.raises(RuntimeError, match="outside the persisted workflow owner policy"):
        _run_dynamic(
            tasks,
            policy={"allowed_tools": ["collect"], "allowed_subagents": []},
            result_for=result_for,
        )


def test_expanded_sub_agent_outside_policy_raises() -> None:
    tasks = [
        {"id": "disc", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "analyze",
            "type": SUB_AGENT_TASK_TYPE,
            "agent": "unlisted",
            "task": "Analyze ${item}.",
            "depends_on": ["disc"],
            "for_each": "${disc.result.items}",
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"id": "disc", "result": {"items": ["x"]}}

    with pytest.raises(RuntimeError, match="outside the persisted workflow owner policy"):
        _run_dynamic(
            tasks,
            policy={"allowed_tools": ["collect"], "allowed_subagents": ["known"]},
            result_for=result_for,
        )


# --- Structured status snapshots -------------------------------------------


def test_dynamic_status_snapshots_track_states_and_counts() -> None:
    tasks = [
        {"id": "disc", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "analyze",
            "type": TOOL_TASK_TYPE,
            "tool": "at",
            "args": {"i": "${index}"},
            "depends_on": ["disc"],
            "for_each": "${disc.result.items}",
            "when": {"ref": "${item.open}", "operator": "equals", "value": True},
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["id"] == "disc":
            return {
                "id": "disc",
                "result": {"items": [{"open": True}, {"open": False}, {"open": True}]},
            }
        return {"id": payload["id"], "result": {"idx": payload["args"]["i"]}}

    _, context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "at"], "allowed_subagents": []},
        result_for=result_for,
    )

    snapshots = [s for s in context.statuses if isinstance(s, dict)]
    assert snapshots, "dynamic path must publish structured snapshots"
    assert all(s["schema_version"] == 2 for s in snapshots)

    analyze_states = [s["nodes"]["analyze"]["state"] for s in snapshots]
    assert "expanded" in analyze_states
    assert "running" in analyze_states

    final = snapshots[-1]
    assert final["nodes"]["analyze"]["state"] == "aggregated"
    assert final["counts"] == {
        "logical_total": 2,
        "materialized_total": 4,  # disc + 3 analyze instances (incl. skipped)
        "completed": 3,  # disc + analyze[0] + analyze[2]
        "skipped": 1,  # analyze[1]
        "running": 0,
    }


# --- Cancellation with a dynamic timer -------------------------------------


def test_dynamic_cancellation_cancels_timer_and_returns_partial() -> None:
    tasks = [
        {"id": "t1", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "w1",
            "type": WAIT_TASK_TYPE,
            "duration": "PT1H",
            "depends_on": ["t1"],
            "when": {"ref": "${t1.result.go}", "operator": "equals", "value": True},
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"id": "t1", "result": {"go": True}}

    context = _DynamicContext(
        tasks,
        result_for,
        policy={"allowed_tools": ["collect"], "allowed_subagents": []},
    )
    context.cancel_task.result = "user-request"
    orchestrator = _registered_function(engine.ORCHESTRATOR_NAME)

    gen = orchestrator(context)
    next(gen)  # yields the t1 wave
    gen.send(context.last_wave)  # completes t1, expands w1, dispatches its timer
    result: dict[str, Any] = {}
    try:
        gen.send(context.cancel_task)  # cancel while the timer is pending
    except StopIteration as stop:
        result = stop.value

    assert result["canceled"] is True
    assert result["reason"] == "user-request"
    assert result["results"]["t1"] == {"go": True}
    assert result["completed_count"] == 1
    assert result["total_count"] == 2
    # The pending durable timer was cancelled.
    assert context.timers and all(timer.cancelled for timer in context.timers)
    assert context.statuses[-1]["nodes"]["w1"]["state"] == "pending"


def test_dynamic_cancellation_preserves_completed_iteration_instances() -> None:
    count = MAX_PARALLELISM + 1
    tasks = [
        {
            "id": "discover",
            "type": TOOL_TASK_TYPE,
            "tool": "collect",
            "args": {},
            "depends_on": [],
        },
        {
            "id": "inspect",
            "type": TOOL_TASK_TYPE,
            "tool": "inspect",
            "args": {"index": "${index}"},
            "depends_on": ["discover"],
            "for_each": "${discover.result.items}",
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["id"] == "discover":
            return {
                "id": "discover",
                "result": {"items": [{} for _ in range(count)]},
            }
        return {"id": payload["id"], "result": payload["args"]}

    context = _DynamicContext(
        tasks,
        result_for,
        policy={
            "allowed_tools": ["collect", "inspect"],
            "allowed_subagents": [],
        },
    )
    context.cancel_task.result = "user-request"
    orchestrator = _registered_function(engine.ORCHESTRATOR_NAME)

    gen = orchestrator(context)
    next(gen)  # discover
    gen.send(context.last_wave)  # dispatches first inspect wave
    for _ in range(MAX_PARALLELISM):
        gen.send(context.last_wave)
    result: dict[str, Any] = {}
    try:
        gen.send(context.cancel_task)
    except StopIteration as stop:
        result = stop.value

    assert result["canceled"] is True
    assert "inspect" not in result["results"]
    inspect_status = context.statuses[-1]["nodes"]["inspect"]
    assert inspect_status["state"] == "expanded"
    assert [
        inspect_status["instances"][f"inspect[{i}]"]["state"]
        for i in range(count)
    ] == [*(["completed"] * MAX_PARALLELISM), "pending"]


def _effective_execution(
    *,
    attempts: int = 3,
    delays: list[int] | None = None,
    continue_on_error: bool = False,
) -> dict[str, Any]:
    return {
        "timeout_ms": 1_000,
        "max_attempts": attempts,
        "retry_delays_ms": delays if delays is not None else [0] * (attempts - 1),
        "continue_on_error": continue_on_error,
        "timeout_source": "task",
        "retry_source": "task",
    }


def _policy_task(
    task_id: str,
    *,
    attempts: int = 3,
    delays: list[int] | None = None,
    continue_on_error: bool = False,
    depends_on: list[str] | None = None,
    args: dict[str, Any] | None = None,
    when: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task = {
        "id": task_id,
        "type": TOOL_TASK_TYPE,
        "tool": "run",
        "args": args or {},
        "depends_on": depends_on or [],
        "execution": _effective_execution(
            attempts=attempts,
            delays=delays,
            continue_on_error=continue_on_error,
        ),
    }
    if when is not None:
        task["when"] = when
    return task


def _native_policy_task(
    task_id: str,
    *,
    attempts: int = 3,
    continue_on_error: bool = False,
) -> dict[str, Any]:
    task = _policy_task(
        task_id,
        attempts=attempts,
        continue_on_error=continue_on_error,
    )
    execution = task["execution"]
    execution["durable_retry_policy"] = {
        "first_retry_interval_ms": 100 if attempts > 1 else 0,
        "max_number_of_attempts": attempts,
        "backoff_coefficient": 2.0 if attempts > 1 else 1.0,
        "max_retry_interval_ms": 1_000 if attempts > 1 else 0,
        "retry_timeout_ms": 3_600_000,
    }
    return task


def _activity_failure(
    *,
    task_id: str = "work",
    code: str = "service_busy",
    kind: str = "handler_transient",
    retryable: bool = True,
    continuable: bool = True,
) -> dict[str, Any]:
    return {
        "id": task_id,
        "ok": False,
        "failure": {
            "error_code": code,
            "error": "Safe failure.",
            "kind": kind,
            "retryable": retryable,
            "continuable": continuable,
        },
    }


def _activity_success(task_id: str, result: Any) -> dict[str, Any]:
    return {"id": task_id, "ok": True, "result": result}


def _native_exhaustion(task_id: str = "work") -> TaskFailedError:
    outcome = _activity_failure(task_id=task_id)
    message = json.dumps(
        {"version": 1, "outcome": outcome},
        separators=(",", ":"),
        sort_keys=True,
    )
    return TaskFailedError(
        "Activity failed.",
        DurableRetryableActivityError(message),
    )


def test_native_retry_exhaustion_restores_sanitized_failure() -> None:
    result, context = _run_dynamic(
        [_native_policy_task("work")],
        policy={"allowed_tools": ["run"], "allowed_subagents": []},
        result_for=lambda _name, _payload: _native_exhaustion(),
    )

    assert result == {
        "failed": True,
        "error": "Safe failure.",
        "error_code": "service_busy",
        "node_id": "work",
        "path": None,
        "results": {},
        "attempts": 3,
        "kind": "handler_transient",
    }
    final_status = context.statuses[-1]
    assert final_status["schema_version"] == 4
    assert final_status["retry_driver"] == "durable"
    assert "retry_wait" not in final_status["counts"]
    assert final_status["nodes"]["work"] == {
        "state": "failed",
        "max_attempts": 3,
    }


def test_native_retry_exhaustion_can_continue_without_exposing_attempts_in_status() -> None:
    result, context = _run_dynamic(
        [_native_policy_task("work", continue_on_error=True)],
        policy={"allowed_tools": ["run"], "allowed_subagents": []},
        result_for=lambda _name, _payload: _native_exhaustion(),
    )

    assert result["results"]["work"] == {
        "failed": True,
        "error_code": "service_busy",
        "error": "Safe failure.",
        "kind": "handler_transient",
        "attempts": 3,
    }
    assert context.statuses[-1]["nodes"]["work"] == {
        "state": "failed_continued",
        "max_attempts": 3,
    }


def test_policy_task_routes_dynamic_and_dispatches_retry_contract() -> None:
    calls: list[dict[str, Any]] = []

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        if payload["attempt"] < 3:
            return _activity_failure(task_id=payload["id"])
        return _activity_success(payload["id"], {"done": True})

    result, context = _run_dynamic(
        [_policy_task("work", delays=[0, 2_000], args={"fixed": "value"})],
        policy={"allowed_tools": ["run"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert result == {"results": {"work": {"done": True}}}
    assert [call["attempt"] for call in calls] == [1, 2, 3]
    assert [call["max_attempts"] for call in calls] == [3, 3, 3]
    assert len({call["idempotency_key"] for call in calls}) == 1
    assert all(call["task_id"] == "work" for call in calls)
    assert all(call["node_instance_id"] == "work" for call in calls)
    assert all(call["args"] == {"fixed": "value"} for call in calls)
    initial = datetime(2024, 1, 1, tzinfo=UTC)
    assert context.timer_deadlines == [initial, initial + timedelta(seconds=2)]
    assert context.statuses[0]["schema_version"] == 3
    retry_status = next(
        status for status in context.statuses
        if status["nodes"]["work"]["state"] == "retry_wait"
    )
    assert retry_status["nodes"]["work"]["attempt"] in {1, 2}
    assert retry_status["nodes"]["work"]["max_attempts"] == 3
    assert retry_status["nodes"]["work"]["next_retry_time"].endswith("+00:00")
    final_status = context.statuses[-1]
    assert final_status["nodes"]["work"]["last_failure_kind"] == "handler_transient"
    assert final_status["nodes"]["work"]["last_error_code"] == "service_busy"
    assert "next_retry_time" not in final_status["nodes"]["work"]
    assert set(final_status["counts"]) == {
        "logical_total",
        "materialized_total",
        "pending",
        "running",
        "retry_wait",
        "completed",
        "skipped",
        "failed_continued",
        "failed",
    }
    for status in context.statuses:
        counts = status["counts"]
        assert sum(
            counts[key]
            for key in (
                "pending",
                "running",
                "retry_wait",
                "completed",
                "skipped",
                "failed_continued",
            )
        ) == counts["materialized_total"]
        serialized = json.dumps(status)
        for secret_key in ("args", "result", "idempotency", "session"):
            assert secret_key not in serialized


def test_v3_counts_blocked_normal_units_from_start() -> None:
    policy_blocked = _policy_task(
        "policy_blocked",
        attempts=4,
        depends_on=["root"],
    )
    policy_free_blocked = {
        "id": "plain_blocked",
        "type": TOOL_TASK_TYPE,
        "tool": "run",
        "args": {},
        "depends_on": ["root"],
    }
    context = _DynamicContext(
        [_policy_task("root", attempts=1), policy_blocked, policy_free_blocked],
        lambda _name, payload: _activity_success(payload["id"], "done"),
        policy={"allowed_tools": ["run"], "allowed_subagents": []},
    )
    generator = _registered_function(engine.ORCHESTRATOR_NAME)(context)

    next(generator)
    status = context.statuses[-1]

    assert status["schema_version"] == 3
    assert status["counts"]["materialized_total"] == 3
    assert status["counts"]["running"] == 1
    assert status["counts"]["pending"] == 2
    assert status["nodes"]["policy_blocked"] == {
        "state": "pending",
        "max_attempts": 4,
    }
    assert status["nodes"]["plain_blocked"] == {"state": "pending"}
    generator.close()


def test_policy_retry_sequence_is_deterministic_across_replay() -> None:
    def execute() -> tuple[list[tuple[int, str, dict[str, Any]]], list[datetime]]:
        observed: list[tuple[int, str, dict[str, Any]]] = []

        def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
            observed.append(
                (payload["attempt"], payload["idempotency_key"], payload["args"])
            )
            if payload["attempt"] == 1:
                return _activity_failure(task_id=payload["id"])
            return _activity_success(payload["id"], "ok")

        _, context = _run_dynamic(
            [_policy_task("work", attempts=2, delays=[1_250], args={"x": 1})],
            policy={"allowed_tools": ["run"], "allowed_subagents": []},
            result_for=result_for,
        )
        return observed, context.timer_deadlines

    assert execute() == execute()


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (
            _activity_failure(
                code="invalid",
                kind="handler_terminal",
                retryable=False,
            ),
            "invalid",
        ),
        (
            _activity_failure(
                code="workflow_task_execution_unknown",
                kind="execution_unknown",
                retryable=False,
            ),
            "workflow_task_execution_unknown",
        ),
    ],
)
def test_terminal_and_unknown_outcomes_do_not_retry(
    failure: dict[str, Any],
    expected_code: str,
) -> None:
    result, context = _run_dynamic(
        [_policy_task("work")],
        policy={"allowed_tools": ["run"], "allowed_subagents": []},
        result_for=lambda _name, _payload: failure,
    )
    assert result["failed"] is True
    assert result["error_code"] == expected_code
    assert result["attempts"] == 1
    assert result["kind"] == failure["failure"]["kind"]
    assert len(context.calls) == 1
    assert context.timers == []
    terminal_status = context.statuses[-1]
    assert terminal_status["nodes"]["work"]["state"] == "failed"
    assert terminal_status["counts"]["running"] == 0
    assert terminal_status["counts"]["failed"] == 1


def test_same_wave_failures_publish_every_logical_node_as_failed() -> None:
    result, context = _run_dynamic(
        [_policy_task("alpha", attempts=1), _policy_task("beta", attempts=1)],
        policy={"allowed_tools": ["run"], "allowed_subagents": []},
        result_for=lambda _name, payload: _activity_failure(task_id=payload["id"]),
    )

    assert result["failed"] is True
    terminal_status = context.statuses[-1]
    assert terminal_status["nodes"]["alpha"]["state"] == "failed"
    assert terminal_status["nodes"]["beta"]["state"] == "failed"
    assert terminal_status["counts"]["running"] == 0
    assert terminal_status["counts"]["failed"] == 2


def test_fail_fast_marks_same_wave_abandoned_retry_as_failed() -> None:
    def result_for(_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["id"] == "alpha":
            return _activity_failure(
                task_id="alpha",
                kind="handler_terminal",
                retryable=False,
            )
        return _activity_failure(task_id="beta")

    result, context = _run_dynamic(
        [_policy_task("alpha", attempts=1), _policy_task("beta", attempts=2)],
        policy={"allowed_tools": ["run"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert result["failed"] is True
    terminal_status = context.statuses[-1]
    assert terminal_status["nodes"]["beta"]["state"] == "failed"
    assert "next_retry_time" not in terminal_status["nodes"]["beta"]
    assert terminal_status["counts"]["retry_wait"] == 0
    assert terminal_status["counts"]["failed"] == 2


def test_materialization_failure_terminalizes_abandoned_retry() -> None:
    fan = _policy_task("fan", attempts=1, depends_on=["source"])
    fan["for_each"] = "${source.result.items}"

    def result_for(_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["id"] == "retrying":
            return _activity_failure(task_id="retrying")
        return _activity_success("source", {"items": "not-an-array"})

    result, context = _run_dynamic(
        [
            _policy_task("retrying", attempts=2, delays=[60_000]),
            _policy_task("source", attempts=1),
            fan,
        ],
        policy={"allowed_tools": ["run"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert result["failed"] is True
    assert result["error_code"] == "workflow_iteration_not_array"
    terminal_status = context.statuses[-1]
    assert terminal_status["nodes"]["retrying"]["state"] == "failed"
    assert "next_retry_time" not in terminal_status["nodes"]["retrying"]
    assert terminal_status["counts"]["retry_wait"] == 0


def test_bare_activity_exception_retries_then_succeeds() -> None:
    def result_for(name: str, payload: dict[str, Any]) -> Any:
        if payload["attempt"] == 1:
            return RuntimeError("ambiguous worker failure")
        return _activity_success(payload["id"], "recovered")

    result, context = _run_dynamic(
        [_policy_task("work", attempts=2, delays=[0])],
        policy={"allowed_tools": ["run"], "allowed_subagents": []},
        result_for=result_for,
    )
    assert result["results"]["work"] == "recovered"
    assert [payload["attempt"] for _, payload in context.calls] == [1, 2]
    assert len(context.timers) == 1


def test_retry_exhaustion_reports_final_attempt_count_when_continued() -> None:
    result, context = _run_dynamic(
        [_policy_task("work", attempts=3, delays=[0, 0], continue_on_error=True)],
        policy={"allowed_tools": ["run"], "allowed_subagents": []},
        result_for=lambda _name, _payload: _activity_failure(),
    )

    assert result["results"]["work"]["attempts"] == 3
    assert [payload["attempt"] for _, payload in context.calls] == [1, 2, 3]
    assert len(context.timers) == 2


def test_retry_reuses_initial_template_resolution() -> None:
    source = {
        "id": "source",
        "type": TOOL_TASK_TYPE,
        "tool": "collect",
        "args": {},
        "depends_on": [],
    }
    target = _policy_task(
        "work",
        attempts=2,
        delays=[0],
        depends_on=["source"],
        args={"value": "${source.result.value}"},
    )

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["id"] == "source":
            return {"id": "source", "result": {"value": "frozen"}}
        if payload["attempt"] == 1:
            return _activity_failure(task_id=payload["id"])
        return _activity_success(payload["id"], payload["args"])

    result, context = _run_dynamic(
        [source, target],
        policy={"allowed_tools": ["collect", "run"], "allowed_subagents": []},
        result_for=result_for,
    )
    work_calls = [payload for _, payload in context.calls if payload["id"] == "work"]
    assert [payload["args"] for payload in work_calls] == [
        {"value": "frozen"},
        {"value": "frozen"},
    ]
    assert result["results"]["work"] == {"value": "frozen"}


def test_retry_does_not_reevaluate_when(monkeypatch: pytest.MonkeyPatch) -> None:
    evaluations = 0
    original = engine.evaluate_condition

    def count_evaluation(*args: Any, **kwargs: Any) -> bool:
        nonlocal evaluations
        evaluations += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "evaluate_condition", count_evaluation)
    tasks = [
        {
            "id": "gate",
            "type": TOOL_TASK_TYPE,
            "tool": "collect",
            "args": {},
            "depends_on": [],
        },
        _policy_task(
            "work",
            attempts=2,
            delays=[0],
            depends_on=["gate"],
            when={
                "ref": "${gate.result.run}",
                "operator": "equals",
                "value": True,
            },
        ),
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["id"] == "gate":
            return {"id": "gate", "result": {"run": True}}
        if payload["attempt"] == 1:
            return _activity_failure(task_id="work")
        return {"id": "work", "ok": True, "result": "done"}

    result, _ = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "run"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert result["results"]["work"] == "done"
    assert evaluations == 1


def test_exhausted_failure_stops_after_same_wave_success_is_committed() -> None:
    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["id"] == "a_fail":
            return _activity_failure(task_id=payload["id"])
        return _activity_success(payload["id"], {"saved": True})

    result, context = _run_dynamic(
        [_policy_task("a_fail", attempts=1), _policy_task("b_success", attempts=1)],
        policy={"allowed_tools": ["run"], "allowed_subagents": []},
        result_for=result_for,
    )
    assert result["failed"] is True
    assert result["node_id"] == "a_fail"
    assert result["results"] == {"b_success": {"saved": True}}
    assert len(context.calls) == 2


def test_continued_failure_unlocks_condition_and_template() -> None:
    tasks = [
        _policy_task("work", attempts=1, continue_on_error=True),
        _policy_task(
            "recover",
            attempts=1,
            depends_on=["work"],
            args={"code": "${work.result.error_code}"},
            when={
                "ref": "${work.result.failed}",
                "operator": "equals",
                "value": True,
            },
        ),
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["id"] == "work":
            return _activity_failure(
                task_id=payload["id"],
                code="service_down",
                kind="handler_terminal",
                retryable=False,
            )
        return _activity_success(payload["id"], payload["args"])

    result, context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["run"], "allowed_subagents": []},
        result_for=result_for,
    )
    assert result["results"]["work"] == {
        "failed": True,
        "error_code": "service_down",
        "error": "Safe failure.",
        "kind": "handler_terminal",
        "attempts": 1,
    }
    assert result["results"]["recover"] == {"code": "service_down"}
    assert context.calls[-1][1]["args"] == {"code": "service_down"}


@pytest.mark.parametrize("kind", ["handler_contract", "authorization"])
def test_noncontinuable_failure_never_continues(kind: str) -> None:
    code = (
        "workflow_task_handler_contract"
        if kind == "handler_contract"
        else "workflow_task_authorization"
    )
    tasks = [
        _policy_task("work", attempts=1, continue_on_error=True),
        _policy_task("downstream", attempts=1, depends_on=["work"]),
    ]
    result, context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["run"], "allowed_subagents": []},
        result_for=lambda _name, _payload: _activity_failure(
            code=code,
            kind=kind,
            retryable=False,
            continuable=False,
        ),
    )
    assert result["failed"] is True
    assert result["error_code"] == code
    assert [payload["id"] for _, payload in context.calls] == ["work"]


def test_for_each_instances_retry_independently_and_aggregate_errors() -> None:
    source = {
        "id": "source",
        "type": TOOL_TASK_TYPE,
        "tool": "collect",
        "args": {},
        "depends_on": [],
    }
    iterated = _policy_task(
        "inspect",
        attempts=2,
        delays=[0],
        continue_on_error=True,
        depends_on=["source"],
        args={"index": "${index}"},
    )
    iterated["for_each"] = "${source.result.items}"

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["id"] == "source":
            return {"id": "source", "result": {"items": [{}, {}]}}
        if payload["id"] == "inspect[0]" and payload["attempt"] == 1:
            return _activity_failure(task_id=payload["id"])
        if payload["id"] == "inspect[1]":
            return _activity_failure(
                task_id=payload["id"],
                code="bad_item",
                kind="handler_terminal",
                retryable=False,
            )
        return _activity_success(payload["id"], payload["args"])

    result, context = _run_dynamic(
        [source, iterated],
        policy={"allowed_tools": ["collect", "run"], "allowed_subagents": []},
        result_for=result_for,
    )
    assert [entry["status"] for entry in result["results"]["inspect"]] == [
        "completed",
        "failed_continued",
    ]
    assert result["results"]["inspect"][1]["result"]["attempts"] == 1
    inspect_calls = [
        payload for _, payload in context.calls if payload["id"].startswith("inspect")
    ]
    assert [(call["id"], call["attempt"]) for call in inspect_calls] == [
        ("inspect[0]", 1),
        ("inspect[1]", 1),
        ("inspect[0]", 2),
    ]
    assert context.statuses[-1]["nodes"]["inspect"]["state"] == "aggregated_with_errors"
    inspect_status = context.statuses[-1]["nodes"]["inspect"]
    assert "max_attempts" not in inspect_status
    assert inspect_status["instances"]["inspect[0]"]["max_attempts"] == 2
    assert inspect_status["instances"]["inspect[0]"]["attempt"] == 2
    assert inspect_status["instances"]["inspect[0]"]["last_failure_kind"] == (
        "handler_transient"
    )
    assert inspect_status["instances"]["inspect[1]"]["state"] == "failed_continued"


def test_cancellation_during_retry_timer_dispatches_nothing_later() -> None:
    context = _DynamicContext(
        [_policy_task("work", attempts=2, delays=[5_000])],
        lambda _name, _payload: _activity_failure(),
        policy={"allowed_tools": ["run"], "allowed_subagents": []},
    )
    context.cancel_task.result = "stop"
    orchestrator = _registered_function(engine.ORCHESTRATOR_NAME)
    generator = orchestrator(context)

    next(generator)
    generator.send(context.last_wave)
    result: dict[str, Any] = {}
    try:
        generator.send(context.cancel_task)
    except StopIteration as stop:
        result = stop.value

    assert result["canceled"] is True
    assert len(context.calls) == 1
    assert len(context.timers) == 1
    assert context.timers[0].cancelled is True


def test_cancellation_during_policy_activity_does_not_cancel_activity() -> None:
    class _TrackingContext(_DynamicContext):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.activity_tasks: list[_Task] = []

        def call_activity(self, name: str, payload: dict[str, Any]) -> _Task:
            task = super().call_activity(name, payload)
            self.activity_tasks.append(task)
            return task

    context = _TrackingContext(
        [_policy_task("work")],
        lambda _name, payload: _activity_success(payload["id"], "late"),
        policy={"allowed_tools": ["run"], "allowed_subagents": []},
    )
    context.cancel_task.result = "stop"
    orchestrator = _registered_function(engine.ORCHESTRATOR_NAME)
    generator = orchestrator(context)

    next(generator)
    result: dict[str, Any] = {}
    try:
        generator.send(context.cancel_task)
    except StopIteration as stop:
        result = stop.value

    assert result["canceled"] is True
    assert len(context.calls) == 1
    assert context.activity_tasks[0].cancelled is False


def test_infrastructure_failure_waits_for_pending_sibling_before_application() -> None:
    class _DeferredSiblingContext(_DynamicContext):
        def call_activity(self, name: str, payload: dict[str, Any]) -> _Task:
            self.calls.append((name, payload))
            if payload["id"] == "a_fail":
                return _Task(RuntimeError("worker disappeared"))
            task = _Task(_activity_success(payload["id"], {"sibling": "applied"}))
            task.is_completed = False
            return task

    tasks = [
        _policy_task("a_fail", attempts=1),
        _policy_task("b_later", attempts=1),
    ]
    context = _DeferredSiblingContext(
        tasks,
        lambda _name, _payload: None,
        policy={"allowed_tools": ["run"], "allowed_subagents": []},
    )
    result = _run_orchestrator(_registered_function(engine.ORCHESTRATOR_NAME), context)

    assert result["error_code"] == "workflow_task_activity_infrastructure"
    assert result["results"] == {"b_later": {"sibling": "applied"}}
    assert result["error_code"] != "workflow_task_handler_contract"


def test_retry_timers_release_independently_in_deadline_order() -> None:
    tasks = [
        _policy_task("early", attempts=2, delays=[1_000]),
        _policy_task("late", attempts=2, delays=[300_000]),
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["attempt"] == 1:
            return _activity_failure(task_id=payload["id"])
        return _activity_success(payload["id"], payload["id"])

    result, context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["run"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert result["results"] == {"early": "early", "late": "late"}
    assert [(payload["id"], payload["attempt"]) for _, payload in context.calls] == [
        ("early", 1),
        ("late", 1),
        ("early", 2),
        ("late", 2),
    ]
    initial = datetime(2024, 1, 1, tzinfo=UTC)
    assert context.timer_deadlines == [
        initial + timedelta(seconds=1),
        initial + timedelta(minutes=5),
    ]


def test_pending_activity_capacity_is_not_blocked_by_long_retry_timer() -> None:
    tasks = [_policy_task("a_long_retry", attempts=2, delays=[300_000])]
    tasks.extend(
        _policy_task(f"b_{index}", attempts=1)
        for index in range(MAX_PARALLELISM)
    )

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["id"] == "a_long_retry" and payload["attempt"] == 1:
            return _activity_failure(task_id=payload["id"])
        return _activity_success(payload["id"], payload["id"])

    _, context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["run"], "allowed_subagents": []},
        result_for=result_for,
    )
    calls = [(payload["id"], payload["attempt"]) for _, payload in context.calls]

    assert calls[MAX_PARALLELISM] == (f"b_{MAX_PARALLELISM - 1}", 1)
    assert calls[-1] == ("a_long_retry", 2)


def test_policy_aware_target_outside_persisted_allowlist_never_dispatches() -> None:
    context = _DynamicContext(
        [_policy_task("crafted", attempts=1)],
        lambda _name, _payload: {"ok": True, "result": "must not run"},
        policy={"allowed_tools": [], "allowed_subagents": []},
    )

    with pytest.raises(RuntimeError, match="outside the persisted workflow owner policy"):
        _run_orchestrator(_registered_function(engine.ORCHESTRATOR_NAME), context)

    assert context.calls == []


@pytest.mark.parametrize(
    "outcome",
    [
        {
            "id": "other",
            "ok": True,
            "result": "forged",
        },
        {
            "id": "work",
            "ok": False,
            "failure": {
                "error_code": "service_busy",
                "error": "Safe failure.",
                "kind": "handler_transient",
                "retryable": True,
            },
        },
        {
            "id": "work",
            "ok": False,
            "failure": {
                "error_code": "workflow_task_authorization",
                "error": "Task target is not authorized.",
                "kind": "authorization",
                "retryable": True,
                "continuable": True,
            },
        },
    ],
)
def test_malformed_or_forged_activity_outcome_becomes_contract_failure(
    outcome: dict[str, Any],
) -> None:
    result, context = _run_dynamic(
        [_policy_task("work", attempts=3, delays=[0, 0], continue_on_error=True)],
        policy={"allowed_tools": ["run"], "allowed_subagents": []},
        result_for=lambda _name, _payload: outcome,
    )

    assert result["error_code"] == "workflow_task_handler_contract"
    assert result["failed"] is True
    assert len(context.calls) == 1
    assert context.timers == []


def test_non_iterated_retry_wait_sets_logical_state() -> None:
    context = _DynamicContext(
        [_policy_task("work", attempts=2, delays=[60_000])],
        lambda _name, payload: _activity_failure(task_id=payload["id"]),
        policy={"allowed_tools": ["run"], "allowed_subagents": []},
    )
    context.cancel_task.result = "stop"
    generator = _registered_function(engine.ORCHESTRATOR_NAME)(context)

    next(generator)
    generator.send(context.last_wave)

    assert context.statuses[-1]["nodes"]["work"]["state"] == "retry_wait"
    with pytest.raises(StopIteration):
        generator.send(context.cancel_task)


@pytest.mark.asyncio
async def test_policy_sub_agent_malformed_context_returns_contract_outcome() -> None:
    activity = _registered_function(
        engine.SUB_AGENT_ACTIVITY_NAME,
        catalog=_catalog("specialist"),
        workflow_agent_policies={
            "coordinator": WorkflowPlanPolicy(
                allowed_tools=frozenset(),
                allowed_subagents=frozenset({"specialist"}),
            )
        },
    )
    payload = _policy_activity_payload()
    payload.pop("tool")
    payload.pop("args")
    payload.update({"agent": "specialist", "task": "work", "attempt": False})

    outcome = await activity(payload)

    assert outcome["failure"]["error_code"] == "workflow_task_handler_contract"
    assert outcome["failure"]["retryable"] is False
    assert outcome["failure"]["continuable"] is False


def test_control_plane_failure_cancels_existing_retry_timer() -> None:
    tasks = [_policy_task("a_retry", attempts=2, delays=[300_000])]
    tasks.extend(
        _policy_task(f"f_{index}", attempts=1)
        for index in range(MAX_PARALLELISM - 1)
    )
    tasks.extend([
        _policy_task("z_source", attempts=1),
        _policy_task(
            "z_bad",
            attempts=1,
            depends_on=["z_source"],
            when={
                "ref": "${z_source.result.missing}",
                "operator": "equals",
                "value": True,
            },
        ),
    ])

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["id"] == "a_retry":
            return _activity_failure(task_id=payload["id"])
        return _activity_success(payload["id"], {"ready": True})

    result, context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["run"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert result["error_code"] == "workflow_reference_unresolved"
    assert len(context.timers) == 1
    assert context.timers[0].cancelled is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "decision"),
    [
        ("success", "complete"),
        ("retry", "retry"),
        ("continue", "continue"),
        ("fail", "fail"),
    ],
)
async def test_policy_activity_emits_safe_actual_delivery_telemetry(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    decision: str,
) -> None:
    starts: list[dict[str, Any]] = []
    completions: list[dict[str, Any]] = []

    class _Recorder:
        def __enter__(self):
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def complete(self, **kwargs: Any) -> None:
            completions.append(kwargs)

    def telemetry(attributes: dict[str, Any]) -> _Recorder:
        starts.append(attributes)
        return _Recorder()

    monkeypatch.setattr(policy, "workflow_task_activity_telemetry", telemetry)

    def handler(args: dict[str, Any]) -> dict[str, bool]:
        if mode == "retry":
            raise WorkflowRetryableError("busy", "Safe.")
        if mode in {"continue", "fail"}:
            raise WorkflowTerminalError("terminal", "Safe.")
        return {"ok": True}

    payload = _policy_activity_payload()
    if mode == "retry":
        payload["max_attempts"] = 2
        payload["execution"]["max_attempts"] = 2
        payload["execution"]["retry_delays_ms"] = [125]
    if mode == "continue":
        payload["execution"]["continue_on_error"] = True
    await _policy_tool_activity(handler)(payload)

    assert len(starts) == len(completions) == 1
    assert completions[0]["retry_decision"] == decision
    serialized = json.dumps([starts, completions])
    for forbidden in ("provider-secret", "idempotency_key", '"args"', '"result"', "session"):
        assert forbidden not in serialized


@pytest.mark.asyncio
async def test_policy_free_activity_emits_no_workflow_task_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        policy,
        "workflow_task_activity_telemetry",
        lambda attributes: calls.append(attributes),
    )
    activity = _registered_function(
        engine._ACTIVITY_NAME,
        handler_catalog=integration.build_workflow_handler_catalog(
            [WorkflowTool("run", "Run", lambda args: args)]
        ),
        workflow_agent_policies={
            "coordinator": WorkflowPlanPolicy(allowed_tools=frozenset({"run"}))
        },
    )
    await activity({
        "id": "work",
        "tool": "run",
        "args": {},
        "workflow_agent_slug": "coordinator",
        "workflow_id": "workflow",
    })
    assert calls == []


@pytest.mark.asyncio
async def test_cancelled_activity_records_completion_and_exceptional_span_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completions: list[dict[str, Any]] = []
    exits: list[tuple[Any, Any, Any]] = []

    class _Recorder:
        def __enter__(self):
            return self

        def __exit__(self, *args: Any) -> None:
            exits.append(args)

        def complete(self, **kwargs: Any) -> None:
            completions.append(kwargs)

    monkeypatch.setattr(
        policy,
        "workflow_task_activity_telemetry",
        lambda _attributes: _Recorder(),
    )

    async def cancel(_args: dict[str, Any]) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _policy_tool_activity(cancel)(_policy_activity_payload())

    assert completions == [{
        "outcome_kind": "canceled",
        "error_code": "workflow_task_canceled",
        "retry_decision": "fail",
        "selected_delay_ms": None,
    }]
    assert exits[0][0] is asyncio.CancelledError
    assert isinstance(exits[0][1], asyncio.CancelledError)
