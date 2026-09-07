#!/usr/bin/env python3
"""Call bounded durable-loop routes while keeping request and response content private."""

from __future__ import annotations

import argparse
import json
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
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

_DEFAULT_BASE_URL = "https://func-durable-loop-0904.azurewebsites.net"
_DEFAULT_TIMEOUT_SECONDS = 120
_MAX_TIMEOUT_SECONDS = 600
_DEFAULT_MAX_BODY_BYTES = 256 * 1024
_MAX_RESPONSE_BYTES = 1024 * 1024
_SAFE_TEMPLATE_VALUE = re.compile(r"[A-Za-z0-9._~-]+")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,255}")
_SAFE_PHASE = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,63}")
_MAX_URL_CHARS = 2048
_MAX_COUNT = 1_000_000
_MAX_DURATION_MS = 7 * 24 * 60 * 60 * 1000


class _RunStatus(StrEnum):
    PENDING = "Pending"
    RUNNING = "Running"
    WAITING = "Waiting"
    COMPLETED = "Completed"
    FAILED = "Failed"
    CANCELLED = "Cancelled"


class QualificationRequestError(Exception):
    """A content-free qualification request or response failure."""


@dataclass(frozen=True, slots=True)
class QualificationResult:
    """Content-free result metadata plus explicitly selected control fields."""

    command: str
    http_status: int
    latency_ms: float
    response_bytes: int
    selected_fields: Mapping[str, str | int | float | bool | None]


class _ReadableResponse(Protocol):
    def read(self, amount: int = -1) -> bytes: ...


def _validated_id(value: Any) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise QualificationRequestError("response_field_invalid:id")
    return value


def _validated_status(value: Any) -> str:
    if not isinstance(value, str):
        raise QualificationRequestError("response_field_invalid:status")
    try:
        return _RunStatus(value).value
    except ValueError:
        raise QualificationRequestError("response_field_invalid:status") from None


def _validated_phase(value: Any) -> str:
    if not isinstance(value, str) or _SAFE_PHASE.fullmatch(value) is None:
        raise QualificationRequestError("response_field_invalid:phase")
    return value


def _validated_url(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_URL_CHARS:
        raise QualificationRequestError("response_field_invalid:url")
    parsed = urllib.parse.urlsplit(value)
    if parsed.query or parsed.fragment:
        raise QualificationRequestError("response_field_invalid:url")
    if parsed.scheme:
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise QualificationRequestError("response_field_invalid:url")
    elif not value.startswith("/") or value.startswith("//"):
        raise QualificationRequestError("response_field_invalid:url")
    return value


def _validated_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_COUNT:
        raise QualificationRequestError("response_field_invalid:count")
    return value


def _validated_duration(value: Any) -> int | float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not 0 <= value <= _MAX_DURATION_MS
    ):
        raise QualificationRequestError("response_field_invalid:duration")
    return value


_FIELD_VALIDATORS: Mapping[str, Callable[[Any], str | int | float]] = {
    "cancel_url": _validated_url,
    "completed_model_count": _validated_count,
    "completed_tool_count": _validated_count,
    "duration_ms": _validated_duration,
    "elapsed_ms": _validated_duration,
    "human_answer_url": _validated_url,
    "phase": _validated_phase,
    "request_id": _validated_id,
    "result_url": _validated_url,
    "run_id": _validated_id,
    "session_id": _validated_id,
    "status": _validated_status,
    "status_url": _validated_url,
    "step_index": _validated_count,
}


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
    if not template.startswith("/") or "://" in template:
        raise QualificationRequestError("route_template_invalid")
    expected = {
        field_name for _, field_name, _, _ in string.Formatter().parse(template) if field_name
    }
    if expected != set(values):
        raise QualificationRequestError("route_values_mismatch")
    try:
        return template.format_map(dict(values))
    except (KeyError, ValueError):
        raise QualificationRequestError("route_template_invalid") from None


def _read_bounded(stream: _ReadableResponse, maximum_bytes: int) -> bytes:
    content = stream.read(maximum_bytes + 1)
    if len(content) > maximum_bytes:
        raise QualificationRequestError("response_body_too_large")
    return content


def _read_request_body(
    *,
    body_file: str | None,
    body_stdin: bool,
    maximum_bytes: int,
    required: bool,
) -> bytes | None:
    if body_file and body_stdin:
        raise QualificationRequestError("request_body_source_conflict")
    if body_file:
        content = Path(body_file).read_bytes()
    elif body_stdin:
        content = sys.stdin.buffer.read(maximum_bytes + 1)
    elif required:
        raise QualificationRequestError("request_body_required")
    else:
        return None
    if len(content) > maximum_bytes:
        raise QualificationRequestError("request_body_too_large")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        raise QualificationRequestError("request_body_invalid_json") from None
    if not isinstance(payload, dict):
        raise QualificationRequestError("request_body_must_be_object")
    return content


def parse_field_selections(selections: Sequence[str]) -> tuple[str, ...]:
    """Validate fixed top-level response fields selected for output."""
    parsed: list[str] = []
    for label in selections:
        if "=" in label or label not in _FIELD_VALIDATORS or label in parsed:
            raise QualificationRequestError("response_field_selection_invalid")
        parsed.append(label)
    return tuple(parsed)


def select_response_fields(
    body: bytes,
    selections: Sequence[str],
) -> dict[str, str | int | float | bool | None]:
    """Select only fixed top-level fields after label-specific validation."""
    if not selections:
        return {}
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise QualificationRequestError("response_body_invalid_json") from None
    if not isinstance(payload, Mapping):
        raise QualificationRequestError("response_body_must_be_object")
    selected: dict[str, str | int | float | bool | None] = {}
    for label in selections:
        if label not in payload:
            raise QualificationRequestError("response_field_missing")
        selected[label] = _FIELD_VALIDATORS[label](payload[label])
    return selected


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
        if not header_name or "\n" in header_name or "\r" in header_name:
            raise QualificationRequestError("auth_header_invalid")
        secret = environment.get(header_secret_env, "")
        if not secret:
            raise QualificationRequestError("auth_secret_missing")
        return {header_name: secret}
    if bearer_secret_env:
        secret = environment.get(bearer_secret_env, "")
        if not secret:
            raise QualificationRequestError("auth_secret_missing")
        return {"Authorization": "Bearer " + secret}
    return {}


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
) -> QualificationResult:
    """Perform one bounded request and retain no unselected response content."""
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
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            response_body = _read_bounded(response, maximum_response_bytes)
            status = response.status
    except urllib.error.HTTPError as error:
        response_body = _read_bounded(error, maximum_response_bytes)
        status = error.code
    except (TimeoutError, urllib.error.URLError, OSError) as error:
        raise QualificationRequestError(f"request_failed:{type(error).__name__}") from None
    latency_ms = (time.perf_counter() - started) * 1000
    selected = select_response_fields(response_body, field_selections)
    return QualificationResult(
        command=command,
        http_status=status,
        latency_ms=latency_ms,
        response_bytes=len(response_body),
        selected_fields=selected,
    )


def render_result(result: QualificationResult) -> str:
    """Render content-free request metadata and selected control fields."""
    payload: dict[str, Any] = {
        "command": result.command,
        "http_status": result.http_status,
        "latency_ms": round(result.latency_ms, 3),
        "response_bytes": result.response_bytes,
    }
    if result.selected_fields:
        payload["selected_fields"] = dict(result.selected_fields)
    return json.dumps(payload, sort_keys=True)


def _validated_base_url(value: str) -> str:
    base_url = value.rstrip("/")
    parsed_base = urllib.parse.urlsplit(base_url)
    if parsed_base.scheme != "https" or not parsed_base.netloc:
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


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", default=_DEFAULT_BASE_URL)
    parser.add_argument("--route-template", required=True)
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
    parser.add_argument(
        "--extract",
        action="append",
        default=[],
        choices=tuple(sorted(_FIELD_VALIDATORS)),
        metavar="SAFE_FIELD",
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
        "result": ("GET", False),
        "cancel": ("POST", False),
        "human-answer": ("POST", True),
    }
    for command, (method, body_required) in methods.items():
        command_parser = subcommands.add_parser(command)
        _add_common_arguments(command_parser)
        if method == "POST":
            _add_body_arguments(command_parser, required=body_required)
        else:
            command_parser.set_defaults(
                body_file=None,
                body_stdin=False,
                body_required=False,
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
        values = parse_template_values(args.value)
        route = render_route(args.route_template, values)
        base_url = _validated_base_url(args.base_url)
        body = _read_request_body(
            body_file=args.body_file,
            body_stdin=args.body_stdin,
            maximum_bytes=_DEFAULT_MAX_BODY_BYTES,
            required=args.body_required,
        )
        headers = _auth_headers(
            effective_environment,
            header_name=args.header_name,
            header_secret_env=args.header_secret_env,
            bearer_secret_env=args.bearer_secret_env,
        )
        result = perform_request(
            command=args.command,
            method=args.method,
            url=f"{base_url}{route}",
            body=body,
            headers=headers,
            timeout_seconds=args.timeout_seconds,
            maximum_response_bytes=args.max_response_bytes,
            field_selections=parse_field_selections(args.extract),
        )
        print(render_result(result))
        return 0 if 200 <= result.http_status < 400 else 1
    except (OSError, QualificationRequestError) as error:
        print(f"Durable loop qualification failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
