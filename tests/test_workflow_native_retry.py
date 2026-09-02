"""Durable native Activity retry: declaration, dispatch, and replay compatibility."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from durabletask.task import TaskFailedError

from azure_functions_agents._function_tool import WorkflowTool
from azure_functions_agents.workflows import engine, integration, registry
from azure_functions_agents.workflows.activity import (
    handler_contract_failure,
    invoke_policy_handler,
    validate_activity_result,
)
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


@pytest.mark.parametrize("execution", [{}, None])
def test_execution_requires_an_explicit_retry_policy(execution: Any) -> None:
    with pytest.raises(PlanValidationError, match="execution"):
        validate_plan(
            {
                "tasks": [
                    {
                        "id": "work",
                        "type": "tool",
                        "tool": "publish",
                        "execution": execution,
                    }
                ]
            },
            policy=WorkflowPlanPolicy(allowed_tools=frozenset({"publish"})),
        )


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
    assert "retry_timeout_ms" not in effective["durable_retry_policy"]


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
                {
                    "id": "retried",
                    "type": "tool",
                    "tool": "publish",
                    "args": {},
                    "execution": {"retry": _RETRY.model_dump()},
                },
            ]
        },
        policy=WorkflowPlanPolicy(allowed_tools=frozenset({"publish"})),
    )

    flattened = plan_to_activity_inputs(
        plan,
        {"retried": resolve_workflow_task_execution(plan.tasks[1])},
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
    # A finite Durable timeout reads wall-clock time during history replay.
    assert policy.retry_timeout is None


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
async def test_truncated_retry_message_keeps_its_application_error_code() -> None:
    from azure_functions_agents import WorkflowRetryableError

    message = "x" * 255 + " " + "private suffix"

    def handler(args: dict[str, Any]) -> None:
        raise WorkflowRetryableError("service_busy", message)

    with pytest.raises(DurableRetryableActivityError) as raised:
        await invoke_policy_handler(handler, {}, task=_native_task(), target="publish")

    decoded = decode_durable_retry_failure(
        "work",
        TaskFailedError("Activity failed.", raised.value),
    )
    assert decoded is not None
    assert decoded["error_code"] == "service_busy"
    assert decoded["error"] == "x" * 255


def _private_failure_payload(outcome: Any, *, version: int = 1, **extra: Any) -> str:
    """Serialize a private exhaustion payload the way ``raise_for_durable_retry`` does."""
    return json.dumps(
        {"version": version, "outcome": outcome, **extra},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _durable_failure(message: str) -> TaskFailedError:
    """Wrap ``message`` so it reaches the decoder's payload-inspection branch.

    Durable rebuilds the failure from the raised exception's type and text, so
    constructing it from a real ``DurableRetryableActivityError`` is what makes
    ``details.is_caused_by()`` accept it — every case below therefore fails on
    the payload itself, not on the earlier type guard.
    """
    return TaskFailedError("Activity failed.", DurableRetryableActivityError(message))


_RETRYABLE_OUTCOME = {
    "id": "work",
    "ok": False,
    "failure": {
        "error_code": "service_busy",
        "error": "Try again.",
        "kind": "handler_transient",
        "retryable": True,
    },
}


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("malformed json", "not json at all"),
        (
            "wrong payload version",
            _private_failure_payload(_RETRYABLE_OUTCOME, version=2),
        ),
        (
            "extra payload key",
            _private_failure_payload(_RETRYABLE_OUTCOME, unexpected="value"),
        ),
        (
            "success outcome",
            _private_failure_payload({"id": "work", "ok": True, "result": {"done": True}}),
        ),
        (
            "non-retryable outcome",
            _private_failure_payload(
                {
                    "id": "work",
                    "ok": False,
                    "failure": {
                        "error_code": "order_rejected",
                        "error": "The order was rejected.",
                        "kind": "handler_terminal",
                        "retryable": False,
                    },
                }
            ),
        ),
    ],
)
def test_decoder_refuses_payloads_it_did_not_write(case: str, message: str) -> None:
    """Anything but this runtime's own retryable payload keeps the raw failure."""
    failure = _durable_failure(message)

    # Guard the guard: each case must actually reach the payload inspection.
    assert failure.details.is_caused_by(DurableRetryableActivityError), case
    assert decode_durable_retry_failure("work", failure) is None, case


def test_decoder_accepts_its_own_retryable_payload() -> None:
    """The positive control for the rejection cases above."""
    failure = _durable_failure(_private_failure_payload(_RETRYABLE_OUTCOME))

    assert decode_durable_retry_failure("work", failure) == _RETRYABLE_OUTCOME["failure"]


@pytest.mark.parametrize(
    "raw",
    [
        {
            "id": "work",
            "ok": False,
            "failure": {
                "error_code": "service_busy",
                "error": "Try again.",
                "kind": "handler_terminal",
                "retryable": True,
            },
        },
        {
            "id": "work",
            "ok": False,
            "failure": {
                "error_code": "Bad-Code",
                "error": "Try again.",
                "kind": "handler_transient",
                "retryable": True,
            },
        },
        {
            "id": "work",
            "ok": False,
            "failure": {
                "error_code": "service_busy",
                "error": " has leading whitespace",
                "kind": "handler_transient",
                "retryable": True,
            },
        },
        {"id": "work", "ok": True, "result": {}, "extra": True},
    ],
)
def test_activity_result_rejects_untrusted_worker_claims(raw: dict[str, Any]) -> None:
    assert validate_activity_result("work", raw) == (False, handler_contract_failure())


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
async def test_context_is_reset_when_a_retryable_failure_becomes_the_marker() -> None:
    """The contextvar must not leak when the handler exits via the retry marker."""
    from azure_functions_agents import WorkflowRetryableError

    observed: list[Any] = []

    def handler(args: dict[str, Any]) -> None:
        observed.append(current_workflow_task_context())
        raise WorkflowRetryableError("service_busy", "Try again.")

    with pytest.raises(DurableRetryableActivityError):
        await invoke_policy_handler(handler, {}, task=_native_task(), target="publish")

    assert observed[0] is not None
    assert current_workflow_task_context() is None


@pytest.mark.asyncio
async def test_cancellation_propagates_unchanged_and_resets_the_context() -> None:
    """Cancellation is not an application failure, so it must not become a retry."""
    observed: list[Any] = []

    def handler(args: dict[str, Any]) -> None:
        observed.append(current_workflow_task_context())
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await invoke_policy_handler(handler, {}, task=_native_task(), target="publish")

    assert observed[0] is not None
    assert current_workflow_task_context() is None


@pytest.mark.asyncio
async def test_malformed_persisted_policy_is_a_terminal_contract_failure() -> None:
    task = _native_task()
    task["execution"]["durable_retry_policy"]["max_number_of_attempts"] = 2
    activity = _tool_activity({"publish"})

    outcome = await activity({**task, "tool": "publish", "args": {}, **_AGENT})

    assert outcome["failure"]["kind"] == "handler_contract"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "policy_overrides", "execution_overrides"),
    [
        (
            "multi-attempt policy has no first interval",
            {"first_retry_interval_ms": 0},
            {},
        ),
        (
            "maximum interval is below the first",
            {"first_retry_interval_ms": 4_000, "max_retry_interval_ms": 1_000},
            {},
        ),
        (
            "single attempt still configures backoff",
            {"max_number_of_attempts": 1},
            {"max_attempts": 1},
        ),
    ],
)
async def test_inconsistent_persisted_schedule_is_a_terminal_contract_failure(
    case: str,
    policy_overrides: dict[str, Any],
    execution_overrides: dict[str, Any],
) -> None:
    """A self-inconsistent persisted policy fails closed instead of being repaired."""
    task = _native_task()
    task["execution"].update(execution_overrides)
    task["execution"]["durable_retry_policy"].update(policy_overrides)
    activity = _tool_activity({"publish"})

    outcome = await activity({**task, "tool": "publish", "args": {}, **_AGENT})

    assert outcome["ok"] is False, case
    assert outcome["failure"]["kind"] == "handler_contract", case
    # Terminal, so Durable is never asked to retry a history it cannot trust.
    assert outcome["failure"]["retryable"] is False, case


@pytest.mark.asyncio
async def test_persisted_policy_tolerates_keys_from_a_later_runtime() -> None:
    task = _native_task()
    task["execution"]["timeout_ms"] = 5_000
    task["execution"]["durable_retry_policy"]["retry_timeout_ms"] = 60_000
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
        self._result = result
        self.is_complete = True

    @property
    def result(self) -> Any:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

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
        self.activity_tags: list[tuple[str, dict[str, str]]] = []
        self.last_wave = _RecordingTask([])
        self.cancel_task = _RecordingTask()

    def get_input(self) -> dict[str, Any]:
        return self._input

    def wait_for_external_event(self, name: str) -> _RecordingTask:
        return self.cancel_task

    def call_activity(
        self,
        name: str,
        *,
        input: dict[str, Any],
        retry_policy: Any = None,
        tags: dict[str, str],
    ) -> _RecordingTask:
        self.activity_tags.append((name, tags))
        if retry_policy is None:
            self.plain.append(input)
        else:
            self.retried.append((input, retry_policy))
        return _RecordingTask(self._results.get(input["id"]))

    def task_any(self, tasks: list[_RecordingTask]) -> _RecordingTask:
        selection = _RecordingTask()
        selection.candidates = list(tasks)
        self.last_wave = selection
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
    generator = orchestrator(context, context._input)
    try:
        selection = next(generator)
        while True:
            candidates = getattr(selection, "_tasks", None)
            winner = (
                next(task for task in candidates if task is not context.cancel_task)
                if candidates is not None
                else selection
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
    assert context.activity_tags == [
        (engine._ACTIVITY_NAME, {"durabletask.displayName": "publish"})
    ]


def test_history_without_a_persisted_policy_keeps_the_legacy_dispatch() -> None:
    """Replay safety: current code must not change old policy-free histories."""
    context = _RecordingContext(
        [_tool_node("work")],
        {"work": {"id": "work", "result": {"published": True}}},
    )

    assert _orchestrate(context) == {"results": {"work": {"published": True}}}
    assert context.retried == []
    [payload] = context.plain
    assert "execution" not in payload
    assert "task_id" not in payload
    assert context.activity_tags == [
        (engine._ACTIVITY_NAME, {"durabletask.displayName": "publish"})
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("policy_aware", [False, True])
async def test_sync_tool_handlers_run_off_the_activity_event_loop(
    policy_aware: bool,
) -> None:
    event_loop_thread = threading.get_ident()
    handler_threads: list[int] = []

    def publish(args: dict[str, Any]) -> dict[str, bool]:
        handler_threads.append(threading.get_ident())
        return {"published": True}

    catalog = integration.build_workflow_handler_catalog(
        [WorkflowTool("publish", "Publish", publish)]
    )
    app = _Blueprints()
    engine.register_workflows(
        app,
        handler_catalog=catalog,
        workflow_agent_policies={
            "coordinator": WorkflowPlanPolicy(allowed_tools=frozenset({"publish"}))
        },
    )
    activity = app.function("agents_workflow_run_tool")
    task = {
        "id": "work",
        "workflow_id": "workflow-1",
        "workflow_agent_slug": "coordinator",
        "tool": "publish",
        "args": {},
    }
    if policy_aware:
        task.update(_native_task())

    await activity(task)

    assert handler_threads
    assert handler_threads[0] != event_loop_thread


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
    assert context.activity_tags == [
        (engine._ACTIVITY_NAME, {"durabletask.displayName": "publish"}),
        (engine._ACTIVITY_NAME, {"durabletask.displayName": "publish"}),
    ]


def test_for_each_instances_each_use_the_persisted_retry_driver() -> None:
    execution = {"max_attempts": 3, "durable_retry_policy": dict(_DURABLE_POLICY)}
    expanded = _tool_node("work", execution)
    expanded["depends_on"] = ["discover"]
    expanded["for_each"] = "${discover.result.items}"
    expanded["args"] = {"item": "${item}"}
    context = _RecordingContext(
        [_tool_node("discover"), expanded],
        {
            "discover": {"id": "discover", "result": {"items": ["a", "b"]}},
            "work[0]": {"id": "work[0]", "ok": True, "result": {"item": "a"}},
            "work[1]": {"id": "work[1]", "ok": True, "result": {"item": "b"}},
        },
    )
    context._input["policy"] = {
        "allowed_tools": ["publish"],
        "allowed_subagents": [],
    }

    result = _orchestrate(context)

    assert [payload["id"] for payload, _ in context.retried] == ["work[0]", "work[1]"]
    assert [payload["task_id"] for payload, _ in context.retried] == ["work", "work"]
    assert context.activity_tags == [
        (engine._ACTIVITY_NAME, {"durabletask.displayName": "publish"}),
        (engine._ACTIVITY_NAME, {"durabletask.displayName": "publish"}),
        (engine._ACTIVITY_NAME, {"durabletask.displayName": "publish"}),
    ]
    assert result["results"]["work"] == [
        {"index": 0, "status": "completed", "result": {"item": "a"}},
        {"index": 1, "status": "completed", "result": {"item": "b"}},
    ]


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
        def call_activity(
            self,
            name: str,
            *,
            input: dict[str, Any],
            retry_policy: Any = None,
            tags: dict[str, str],
        ) -> _RecordingTask:
            assert retry_policy is not None
            self.retried.append((input, retry_policy))
            self.activity_tags.append((name, tags))
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
async def test_start_workflow_persists_only_plan_authored_retry() -> None:
    from azure_functions_agents.workflows import tools as workflow_tools

    started: dict[str, Any] = {}

    class _Client:
        async def get_status_all(self) -> list[Any]:
            return []

        async def schedule_new_orchestration(
            self,
            name: str,
            *,
            instance_id: str,
            input: Any,
            tags: dict[str, str],
        ) -> str:
            started.update(input)
            started["tags"] = tags
            return instance_id

    session = workflow_tools.WorkflowSessionContext(
        workflow_agent_slug="coordinator",
        session_id="session-1",
        agent_name="main",
        durable_client=_Client(),  # type: ignore[arg-type]
    )
    response = await workflow_tools.start_workflow(
        workflow_tools.StartWorkflowParams.model_validate(
            {
                "tasks": [
                    {
                        "id": "work",
                        "type": "tool",
                        "tool": "publish",
                        "args": {},
                        "execution": {"retry": _RETRY.model_dump()},
                    },
                    {
                        "id": "plain",
                        "type": "tool",
                        "tool": "publish",
                        "args": {},
                        "depends_on": ["work"],
                    },
                    {
                        "id": "analyze",
                        "type": "sub_agent",
                        "agent": "analyst",
                        "task": "Analyze the reservation.",
                        "depends_on": ["work"],
                        "execution": {"retry": _RETRY.model_dump()},
                    },
                ]
            }
        ),
        session,
        policy=WorkflowPlanPolicy(
            allowed_tools=frozenset({"publish"}),
            allowed_subagents=frozenset({"analyst"}),
        ),
    )

    assert "workflow_id" in json.loads(response)
    retried, plain, subagent = started["tasks"]
    assert retried["execution"] == {
        "max_attempts": 3,
        "durable_retry_policy": _DURABLE_POLICY,
    }
    assert "execution" not in plain
    assert started["tags"] == {"durabletask.displayName": "main-orchestration"}
    assert subagent["execution"] == retried["execution"]
