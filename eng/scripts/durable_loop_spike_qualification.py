#!/usr/bin/env python3
"""Call bounded durable-loop routes while keeping request and response content private."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

_DEFAULT_BASE_URL = "https://func-durable-loop-0904.azurewebsites.net"
_DEFAULT_TIMEOUT_SECONDS = 120
_MAX_TIMEOUT_SECONDS = 600
_DEFAULT_MAX_BODY_BYTES = 256 * 1024
_MAX_RESPONSE_BYTES = 1024 * 1024
_DEFAULT_POLL_INTERVAL_SECONDS = 2.0
_MIN_POLL_INTERVAL_SECONDS = 0.5
_MAX_POLL_INTERVAL_SECONDS = 60.0
_DEFAULT_POLL_DEADLINE_SECONDS = 600
_MAX_POLL_DEADLINE_SECONDS = 6 * 60 * 60
_POLL_STARTUP_GRACE_SECONDS = 60
_MAX_POLL_ATTEMPTS = math.ceil(
    _MAX_POLL_DEADLINE_SECONDS / _MIN_POLL_INTERVAL_SECONDS
)
_MAX_OPERATION_COUNT = 1_000_000
_MAX_TOKEN_COUNT = 1_000_000_000
_MAX_COST_MICROUNITS = 1_000_000_000_000_000
_MAX_EXTERNAL_CONTENT_BYTES = 1024 * 1024 * 1024 * 1024
_MAX_PARKED_SECONDS = 7 * 24 * 60 * 60
_MAX_URL_CHARS = 2048
_MAX_TIMESTAMP_CHARS = 64
_MAX_HUMAN_CHOICES = 100
_MAX_HEADER_VALUE_CHARS = 8192
_SAFE_TEMPLATE_VALUE = re.compile(r"[A-Za-z0-9._~-]+")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,255}")
_SAFE_RUN_ID = re.compile(r"run-[0-9a-f]{32}")
_SAFE_HUMAN_REQUEST_ID = re.compile(r"human-[0-9]+-[0-9a-f]{16}")
_SAFE_HUMAN_URL = re.compile(
    r"/api/experimental/durable-agent-runs/run-[0-9a-f]{32}/"
    r"input/human-[0-9]+-[0-9a-f]{16}"
)
_SAFE_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_SANDBOX_PROFILES = frozenset({"per_call", "retained_session"})
_FAULT_PROFILES = frozenset(
    {
        "none",
        "model_apim_429_once",
        "model_timeout_once",
        "tool_activity_ack_loss_once",
        "sandbox_loss_after_checkpoint",
        "cleanup_failure_once",
        "commit_ack_loss_once",
    }
)

_DEFAULT_ROUTES = {
    "start": "/api/experimental/durable-agent-runs",
    "status": "/api/experimental/durable-agent-runs/{run_id}",
    "poll": "/api/experimental/durable-agent-runs/{run_id}",
    "result": "/api/experimental/durable-agent-runs/{run_id}/result",
    "cancel": "/api/experimental/durable-agent-runs/{run_id}/cancel",
    "human-detail": (
        "/api/experimental/durable-agent-runs/{run_id}/input/{request_id}"
    ),
    "human-answer": (
        "/api/experimental/durable-agent-runs/{run_id}/input/{request_id}"
    ),
}

type SafeValue = str | int | float | bool | None


class _RunStatus(StrEnum):
    PENDING = "Pending"
    RUNNING = "Running"
    WAITING = "Waiting"
    COMPLETED = "Completed"
    FAILED = "Failed"
    CANCELLED = "Cancelled"


class _RunPhase(StrEnum):
    DURABLE = "durable"
    MODEL_STEP = "model_step"
    HUMAN_WAIT = "human_wait"
    COMPLETED = "completed"


class _Delivery(StrEnum):
    DELIVERED = "delivered"
    RETRY_PENDING = "retry_pending"
    ORPHANED = "orphaned"


class _Disposition(StrEnum):
    CERTAIN = "Certain"
    AMBIGUOUS = "Ambiguous"
    ACCEPTED = "accepted"
    CONSUMED = "consumed"
    ORPHANED = "orphaned"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_ERROR_CODES = frozenset(
    {
        "admission_unavailable",
        "cancel_conflict",
        "content_persistence_failed",
        "durable_run_failed",
        "human_input_acceptance_unavailable",
        "human_input_conflict",
        "human_input_gone",
        "human_input_not_found",
        "human_input_not_pending",
        "human_input_reservation_unavailable",
        "human_input_timed_out",
        "idempotency_conflict",
        "invalid_admission_receipt",
        "invalid_json",
        "missing_answer",
        "missing_submission_id",
        "result_expired",
        "result_not_ready",
        "result_unavailable",
        "run_not_found",
        "run_start_acknowledgement_lost",
        "run_terminal",
        "sandbox_capacity_exhausted",
        "session_busy",
    }
)


_POLL_TERMINAL_STATUSES = frozenset(
    {
        _RunStatus.WAITING.value,
        _RunStatus.COMPLETED.value,
        _RunStatus.FAILED.value,
        _RunStatus.CANCELLED.value,
    }
)


class QualificationRequestError(Exception):
    """A content-free qualification request or response failure."""


class _DuplicateJsonKeyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class QualificationResult:
    """Content-free result metadata plus explicitly selected control fields."""

    command: str
    http_status: int
    latency_ms: float
    response_bytes: int
    selected_fields: Mapping[str, SafeValue]


@dataclass(frozen=True, slots=True)
class PollResult:
    """Content-free aggregate metrics and the final safe status snapshot."""

    command: str
    http_status: int
    attempts: int
    total_elapsed_ms: float
    response_bytes: int
    request_latency_p50_ms: float
    request_latency_p95_ms: float | None
    timed_out: bool
    selected_fields: Mapping[str, SafeValue]


@dataclass(frozen=True, slots=True)
class _FieldSelector:
    path: tuple[str, ...]
    validator: Callable[[Any], SafeValue]


class _ReadableResponse(Protocol):
    status: int

    def read(self, amount: int = -1) -> bytes: ...


def _validated_id(value: Any) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise QualificationRequestError("response_field_invalid:id")
    return value


def _validated_run_id(value: Any) -> str:
    if not isinstance(value, str) or _SAFE_RUN_ID.fullmatch(value) is None:
        raise QualificationRequestError("response_field_invalid:run_id")
    return value


def _validated_human_request_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or _SAFE_HUMAN_REQUEST_ID.fullmatch(value) is None
    ):
        raise QualificationRequestError("response_field_invalid:human_request_id")
    return value


def _validated_status(value: Any) -> str:
    if not isinstance(value, str):
        raise QualificationRequestError("response_field_invalid:status")
    try:
        return _RunStatus(value).value
    except ValueError:
        raise QualificationRequestError("response_field_invalid:status") from None


def _validated_phase(value: Any) -> str:
    if not isinstance(value, str):
        raise QualificationRequestError("response_field_invalid:phase")
    try:
        return _RunPhase(value).value
    except ValueError:
        raise QualificationRequestError("response_field_invalid:phase") from None


def _validated_url(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_URL_CHARS:
        raise QualificationRequestError("response_field_invalid:url")
    parsed = urllib.parse.urlsplit(value)
    if parsed.query or parsed.fragment:
        raise QualificationRequestError("response_field_invalid:url")
    if parsed.scheme:
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
        ):
            raise QualificationRequestError("response_field_invalid:url")
    elif not value.startswith("/") or value.startswith("//"):
        raise QualificationRequestError("response_field_invalid:url")
    return value


def _validated_human_url_shape(value: Any) -> str:
    validated = _validated_url(value)
    if _SAFE_HUMAN_URL.fullmatch(validated) is None:
        raise QualificationRequestError("response_field_invalid:url")
    return validated


def _validated_timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_TIMESTAMP_CHARS:
        raise QualificationRequestError("response_field_invalid:timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise QualificationRequestError("response_field_invalid:timestamp") from None
    if parsed.tzinfo is None:
        raise QualificationRequestError("response_field_invalid:timestamp")
    return value


def _validated_bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise QualificationRequestError("response_field_invalid:bool")
    return value


def _bounded_integer_validator(
    label: str,
    maximum: int,
) -> Callable[[Any], int]:
    def validate(value: Any) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= maximum
        ):
            raise QualificationRequestError(f"response_field_invalid:{label}")
        return value

    return validate


def _bounded_number_validator(
    label: str,
    maximum: int,
) -> Callable[[Any], int | float]:
    def validate(value: Any) -> int | float:
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or not 0 <= value <= maximum
        ):
            raise QualificationRequestError(f"response_field_invalid:{label}")
        return value

    return validate


def _validated_delivery(value: Any) -> str:
    if not isinstance(value, str):
        raise QualificationRequestError("response_field_invalid:delivery")
    try:
        return _Delivery(value).value
    except ValueError:
        raise QualificationRequestError("response_field_invalid:delivery") from None


def _validated_disposition(value: Any) -> str:
    if not isinstance(value, str):
        raise QualificationRequestError("response_field_invalid:disposition")
    try:
        return _Disposition(value).value
    except ValueError:
        raise QualificationRequestError("response_field_invalid:disposition") from None


def _validated_error_code(value: Any) -> str:
    if not isinstance(value, str) or value not in _ERROR_CODES:
        raise QualificationRequestError("response_field_invalid:error_code")
    return value


def _validated_choice_count(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _MAX_HUMAN_CHOICES
    ):
        raise QualificationRequestError("response_field_invalid:human_choice_count")
    return value


_FIELD_SELECTORS: Mapping[str, _FieldSelector] = {
    "cost_microunits": _FieldSelector(
        ("cost_microunits",),
        _bounded_integer_validator("cost_microunits", _MAX_COST_MICROUNITS),
    ),
    "delivery": _FieldSelector(("delivery",), _validated_delivery),
    "disposition": _FieldSelector(
        ("disposition",),
        _validated_disposition,
    ),
    "error_code": _FieldSelector(
        ("error",),
        _validated_error_code,
    ),
    "external_content_bytes": _FieldSelector(
        ("external_content_bytes",),
        _bounded_integer_validator(
            "external_content_bytes",
            _MAX_EXTERNAL_CONTENT_BYTES,
        ),
    ),
    "human_allow_free_text": _FieldSelector(
        ("human_input", "allow_free_text"),
        _validated_bool,
    ),
    "human_choice_count": _FieldSelector(
        ("human_input", "choice_count"),
        _validated_choice_count,
    ),
    "human_detail_url": _FieldSelector(
        ("human_input", "detail_url"),
        _validated_human_url_shape,
    ),
    "human_expires_at": _FieldSelector(
        ("human_input", "expires_at"),
        _validated_timestamp,
    ),
    "human_request_id": _FieldSelector(
        ("human_input", "request_id"),
        _validated_human_request_id,
    ),
    "human_respond_url": _FieldSelector(
        ("human_input", "respond_url"),
        _validated_human_url_shape,
    ),
    "human_schema_present": _FieldSelector(
        ("human_input", "schema_present"),
        _validated_bool,
    ),
    "human_waits": _FieldSelector(
        ("human_waits",),
        _bounded_integer_validator("human_waits", _MAX_OPERATION_COUNT),
    ),
    "input_tokens": _FieldSelector(
        ("input_tokens",),
        _bounded_integer_validator("input_tokens", _MAX_TOKEN_COUNT),
    ),
    "model_steps": _FieldSelector(
        ("model_steps",),
        _bounded_integer_validator("model_steps", _MAX_OPERATION_COUNT),
    ),
    "output_tokens": _FieldSelector(
        ("output_tokens",),
        _bounded_integer_validator("output_tokens", _MAX_TOKEN_COUNT),
    ),
    "parked_seconds": _FieldSelector(
        ("parked_seconds",),
        _bounded_number_validator("parked_seconds", _MAX_PARKED_SECONDS),
    ),
    "phase": _FieldSelector(("phase",), _validated_phase),
    "possibly_committed": _FieldSelector(
        ("possibly_committed",),
        _validated_bool,
    ),
    "reasoning_tokens": _FieldSelector(
        ("reasoning_tokens",),
        _bounded_integer_validator("reasoning_tokens", _MAX_TOKEN_COUNT),
    ),
    "run_id": _FieldSelector(("run_id",), _validated_run_id),
    "session_id": _FieldSelector(("session_id",), _validated_id),
    "status": _FieldSelector(("status",), _validated_status),
    "step_index": _FieldSelector(
        ("step_index",),
        _bounded_integer_validator("step_index", _MAX_OPERATION_COUNT),
    ),
    "tool_calls": _FieldSelector(
        ("tool_calls",),
        _bounded_integer_validator("tool_calls", _MAX_OPERATION_COUNT),
    ),
}
_POLL_FIELD_SELECTIONS = tuple(_FIELD_SELECTORS)


def parse_template_values(values: Sequence[str]) -> dict[str, str]:
    """Parse repeated NAME=VALUE route substitutions."""
    parsed: dict[str, str] = {}
    for item in values:
        name, separator, value = item.partition("=")
        if (
            not separator
            or not name
            or name in parsed
            or _SAFE_TEMPLATE_VALUE.fullmatch(value) is None
        ):
            raise QualificationRequestError("route_value_invalid")
        parsed[name] = value
    return parsed


def render_route(template: str, values: Mapping[str, str]) -> str:
    """Render a relative route without allowing path or host injection."""
    if (
        not template.startswith("/")
        or template.startswith("//")
        or "://" in template
        or "?" in template
        or "#" in template
    ):
        raise QualificationRequestError("route_template_invalid")
    try:
        parsed_template = tuple(string.Formatter().parse(template))
    except ValueError:
        raise QualificationRequestError("route_template_invalid") from None
    expected: set[str] = set()
    for _, field_name, format_spec, conversion in parsed_template:
        if field_name is None:
            continue
        if (
            not field_name
            or format_spec
            or conversion
            or _SAFE_HEADER_NAME.fullmatch(field_name) is None
        ):
            raise QualificationRequestError("route_template_invalid")
        expected.add(field_name)
    if expected != set(values):
        raise QualificationRequestError("route_values_mismatch")
    try:
        rendered = template.format_map(dict(values))
    except (KeyError, ValueError):
        raise QualificationRequestError("route_template_invalid") from None
    if "//" in rendered:
        raise QualificationRequestError("route_template_invalid")
    return rendered


def _read_bounded(stream: _ReadableResponse, maximum_bytes: int) -> bytes:
    content = stream.read(maximum_bytes + 1)
    if len(content) > maximum_bytes:
        raise QualificationRequestError("response_body_too_large")
    return content


def _json_object_without_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError
        result[key] = value
    return result


def _decode_json_document(content: bytes, *, source: str) -> object:
    try:
        return json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_json_object_without_duplicates,
        )
    except _DuplicateJsonKeyError:
        raise QualificationRequestError(f"{source}_duplicate_key") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise QualificationRequestError(f"{source}_invalid_json") from None


def _read_request_body(
    *,
    body_file: str | None,
    body_stdin: bool,
    maximum_bytes: int,
    required: bool,
) -> tuple[bytes | None, Mapping[str, object] | None]:
    if body_file and body_stdin:
        raise QualificationRequestError("request_body_source_conflict")
    if body_file:
        try:
            content = Path(body_file).read_bytes()
        except OSError:
            raise QualificationRequestError("request_body_read_failed") from None
    elif body_stdin:
        content = sys.stdin.buffer.read(maximum_bytes + 1)
    elif required:
        raise QualificationRequestError("request_body_required")
    else:
        return None, None
    if len(content) > maximum_bytes:
        raise QualificationRequestError("request_body_too_large")
    payload = _decode_json_document(content, source="request_body")
    if not isinstance(payload, Mapping):
        raise QualificationRequestError("request_body_must_be_object")
    return content, payload


def _validate_start_payload(
    payload: Mapping[str, object] | None,
    *,
    has_idempotency_key: bool,
) -> None:
    if payload is None:
        raise QualificationRequestError("request_body_required")
    if set(payload) - {
        "fault_profile",
        "prompt",
        "request_id",
        "sandbox_profile",
        "session_id",
    }:
        raise QualificationRequestError("start_body_field_invalid")
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        raise QualificationRequestError("start_prompt_invalid")
    request_id = payload.get("request_id")
    if "request_id" in payload and (
        not isinstance(request_id, str) or _SAFE_ID.fullmatch(request_id) is None
    ):
        raise QualificationRequestError("start_request_id_invalid")
    session_id = payload.get("session_id")
    if "session_id" in payload and (
        not isinstance(session_id, str) or _SAFE_ID.fullmatch(session_id) is None
    ):
        raise QualificationRequestError("start_session_id_invalid")
    sandbox_profile = payload.get("sandbox_profile", "per_call")
    if not isinstance(sandbox_profile, str) or sandbox_profile not in _SANDBOX_PROFILES:
        raise QualificationRequestError("start_sandbox_profile_invalid")
    fault_profile = payload.get("fault_profile", "none")
    if not isinstance(fault_profile, str) or fault_profile not in _FAULT_PROFILES:
        raise QualificationRequestError("start_fault_profile_invalid")
    if request_id is None and not has_idempotency_key:
        raise QualificationRequestError("start_idempotency_required")


def _validate_human_answer_payload(
    payload: Mapping[str, object] | None,
    *,
    has_idempotency_key: bool,
) -> None:
    if payload is None:
        raise QualificationRequestError("request_body_required")
    if set(payload) != {"answer"}:
        raise QualificationRequestError("human_answer_body_invalid")
    if payload["answer"] is None:
        raise QualificationRequestError("human_answer_invalid")
    if not has_idempotency_key:
        raise QualificationRequestError("human_answer_idempotency_required")


def _validate_request_payload(
    command: str,
    payload: Mapping[str, object] | None,
    *,
    has_idempotency_key: bool,
) -> None:
    if command == "start":
        _validate_start_payload(payload, has_idempotency_key=has_idempotency_key)
    elif command == "human-answer":
        _validate_human_answer_payload(
            payload,
            has_idempotency_key=has_idempotency_key,
        )
    elif payload is not None:
        raise QualificationRequestError("request_body_not_allowed")


def parse_field_selections(selections: Sequence[str]) -> tuple[str, ...]:
    """Validate fixed content-free response selectors."""
    parsed: list[str] = []
    for label in selections:
        if "=" in label or label not in _FIELD_SELECTORS or label in parsed:
            raise QualificationRequestError("response_field_selection_invalid")
        parsed.append(label)
    return tuple(parsed)


def _selector_value(payload: Mapping[str, object], selector: _FieldSelector) -> object:
    current: object = payload
    for path_part in selector.path:
        if not isinstance(current, Mapping) or path_part not in current:
            raise QualificationRequestError("response_field_missing")
        current = current[path_part]
    return current


def _validated_human_control_url(
    payload: Mapping[str, object],
    label: str,
    value: object,
) -> str:
    observed = _validated_url(value)
    run_id = _validated_run_id(_selector_value(payload, _FIELD_SELECTORS["run_id"]))
    request_id = _validated_human_request_id(
        _selector_value(payload, _FIELD_SELECTORS["human_request_id"])
    )
    expected = (
        f"/api/experimental/durable-agent-runs/{run_id}/input/{request_id}"
    )
    if observed != expected:
        raise QualificationRequestError(f"response_field_invalid:{label}")
    return expected


def select_response_fields(
    body: bytes,
    selections: Sequence[str],
    *,
    allow_missing: bool = False,
) -> dict[str, SafeValue]:
    """Select only fixed fields after path-specific validation."""
    if not body:
        if selections and not allow_missing:
            raise QualificationRequestError("response_field_missing")
        return {}
    payload = _decode_json_document(body, source="response_body")
    if not selections:
        return {}
    if not isinstance(payload, Mapping):
        raise QualificationRequestError("response_body_must_be_object")
    selected: dict[str, SafeValue] = {}
    for label in selections:
        selector = _FIELD_SELECTORS[label]
        try:
            value = _selector_value(payload, selector)
        except QualificationRequestError:
            if allow_missing:
                continue
            raise
        if label in {"human_respond_url", "human_detail_url"}:
            selected[label] = _validated_human_control_url(payload, label, value)
        else:
            selected[label] = selector.validator(value)
    return selected


def _validated_header_secret(environment: Mapping[str, str], variable_name: str) -> str:
    secret = environment.get(variable_name, "")
    if (
        not secret
        or len(secret) > _MAX_HEADER_VALUE_CHARS
        or "\n" in secret
        or "\r" in secret
    ):
        raise QualificationRequestError("auth_secret_missing_or_invalid")
    return secret


def _auth_headers(
    environment: Mapping[str, str],
    *,
    header_name: str | None,
    header_secret_env: str | None,
    bearer_secret_env: str | None,
) -> dict[str, str]:
    if header_secret_env and bearer_secret_env:
        raise QualificationRequestError("auth_source_conflict")
    if header_secret_env:
        if not header_name or _SAFE_HEADER_NAME.fullmatch(header_name) is None:
            raise QualificationRequestError("auth_header_invalid")
        return {
            header_name: _validated_header_secret(environment, header_secret_env)
        }
    if bearer_secret_env:
        return {
            "Authorization": (
                "Bearer " + _validated_header_secret(environment, bearer_secret_env)
            )
        }
    if header_name:
        raise QualificationRequestError("auth_header_invalid")
    return {}


def _idempotency_headers(
    environment: Mapping[str, str],
    *,
    idempotency_key_env: str | None,
) -> dict[str, str]:
    if idempotency_key_env is None:
        return {}
    value = _validated_header_secret(environment, idempotency_key_env)
    if _SAFE_ID.fullmatch(value) is None:
        raise QualificationRequestError("idempotency_key_invalid")
    return {"Idempotency-Key": value}


def _combine_request_headers(
    auth_headers: Mapping[str, str],
    idempotency_headers: Mapping[str, str],
) -> dict[str, str]:
    if any(name.lower() == "idempotency-key" for name in auth_headers):
        raise QualificationRequestError("idempotency_header_conflict")
    return {**auth_headers, **idempotency_headers}


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request:
        del req, fp, code, msg, headers, newurl
        raise QualificationRequestError("redirect_rejected")


_NO_REDIRECT_OPENER = urllib.request.build_opener(_RejectRedirectHandler())


def _urlopen(
    request: urllib.request.Request,
    *,
    timeout: int,
) -> _ReadableResponse:
    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


def perform_request(
    *,
    command: str,
    method: str,
    url: str,
    body: bytes | None,
    headers: Mapping[str, str],
    timeout_seconds: int,
    maximum_response_bytes: int,
    field_selections: Sequence[str],
    allow_missing_fields: bool = False,
) -> QualificationResult:
    """Perform one bounded request and retain no response content."""
    request_headers = {"Accept": "application/json", **headers}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=body,
        headers=request_headers,
        method=method,
    )
    started = time.perf_counter()
    try:
        with _urlopen(request, timeout=timeout_seconds) as response:
            response_body = _read_bounded(response, maximum_response_bytes)
            status = response.status
    except urllib.error.HTTPError as error:
        response_body = _read_bounded(error, maximum_response_bytes)
        status = error.code
    except (TimeoutError, urllib.error.URLError, OSError) as error:
        raise QualificationRequestError(f"request_failed:{type(error).__name__}") from None
    latency_ms = (time.perf_counter() - started) * 1000
    selected = select_response_fields(
        response_body,
        field_selections,
        allow_missing=allow_missing_fields,
    )
    return QualificationResult(
        command=command,
        http_status=status,
        latency_ms=latency_ms,
        response_bytes=len(response_body),
        selected_fields=selected,
    )


def _nearest_rank(values: Sequence[float], percentile: int) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(percentile * len(ordered) / 100) - 1]


def poll_status(
    *,
    url: str,
    headers: Mapping[str, str],
    timeout_seconds: int,
    maximum_response_bytes: int,
    interval_seconds: float,
    deadline_seconds: int,
    field_selections: Sequence[str],
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.perf_counter,
) -> PollResult:
    """Poll until a terminal or waiting state without retaining response bodies."""
    selections = tuple(dict.fromkeys((*_POLL_FIELD_SELECTIONS, *field_selections)))
    started = clock()
    latencies: list[float] = []
    attempts = 0
    response_bytes = 0
    last_status = 0
    last_fields: Mapping[str, SafeValue] = {}
    timed_out = False

    while attempts < _MAX_POLL_ATTEMPTS:
        elapsed_seconds = clock() - started
        if attempts and elapsed_seconds >= deadline_seconds:
            timed_out = True
            break
        request_timeout = min(
            timeout_seconds,
            max(1, math.ceil(deadline_seconds - elapsed_seconds)),
        )
        result = perform_request(
            command="poll",
            method="GET",
            url=url,
            body=None,
            headers=headers,
            timeout_seconds=request_timeout,
            maximum_response_bytes=maximum_response_bytes,
            field_selections=selections,
            allow_missing_fields=True,
        )
        attempts += 1
        latencies.append(result.latency_ms)
        response_bytes += result.response_bytes
        last_status = result.http_status
        last_fields = result.selected_fields
        if (
            result.http_status == 404
            and result.selected_fields.get("error_code") == "run_not_found"
            and elapsed_seconds < min(deadline_seconds, _POLL_STARTUP_GRACE_SECONDS)
        ):
            sleeper(min(interval_seconds, deadline_seconds - elapsed_seconds))
            continue
        if not 200 <= result.http_status < 400:
            break
        status = result.selected_fields.get("status")
        if status is None:
            raise QualificationRequestError("response_field_missing")
        if status in _POLL_TERMINAL_STATUSES:
            break
        elapsed_seconds = clock() - started
        if elapsed_seconds >= deadline_seconds:
            timed_out = True
            break
        sleeper(min(interval_seconds, deadline_seconds - elapsed_seconds))
    else:
        timed_out = True

    total_elapsed_ms = (clock() - started) * 1000
    if not latencies:
        raise QualificationRequestError("poll_no_attempts")
    return PollResult(
        command="poll",
        http_status=last_status,
        attempts=attempts,
        total_elapsed_ms=total_elapsed_ms,
        response_bytes=response_bytes,
        request_latency_p50_ms=_nearest_rank(latencies, 50),
        request_latency_p95_ms=(
            _nearest_rank(latencies, 95) if len(latencies) >= 2 else None
        ),
        timed_out=timed_out,
        selected_fields=last_fields,
    )


def _validated_output_fields(fields: Mapping[str, SafeValue]) -> dict[str, SafeValue]:
    validated: dict[str, SafeValue] = {}
    for label, value in fields.items():
        selector = _FIELD_SELECTORS.get(label)
        if selector is None:
            raise QualificationRequestError("result_field_invalid")
        if label == "human_choice_count":
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= _MAX_HUMAN_CHOICES
            ):
                raise QualificationRequestError("result_field_invalid")
            validated[label] = value
        else:
            validated[label] = selector.validator(value)
    return validated


def _result_payload(result: QualificationResult | PollResult) -> dict[str, Any]:
    if isinstance(result, PollResult):
        payload: dict[str, Any] = {
            "attempts": result.attempts,
            "command": result.command,
            "http_status": result.http_status,
            "request_latency_p50_ms": round(result.request_latency_p50_ms, 3),
            "response_bytes": result.response_bytes,
            "timed_out": result.timed_out,
            "total_elapsed_ms": round(result.total_elapsed_ms, 3),
        }
        if result.request_latency_p95_ms is not None:
            payload["request_latency_p95_ms"] = round(
                result.request_latency_p95_ms,
                3,
            )
    else:
        payload = {
            "command": result.command,
            "http_status": result.http_status,
            "latency_ms": round(result.latency_ms, 3),
            "response_bytes": result.response_bytes,
        }
    if result.selected_fields:
        payload["selected_fields"] = _validated_output_fields(result.selected_fields)
    return payload


def render_result(result: QualificationResult | PollResult) -> str:
    """Render content-free request metadata and selected control fields."""
    return json.dumps(_result_payload(result), sort_keys=True)


def _validated_metrics_path(value: str, metrics_format: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        raise QualificationRequestError("metrics_path_must_be_absolute")
    if candidate.suffix.lower() != f".{metrics_format}":
        raise QualificationRequestError("metrics_path_extension_invalid")
    try:
        parent = candidate.parent.resolve(strict=True)
        resolved = parent / candidate.name
    except OSError:
        raise QualificationRequestError("metrics_parent_invalid") from None
    if resolved.is_relative_to(_REPOSITORY_ROOT):
        raise QualificationRequestError("metrics_path_inside_repository")
    if resolved.is_symlink() or (resolved.exists() and not resolved.is_file()):
        raise QualificationRequestError("metrics_path_unsafe")
    return resolved


def _prepare_metrics_path(path_value: str, metrics_format: str) -> Path:
    path = _validated_metrics_path(path_value, metrics_format)
    try:
        with path.open("a", encoding="utf-8", newline="\n"):
            pass
    except OSError:
        raise QualificationRequestError("metrics_write_failed") from None
    return path


def _write_metrics_path(
    path: Path,
    metrics_format: str,
    result: QualificationResult | PollResult,
) -> None:
    if path.is_symlink():
        raise QualificationRequestError("metrics_path_unsafe")
    rendered = json.dumps(_result_payload(result), sort_keys=True)
    try:
        if metrics_format == "jsonl":
            with path.open("a", encoding="utf-8", newline="\n") as output:
                output.write(rendered + "\n")
        else:
            path.write_text(rendered + "\n", encoding="utf-8")
    except OSError:
        raise QualificationRequestError("metrics_write_failed") from None


def write_metrics(
    path_value: str,
    metrics_format: str,
    result: QualificationResult | PollResult,
) -> None:
    """Write one content-free metrics record outside the repository."""
    path = _prepare_metrics_path(path_value, metrics_format)
    _write_metrics_path(path, metrics_format, result)


def _validated_base_url(value: str) -> str:
    base_url = value.rstrip("/")
    parsed_base = urllib.parse.urlsplit(base_url)
    if (
        parsed_base.scheme != "https"
        or not parsed_base.netloc
        or parsed_base.username
        or parsed_base.password
        or parsed_base.query
        or parsed_base.fragment
        or parsed_base.path not in {"", "/"}
    ):
        raise QualificationRequestError("base_url_invalid")
    return base_url


def _bounded_integer(value: str, *, maximum: int, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{name} must be an integer") from None
    if not 1 <= parsed <= maximum:
        raise argparse.ArgumentTypeError(f"{name} must be between 1 and {maximum}")
    return parsed


def _bounded_float(
    value: str,
    *,
    minimum: float,
    maximum: float,
    name: str,
) -> float:
    try:
        parsed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{name} must be a number") from None
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise argparse.ArgumentTypeError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return parsed


def _add_common_arguments(
    parser: argparse.ArgumentParser,
    *,
    default_route: str,
) -> None:
    parser.add_argument("--base-url", default=_DEFAULT_BASE_URL)
    parser.add_argument("--route-template", default=default_route)
    parser.add_argument("--value", action="append", default=[])
    parser.add_argument(
        "--timeout-seconds",
        type=lambda value: _bounded_integer(
            value,
            maximum=_MAX_TIMEOUT_SECONDS,
            name="timeout-seconds",
        ),
        default=_DEFAULT_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--max-response-bytes",
        type=lambda value: _bounded_integer(
            value,
            maximum=_MAX_RESPONSE_BYTES,
            name="max-response-bytes",
        ),
        default=_MAX_RESPONSE_BYTES,
    )
    parser.add_argument("--header-name")
    parser.add_argument("--header-secret-env")
    parser.add_argument("--bearer-secret-env")
    parser.add_argument("--idempotency-key-env")
    parser.add_argument("--extract", action="append", default=[], metavar="SAFE_FIELD")
    parser.add_argument("--metrics-output")
    parser.add_argument(
        "--metrics-format",
        choices=("jsonl", "json"),
        default="jsonl",
    )


def _add_body_arguments(
    parser: argparse.ArgumentParser,
    *,
    required: bool,
) -> None:
    parser.add_argument("--body-file")
    parser.add_argument("--body-stdin", action="store_true")
    parser.set_defaults(body_required=required)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    methods = {
        "start": ("POST", True),
        "status": ("GET", False),
        "poll": ("GET", False),
        "result": ("GET", False),
        "human-detail": ("GET", False),
        "cancel": ("POST", False),
        "human-answer": ("POST", True),
    }
    for command, (method, body_required) in methods.items():
        command_parser = subcommands.add_parser(command)
        _add_common_arguments(
            command_parser,
            default_route=_DEFAULT_ROUTES[command],
        )
        if method == "POST":
            _add_body_arguments(command_parser, required=body_required)
        else:
            command_parser.set_defaults(
                body_file=None,
                body_stdin=False,
                body_required=False,
            )
        if command == "poll":
            command_parser.add_argument(
                "--poll-interval-seconds",
                type=lambda value: _bounded_float(
                    value,
                    minimum=_MIN_POLL_INTERVAL_SECONDS,
                    maximum=_MAX_POLL_INTERVAL_SECONDS,
                    name="poll-interval-seconds",
                ),
                default=_DEFAULT_POLL_INTERVAL_SECONDS,
            )
            command_parser.add_argument(
                "--poll-deadline-seconds",
                type=lambda value: _bounded_integer(
                    value,
                    maximum=_MAX_POLL_DEADLINE_SECONDS,
                    name="poll-deadline-seconds",
                ),
                default=_DEFAULT_POLL_DEADLINE_SECONDS,
            )
        command_parser.set_defaults(method=method)
    return parser


def main(
    arguments: Sequence[str] | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    """Dispatch one content-safe qualification request."""
    args = _parser().parse_args(arguments)
    effective_environment = os.environ if environment is None else environment
    try:
        metrics_path = (
            _prepare_metrics_path(args.metrics_output, args.metrics_format)
            if args.metrics_output
            else None
        )
        values = parse_template_values(args.value)
        route = render_route(args.route_template, values)
        base_url = _validated_base_url(args.base_url)
        body, payload = _read_request_body(
            body_file=args.body_file,
            body_stdin=args.body_stdin,
            maximum_bytes=_DEFAULT_MAX_BODY_BYTES,
            required=args.body_required,
        )
        headers = _combine_request_headers(
            _auth_headers(
                effective_environment,
                header_name=args.header_name,
                header_secret_env=args.header_secret_env,
                bearer_secret_env=args.bearer_secret_env,
            ),
            _idempotency_headers(
                effective_environment,
                idempotency_key_env=args.idempotency_key_env,
            ),
        )
        _validate_request_payload(
            args.command,
            payload,
            has_idempotency_key="Idempotency-Key" in headers,
        )
        selections = parse_field_selections(args.extract)
        url = f"{base_url}{route}"
        if args.command == "poll":
            result: QualificationResult | PollResult = poll_status(
                url=url,
                headers=headers,
                timeout_seconds=args.timeout_seconds,
                maximum_response_bytes=args.max_response_bytes,
                interval_seconds=args.poll_interval_seconds,
                deadline_seconds=args.poll_deadline_seconds,
                field_selections=selections,
            )
        else:
            result = perform_request(
                command=args.command,
                method=args.method,
                url=url,
                body=body,
                headers=headers,
                timeout_seconds=args.timeout_seconds,
                maximum_response_bytes=args.max_response_bytes,
                field_selections=selections,
            )
    except (OSError, QualificationRequestError) as error:
        error_code = (
            str(error) if isinstance(error, QualificationRequestError) else "local_io_failed"
        )
        print(f"Durable loop qualification failed: {error_code}", file=sys.stderr)
        return 1
    if metrics_path is not None:
        try:
            _write_metrics_path(metrics_path, args.metrics_format, result)
        except QualificationRequestError as error:
            print(render_result(result))
            print(
                f"Durable loop qualification metrics failed after request: {error}",
                file=sys.stderr,
            )
            return 2
    print(render_result(result))
    if isinstance(result, PollResult) and result.timed_out:
        return 1
    return 0 if 200 <= result.http_status < 400 else 1


if __name__ == "__main__":
    raise SystemExit(main())
