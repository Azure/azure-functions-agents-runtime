"""Durable Python 2.x retry mapping and private failure bridge."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from durabletask.task import RetryPolicy, TaskFailedError

from .policy import ActivityFailure, ActivityFailureOutcome, validate_activity_result
from .schema import DurableRetryPolicyInput

_FAILURE_VERSION = 1


class DurableRetryableActivityError(Exception):
    """Private exception used only to ask Durable to retry a sanitized outcome."""


def create_durable_retry_policy(spec: DurableRetryPolicyInput) -> RetryPolicy:
    """Map the persisted wire shape to the installed Durable retry policy."""
    return RetryPolicy(
        first_retry_interval=timedelta(milliseconds=spec["first_retry_interval_ms"]),
        max_number_of_attempts=spec["max_number_of_attempts"],
        backoff_coefficient=spec["backoff_coefficient"],
        max_retry_interval=timedelta(milliseconds=spec["max_retry_interval_ms"]),
        retry_timeout=timedelta(milliseconds=spec["retry_timeout_ms"]),
    )


def raise_for_durable_retry(outcome: ActivityFailureOutcome) -> None:
    """Raise one bounded, versioned failure without chaining handler exceptions."""
    message = json.dumps(
        {
            "version": _FAILURE_VERSION,
            "outcome": outcome,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    raise DurableRetryableActivityError(message) from None


def decode_durable_retry_failure(
    instance_id: str,
    error: BaseException,
) -> ActivityFailure | None:
    """Decode only this runtime's private, sanitized exhaustion payload."""
    if not isinstance(error, TaskFailedError):
        return None
    if not error.details.is_caused_by(DurableRetryableActivityError):
        return None
    try:
        payload: Any = json.loads(error.details.message)
    except (TypeError, ValueError):
        return None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"version", "outcome"}
        or payload["version"] != _FAILURE_VERSION
    ):
        return None
    succeeded, result = validate_activity_result(instance_id, payload["outcome"])
    if succeeded:
        return None
    failure = result
    return failure if failure["retryable"] else None


__all__ = [
    "DurableRetryableActivityError",
    "create_durable_retry_policy",
    "decode_durable_retry_failure",
    "raise_for_durable_retry",
]
