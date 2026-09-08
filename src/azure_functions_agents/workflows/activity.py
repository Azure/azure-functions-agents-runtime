"""Policy-aware Activity execution and the retry failure contract.

A task carries an ``execution`` payload only when its retry policy was frozen at
submission time. Those Activity deliveries use a structured outcome envelope
(``{"id", "ok": true, "result"}`` / ``{"id", "ok": false, "failure"}``) instead
of the legacy ``{"id", "result"}`` shape, because Durable native retry needs the
Activity to distinguish "retry me" (raise) from "this is terminal" (return).

Tasks without a persisted ``execution`` payload keep the legacy shape untouched
so histories written before this runtime version replay unchanged.
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

from .context import (
    WorkflowTaskContext,
    _reset_workflow_task_context,
    _set_workflow_task_context,
    workflow_task_idempotency_key,
)
from .schema import (
    MAX_BACKOFF_MS,
    MAX_POLICY_ATTEMPTS,
    WorkflowRetryableError,
    WorkflowTerminalError,
)

type ActivityFailureKind = Literal[
    "handler_transient",
    "handler_terminal",
    "execution_unknown",
    "handler_contract",
    "authorization",
]


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

    @model_validator(mode="after")
    def validate_schedule(self) -> EffectiveExecutionModel:
        durable = self.durable_retry_policy
        if durable.max_number_of_attempts != self.max_attempts:
            raise ValueError("Durable maximum attempts must match execution policy")
        if self.max_attempts == 1:
            if durable.first_retry_interval_ms != 0 or durable.max_retry_interval_ms != 0:
                raise ValueError("single-attempt Durable policy must not configure backoff")
        else:
            if durable.first_retry_interval_ms < 1:
                raise ValueError("multi-attempt Durable policy requires a retry interval")
            if durable.max_retry_interval_ms < durable.first_retry_interval_ms:
                raise ValueError("Durable maximum retry interval is below the first interval")
        return self

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
    "handler_transient": True,
    "handler_terminal": False,
    "execution_unknown": False,
    "handler_contract": False,
    "authorization": False,
}


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


def _policy_activity_context(task: Mapping[str, Any]) -> WorkflowTaskContext:
    """Validate persisted policy-aware Activity fields without repairing bad history."""
    validated = PolicyActivityInputModel.model_validate(task)
    return WorkflowTaskContext(
        workflow_id=validated.workflow_id,
        task_id=validated.task_id,
        node_instance_id=validated.id,
        max_attempts=validated.execution.max_attempts,
        idempotency_key=workflow_task_idempotency_key(
            validated.workflow_id,
            validated.id,
        ),
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
) -> ActivityOutcome:
    """Run one validated policy-aware Activity delivery.

    Retryable failures never return: they raise so Durable owns the next
    attempt. Everything else returns a terminal outcome the orchestrator
    interprets deterministically during replay.
    """
    context = _policy_activity_context(task)
    token = _set_workflow_task_context(context)
    task_id = str(task["id"])

    def finish(outcome: ActivityOutcome) -> ActivityOutcome:
        if not outcome["ok"] and outcome["failure"]["retryable"]:
            from .native_retry import raise_for_durable_retry

            raise_for_durable_retry(outcome)
        return outcome

    try:
        outcome: ActivityOutcome | None = None
        result: Any = None
        try:
            result = await invoke_handler(handler, args)
        except asyncio.CancelledError:
            raise
        except WorkflowRetryableError as exc:
            outcome = failure_outcome(
                task_id,
                error_code=exc.error_code,
                error=exc.message,
                kind="handler_transient",
            )
        except WorkflowTerminalError as exc:
            outcome = failure_outcome(
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
            outcome = failure_outcome(
                task_id,
                error_code="workflow_task_execution_unknown",
                error="Task execution failed.",
                kind="execution_unknown",
            )
        if outcome is None:
            try:
                json.dumps(result, allow_nan=False)
            except (TypeError, ValueError):
                logger.error(
                    "workflow task returned a non-JSON result: workflow_id=%s node_id=%s target=%s",
                    context.workflow_id,
                    context.node_instance_id,
                    target,
                )
                outcome = failure_outcome(
                    task_id,
                    error_code="workflow_task_handler_contract",
                    error="Task handler returned an invalid result.",
                    kind="handler_contract",
                )
            else:
                outcome = {"id": task_id, "ok": True, "result": result}
        return finish(outcome)
    finally:
        _reset_workflow_task_context(token)


__all__ = [
    "ActivityFailure",
    "ActivityFailureKind",
    "ActivityFailureOutcome",
    "ActivityOutcome",
    "ActivitySuccessOutcome",
    "authorization_outcome",
    "failure_outcome",
    "handler_contract_outcome",
    "invoke_handler",
    "invoke_policy_handler",
    "validate_activity_result",
    "validate_policy_activity_input",
]
