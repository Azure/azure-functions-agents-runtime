"""Durable native Activity retry: declaration, dispatch, and replay compatibility."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from durabletask.task import TaskFailedError

from azure_functions_agents._function_tool import WorkflowTool, workflow_tool
from azure_functions_agents.workflows import engine, integration, registry
from azure_functions_agents.workflows.activity import invoke_policy_handler
from azure_functions_agents.workflows.context import (
    current_workflow_task_context,
    workflow_task_idempotency_key,
)
from azure_functions_agents.workflows.native_retry import (
    DurableRetryableActivityError,
    create_durable_retry_policy,
    decode_durable_retry_failure,
)
from azure_functions_agents.workflows.schema import (
    TOOL_TASK_TYPE,
    PlanValidationError,
    WorkflowPlanPolicy,
    WorkflowRetryBackoff,
    WorkflowRetryPolicy,
    WorkflowTask,
    WorkflowTaskExecution,
    WorkflowToolExecutionPolicy,
    native_retry_delays_ceiling_ms,
    plan_to_activity_inputs,
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


def _native_task(node_instance_id: str = "work") -> dict[str, Any]:
    return {
        "id": node_instance_id,
        "workflow_id": "workflow-1",
        "task_id": "work",
        "execution": {"max_attempts": 3, "durable_retry_policy": dict(_DURABLE_POLICY)},
    }


# ---- declaration contract ---------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 3},
        {"max_attempts": 0, "backoff": _BACKOFF},
        {"max_attempts": 6, "backoff": _BACKOFF},
        {"max_attempts": 1, "backoff": _BACKOFF},
    ],
)
def test_retry_declaration_rejects_unusable_attempt_counts(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        WorkflowRetryPolicy(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"initial": "PT0S", "multiplier": 2.0, "max": "PT4S"},
        {"initial": "PT1S", "multiplier": 2.0, "max": "PT0.5S"},
        {"initial": "PT1S", "multiplier": 0.5, "max": "PT4S"},
        {"initial": "PT1S", "multiplier": 11.0, "max": "PT4S"},
        {"initial": "PT10M", "multiplier": 2.0, "max": "PT20M"},
        {"initial": "1s", "multiplier": 2.0, "max": "PT4S"},
    ],
)
def test_retry_backoff_rejects_out_of_range_declarations(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        WorkflowRetryBackoff(**kwargs)


def test_policy_free_task_persists_no_execution_payload() -> None:
    task = WorkflowTask(id="work", type=TOOL_TASK_TYPE, tool="publish")

    assert resolve_workflow_task_execution(task) is None


def test_authored_retry_freezes_the_durable_wire_shape() -> None:
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


def test_tool_declaration_overrides_the_plan_authored_retry() -> None:
    task = WorkflowTask(
        id="work",
        type=TOOL_TASK_TYPE,
        tool="publish",
        execution=WorkflowTaskExecution(retry=WorkflowRetryPolicy(max_attempts=1)),
    )

    effective = resolve_workflow_task_execution(task, decorator_retry=_RETRY)

    assert effective is not None
    assert effective["max_attempts"] == 3


def test_tool_declaration_alone_makes_a_task_policy_aware() -> None:
    task = WorkflowTask(id="work", type=TOOL_TASK_TYPE, tool="publish")

    effective = resolve_workflow_task_execution(task, decorator_retry=_RETRY)

    assert effective is not None
    assert effective["durable_retry_policy"]["backoff_coefficient"] == 2.0


def test_default_retry_declares_a_single_attempt_without_backoff() -> None:
    task = WorkflowTask(
        id="work",
        type=TOOL_TASK_TYPE,
        tool="publish",
        execution=WorkflowTaskExecution(),
    )

    effective = resolve_workflow_task_execution(task)

    assert effective == {
        "max_attempts": 1,
        "durable_retry_policy": {
            "first_retry_interval_ms": 0,
            "max_number_of_attempts": 1,
            "backoff_coefficient": 1.0,
            "max_retry_interval_ms": 0,
            "retry_timeout_ms": 3_600_000,
        },
    }


def test_the_widest_retry_declaration_stays_inside_the_execution_ceiling() -> None:
    """The authored bounds cannot produce a schedule beyond the internal PT1H guard."""
    task = WorkflowTask(
        id="work",
        type=TOOL_TASK_TYPE,
        tool="publish",
        execution=WorkflowTaskExecution(
            retry=WorkflowRetryPolicy(
                max_attempts=5,
                backoff=WorkflowRetryBackoff(
                    initial="PT5M", multiplier=10.0, max="PT15M"
                ),
            )
        ),
    )

    effective = resolve_workflow_task_execution(task)

    assert effective is not None
    assert sum(native_retry_delays_ceiling_ms(_widest_retry())) < 3_600_000
    assert effective["durable_retry_policy"]["retry_timeout_ms"] == 3_600_000


def _widest_retry() -> WorkflowRetryPolicy:
    return WorkflowRetryPolicy(
        max_attempts=5,
        backoff=WorkflowRetryBackoff(initial="PT5M", multiplier=10.0, max="PT15M"),
    )


def test_wait_tasks_may_not_declare_execution() -> None:
    with pytest.raises(PlanValidationError, match="not valid on type=wait"):
        validate_plan(
            {
                "tasks": [
                    {
                        "id": "pause",
                        "type": "wait",
                        "duration": "PT1S",
                        "execution": {"retry": {"max_attempts": 1}},
                    }
                ]
            },
            policy=WorkflowPlanPolicy(allowed_tools=frozenset()),
        )


def test_plan_flattening_attaches_only_resolved_policies() -> None:
    plan = validate_plan(
        {
            "tasks": [
                {"id": "plain", "type": "tool", "tool": "publish", "args": {}},
                {"id": "retried", "type": "tool", "tool": "publish", "args": {}},
            ]
        },
        policy=WorkflowPlanPolicy(allowed_tools=frozenset({"publish"})),
    )

    flattened = plan_to_activity_inputs(
        plan,
        {"retried": resolve_workflow_task_execution(plan.tasks[1], decorator_retry=_RETRY)},
    )

    assert "execution" not in flattened[0]
    assert flattened[1]["execution"]["max_attempts"] == 3


# ---- Durable mapping and the private failure bridge -------------------------


def test_durable_policy_mapping_preserves_the_authored_shape() -> None:
    policy = create_durable_retry_policy(_DURABLE_POLICY)

    assert policy.first_retry_interval == timedelta(seconds=1)
    assert policy.max_number_of_attempts == 3
    assert policy.backoff_coefficient == 2.0
    assert policy.max_retry_interval == timedelta(seconds=4)
    assert policy.retry_timeout == timedelta(hours=1)


@pytest.mark.asyncio
async def test_retryable_failure_raises_a_sanitized_private_marker() -> None:
    from azure_functions_agents import WorkflowRetryableError

    def handler(args: dict[str, Any]) -> None:
        raise WorkflowRetryableError("service_busy", "Try again.")

    with pytest.raises(DurableRetryableActivityError) as raised:
        await invoke_policy_handler(handler, {}, task=_native_task(), target="publish")

    assert json.loads(str(raised.value)) == {
        "version": 1,
        "outcome": {
            "id": "work",
            "ok": False,
            "failure": {
                "error_code": "service_busy",
                "error": "Try again.",
                "kind": "handler_transient",
                "retryable": True,
            },
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raised", "expected_kind"),
    [
        (RuntimeError("private connection string"), "execution_unknown"),
        (None, "handler_terminal"),
    ],
)
async def test_terminal_failure_is_returned_so_durable_does_not_retry(
    raised: Exception | None,
    expected_kind: str,
) -> None:
    from azure_functions_agents import WorkflowTerminalError

    error = raised if raised is not None else WorkflowTerminalError("rejected", "No.")

    def handler(args: dict[str, Any]) -> None:
        raise error

    outcome = await invoke_policy_handler(
        handler, {}, task=_native_task(), target="publish"
    )

    assert outcome["ok"] is False
    assert outcome["failure"]["kind"] == expected_kind
    assert outcome["failure"]["retryable"] is False
    assert "private connection string" not in outcome["failure"]["error"]


@pytest.mark.asyncio
async def test_non_serializable_result_is_a_terminal_contract_failure() -> None:
    outcome = await invoke_policy_handler(
        lambda args: object(), {}, task=_native_task(), target="publish"
    )

    assert outcome == {
        "id": "work",
        "ok": False,
        "failure": {
            "error_code": "workflow_task_handler_contract",
            "error": "Task handler returned an invalid result.",
            "kind": "handler_contract",
            "retryable": False,
        },
    }


@pytest.mark.asyncio
async def test_exhaustion_round_trips_only_this_runtimes_private_payload() -> None:
    from azure_functions_agents import WorkflowRetryableError

    def handler(args: dict[str, Any]) -> None:
        raise WorkflowRetryableError("service_busy", "Try again.")

    with pytest.raises(DurableRetryableActivityError) as raised:
        await invoke_policy_handler(handler, {}, task=_native_task(), target="publish")

    assert decode_durable_retry_failure(
        "work", TaskFailedError("Activity failed.", raised.value)
    ) == {
        "error_code": "service_busy",
        "error": "Try again.",
        "kind": "handler_transient",
        "retryable": True,
    }
    # A different node, an unrelated Durable failure, and a non-Durable error
    # must all keep their original failure rather than borrowing this one.
    assert (
        decode_durable_retry_failure(
            "other", TaskFailedError("Activity failed.", raised.value)
        )
        is None
    )
    assert (
        decode_durable_retry_failure(
            "work", TaskFailedError("Activity failed.", ValueError("private detail"))
        )
        is None
    )
    assert decode_durable_retry_failure("work", RuntimeError("boom")) is None


@pytest.mark.asyncio
async def test_task_context_exposes_an_attempt_stable_idempotency_key() -> None:
    observed: list[Any] = []

    def handler(args: dict[str, Any]) -> dict[str, bool]:
        observed.append(current_workflow_task_context())
        return {"ok": True}

    await invoke_policy_handler(handler, {}, task=_native_task(), target="publish")
    await invoke_policy_handler(handler, {}, task=_native_task(), target="publish")

    first, second = observed
    assert first.idempotency_key == second.idempotency_key
    assert first.idempotency_key == workflow_task_idempotency_key("workflow-1", "work")
    assert first.task_id == "work"
    assert first.max_attempts == 3
    assert current_workflow_task_context() is None


@pytest.mark.asyncio
async def test_malformed_persisted_policy_is_a_terminal_contract_failure() -> None:
    task = _native_task()
    task["execution"]["durable_retry_policy"]["max_number_of_attempts"] = 2
    activity = _tool_activity({"publish"})

    outcome = await activity({**task, "tool": "publish", "args": {}, **_AGENT})

    assert outcome["failure"]["kind"] == "handler_contract"


@pytest.mark.asyncio
async def test_persisted_policy_tolerates_keys_from_a_later_runtime() -> None:
    task = _native_task()
    task["execution"]["timeout_ms"] = 5_000
    activity = _tool_activity({"publish"})

    outcome = await activity({**task, "tool": "publish", "args": {}, **_AGENT})

    assert outcome == {"id": "work", "ok": True, "result": {"echoed": {}}}


@pytest.mark.asyncio
async def test_denied_target_returns_a_terminal_outcome_instead_of_retrying() -> None:
    activity = _tool_activity(frozenset())

    outcome = await activity({**_native_task(), "tool": "publish", "args": {}, **_AGENT})

    assert outcome["failure"] == {
        "error_code": "workflow_task_authorization",
        "error": "Task target is not authorized.",
        "kind": "authorization",
        "retryable": False,
    }


_AGENT = {"workflow_agent_slug": "coordinator"}


@pytest.fixture(autouse=True)
def _reset_registry():
    """Restore the process-global workflow tool registry around every test."""
    saved_entries = dict(registry._REGISTRY)
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(saved_entries)


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


# ---- orchestrator dispatch and replay compatibility -------------------------


class _RecordingTask:
    def __init__(self, result: Any = None) -> None:
        self.result = result
        self.is_completed = True

    def cancel(self) -> None:
        return None


class _RecordingContext:
    """Fake orchestration context recording which retry driver was selected."""

    def __init__(self, tasks: list[dict[str, Any]], results: dict[str, Any]) -> None:
        self.instance_id = "workflow-parent"
        self._input = {"workflow_agent_slug": "coordinator", "tasks": tasks}
        self._results = results
        self.plain: list[dict[str, Any]] = []
        self.retried: list[tuple[dict[str, Any], Any]] = []
        self.last_wave = _RecordingTask([])
        self.cancel_task = _RecordingTask()

    def get_input(self) -> dict[str, Any]:
        return self._input

    def wait_for_external_event(self, name: str) -> _RecordingTask:
        return self.cancel_task

    def call_activity(self, name: str, payload: dict[str, Any]) -> _RecordingTask:
        self.plain.append(payload)
        return _RecordingTask(self._results.get(payload["id"]))

    def call_activity_with_retry(
        self, name: str, retry_policy: Any, payload: dict[str, Any]
    ) -> _RecordingTask:
        self.retried.append((payload, retry_policy))
        return _RecordingTask(self._results.get(payload["id"]))

    def task_all(self, tasks: list[_RecordingTask]) -> _RecordingTask:
        self.last_wave = _RecordingTask([task.result for task in tasks])
        return self.last_wave

    def task_any(self, tasks: list[_RecordingTask]) -> _RecordingTask:
        selection = _RecordingTask()
        selection.candidates = list(tasks)
        return selection

    def set_custom_status(self, status: str) -> None:
        return None


def _orchestrate(context: _RecordingContext) -> dict[str, Any]:
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


def _tool_node(task_id: str, execution: dict[str, Any] | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": task_id,
        "type": TOOL_TASK_TYPE,
        "tool": "publish",
        "args": {},
        "depends_on": [],
    }
    if execution is not None:
        node["execution"] = execution
    return node


def test_persisted_policy_selects_the_durable_retry_driver() -> None:
    execution = {"max_attempts": 3, "durable_retry_policy": dict(_DURABLE_POLICY)}
    context = _RecordingContext(
        [_tool_node("work", execution)],
        {"work": {"id": "work", "ok": True, "result": {"published": True}}},
    )

    assert _orchestrate(context) == {"results": {"work": {"published": True}}}
    assert context.plain == []
    [(payload, retry_policy)] = context.retried
    assert payload["execution"] == execution
    assert payload["task_id"] == "work"
    assert retry_policy.max_number_of_attempts == 3


def test_history_without_a_persisted_policy_keeps_the_legacy_dispatch() -> None:
    """Replay safety: a tool that now declares retry must not change old histories."""
    registry.register_workflow_tool(
        "publish", "Publish", lambda args: args, retry=_RETRY
    )
    context = _RecordingContext(
        [_tool_node("work")],
        {"work": {"id": "work", "result": {"published": True}}},
    )

    assert _orchestrate(context) == {"results": {"work": {"published": True}}}
    assert context.retried == []
    [payload] = context.plain
    assert "execution" not in payload
    assert "task_id" not in payload


def test_data_driven_plans_use_the_same_retry_driver() -> None:
    """A ``when``/``for_each`` plan runs on the dynamic scheduler; retry still applies."""
    execution = {"max_attempts": 3, "durable_retry_policy": dict(_DURABLE_POLICY)}
    retried_node = _tool_node("work", execution)
    retried_node["depends_on"] = ["gate"]
    retried_node["when"] = {
        "ref": "${gate.result.go}",
        "operator": "equals",
        "value": True,
    }
    context = _RecordingContext(
        [_tool_node("gate"), retried_node],
        {
            "gate": {"id": "gate", "result": {"go": True}},
            "work": {"id": "work", "ok": True, "result": {"published": True}},
        },
    )
    context._input["policy"] = {
        "allowed_tools": ["publish"],
        "allowed_subagents": [],
    }

    assert _orchestrate(context) == {
        "results": {"gate": {"go": True}, "work": {"published": True}}
    }
    # The policy-free node keeps the legacy driver and envelope; only the node
    # whose policy was persisted is handed to Durable's retry driver.
    assert [payload["id"] for payload in context.plain] == ["gate"]
    [(payload, retry_policy)] = context.retried
    assert payload["id"] == "work"
    assert payload["execution"] == execution
    assert retry_policy.max_number_of_attempts == 3


def test_terminal_outcome_fails_the_workflow_with_the_application_error_code() -> None:
    context = _RecordingContext(
        [
            _tool_node(
                "work", {"max_attempts": 3, "durable_retry_policy": dict(_DURABLE_POLICY)}
            )
        ],
        {
            "work": {
                "id": "work",
                "ok": False,
                "failure": {
                    "error_code": "order_rejected",
                    "error": "The order was rejected.",
                    "kind": "handler_terminal",
                    "retryable": False,
                },
            }
        },
    )

    with pytest.raises(RuntimeError, match="order_rejected"):
        _orchestrate(context)


def test_exhausted_native_retry_reports_the_sanitized_application_failure() -> None:
    from azure_functions_agents import WorkflowRetryableError

    def handler(args: dict[str, Any]) -> None:
        raise WorkflowRetryableError("service_busy", "Try again.")

    with pytest.raises(DurableRetryableActivityError) as raised:
        asyncio.run(
            invoke_policy_handler(handler, {}, task=_native_task(), target="publish")
        )

    class _ExhaustedContext(_RecordingContext):
        def call_activity_with_retry(
            self, name: str, retry_policy: Any, payload: dict[str, Any]
        ) -> _RecordingTask:
            self.retried.append((payload, retry_policy))
            return _RecordingTask(TaskFailedError("Activity failed.", raised.value))

    context = _ExhaustedContext(
        [
            _tool_node(
                "work", {"max_attempts": 3, "durable_retry_policy": dict(_DURABLE_POLICY)}
            )
        ],
        {},
    )

    with pytest.raises(RuntimeError, match=r"Try again\. \(service_busy\)"):
        _orchestrate(context)


# ---- end-to-end submission --------------------------------------------------


@pytest.mark.asyncio
async def test_start_workflow_persists_the_tool_declared_retry() -> None:
    from azure_functions_agents.workflows import tools as workflow_tools

    @workflow_tool(description="Publish an order.", retry=_RETRY)
    def publish(args: dict[str, Any]) -> dict[str, Any]:
        return args

    catalog = integration.build_workflow_handler_catalog(
        [
            WorkflowTool(
                "publish",
                "Publish an order.",
                publish,
                retry=_RETRY,
            )
        ]
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
    response = await workflow_tools.start_workflow(
        workflow_tools.StartWorkflowParams.model_validate(
            {"tasks": [{"id": "work", "type": "tool", "tool": "publish", "args": {}}]}
        ),
        session,
        policy=WorkflowPlanPolicy(
            allowed_tools=frozenset({"publish"}),
            tool_execution={"publish": WorkflowToolExecutionPolicy(retry=catalog["publish"].retry)},
        ),
    )

    assert "workflow_id" in json.loads(response)
    [task] = started["tasks"]
    assert task["execution"] == {
        "max_attempts": 3,
        "durable_retry_policy": _DURABLE_POLICY,
    }
