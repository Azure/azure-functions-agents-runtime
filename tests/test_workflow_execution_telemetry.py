"""Observability for the workflow task execution policy (FRD 0004).

Two surfaces, deliberately separated:

- the ``workflow.task.activity`` span an *Activity* emits for one delivery, and
- the versioned structured ``custom_status`` the *orchestrator* publishes.

The split matters for replay: the orchestrator re-executes from history on every
replay, so nothing that emits telemetry may live there, and everything the status
reports must be a pure function of persisted history.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from durabletask.task import TaskFailedError

import azure_functions_agents._observability as obs
from azure_functions_agents._function_tool import WorkflowTool
from azure_functions_agents.workflows import engine, integration
from azure_functions_agents.workflows.native_retry import DurableRetryableActivityError
from azure_functions_agents.workflows.schema import (
    MAX_NODES,
    MAX_PARALLELISM,
    TOOL_TASK_TYPE,
    WorkflowPlanPolicy,
    WorkflowRetryableError,
    WorkflowTerminalError,
)

_SINGLE_ATTEMPT_POLICY = {
    "first_retry_interval_ms": 0,
    "max_number_of_attempts": 1,
    "backoff_coefficient": 1.0,
    "max_retry_interval_ms": 0,
    "retry_timeout_ms": 3_600_000,
}
_MULTI_ATTEMPT_POLICY = {
    "first_retry_interval_ms": 1_000,
    "max_number_of_attempts": 3,
    "backoff_coefficient": 2.0,
    "max_retry_interval_ms": 4_000,
    "retry_timeout_ms": 3_600_000,
}


def _execution(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "max_attempts": 1,
        "durable_retry_policy": dict(_SINGLE_ATTEMPT_POLICY),
    }
    payload.update(overrides)
    return payload


def _retry_execution(**overrides: Any) -> dict[str, Any]:
    return _execution(
        max_attempts=3,
        durable_retry_policy=dict(_MULTI_ATTEMPT_POLICY),
        **overrides,
    )


# ---- span capture -----------------------------------------------------------


class _RecordedSpan:
    def __init__(self, name: str) -> None:
        self.name = name
        self.attributes: dict[str, Any] = {}
        self.exceptions: list[BaseException] = []

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_status(self, status: Any) -> None:
        return None

    def record_exception(self, exc: BaseException) -> None:
        self.exceptions.append(exc)


@contextmanager
def _capture_spans(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[_RecordedSpan]]:
    """Run the real ``start_span`` against a recording tracer."""
    spans: list[_RecordedSpan] = []

    class _Tracer:
        @contextmanager
        def start_as_current_span(self, name: str) -> Iterator[_RecordedSpan]:
            span = _RecordedSpan(name)
            spans.append(span)
            yield span

    monkeypatch.setattr(obs, "_enabled", True)
    monkeypatch.setattr(obs, "_metrics_ready", True)
    monkeypatch.setattr(obs, "_workflow_task_start_counter", None)
    monkeypatch.setattr(obs, "_workflow_task_completion_counter", None)
    monkeypatch.setattr(obs, "get_tracer", lambda: _Tracer())
    yield spans


def _task_span(spans: list[_RecordedSpan]) -> _RecordedSpan:
    [span] = [span for span in spans if span.name == "workflow.task.activity"]
    return span


# ---- Activity telemetry -----------------------------------------------------


@pytest.mark.asyncio
async def test_a_successful_delivery_is_recorded_without_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activity = _tool_activity(lambda args: {"ok": True})

    with _capture_spans(monkeypatch) as spans:
        outcome = await activity(_activity_task(execution=_retry_execution(timeout_ms=5_000)))

    assert outcome["ok"] is True
    span = _task_span(spans)
    assert span.attributes == {
        "af.workflow_task.workflow_id": "workflow-1",
        "af.workflow_task.task_id": "work",
        "af.workflow_task.node_instance_id": "work",
        "af.workflow_task.target_type": "tool",
        "af.workflow_task.target_name": "publish",
        "af.workflow_task.max_attempts": 3,
        "af.workflow_task.timeout_ms": 5_000,
        "af.workflow_task.continue_on_error": False,
        "af.workflow_task.retry_driver": "durable",
        "af.workflow_task.outcome_kind": "success",
        "af.workflow_task.disposition": "return_result",
    }
    assert obs.ATTR_FAULT_DOMAIN not in span.attributes
    assert span.exceptions == []


@pytest.mark.asyncio
async def test_a_retryable_failure_is_recorded_before_the_durable_marker_is_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The private retry marker must not be recorded as a span exception.

    A recorded exception per attempt would make every declared retry look like
    an unhandled runtime fault in the trace.
    """

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        raise WorkflowRetryableError("inventory_busy", "Try again shortly.")

    activity = _tool_activity(handler)

    with (
        _capture_spans(monkeypatch) as spans,
        pytest.raises(DurableRetryableActivityError),
    ):
        await activity(_activity_task(execution=_retry_execution()))

    span = _task_span(spans)
    assert span.attributes["af.workflow_task.outcome_kind"] == "handler_transient"
    assert span.attributes["af.workflow_task.disposition"] == "request_durable_retry"
    assert span.attributes["af.workflow_task.error_code"] == "inventory_busy"
    assert span.attributes[obs.ATTR_FAULT_DOMAIN] == obs.FaultDomain.APP
    assert span.exceptions == []


@pytest.mark.asyncio
async def test_a_terminal_failure_is_recorded_as_an_application_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(args: dict[str, Any]) -> dict[str, Any]:
        raise WorkflowTerminalError("order_rejected", "The order was rejected.")

    activity = _tool_activity(handler)

    with _capture_spans(monkeypatch) as spans:
        outcome = await activity(_activity_task())

    assert outcome["ok"] is False
    span = _task_span(spans)
    assert span.attributes["af.workflow_task.outcome_kind"] == "handler_terminal"
    assert span.attributes["af.workflow_task.disposition"] == "return_failure"
    assert span.attributes[obs.ATTR_FAULT_DOMAIN] == obs.FaultDomain.APP


@pytest.mark.asyncio
async def test_an_authorization_denial_is_recorded_as_a_runtime_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A denied target never reaches the handler, so it needs its own span."""
    activity = _tool_activity(lambda args: {"ok": True}, allowed_tools=())

    with _capture_spans(monkeypatch) as spans:
        outcome = await activity(_activity_task())

    assert outcome["failure"]["kind"] == "authorization"
    span = _task_span(spans)
    assert span.attributes["af.workflow_task.target_name"] == "publish"
    assert span.attributes["af.workflow_task.disposition"] == "return_failure"
    assert span.attributes[obs.ATTR_FAULT_DOMAIN] == obs.FaultDomain.RUNTIME


@pytest.mark.asyncio
async def test_malformed_policy_input_is_recorded_without_trusting_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activity = _tool_activity(lambda args: {"ok": True})
    task = _activity_task()
    task["execution"] = {"max_attempts": 99}

    with _capture_spans(monkeypatch) as spans:
        outcome = await activity(task)

    assert outcome["failure"]["kind"] == "handler_contract"
    span = _task_span(spans)
    assert span.attributes["af.workflow_task.node_instance_id"] == "work"
    # ``max_attempts`` is outside its validated domain, so it is dropped rather
    # than reported: it is a metric dimension, and this is the one path whose
    # payload is by definition the one that just failed validation.
    assert "af.workflow_task.max_attempts" not in span.attributes
    assert span.attributes[obs.ATTR_FAULT_DOMAIN] == obs.FaultDomain.RUNTIME


@pytest.mark.asyncio
async def test_out_of_domain_policy_fields_never_become_metric_dimensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dimensions: list[dict[str, Any]] = []
    activity = _tool_activity(lambda args: {"ok": True})
    task = _activity_task()
    task["execution"] = {
        "max_attempts": 12_345,
        "continue_on_error": "sometimes",
        "timeout_ms": -1,
    }

    with _capture_spans(monkeypatch):
        monkeypatch.setattr(
            obs,
            "_workflow_task_start_counter",
            SimpleNamespace(add=lambda count, attrs: dimensions.append(attrs)),
        )
        monkeypatch.setattr(
            obs,
            "_workflow_task_completion_counter",
            SimpleNamespace(add=lambda count, attrs: dimensions.append(attrs)),
        )
        await activity(task)

    assert dimensions
    for attrs in dimensions:
        assert "af.workflow_task.max_attempts" not in attrs
        assert "af.workflow_task.continue_on_error" not in attrs
        assert attrs["af.workflow_task.target_type"] == "tool"


@pytest.mark.asyncio
async def test_in_domain_policy_fields_survive_an_early_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A denied delivery still reports the policy it was actually carrying."""
    activity = _tool_activity(lambda args: {"ok": True}, allowed_tools=())

    with _capture_spans(monkeypatch) as spans:
        await activity(_activity_task(execution=_retry_execution(timeout_ms=5_000)))

    span = _task_span(spans)
    assert span.attributes["af.workflow_task.max_attempts"] == 3
    assert span.attributes["af.workflow_task.timeout_ms"] == 5_000
    # A default ``continue_on_error`` is not persisted, and the early path
    # reports the payload rather than re-deriving the effective policy.
    assert "af.workflow_task.continue_on_error" not in span.attributes

    with _capture_spans(monkeypatch) as spans:
        await activity(_activity_task(execution=_execution(continue_on_error=True)))

    assert _task_span(spans).attributes["af.workflow_task.continue_on_error"] is True


@pytest.mark.asyncio
async def test_the_attempt_number_is_never_attached_to_a_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Durable owns the attempt budget and does not report the current attempt."""
    activity = _tool_activity(lambda args: {"ok": True})

    with _capture_spans(monkeypatch) as spans:
        await activity(_activity_task(execution=_retry_execution()))

    assert not any(
        key.endswith(".attempt") for key in _task_span(spans).attributes
    )


@pytest.mark.asyncio
async def test_a_legacy_task_emits_no_workflow_task_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A history written before the policy existed keeps its exact behavior."""
    activity = _tool_activity(lambda args: {"ok": True})
    task = _activity_task()
    task.pop("execution")
    task.pop("task_id")

    with _capture_spans(monkeypatch) as spans:
        assert await activity(task) == {"id": "work", "result": {"ok": True}}

    assert [span for span in spans if span.name == "workflow.task.activity"] == []


# ---- structured status ------------------------------------------------------


def test_a_plan_without_a_frozen_policy_keeps_the_v2_status() -> None:
    context = _Context(
        [
            _tool_node("seed"),
            {**_tool_node("fan", depends_on=["seed"]), "for_each": "${seed.result.items}"},
        ],
        {
            "seed": {"id": "seed", "result": {"items": [1, 2]}},
            "fan[0]": {"id": "fan[0]", "result": {"n": 1}},
            "fan[1]": {"id": "fan[1]", "result": {"n": 2}},
        },
    )

    _orchestrate(context)

    assert {status["schema_version"] for status in context.statuses} == {2}
    assert "retry_driver" not in context.statuses[-1]
    assert set(context.statuses[-1]["counts"]) == {
        "logical_total",
        "materialized_total",
        "completed",
        "skipped",
        "running",
    }


def test_a_policy_aware_plan_publishes_v3_with_the_durable_retry_driver() -> None:
    context = _Context(
        [
            _tool_node("seed"),
            _gated(_tool_node("work", _retry_execution(timeout_ms=5_000), depends_on=["seed"])),
            _tool_node("after", depends_on=["work"]),
        ],
        {
            "seed": {"id": "seed", "result": {"ready": True}},
            "work": {"id": "work", "ok": True, "result": {"done": True}},
            "after": {"id": "after", "result": {"ran": True}},
        },
    )

    _orchestrate(context)

    final = context.statuses[-1]
    assert final["schema_version"] == 3
    assert final["retry_driver"] == "durable"
    assert final["counts"]["completed"] == 3
    assert final["counts"]["failed"] == 0
    assert final["counts"]["failed_continued"] == 0
    # The declared budget is disclosed; the attempt in flight is not.
    assert final["nodes"]["work"] == {"state": "completed", "max_attempts": 3}
    assert final["nodes"]["after"] == {"state": "completed"}


def test_instances_queued_behind_max_parallelism_are_reported_as_pending() -> None:
    items = list(range(MAX_PARALLELISM + 3))
    context = _Context(
        [
            _tool_node("seed"),
            {
                **_tool_node("fan", _execution(), depends_on=["seed"]),
                "for_each": "${seed.result.items}",
            },
        ],
        {
            "seed": {"id": "seed", "result": {"items": items}},
            **{
                f"fan[{index}]": {"id": f"fan[{index}]", "ok": True, "result": {}}
                for index in items
            },
        },
    )

    _orchestrate(context)

    assert any(status["counts"]["pending"] == 3 for status in context.statuses)
    assert context.statuses[-1]["counts"]["pending"] == 0


def test_a_continued_failure_is_reported_without_changing_the_aggregate() -> None:
    context = _Context(
        [
            _tool_node("seed"),
            {
                **_tool_node(
                    "fan",
                    _execution(continue_on_error=True),
                    depends_on=["seed"],
                ),
                "for_each": "${seed.result.items}",
            },
        ],
        {
            "seed": {"id": "seed", "result": {"items": [1, 2]}},
            "fan[0]": {"id": "fan[0]", "ok": True, "result": {"n": 1}},
            "fan[1]": _terminal_failure("fan[1]"),
        },
    )

    outcome = _orchestrate(context)

    final = context.statuses[-1]
    assert final["counts"]["failed_continued"] == 1
    assert final["counts"]["completed"] == 2  # seed + fan[0]
    assert final["nodes"]["fan"]["instances"]["fan[1]"] == {
        "state": "failed_continued",
        "max_attempts": 1,
        "last_failure_kind": "handler_terminal",
        "last_error_code": "order_rejected",
    }
    # The committed fan-in contract is unchanged: the instance ran to a result.
    assert [entry["status"] for entry in outcome["results"]["fan"]] == [
        "completed",
        "completed",
    ]


def test_a_continued_normal_node_is_projected_without_a_new_scheduler_state() -> None:
    context = _Context(
        [
            _tool_node("seed"),
            _gated(
                _tool_node("work", _execution(continue_on_error=True), depends_on=["seed"])
            ),
            _tool_node("after", depends_on=["work"]),
        ],
        {
            "seed": {"id": "seed", "result": {"ready": True}},
            "work": _terminal_failure("work"),
            "after": {"id": "after", "result": {"ran": True}},
        },
    )

    outcome = _orchestrate(context)

    assert context.statuses[-1]["nodes"]["work"] == {
        "state": "failed_continued",
        "max_attempts": 1,
        "last_failure_kind": "handler_terminal",
        "last_error_code": "order_rejected",
    }
    assert outcome["results"]["work"]["failed"] is True
    assert outcome["results"]["after"] == {"ran": True}


def test_the_node_that_failed_the_workflow_is_named_in_the_final_status() -> None:
    context = _Context(
        [
            _tool_node("seed"),
            _gated(_tool_node("work", _execution(), depends_on=["seed"])),
        ],
        {
            "seed": {"id": "seed", "result": {"ready": True}},
            "work": _terminal_failure("work"),
        },
    )

    with pytest.raises(RuntimeError, match="order_rejected"):
        _orchestrate(context)

    final = context.statuses[-1]
    assert final["nodes"]["work"]["state"] == "failed"
    assert final["counts"]["failed"] == 1


def test_an_exhausted_native_retry_is_named_in_the_final_status() -> None:
    exhausted = TaskFailedError(
        "Activity failed.",
        DurableRetryableActivityError(_retryable_marker("work")),
    )
    context = _Context(
        [
            _tool_node("seed"),
            _gated(_tool_node("work", _retry_execution(), depends_on=["seed"])),
        ],
        {"seed": {"id": "seed", "result": {"ready": True}}, "work": exhausted},
    )

    with pytest.raises(RuntimeError, match="service_busy"):
        _orchestrate(context)

    assert context.statuses[-1]["nodes"]["work"]["state"] == "failed"


def test_an_unclassified_durable_failure_is_still_attributed_to_its_node() -> None:
    """``_await_wave`` reports per-node outcomes, so no decode is needed."""
    context = _Context(
        [
            _tool_node("seed"),
            _gated(_tool_node("work", _execution(), depends_on=["seed"])),
        ],
        {
            "seed": {"id": "seed", "result": {"ready": True}},
            "work": TaskFailedError("Activity failed.", ValueError("private detail")),
        },
    )

    with pytest.raises(TaskFailedError):
        _orchestrate(context)

    final = context.statuses[-1]
    assert final["nodes"]["work"]["state"] == "failed"
    assert final["counts"]["failed"] == 1
    # The opaque Durable error stays the authority on *why*: nothing is invented
    # for a failure that carries no sanitized classification.
    assert "last_error_code" not in final["nodes"]["work"]


def test_a_fully_expanded_status_stays_inside_the_durable_size_limit() -> None:
    """A maximal expansion must not overflow Durable's 16 KB custom status."""
    items = list(range(MAX_NODES - 1))
    long_id = "f" * 60
    context = _Context(
        [
            _tool_node("seed"),
            {
                **_tool_node(
                    long_id,
                    _retry_execution(continue_on_error=True),
                    depends_on=["seed"],
                ),
                "for_each": "${seed.result.items}",
            },
        ],
        {
            "seed": {"id": "seed", "result": {"items": items}},
            **{
                f"{long_id}[{index}]": _terminal_failure(f"{long_id}[{index}]")
                for index in items
            },
        },
    )

    _orchestrate(context)

    final = context.statuses[-1]
    instances = final["nodes"][long_id]["instances"]
    detailed = [
        instance for instance in instances.values() if "last_error_code" in instance
    ]
    assert len(detailed) == engine._STATUS_FAILURE_DETAIL_LIMIT
    # Deterministic truncation: the cap follows index order, so the same history
    # reproduces the same status on replay.
    assert [
        instance_id for instance_id, value in instances.items() if "last_error_code" in value
    ] == [f"{long_id}[{index}]" for index in items[: engine._STATUS_FAILURE_DETAIL_LIMIT]]
    assert len(json.dumps(final)) < 16_384


def test_a_policy_aware_static_plan_keeps_its_string_status() -> None:
    """Only the dynamic scheduler publishes structured status."""
    context = _Context(
        [_tool_node("work", _execution())],
        {"work": {"id": "work", "ok": True, "result": {}}},
    )

    _orchestrate(context)

    assert all(isinstance(status, str) for status in context.statuses)


# ---- harness ----------------------------------------------------------------


def _activity_task(**overrides: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": "work",
        "tool": "publish",
        "args": {},
        "workflow_id": "workflow-1",
        "workflow_agent_slug": "coordinator",
        "task_id": "work",
        "execution": _execution(),
    }
    task.update(overrides)
    return task


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


def _retryable_marker(node_id: str) -> str:
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


def _gated(node: dict[str, Any]) -> dict[str, Any]:
    """Attach a satisfied predicate so the plan selects the dynamic scheduler.

    Structured ``custom_status`` is a dynamic-scheduler surface; a fully static
    plan keeps its legacy string status.
    """
    return {
        **node,
        "when": {"ref": "${seed.result.ready}", "operator": "equals", "value": True},
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
        self.statuses: list[Any] = []

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
        self.statuses.append(status)


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


def _tool_activity(
    handler: Callable[[dict[str, Any]], Any],
    *,
    allowed_tools: tuple[str, ...] = ("publish",),
) -> Callable[..., Any]:
    app = _Blueprints()
    engine.register_workflows(
        app,
        handler_catalog=integration.build_workflow_handler_catalog(
            [WorkflowTool("publish", "Publish", handler)]
        ),
        workflow_agent_policies={
            "coordinator": WorkflowPlanPolicy(allowed_tools=frozenset(allowed_tools))
        },
    )
    return app.function("agents_workflow_run_tool")


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
            # ``_await_wave`` selects over the individual wave tasks, so the
            # driver resolves one leaf at a time in candidate order.
            winner = next(
                task for task in selection.candidates if task is not context.cancel_task
            )
            selection = generator.send(winner)
    except StopIteration as stop:
        return stop.value
