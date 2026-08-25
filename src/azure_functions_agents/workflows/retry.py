"""Internal retry contracts shared by workflow Activities and orchestration."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .context import _workflow_task_idempotency_key
from .schema import (
    MAX_BACKOFF_MS,
    MAX_POLICY_ATTEMPTS,
    MAX_POLICY_ELAPSED_MS,
    MAX_POLICY_TIMEOUT_MS,
    MIN_POLICY_TIMEOUT_MS,
    EffectiveWorkflowTaskExecution,
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

    @model_validator(mode="after")
    def validate_schedule(self) -> EffectiveExecutionModel:
        if len(self.retry_delays_ms) != self.max_attempts - 1:
            raise ValueError("retry delay count must match maximum attempts")
        elapsed_ms = self.max_attempts * self.timeout_ms + sum(self.retry_delays_ms)
        if elapsed_ms > MAX_POLICY_ELAPSED_MS:
            raise ValueError("attempt deadlines and retry delays exceed the elapsed limit")
        return self

    def to_wire(self) -> EffectiveWorkflowTaskExecution:
        """Return the original JSON-safe TypedDict contract."""
        return cast(EffectiveWorkflowTaskExecution, self.model_dump())


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
    attempt: int = Field(ge=1, le=MAX_POLICY_ATTEMPTS)
    max_attempts: int = Field(ge=1, le=MAX_POLICY_ATTEMPTS)
    idempotency_key: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_identity_and_attempt(self) -> PolicyActivityInputModel:
        if self.id != self.node_instance_id:
            raise ValueError("Activity id must match node instance id")
        if self.max_attempts != self.execution.max_attempts:
            raise ValueError("Activity maximum attempts must match execution policy")
        if self.attempt > self.execution.max_attempts:
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
