"""Policy-aware Activity execution and retry contracts."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Annotated, Any, Literal, TypedDict, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from azure_functions_agents._logger import logger
from azure_functions_agents._observability import workflow_task_activity_telemetry

from .context import (
    WorkflowTaskContext,
    _reset_workflow_task_context,
    _set_workflow_task_context,
    _workflow_task_idempotency_key,
)
from .schema import (
    MAX_BACKOFF_MS,
    MAX_POLICY_ATTEMPTS,
    MAX_POLICY_ELAPSED_MS,
    MAX_POLICY_TIMEOUT_MS,
    MIN_POLICY_TIMEOUT_MS,
    DurableRetryPolicyInput,
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
    "activity_infrastructure",
    "authorization",
]
type RetryAction = Literal["retry", "continue", "fail"]
type PolicySource = Literal["decorator", "task", "runtime_default"]


class ActivityFailure(TypedDict):
    error_code: str
    error: str
    kind: ActivityFailureKind
    retryable: bool
    continuable: bool


class ActivitySuccessOutcome(TypedDict):
    id: str
    ok: Literal[True]
    result: Any


class ActivityFailureOutcome(TypedDict):
    id: str
    ok: Literal[False]
    failure: ActivityFailure


type ActivityOutcome = ActivitySuccessOutcome | ActivityFailureOutcome


@dataclass(frozen=True)
class RetryDisposition:
    """Runtime action selected for one validated Activity failure."""

    action: RetryAction
    delay_ms: int | None = None


def decide_retry(
    execution: EffectiveWorkflowTaskExecution,
    *,
    attempt: int,
    failure: ActivityFailure,
) -> RetryDisposition:
    """Select retry, continuation, or terminal failure from the frozen policy."""
    if failure["retryable"] and attempt < execution["max_attempts"]:
        return RetryDisposition(
            action="retry",
            delay_ms=execution["retry_delays_ms"][attempt - 1],
        )
    if failure["continuable"] and execution["continue_on_error"]:
        return RetryDisposition(action="continue")
    return RetryDisposition(action="fail")


type _RetryDelayMs = Annotated[int, Field(ge=0, le=MAX_BACKOFF_MS)]


class DurableRetryPolicyModel(BaseModel):
    """Strict Activity-side validation of the persisted native retry marker."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )

    first_retry_interval_ms: int = Field(ge=0, le=MAX_BACKOFF_MS)
    max_number_of_attempts: int = Field(ge=1, le=MAX_POLICY_ATTEMPTS)
    backoff_coefficient: float = Field(ge=1.0, le=10.0, allow_inf_nan=False)
    max_retry_interval_ms: int = Field(ge=0, le=MAX_BACKOFF_MS)
    retry_timeout_ms: int = Field(ge=1, le=MAX_POLICY_ELAPSED_MS)

    def to_wire(self) -> DurableRetryPolicyInput:
        """Return the original JSON-safe TypedDict contract."""
        return cast(DurableRetryPolicyInput, self.model_dump())


class EffectiveExecutionModel(BaseModel):
    """Strict Activity-side validation of the persisted execution policy."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )

    timeout_ms: int = Field(ge=MIN_POLICY_TIMEOUT_MS, le=MAX_POLICY_TIMEOUT_MS)
    max_attempts: int = Field(ge=1, le=MAX_POLICY_ATTEMPTS)
    retry_delays_ms: list[_RetryDelayMs]
    continue_on_error: bool
    timeout_source: PolicySource
    retry_source: PolicySource
    durable_retry_policy: DurableRetryPolicyModel | None = None

    @model_validator(mode="after")
    def validate_schedule(self) -> EffectiveExecutionModel:
        if len(self.retry_delays_ms) != self.max_attempts - 1:
            raise ValueError("retry delay count must match maximum attempts")
        elapsed_ms = self.max_attempts * self.timeout_ms + sum(self.retry_delays_ms)
        if elapsed_ms > MAX_POLICY_ELAPSED_MS:
            raise ValueError("attempt deadlines and retry delays exceed the elapsed limit")
        durable = self.durable_retry_policy
        if durable is not None:
            if durable.max_number_of_attempts != self.max_attempts:
                raise ValueError("Durable maximum attempts must match execution policy")
            if durable.retry_timeout_ms != MAX_POLICY_ELAPSED_MS:
                raise ValueError("Durable retry timeout must match the runtime elapsed limit")
            if self.max_attempts == 1:
                if (
                    durable.first_retry_interval_ms != 0
                    or durable.max_retry_interval_ms != 0
                ):
                    raise ValueError("single-attempt Durable policy must not configure backoff")
            else:
                if durable.first_retry_interval_ms < 1:
                    raise ValueError("multi-attempt Durable policy requires a retry interval")
                if durable.max_retry_interval_ms < durable.first_retry_interval_ms:
                    raise ValueError("Durable maximum retry interval is below the first interval")
                multiplier = Decimal(str(durable.backoff_coefficient))
                delays = [
                    min(
                        durable.max_retry_interval_ms,
                        int(
                            (
                                Decimal(durable.first_retry_interval_ms)
                                * multiplier**index
                            ).to_integral_value(rounding=ROUND_CEILING)
                        ),
                    )
                    for index in range(self.max_attempts - 1)
                ]
                if self.max_attempts * self.timeout_ms + sum(delays) > MAX_POLICY_ELAPSED_MS:
                    raise ValueError("Durable retry schedule exceeds the elapsed limit")
        return self

    def to_wire(self) -> EffectiveWorkflowTaskExecution:
        """Return the original JSON-safe TypedDict contract."""
        return cast(EffectiveWorkflowTaskExecution, self.model_dump(exclude_none=True))


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
    execution: EffectiveExecutionModel
    task_id: str = Field(min_length=1)
    node_instance_id: str = Field(min_length=1)
    attempt: int | None = Field(default=None, ge=1, le=MAX_POLICY_ATTEMPTS)
    max_attempts: int = Field(ge=1, le=MAX_POLICY_ATTEMPTS)
    idempotency_key: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_identity_and_attempt(self) -> PolicyActivityInputModel:
        if self.id != self.node_instance_id:
            raise ValueError("Activity id must match node instance id")
        if self.max_attempts != self.execution.max_attempts:
            raise ValueError("Activity maximum attempts must match execution policy")
        native = self.execution.durable_retry_policy is not None
        if native == (self.attempt is not None):
            raise ValueError("Activity attempt shape does not match retry driver")
        if self.attempt is not None and self.attempt > self.execution.max_attempts:
            raise ValueError("Activity attempt exceeds execution policy")
        expected_key = _workflow_task_idempotency_key(
            self.workflow_id,
            self.node_instance_id,
        )
        if self.idempotency_key != expected_key:
            raise ValueError("Activity idempotency key is invalid")
        return self


_ACTIVITY_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ACTIVITY_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_ACTIVITY_WHITESPACE_RE = re.compile(r"\s+")
_FAILURE_CLASSIFICATION: dict[ActivityFailureKind, tuple[bool, bool]] = {
    "timeout": (True, True),
    "handler_transient": (True, True),
    "activity_infrastructure": (True, True),
    "handler_terminal": (False, True),
    "execution_unknown": (False, True),
    "handler_contract": (False, False),
    "authorization": (False, False),
}


def handler_contract_failure() -> ActivityFailure:
    """Return the stable failure used for malformed Activity contracts."""
    return {
        "error_code": "workflow_task_handler_contract",
        "error": "Task Activity returned an invalid outcome.",
        "kind": "handler_contract",
        "retryable": False,
        "continuable": False,
    }


def validate_activity_result(
    instance_id: str,
    raw: Any,
) -> tuple[bool, Any | ActivityFailure]:
    """Validate an Activity result without trusting worker-returned classifications."""
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
        "continuable",
    }:
        return False, handler_contract_failure()
    error_code = failure["error_code"]
    error = failure["error"]
    kind = failure["kind"]
    retryable = failure["retryable"]
    continuable = failure["continuable"]
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
        or kind not in _FAILURE_CLASSIFICATION
        or type(retryable) is not bool
        or type(continuable) is not bool
        or (retryable, continuable) != _FAILURE_CLASSIFICATION[kind]
    ):
        return False, handler_contract_failure()
    return False, cast(ActivityFailure, failure)


def _policy_activity_context(
    task: Mapping[str, Any],
) -> tuple[WorkflowTaskContext, float, EffectiveWorkflowTaskExecution]:
    """Validate persisted policy-aware Activity fields without repairing bad history."""
    validated = PolicyActivityInputModel.model_validate(task)
    execution = validated.execution.to_wire()
    timeout_ms = execution["timeout_ms"]
    deadline = datetime.now(UTC) + timedelta(milliseconds=timeout_ms)
    return (
        WorkflowTaskContext(
            workflow_id=validated.workflow_id,
            task_id=validated.task_id,
            node_instance_id=validated.node_instance_id,
            attempt=validated.attempt,
            max_attempts=validated.max_attempts,
            idempotency_key=validated.idempotency_key,
            deadline=deadline,
        ),
        timeout_ms / 1000,
        execution,
    )


def failure_outcome(
    task_id: str,
    *,
    error_code: str,
    error: str,
    kind: ActivityFailureKind,
    retryable: bool,
    continuable: bool,
) -> ActivityFailureOutcome:
    """Build a sanitized policy-aware Activity failure."""
    return {
        "id": task_id,
        "ok": False,
        "failure": {
            "error_code": error_code,
            "error": error,
            "kind": kind,
            "retryable": retryable,
            "continuable": continuable,
        },
    }


async def invoke_policy_handler(
    handler: Any,
    args: dict[str, Any],
    *,
    task: Mapping[str, Any],
    target: str,
    target_type: str = "tool",
) -> ActivityOutcome:
    """Run one validated policy-aware Activity delivery."""
    context, timeout, execution = _policy_activity_context(task)
    token = _set_workflow_task_context(context)
    task_id = str(task["id"])
    telemetry_manager = workflow_task_activity_telemetry({
        "af.workflow_task.workflow_id": context.workflow_id,
        "af.workflow_task.task_id": context.task_id,
        "af.workflow_task.node_instance_id": context.node_instance_id,
        "af.workflow_task.attempt": context.attempt,
        "af.workflow_task.max_attempts": context.max_attempts,
        "af.workflow_task.target_type": target_type,
        "af.workflow_task.target_name": target,
        "af.workflow_task.timeout_source": execution["timeout_source"],
        "af.workflow_task.retry_source": execution["retry_source"],
        "af.workflow_task.timeout_ms": execution["timeout_ms"],
    })
    telemetry = telemetry_manager.__enter__()
    telemetry_open = True

    def finish(outcome: ActivityOutcome) -> ActivityOutcome:
        native = "durable_retry_policy" in execution
        if outcome["ok"]:
            kind = "success"
            error_code = None
            decision = "complete"
            delay = None
        else:
            failure = outcome["failure"]
            kind = failure["kind"]
            error_code = failure["error_code"]
            if native and failure["retryable"]:
                decision = "durable"
                delay = None
            else:
                disposition = decide_retry(
                    execution,
                    attempt=context.attempt or execution["max_attempts"],
                    failure=failure,
                )
                decision = disposition.action
                delay = disposition.delay_ms
        telemetry.complete(
            outcome_kind=kind,
            error_code=error_code,
            retry_decision=decision,
            selected_delay_ms=delay,
        )
        if native and not outcome["ok"] and outcome["failure"]["retryable"]:
            from .native_retry import raise_for_durable_retry

            raise_for_durable_retry(outcome)
        return outcome

    try:
        timeout_scope = asyncio.timeout(timeout)
        try:
            async with timeout_scope:
                if inspect.iscoroutinefunction(handler):
                    result = await handler(args)
                else:
                    result = await asyncio.to_thread(handler, args)
                    if inspect.isawaitable(result):
                        result = await result
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            if timeout_scope.expired():
                return finish(failure_outcome(
                    task_id,
                    error_code="workflow_task_timeout",
                    error="Task attempt timed out.",
                    kind="timeout",
                    retryable=True,
                    continuable=True,
                ))
            logger.exception(
                "workflow task execution failed: workflow_id=%s node_id=%s target=%s",
                context.workflow_id,
                context.node_instance_id,
                target,
            )
            return finish(failure_outcome(
                task_id,
                error_code="workflow_task_execution_unknown",
                error="Task execution failed.",
                kind="execution_unknown",
                retryable=False,
                continuable=True,
            ))
        except WorkflowRetryableError as exc:
            return finish(failure_outcome(
                task_id,
                error_code=exc.error_code,
                error=exc.message,
                kind="handler_transient",
                retryable=True,
                continuable=True,
            ))
        except WorkflowTerminalError as exc:
            return finish(failure_outcome(
                task_id,
                error_code=exc.error_code,
                error=exc.message,
                kind="handler_terminal",
                retryable=False,
                continuable=True,
            ))
        except Exception:
            logger.exception(
                "workflow task execution failed: workflow_id=%s node_id=%s target=%s",
                context.workflow_id,
                context.node_instance_id,
                target,
            )
            return finish(failure_outcome(
                task_id,
                error_code="workflow_task_execution_unknown",
                error="Task execution failed.",
                kind="execution_unknown",
                retryable=False,
                continuable=True,
            ))
        try:
            json.dumps(result, allow_nan=False)
        except (TypeError, ValueError):
            logger.error(
                "workflow task returned a non-JSON result: workflow_id=%s node_id=%s target=%s",
                context.workflow_id,
                context.node_instance_id,
                target,
            )
            return finish(failure_outcome(
                task_id,
                error_code="workflow_task_handler_contract",
                error="Task handler returned an invalid result.",
                kind="handler_contract",
                retryable=False,
                continuable=False,
            ))
        return finish({"id": task_id, "ok": True, "result": result})
    except asyncio.CancelledError as exc:
        telemetry.complete(
            outcome_kind="canceled",
            error_code="workflow_task_canceled",
            retry_decision="fail",
            selected_delay_ms=None,
        )
        telemetry_manager.__exit__(type(exc), exc, exc.__traceback__)
        telemetry_open = False
        raise
    finally:
        if telemetry_open:
            telemetry_manager.__exit__(None, None, None)
        _reset_workflow_task_context(token)


def authorization_outcome(task_id: str) -> ActivityFailureOutcome:
    """Return the stable failure for a denied Activity target."""
    return failure_outcome(
        task_id,
        error_code="workflow_task_authorization",
        error="Task target is not authorized.",
        kind="authorization",
        retryable=False,
        continuable=False,
    )


def handler_contract_outcome(task_id: str) -> ActivityFailureOutcome:
    """Return the stable failure for malformed Activity input or output."""
    return {"id": task_id, "ok": False, "failure": handler_contract_failure()}


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
        return handler_contract_outcome(
            task_id if isinstance(task_id, str) else "<invalid>"
        )
    return None


def early_policy_outcome_with_telemetry(
    task: Mapping[str, Any],
    *,
    target_type: str,
    target_name: Any,
    outcome: ActivityFailureOutcome,
) -> dict[str, Any]:
    """Emit bounded telemetry for a failure before handler invocation."""
    execution = task.get("execution")
    execution_map = execution if isinstance(execution, Mapping) else {}
    attributes = {
        "af.workflow_task.workflow_id": task.get("workflow_id"),
        "af.workflow_task.task_id": task.get("task_id"),
        "af.workflow_task.node_instance_id": task.get("node_instance_id"),
        "af.workflow_task.attempt": task.get("attempt"),
        "af.workflow_task.max_attempts": task.get("max_attempts"),
        "af.workflow_task.target_type": target_type,
        "af.workflow_task.target_name": target_name,
        "af.workflow_task.timeout_source": execution_map.get("timeout_source"),
        "af.workflow_task.retry_source": execution_map.get("retry_source"),
        "af.workflow_task.timeout_ms": execution_map.get("timeout_ms"),
    }
    safe_attributes = {
        key: value
        for key, value in attributes.items()
        if isinstance(value, (str, int)) and not isinstance(value, bool)
    }
    with workflow_task_activity_telemetry(safe_attributes) as telemetry:
        failure = outcome["failure"]
        telemetry.complete(
            outcome_kind=failure["kind"],
            error_code=failure["error_code"],
            retry_decision="fail",
            selected_delay_ms=None,
        )
    return dict(outcome)
