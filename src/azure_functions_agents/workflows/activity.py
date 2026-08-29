"""Policy-aware Activity execution and the retry failure contract.

A task carries an ``execution`` payload only when its retry policy was frozen at
submission time. Those Activity deliveries use a structured outcome envelope
(``{"id", "ok": true, "result"}`` / ``{"id", "ok": false, "failure"}``) instead
of the legacy ``{"id", "result"}`` shape, because Durable native retry needs the
Activity to distinguish "retry me" (raise) from "this is terminal" (return).

Tasks without a persisted ``execution`` payload keep the legacy shape untouched
so histories written before this runtime version replay unchanged.

A persisted ``timeout_ms`` bounds *the attempt the orchestration waits for*, not
the worker: workflow tool handlers are synchronous and run on a worker thread,
and a thread cannot be cancelled from the outside. When an attempt deadline
expires the delivery reports a retryable timeout immediately while the handler
may still be running. That is the same at-least-once exposure Durable already
has for a redelivered Activity, and the mitigation is the same: a handler with
side effects keys them on
:attr:`~azure_functions_agents.WorkflowTaskContext.idempotency_key`.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections.abc import Mapping
from typing import Any, Literal, TypedDict, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from azure_functions_agents._logger import logger
from azure_functions_agents._observability import (
    FaultDomain,
    WorkflowTaskActivityTelemetry,
    workflow_task_activity_telemetry,
)

from .context import (
    WorkflowTaskContext,
    _reset_workflow_task_context,
    _set_workflow_task_context,
    workflow_task_idempotency_key,
)
from .schema import (
    MAX_BACKOFF_MS,
    MAX_POLICY_ATTEMPTS,
    MAX_POLICY_ELAPSED_MS,
    MAX_POLICY_TIMEOUT_MS,
    MIN_POLICY_TIMEOUT_MS,
    EffectiveWorkflowTaskExecution,
    WorkflowRetryableError,
    WorkflowTerminalError,
)

type ActivityFailureKind = Literal[
    "timeout",
    "handler_transient",
    "handler_terminal",
    "execution_unknown",
    "handler_contract",
    "authorization",
]


class WorkflowTaskTimeoutError(Exception):
    """Internal signal that a nested execution bound expired inside one attempt.

    A Workflow Sub Agent already carries its own resolved agent timeout, which
    can be tighter than the attempt deadline. Raising this from that inner bound
    keeps both timeouts on the same classification instead of degrading the
    inner one to an unknown execution failure.
    """



class ActivityFailure(TypedDict):
    error_code: str
    error: str
    kind: ActivityFailureKind
    retryable: bool


class ActivitySuccessOutcome(TypedDict):
    id: str
    ok: Literal[True]
    result: Any


class ActivityFailureOutcome(TypedDict):
    id: str
    ok: Literal[False]
    failure: ActivityFailure


type ActivityOutcome = ActivitySuccessOutcome | ActivityFailureOutcome


class DurableRetryPolicyModel(BaseModel):
    """Strict Activity-side validation of the persisted native retry marker."""

    # ``extra="ignore"``: this shape is read back out of Durable history, so a
    # payload written by a newer runtime must still validate against this one.
    model_config = ConfigDict(
        extra="ignore",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )

    first_retry_interval_ms: int = Field(ge=0, le=MAX_BACKOFF_MS)
    max_number_of_attempts: int = Field(ge=1, le=MAX_POLICY_ATTEMPTS)
    backoff_coefficient: float = Field(ge=1.0, le=10.0, allow_inf_nan=False)
    max_retry_interval_ms: int = Field(ge=0, le=MAX_BACKOFF_MS)
    retry_timeout_ms: int = Field(ge=1, le=MAX_POLICY_ELAPSED_MS)


class EffectiveExecutionModel(BaseModel):
    """Strict Activity-side validation of the persisted execution policy."""

    # ``extra="ignore"``: see DurableRetryPolicyModel. Fields added by a later
    # runtime version must also be optional there, so that a history written by
    # this version keeps validating after an upgrade.
    model_config = ConfigDict(
        extra="ignore",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )

    max_attempts: int = Field(ge=1, le=MAX_POLICY_ATTEMPTS)
    durable_retry_policy: DurableRetryPolicyModel
    # Optional so a history written before per-attempt deadlines existed still
    # validates; ``None`` reproduces that history's unbounded-attempt behavior.
    timeout_ms: int | None = Field(
        default=None,
        ge=MIN_POLICY_TIMEOUT_MS,
        le=MAX_POLICY_TIMEOUT_MS,
    )
    continue_on_error: bool = False

    @model_validator(mode="after")
    def validate_schedule(self) -> EffectiveExecutionModel:
        durable = self.durable_retry_policy
        if durable.max_number_of_attempts != self.max_attempts:
            raise ValueError("Durable maximum attempts must match execution policy")
        if durable.retry_timeout_ms != MAX_POLICY_ELAPSED_MS:
            raise ValueError("Durable retry timeout must match the runtime elapsed limit")
        if self.max_attempts == 1:
            if durable.first_retry_interval_ms != 0 or durable.max_retry_interval_ms != 0:
                raise ValueError("single-attempt Durable policy must not configure backoff")
        else:
            if durable.first_retry_interval_ms < 1:
                raise ValueError("multi-attempt Durable policy requires a retry interval")
            if durable.max_retry_interval_ms < durable.first_retry_interval_ms:
                raise ValueError("Durable maximum retry interval is below the first interval")
        return self

    def to_wire(self) -> EffectiveWorkflowTaskExecution:
        """Return the original JSON-safe TypedDict contract.

        ``exclude_defaults`` keeps optional keys out of the payload unless they
        were persisted, so the wire shape stays identical to the one written by
        a runtime that did not know about them.
        """
        return cast(
            EffectiveWorkflowTaskExecution,
            self.model_dump(exclude_defaults=True),
        )


class PolicyActivityInputModel(BaseModel):
    """Strict policy fields from a tool or Sub Agent Activity input."""

    model_config = ConfigDict(
        extra="allow",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )

    id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    execution: EffectiveExecutionModel


_ACTIVITY_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ACTIVITY_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_ACTIVITY_WHITESPACE_RE = re.compile(r"\s+")
# Retryability is a property of the classification, never of what a worker
# claims: an outcome whose flag disagrees with its kind is a contract failure.
_FAILURE_RETRYABLE: dict[ActivityFailureKind, bool] = {
    "timeout": True,
    "handler_transient": True,
    "handler_terminal": False,
    "execution_unknown": False,
    "handler_contract": False,
    "authorization": False,
}
# Whether ``execution.continue_on_error`` may convert an already-terminal
# failure into a satisfied dependency edge. A denied target and a violated
# Activity contract never can: continuation is an application-level decision
# about application-level failures, not a way around the authorization boundary
# or a malformed history.
_FAILURE_CONTINUABLE: dict[ActivityFailureKind, bool] = {
    "timeout": True,
    "handler_transient": True,
    "handler_terminal": True,
    "execution_unknown": True,
    "handler_contract": False,
    "authorization": False,
}
# Which layer owns each classification, for the telemetry span's error status.
# Everything a handler can produce is the application's; only a policy denial
# and a violated runtime contract belong to the runtime.
_FAILURE_FAULT_DOMAIN: dict[ActivityFailureKind, str] = {
    "timeout": FaultDomain.APP,
    "handler_transient": FaultDomain.APP,
    "handler_terminal": FaultDomain.APP,
    "execution_unknown": FaultDomain.APP,
    "handler_contract": FaultDomain.RUNTIME,
    "authorization": FaultDomain.RUNTIME,
}


def failure_is_continuable(failure: ActivityFailure) -> bool:
    """Return whether ``continue_on_error`` may apply to a validated failure."""
    return _FAILURE_CONTINUABLE.get(failure["kind"], False)


def handler_contract_failure() -> ActivityFailure:
    """Return the stable failure used for malformed Activity contracts."""
    return {
        "error_code": "workflow_task_handler_contract",
        "error": "Task Activity returned an invalid outcome.",
        "kind": "handler_contract",
        "retryable": False,
    }


def validate_activity_result(
    instance_id: str,
    raw: Any,
) -> tuple[bool, Any]:
    """Validate an Activity result without trusting worker-returned classifications.

    Returns ``(True, result)`` for a valid success and ``(False, failure)``
    otherwise, where ``failure`` is always a validated :class:`ActivityFailure`.
    """
    if not isinstance(raw, dict) or raw.get("id") != instance_id:
        return False, handler_contract_failure()
    if raw.get("ok") is True:
        if set(raw) != {"id", "ok", "result"}:
            return False, handler_contract_failure()
        return True, raw["result"]
    if raw.get("ok") is not False or set(raw) != {"id", "ok", "failure"}:
        return False, handler_contract_failure()
    failure = raw["failure"]
    if not isinstance(failure, dict) or set(failure) != {
        "error_code",
        "error",
        "kind",
        "retryable",
    }:
        return False, handler_contract_failure()
    error_code = failure["error_code"]
    error = failure["error"]
    kind = failure["kind"]
    retryable = failure["retryable"]
    normalized_error = (
        _ACTIVITY_WHITESPACE_RE.sub(
            " ",
            _ACTIVITY_CONTROL_RE.sub(" ", error),
        ).strip()
        if isinstance(error, str)
        else None
    )
    if (
        not isinstance(error_code, str)
        or _ACTIVITY_ERROR_CODE_RE.fullmatch(error_code) is None
        or not isinstance(error, str)
        or not error
        or len(error) > 256
        or normalized_error != error
        or not isinstance(kind, str)
        or kind not in _FAILURE_RETRYABLE
        or type(retryable) is not bool
        or retryable != _FAILURE_RETRYABLE[kind]
    ):
        return False, handler_contract_failure()
    return False, cast(ActivityFailure, failure)


def failure_outcome(
    task_id: str,
    *,
    error_code: str,
    error: str,
    kind: ActivityFailureKind,
) -> ActivityFailureOutcome:
    """Build a sanitized policy-aware Activity failure."""
    return {
        "id": task_id,
        "ok": False,
        "failure": {
            "error_code": error_code,
            "error": error,
            "kind": kind,
            "retryable": _FAILURE_RETRYABLE[kind],
        },
    }


def authorization_outcome(task_id: str) -> ActivityFailureOutcome:
    """Return the stable failure for a denied Activity target."""
    return failure_outcome(
        task_id,
        error_code="workflow_task_authorization",
        error="Task target is not authorized.",
        kind="authorization",
    )


def handler_contract_outcome(task_id: str) -> ActivityFailureOutcome:
    """Return the stable failure for malformed Activity input or output."""
    return {"id": task_id, "ok": False, "failure": handler_contract_failure()}


def _policy_activity_context(
    task: Mapping[str, Any],
) -> tuple[WorkflowTaskContext, float | None, EffectiveExecutionModel]:
    """Validate persisted policy-aware Activity fields without repairing bad history."""
    validated = PolicyActivityInputModel.model_validate(task)
    timeout_ms = validated.execution.timeout_ms
    return (
        WorkflowTaskContext(
            workflow_id=validated.workflow_id,
            task_id=validated.task_id,
            node_instance_id=validated.id,
            max_attempts=validated.execution.max_attempts,
            idempotency_key=workflow_task_idempotency_key(
                validated.workflow_id,
                validated.id,
            ),
        ),
        None if timeout_ms is None else timeout_ms / 1000,
        validated.execution,
    )


def validate_policy_activity_input(
    task: Mapping[str, Any],
    *,
    target_type: str,
) -> ActivityFailureOutcome | None:
    """Validate policy-aware fields before authorization or handler lookup."""
    try:
        _policy_activity_context(task)
    except (KeyError, TypeError, ValueError):
        logger.error(
            "malformed policy-aware workflow Activity input: target_type=%s",
            target_type,
        )
        task_id = task.get("id")
        return handler_contract_outcome(task_id if isinstance(task_id, str) else "<invalid>")
    return None


async def invoke_handler(handler: Any, args: dict[str, Any]) -> Any:
    """Await an async handler, or run a synchronous one off the event loop."""
    if inspect.iscoroutinefunction(handler):
        return await handler(args)
    result = await asyncio.to_thread(handler, args)
    if inspect.isawaitable(result):
        return await result
    return result


async def invoke_policy_handler(
    handler: Any,
    args: dict[str, Any],
    *,
    task: Mapping[str, Any],
    target: str,
    target_type: str = "tool",
) -> ActivityOutcome:
    """Run one validated policy-aware Activity delivery.

    Retryable failures never return: they raise so Durable owns the next
    attempt. Everything else returns a terminal outcome the orchestrator
    interprets deterministically during replay.

    The delivery is wrapped in a ``workflow.task.activity`` span. Telemetry is
    recorded *here*, in the Activity, and never in the orchestrator: the
    orchestrator replays, so a span opened there would be re-emitted on every
    replay, while this body runs once per actual delivery. The span is closed
    before the private Durable retry marker is raised, so a requested retry is
    not recorded as a span exception.
    """
    context, timeout, execution = _policy_activity_context(task)
    token = _set_workflow_task_context(context)
    try:
        with workflow_task_activity_telemetry(
            _task_span_attributes(
                workflow_id=context.workflow_id,
                task_id=context.task_id,
                node_instance_id=context.node_instance_id,
                target_type=target_type,
                target_name=target,
                max_attempts=execution.max_attempts,
                timeout_ms=execution.timeout_ms,
                continue_on_error=execution.continue_on_error,
            )
        ) as telemetry:
            try:
                outcome = await _run_policy_attempt(
                    handler,
                    args,
                    context=context,
                    target=target,
                    task_id=str(task["id"]),
                    timeout=timeout,
                )
            except asyncio.CancelledError:
                telemetry.complete(
                    outcome_kind="canceled",
                    disposition="abort",
                    error_code="workflow_task_canceled",
                )
                raise
            _record_outcome(telemetry, outcome)
    finally:
        _reset_workflow_task_context(token)
    if not outcome["ok"] and outcome["failure"]["retryable"]:
        from .native_retry import raise_for_durable_retry

        raise_for_durable_retry(outcome)
    return outcome


def _task_span_attributes(
    *,
    workflow_id: Any,
    task_id: Any,
    node_instance_id: Any,
    target_type: str,
    target_name: Any,
    max_attempts: Any,
    timeout_ms: Any,
    continue_on_error: Any,
) -> dict[str, Any]:
    """Build the ``af.workflow_task.*`` attributes for one delivery.

    Only identifiers, the frozen policy, and runtime classifications — never
    task arguments, handler output, or Sub Agent text. The attempt number is
    deliberately absent (FRD 0004 Decision 73): Durable owns the attempt budget
    and does not report which attempt is being delivered.
    """
    return {
        "af.workflow_task.workflow_id": workflow_id,
        "af.workflow_task.task_id": task_id,
        "af.workflow_task.node_instance_id": node_instance_id,
        "af.workflow_task.target_type": target_type,
        "af.workflow_task.target_name": target_name,
        "af.workflow_task.max_attempts": max_attempts,
        "af.workflow_task.timeout_ms": timeout_ms,
        "af.workflow_task.continue_on_error": continue_on_error,
        "af.workflow_task.retry_driver": "durable",
    }


def _record_outcome(
    telemetry: WorkflowTaskActivityTelemetry,
    outcome: ActivityOutcome,
) -> None:
    """Attach a delivery's terminal classification to its span.

    ``disposition`` states what this Activity did, not what Durable will decide:
    a retryable failure is reported as ``request_durable_retry`` because the
    Activity only raises the marker — whether another attempt follows depends on
    the budget Durable is tracking, which the Activity cannot see.
    """
    if outcome["ok"]:
        telemetry.complete(outcome_kind="success", disposition="return_result")
        return
    failure = outcome["failure"]
    telemetry.complete(
        outcome_kind=failure["kind"],
        disposition=(
            "request_durable_retry" if failure["retryable"] else "return_failure"
        ),
        error_code=failure["error_code"],
        fault_domain=_FAILURE_FAULT_DOMAIN[failure["kind"]],
    )


async def _run_policy_attempt(
    handler: Any,
    args: dict[str, Any],
    *,
    context: WorkflowTaskContext,
    target: str,
    task_id: str,
    timeout: float | None,
) -> ActivityOutcome:
    """Run the handler for one attempt, classifying every failure it can produce.

    Never raises for an application failure — the caller records telemetry for
    the returned outcome first, and only then raises the Durable retry marker.
    """

    def timed_out() -> ActivityOutcome:
        logger.warning(
            "workflow task attempt timed out: workflow_id=%s node_id=%s target=%s",
            context.workflow_id,
            context.node_instance_id,
            target,
        )
        return failure_outcome(
            task_id,
            error_code="workflow_task_timeout",
            error="Task attempt timed out.",
            kind="timeout",
        )

    # ``asyncio.timeout(None)`` is a no-op scope, so a task without a
    # persisted deadline takes exactly the same path as one with it.
    timeout_scope = asyncio.timeout(timeout)
    try:
        async with timeout_scope:
            result = await invoke_handler(handler, args)
    except asyncio.CancelledError:
        # Cancellation the deadline caused was already converted to
        # TimeoutError on scope exit, so this is the host cancelling us.
        raise
    except WorkflowTaskTimeoutError:
        return timed_out()
    except TimeoutError:
        if timeout_scope.expired():
            return timed_out()
        logger.exception(
            "workflow task execution failed: workflow_id=%s node_id=%s target=%s",
            context.workflow_id,
            context.node_instance_id,
            target,
        )
        return failure_outcome(
            task_id,
            error_code="workflow_task_execution_unknown",
            error="Task execution failed.",
            kind="execution_unknown",
        )
    except WorkflowRetryableError as exc:
        return failure_outcome(
            task_id,
            error_code=exc.error_code,
            error=exc.message,
            kind="handler_transient",
        )
    except WorkflowTerminalError as exc:
        return failure_outcome(
            task_id,
            error_code=exc.error_code,
            error=exc.message,
            kind="handler_terminal",
        )
    except Exception:
        logger.exception(
            "workflow task execution failed: workflow_id=%s node_id=%s target=%s",
            context.workflow_id,
            context.node_instance_id,
            target,
        )
        return failure_outcome(
            task_id,
            error_code="workflow_task_execution_unknown",
            error="Task execution failed.",
            kind="execution_unknown",
        )
    try:
        json.dumps(result, allow_nan=False)
    except (TypeError, ValueError):
        logger.error(
            "workflow task returned a non-JSON result: workflow_id=%s node_id=%s target=%s",
            context.workflow_id,
            context.node_instance_id,
            target,
        )
        return failure_outcome(
            task_id,
            error_code="workflow_task_handler_contract",
            error="Task handler returned an invalid result.",
            kind="handler_contract",
        )
    return {"id": task_id, "ok": True, "result": result}


def _in_range(value: Any, low: int, high: int) -> int | None:
    """Return ``value`` only when it is an int inside the validated domain."""
    if type(value) is int and low <= value <= high:
        return value
    return None


def early_policy_outcome_with_telemetry(
    task: Mapping[str, Any],
    *,
    target_type: str,
    target_name: Any,
    outcome: ActivityFailureOutcome,
) -> ActivityFailureOutcome:
    """Record telemetry for a failure raised before the handler was invoked.

    Authorization denials, unregistered targets, and malformed policy input
    never reach :func:`invoke_policy_handler`, so they would otherwise be the
    only Activity deliveries with no span at all.

    The policy fields are read from a payload that, for a ``handler_contract``
    outcome, is exactly the one that just failed validation — and two of them
    are metric dimensions, where an out-of-domain value would create a bogus
    series instead of a bogus span attribute. They are therefore admitted only
    inside their validated domain and dropped otherwise; identifiers stay
    best-effort because they are span-only.
    """
    execution = task.get("execution")
    execution_map: Mapping[str, Any] = execution if isinstance(execution, Mapping) else {}
    continue_on_error = execution_map.get("continue_on_error")
    attributes = {
        key: value
        for key, value in _task_span_attributes(
            workflow_id=task.get("workflow_id"),
            task_id=task.get("task_id"),
            node_instance_id=task.get("id"),
            target_type=target_type,
            target_name=target_name,
            max_attempts=_in_range(
                execution_map.get("max_attempts"),
                1,
                MAX_POLICY_ATTEMPTS,
            ),
            timeout_ms=_in_range(
                execution_map.get("timeout_ms"),
                MIN_POLICY_TIMEOUT_MS,
                MAX_POLICY_TIMEOUT_MS,
            ),
            continue_on_error=(
                continue_on_error if type(continue_on_error) is bool else None
            ),
        ).items()
        if isinstance(value, str | int | bool)
    }
    with workflow_task_activity_telemetry(attributes) as telemetry:
        _record_outcome(telemetry, outcome)
    return outcome


__all__ = [
    "ActivityFailure",
    "ActivityFailureKind",
    "ActivityFailureOutcome",
    "ActivityOutcome",
    "ActivitySuccessOutcome",
    "WorkflowTaskTimeoutError",
    "authorization_outcome",
    "early_policy_outcome_with_telemetry",
    "failure_is_continuable",
    "failure_outcome",
    "handler_contract_outcome",
    "invoke_handler",
    "invoke_policy_handler",
    "validate_activity_result",
    "validate_policy_activity_input",
]
