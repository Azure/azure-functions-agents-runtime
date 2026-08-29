"""Per-attempt task timeout and `continue_on_error` DAG continuation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from durabletask.task import TaskFailedError

from azure_functions_agents._function_tool import WorkflowTool, workflow_tool
from azure_functions_agents.config.schema import (
    BuiltinEndpointsConfig,
    ResolvedAgent,
    ToolsFilter,
)
from azure_functions_agents.registration.capabilities import AgentCapabilities
from azure_functions_agents.registration.catalog import CatalogEntry, build_catalog
from azure_functions_agents.workflows import engine, integration, registry
from azure_functions_agents.workflows.activity import (
    WorkflowTaskTimeoutError,
    invoke_policy_handler,
)
from azure_functions_agents.workflows.native_retry import DurableRetryableActivityError
from azure_functions_agents.workflows.schema import (
    SUB_AGENT_TASK_TYPE,
    TOOL_TASK_TYPE,
    PlanValidationError,
    WorkflowPlanPolicy,
    WorkflowRetryBackoff,
    WorkflowRetryPolicy,
    WorkflowTask,
    WorkflowTaskExecution,
    WorkflowToolExecutionPolicy,
    resolve_workflow_task_execution,
    validate_plan,
)

_BACKOFF = WorkflowRetryBackoff(initial="PT1S", multiplier=2.0, max="PT4S")
_RETRY = WorkflowRetryPolicy(max_attempts=3, backoff=_BACKOFF)
_DURABLE_POLICY = {
    "first_retry_interval_ms": 1_000,
    "max_number_of_attempts": 3,
    "backoff_coefficient": 2.0,
    "max_retry_interval_ms": 4_000,
    "retry_timeout_ms": 3_600_000,
}
_SINGLE_ATTEMPT_POLICY = {
    "first_retry_interval_ms": 0,
    "max_number_of_attempts": 1,
    "backoff_coefficient": 1.0,
    "max_retry_interval_ms": 0,
    "retry_timeout_ms": 3_600_000,
}
_AGENT = {"workflow_agent_slug": "coordinator"}


@pytest.fixture(autouse=True)
def _reset_registry():
    """Restore the process-global workflow tool registry around every test."""
    saved_entries = dict(registry._REGISTRY)
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(saved_entries)


def _execution(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "max_attempts": 1,
        "durable_retry_policy": dict(_SINGLE_ATTEMPT_POLICY),
    }
    payload.update(overrides)
    return payload


def _activity_task(**overrides: Any) -> dict[str, Any]:
    return {
        "id": "work",
        "workflow_id": "workflow-1",
        "task_id": "work",
        "execution": _execution(**overrides),
    }


# ---- authored declaration ---------------------------------------------------


@pytest.mark.parametrize("timeout", ["PT0.5S", "PT11M", "30s", "PT0S", "P1D"])
def test_authored_timeout_rejects_durations_outside_the_attempt_bounds(
    timeout: str,
) -> None:
    with pytest.raises(ValueError):
        WorkflowTaskExecution(timeout=timeout)


def test_a_task_using_neither_new_field_freezes_the_previous_payload() -> None:
    """Replay compatibility: the wire shape only grows when it is asked to."""
    task = WorkflowTask(
        id="work",
        type=TOOL_TASK_TYPE,
        tool="publish",
        execution=WorkflowTaskExecution(retry=_RETRY),
    )

    assert resolve_workflow_task_execution(task) == {
        "max_attempts": 3,
        "durable_retry_policy": _DURABLE_POLICY,
    }


def test_authored_timeout_and_continuation_freeze_into_the_payload() -> None:
    task = WorkflowTask(
        id="work",
        type=TOOL_TASK_TYPE,
        tool="publish",
        execution=WorkflowTaskExecution(timeout="PT30S", continue_on_error=True),
    )

    assert resolve_workflow_task_execution(task) == {
        "max_attempts": 1,
        "durable_retry_policy": _SINGLE_ATTEMPT_POLICY,
        "timeout_ms": 30_000,
        "continue_on_error": True,
    }


def test_tool_declaration_overrides_the_plan_authored_timeout() -> None:
    task = WorkflowTask(
        id="work",
        type=TOOL_TASK_TYPE,
        tool="publish",
        execution=WorkflowTaskExecution(timeout="PT9M"),
    )

    effective = resolve_workflow_task_execution(task, decorator_timeout="PT5S")

    assert effective is not None
    assert effective["timeout_ms"] == 5_000


def test_tool_declared_timeout_alone_makes_a_task_policy_aware() -> None:
    task = WorkflowTask(id="work", type=TOOL_TASK_TYPE, tool="publish")

    effective = resolve_workflow_task_execution(task, decorator_timeout="PT5S")

    assert effective is not None
    assert effective["timeout_ms"] == 5_000
    assert effective["max_attempts"] == 1


def test_continuation_is_task_local_and_valid_on_a_sub_agent_task() -> None:
    task = WorkflowTask(
        id="analyze",
        type=SUB_AGENT_TASK_TYPE,
        agent="analyst",
        task="Analyze PR 117.",
        execution=WorkflowTaskExecution(timeout="PT2M", continue_on_error=True),
    )

    effective = resolve_workflow_task_execution(task)

    assert effective is not None
    assert effective["timeout_ms"] == 120_000
    assert effective["continue_on_error"] is True


def test_tool_declared_timeout_is_rejected_on_a_sub_agent_task() -> None:
    task = WorkflowTask(
        id="analyze",
        type=SUB_AGENT_TASK_TYPE,
        agent="analyst",
        task="Analyze PR 117.",
    )

    with pytest.raises(ValueError, match="only valid on type=tool"):
        resolve_workflow_task_execution(task, decorator_timeout="PT5S")


def test_attempt_deadlines_plus_retry_delays_stay_inside_the_elapsed_ceiling() -> None:
    task = WorkflowTask(
        id="work",
        type=TOOL_TASK_TYPE,
        tool="publish",
        execution=WorkflowTaskExecution(
            timeout="PT10M",
            retry=WorkflowRetryPolicy(
                max_attempts=5,
                backoff=WorkflowRetryBackoff(initial="PT5M", multiplier=2.0, max="PT15M"),
            ),
        ),
    )

    with pytest.raises(ValueError, match="must not exceed PT1H"):
        resolve_workflow_task_execution(task)


def test_a_plan_authored_timeout_is_validated_at_submission() -> None:
    with pytest.raises(PlanValidationError):
        validate_plan(
            {
                "tasks": [
                    {
                        "id": "work",
                        "type": "tool",
                        "tool": "publish",
                        "args": {},
                        "execution": {"timeout": "PT11M"},
                    }
                ]
            },
            policy=WorkflowPlanPolicy(allowed_tools=frozenset({"publish"})),
        )


def test_workflow_tool_decorator_rejects_an_unusable_timeout() -> None:
    with pytest.raises(ValueError):

        @workflow_tool(description="Publish an order.", timeout="PT0.5S")
        def publish(args: dict[str, Any]) -> dict[str, Any]:
            return args


def test_registry_entry_carries_and_validates_the_declared_timeout() -> None:
    entry = registry.make_workflow_tool_entry(
        "publish", "Publish", lambda args: args, timeout="PT5S"
    )
    assert entry.timeout == "PT5S"

    with pytest.raises(ValueError, match="invalid timeout"):
        registry.make_workflow_tool_entry(
            "publish", "Publish", lambda args: args, timeout="PT11M"
        )


# ---- attempt deadline at the Activity boundary ------------------------------


@pytest.mark.asyncio
async def test_a_synchronous_handler_past_its_deadline_is_a_retryable_timeout() -> None:
    """The deadline bounds the wait, not the worker thread the handler runs on."""
    import threading

    release = threading.Event()
    finished = threading.Event()

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        release.wait(10)
        finished.set()
        return {"never": "returned"}

    try:
        with pytest.raises(DurableRetryableActivityError) as raised:
            await invoke_policy_handler(
                handler,
                {},
                task=_activity_task(timeout_ms=1_000),
                target="publish",
            )
    finally:
        release.set()

    assert '"kind":"timeout"' in str(raised.value)
    assert '"error_code":"workflow_task_timeout"' in str(raised.value)
    assert '"retryable":true' in str(raised.value)
    # The handler was still running when the attempt was reported as timed out.
    assert finished.wait(10)


@pytest.mark.asyncio
async def test_an_async_handler_past_its_deadline_is_a_retryable_timeout() -> None:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(5)
        return {"never": "returned"}

    with pytest.raises(DurableRetryableActivityError) as raised:
        await invoke_policy_handler(
            handler,
            {},
            task=_activity_task(timeout_ms=1_000),
            target="publish",
        )

    assert '"kind":"timeout"' in str(raised.value)


@pytest.mark.asyncio
async def test_a_nested_execution_bound_reports_the_same_timeout_classification() -> None:
    def handler(args: dict[str, Any]) -> dict[str, Any]:
        raise WorkflowTaskTimeoutError

    with pytest.raises(DurableRetryableActivityError) as raised:
        await invoke_policy_handler(
            handler, {}, task=_activity_task(), target="analyst"
        )

    assert '"error_code":"workflow_task_timeout"' in str(raised.value)


@pytest.mark.asyncio
async def test_a_handler_raised_timeout_error_stays_an_unknown_execution_failure() -> None:
    def handler(args: dict[str, Any]) -> dict[str, Any]:
        raise TimeoutError("upstream HTTP client timed out")

    outcome = await invoke_policy_handler(
        handler,
        {},
        task=_activity_task(timeout_ms=60_000),
        target="publish",
    )

    assert outcome["failure"]["kind"] == "execution_unknown"
    assert outcome["failure"]["retryable"] is False
    assert "upstream HTTP client" not in outcome["failure"]["error"]


@pytest.mark.asyncio
async def test_a_task_without_a_persisted_deadline_is_never_interrupted() -> None:
    """A history written before deadlines existed keeps its unbounded attempt."""

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0.05)
        return {"published": True}

    outcome = await invoke_policy_handler(
        handler, {}, task=_activity_task(), target="publish"
    )

    assert outcome == {"id": "work", "ok": True, "result": {"published": True}}


@pytest.mark.asyncio
async def test_host_cancellation_is_not_reported_as_a_timeout() -> None:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await invoke_policy_handler(
            handler,
            {},
            task=_activity_task(timeout_ms=60_000),
            target="publish",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_ms", [999, 600_001, "PT5S", 5.0])
async def test_a_persisted_deadline_outside_the_bounds_is_a_contract_failure(
    timeout_ms: Any,
) -> None:
    activity = _tool_activity({"publish"})

    outcome = await activity({
        **_activity_task(timeout_ms=timeout_ms),
        "tool": "publish",
        "args": {},
        **_AGENT,
    })

    assert outcome["failure"]["kind"] == "handler_contract"


@pytest.mark.asyncio
async def test_a_sub_agent_timeout_is_reported_as_a_retryable_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run_leaf(*args: Any, **kwargs: Any) -> str:
        raise TimeoutError("leaf agent exceeded its resolved timeout")

    monkeypatch.setattr(engine, "run_leaf_agent_task", run_leaf)
    activity = _sub_agent_activity()

    with pytest.raises(DurableRetryableActivityError) as raised:
        await activity({
            "id": "analyze",
            "task_id": "analyze",
            "agent": "analyst",
            "task": "Analyze PR 117.",
            "workflow_id": "workflow-1",
            "execution": _execution(),
            **_AGENT,
        })

    assert '"error_code":"workflow_task_timeout"' in str(raised.value)
    assert "resolved timeout" not in str(raised.value)


# ---- continue_on_error ------------------------------------------------------


def test_a_terminal_failure_is_committed_as_a_sanitized_result() -> None:
    context = _Context(
        [
            _tool_node("work", _execution(continue_on_error=True)),
            _tool_node("after", depends_on=["work"]),
        ],
        {
            "work": _terminal_failure("work"),
            "after": {"id": "after", "result": {"ran": True}},
        },
    )

    assert _orchestrate(context) == {
        "results": {
            "work": {
                "failed": True,
                "error_code": "order_rejected",
                "error": "The order was rejected.",
                "kind": "handler_terminal",
            },
            "after": {"ran": True},
        }
    }


def test_a_terminal_failure_still_fails_the_workflow_by_default() -> None:
    context = _Context(
        [_tool_node("work", _execution())],
        {"work": _terminal_failure("work")},
    )

    with pytest.raises(RuntimeError, match="order_rejected"):
        _orchestrate(context)


def test_exhausted_native_retry_is_continued_only_after_the_budget_is_spent() -> None:
    exhausted = TaskFailedError(
        "Activity failed.",
        DurableRetryableActivityError(_retryable_marker("work")),
    )
    context = _Context(
        [
            _tool_node(
                "work",
                _execution(
                    max_attempts=3,
                    durable_retry_policy=dict(_DURABLE_POLICY),
                    timeout_ms=5_000,
                    continue_on_error=True,
                ),
            ),
            _tool_node("after", depends_on=["work"]),
        ],
        {"work": exhausted, "after": {"id": "after", "result": {"ran": True}}},
    )

    outcome = _orchestrate(context)

    assert outcome["results"]["work"] == {
        "failed": True,
        "error_code": "service_busy",
        "error": "Try again.",
        "kind": "timeout",
    }
    assert outcome["results"]["after"] == {"ran": True}


@pytest.mark.parametrize(
    "failure",
    [
        {
            "error_code": "workflow_task_authorization",
            "error": "Task target is not authorized.",
            "kind": "authorization",
            "retryable": False,
        },
        {
            "error_code": "workflow_task_handler_contract",
            "error": "Task Activity returned an invalid outcome.",
            "kind": "handler_contract",
            "retryable": False,
        },
    ],
)
def test_continuation_never_absorbs_a_denied_or_malformed_task(
    failure: dict[str, Any],
) -> None:
    context = _Context(
        [_tool_node("work", _execution(continue_on_error=True))],
        {"work": {"id": "work", "ok": False, "failure": failure}},
    )

    with pytest.raises(RuntimeError, match=failure["error_code"]):
        _orchestrate(context)


def test_continuation_never_absorbs_an_unclassified_durable_failure() -> None:
    context = _Context(
        [_tool_node("work", _execution(continue_on_error=True))],
        {"work": TaskFailedError("Activity failed.", ValueError("private detail"))},
    )

    with pytest.raises(TaskFailedError):
        _orchestrate(context)


def test_a_legacy_sibling_failure_still_fails_a_continuable_wave() -> None:
    """Continuation is per task: it never rescues a node that did not declare it."""
    context = _Context(
        [
            _tool_node("legacy"),
            _tool_node("work", _execution(continue_on_error=True)),
        ],
        {
            "legacy": RuntimeError("legacy task failed"),
            "work": _terminal_failure("work"),
        },
    )

    with pytest.raises(RuntimeError, match="legacy task failed"):
        _orchestrate(context)


def test_a_wait_timer_beside_a_continued_task_still_commits_its_result() -> None:
    context = _Context(
        [
            _tool_node("work", _execution(continue_on_error=True)),
            {"id": "pause", "type": "wait", "duration": "PT1S", "depends_on": []},
        ],
        {"work": _terminal_failure("work")},
    )

    outcome = _orchestrate(context)

    assert outcome["results"]["work"]["failed"] is True
    assert "waited_until" in outcome["results"]["pause"]


def test_a_continued_node_is_visible_to_a_downstream_condition() -> None:
    context = _Context(
        [
            _tool_node("work", _execution(continue_on_error=True)),
            {
                **_tool_node("recover", depends_on=["work"]),
                "when": {
                    "ref": "${work.result.failed}",
                    "operator": "equals",
                    "value": True,
                },
            },
        ],
        {
            "work": _terminal_failure("work"),
            "recover": {"id": "recover", "result": {"recovered": True}},
        },
    )

    outcome = _orchestrate(context)

    assert outcome["results"]["recover"] == {"recovered": True}


def test_a_continued_instance_is_aggregated_with_its_iterated_siblings() -> None:
    context = _Context(
        [
            _tool_node("seed"),
            {
                **_tool_node("fan", _execution(continue_on_error=True), depends_on=["seed"]),
                "for_each": "${seed.result.items}",
            },
        ],
        {
            "seed": {"id": "seed", "result": {"items": [1, 2]}},
            "fan[0]": {"id": "fan[0]", "ok": True, "result": {"ok": 1}},
            "fan[1]": _terminal_failure("fan[1]"),
        },
    )

    outcome = _orchestrate(context)

    assert outcome["results"]["fan"] == [
        {"index": 0, "status": "completed", "result": {"ok": 1}},
        {
            "index": 1,
            "status": "completed",
            "result": {
                "failed": True,
                "error_code": "order_rejected",
                "error": "The order was rejected.",
                "kind": "handler_terminal",
            },
        },
    ]


# ---- end-to-end submission --------------------------------------------------


@pytest.mark.asyncio
async def test_start_workflow_persists_the_tool_declared_timeout() -> None:
    from azure_functions_agents.workflows import tools as workflow_tools

    catalog = integration.build_workflow_handler_catalog(
        [WorkflowTool("publish", "Publish an order.", lambda args: args, timeout="PT5S")]
    )
    started: dict[str, Any] = {}

    class _Client:
        async def get_status_all(self) -> list[Any]:
            return []

        async def start_new(self, name: str, *, instance_id: str, client_input: Any) -> str:
            started.update(client_input)
            return instance_id

    session = workflow_tools.WorkflowSessionContext(
        workflow_agent_slug="coordinator",
        session_id="session-1",
        agent_name="main",
        durable_client=_Client(),  # type: ignore[arg-type]
    )
    await workflow_tools.start_workflow(
        workflow_tools.StartWorkflowParams.model_validate({
            "tasks": [
                {
                    "id": "work",
                    "type": "tool",
                    "tool": "publish",
                    "args": {},
                    "execution": {"continue_on_error": True},
                }
            ]
        }),
        session,
        policy=WorkflowPlanPolicy(
            allowed_tools=frozenset({"publish"}),
            tool_execution={
                "publish": WorkflowToolExecutionPolicy(timeout=catalog["publish"].timeout)
            },
        ),
    )

    [task] = started["tasks"]
    assert task["execution"] == {
        "max_attempts": 1,
        "durable_retry_policy": _SINGLE_ATTEMPT_POLICY,
        "timeout_ms": 5_000,
        "continue_on_error": True,
    }


# ---- harness ----------------------------------------------------------------


def _retryable_marker(node_id: str) -> str:
    import json

    return json.dumps(
        {
            "version": 1,
            "outcome": {
                "id": node_id,
                "ok": False,
                "failure": {
                    "error_code": "service_busy",
                    "error": "Try again.",
                    "kind": "timeout",
                    "retryable": True,
                },
            },
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _terminal_failure(node_id: str) -> dict[str, Any]:
    return {
        "id": node_id,
        "ok": False,
        "failure": {
            "error_code": "order_rejected",
            "error": "The order was rejected.",
            "kind": "handler_terminal",
            "retryable": False,
        },
    }


def _tool_node(
    task_id: str,
    execution: dict[str, Any] | None = None,
    *,
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": task_id,
        "type": TOOL_TASK_TYPE,
        "tool": "publish",
        "args": {},
        "depends_on": list(depends_on or []),
    }
    if execution is not None:
        node["execution"] = execution
    return node


class _Task:
    """A Durable task whose ``result`` raises, matching Durable Python 2.x."""

    def __init__(self, result: Any = None) -> None:
        self._result = result
        self.is_completed = True
        self.candidates: list[_Task] = []

    @property
    def result(self) -> Any:
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result

    def cancel(self) -> None:
        return None


class _Context:
    """Fake orchestration context selecting over individual wave tasks."""

    def __init__(self, tasks: list[dict[str, Any]], results: dict[str, Any]) -> None:
        self.instance_id = "workflow-parent"
        self.current_utc_datetime = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
        self._input = {
            "workflow_agent_slug": "coordinator",
            "tasks": tasks,
            "policy": {"allowed_tools": ["publish"], "allowed_subagents": []},
        }
        self._results = results
        self.cancel_task = _Task()

    def create_timer(self, deadline: datetime) -> _Task:
        return _Task({"fired_at": deadline.isoformat()})

    def get_input(self) -> dict[str, Any]:
        return self._input

    def wait_for_external_event(self, name: str) -> _Task:
        return self.cancel_task

    def call_activity(self, name: str, payload: dict[str, Any]) -> _Task:
        return _Task(self._results.get(payload["id"]))

    def call_activity_with_retry(
        self, name: str, retry_policy: Any, payload: dict[str, Any]
    ) -> _Task:
        return _Task(self._results.get(payload["id"]))

    def task_any(self, tasks: list[_Task]) -> _Task:
        selection = _Task()
        selection.candidates = list(tasks)
        return selection

    def set_custom_status(self, status: Any) -> None:
        return None


class _Blueprints:
    def __init__(self) -> None:
        self.blueprints: list[Any] = []

    def register_blueprint(self, blueprint: Any) -> None:
        self.blueprints.append(blueprint)

    def function(self, name: str) -> Callable[..., Any]:
        [blueprint] = self.blueprints
        for builder in blueprint._function_builders:
            if builder._function._name == name:
                return builder._function._func
        raise AssertionError(f"workflow function {name!r} was not registered")


def _tool_activity(allowed_tools: Any) -> Callable[..., Any]:
    app = _Blueprints()
    engine.register_workflows(
        app,
        handler_catalog=integration.build_workflow_handler_catalog(
            [WorkflowTool("publish", "Publish", lambda args: {"echoed": args})]
        ),
        workflow_agent_policies={
            "coordinator": WorkflowPlanPolicy(allowed_tools=frozenset(allowed_tools))
        },
    )
    return app.function("agents_workflow_run_tool")


def _sub_agent_activity() -> Callable[..., Any]:
    resolved = ResolvedAgent(
        name="analyst",
        slug="analyst",
        description="analyst description",
        trigger=None,
        instructions="analyst instructions",
        is_main=False,
        builtin_endpoints=BuiltinEndpointsConfig(),
        model="test-model",
        timeout=12.0,
        enabled_mcp_names=[],
        enabled_skills_names=[],
        tool_filter=ToolsFilter(),
        sandbox_config=None,
        input_schema=None,
        response_schema=None,
        response_example=None,
        metadata={},
        source_file="analyst.agent.md",
    )
    app = _Blueprints()
    engine.register_workflows(
        app,
        catalog=build_catalog({"analyst": CatalogEntry(resolved, AgentCapabilities())}),
        workflow_agent_policies={
            "coordinator": WorkflowPlanPolicy(
                allowed_tools=frozenset(),
                allowed_subagents=frozenset({"analyst"}),
            )
        },
    )
    return app.function(engine.SUB_AGENT_ACTIVITY_NAME)


def _orchestrate(context: _Context) -> dict[str, Any]:
    app = _Blueprints()
    engine.register_workflows(
        app,
        workflow_agent_policies={
            "coordinator": WorkflowPlanPolicy(allowed_tools=frozenset({"publish"}))
        },
    )
    orchestrator = app.function(engine.ORCHESTRATOR_NAME).__closure__[0].cell_contents
    generator = orchestrator(context)
    try:
        selection = next(generator)
        while True:
            winner = next(
                task for task in selection.candidates if task is not context.cancel_task
            )
            selection = generator.send(winner)
    except StopIteration as stop:
        return stop.value
