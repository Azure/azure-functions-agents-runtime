#!/usr/bin/env python3
"""Scan durable-loop persistence and telemetry without emitting inspected content."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

_MAX_OPERATOR_BYTES = 1024 * 1024
_MAX_STRING_BYTES = 512 * 1024
_MAX_BLOB_BYTES = 2 * 1024 * 1024
_MAX_ITEMS = 10_000
_MAX_CANARIES_PER_CATEGORY = 32
_MAX_CANARY_BYTES = 4096
_MAX_RULE_COUNTS = 32
_MAX_ERROR_CODE_CHARS = 80
_DEFAULT_TIMEOUT_SECONDS = 120
_MAX_TIMEOUT_SECONDS = 900
_DEFAULT_TELEMETRY_LAG_RETRIES = 3
_MAX_TELEMETRY_LAG_RETRIES = 10
_DEFAULT_TELEMETRY_LAG_SECONDS = 30
_MAX_TELEMETRY_LAG_SECONDS = 300
_DEFAULT_LATE_SCAN_SECONDS = 0
_MAX_LATE_SCAN_SECONDS = 1800
_DEFAULT_SANDBOX_WAIT_SECONDS = 120
_MAX_SANDBOX_WAIT_SECONDS = 900
_DEFAULT_SANDBOX_POLL_SECONDS = 10
_DEFAULT_LOOKBACK_MINUTES = 120

_DEFAULT_RESOURCE_GROUP = "larohra-durable-agent-loop"
_DEFAULT_FUNCTION_NAME = "func-durable-loop-0904"
_DEFAULT_APP_INSIGHTS_NAME = "appi-durable-loop-0904"
_DEFAULT_LOG_ANALYTICS_NAME = "log-durable-loop-0904"
_DEFAULT_STORAGE_ACCOUNT = "stdurableloop0904e2"
_DEFAULT_CONTENT_CONTAINER = "durable-loop-content"
_DEFAULT_SANDBOX_GROUP = "sbg-durable-loop-0904"
_DEFAULT_SANDBOX_REGION = "eastus2"
_DEFAULT_APIM_RESOURCE_GROUP = "larohra-operations-agent-3p-rg"
_DEFAULT_APIM_SERVICE = "larohra-ai-gateway"
_DEFAULT_APIM_APIS = (
    "durable-agent-loop-model",
    "durable-agent-loop-model-control",
    "durable-agent-loop-mcp",
)
_MODEL_CONTROL_API = "durable-agent-loop-model-control"

_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,126}")
_SAFE_REGION = re.compile(r"[a-z0-9]{2,32}")
_SAFE_SUBSCRIPTION = re.compile(
    r"(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,127})"
)
_SAFE_LABEL_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,62}")
_SAFE_LABEL_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,254}")
_SAFE_ERROR_CLASS = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
_SAFE_BLOB_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@:-]{0,511}")
_SHA256 = re.compile(r"(?:sha256:)?([0-9a-f]{64})")
_HASH_ADDRESSED_NAME = re.compile(r"(?:^|/)(?:sha256[-/:])?([0-9a-f]{64})(?:\.[A-Za-z0-9]{1,12})?$")
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,511}")
_ISO_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,7})?(?:Z|[+-]\d{2}:\d{2})"
)

_CANARY_CATEGORIES = (
    "prompt",
    "human",
    "tool_argument",
    "tool_result",
    "mcp_result",
    "stdout_stderr",
    "reasoning",
    "synthetic_credential",
    "synthetic_blob_sas_url",
)
_ALWAYS_FORBIDDEN_CATEGORIES = frozenset({"synthetic_credential", "synthetic_blob_sas_url"})
_CONTENT_CLASS_CATEGORIES: Mapping[str, frozenset[str]] = {
    "prompt": frozenset({"prompt"}),
    "human": frozenset({"human"}),
    "tool_argument": frozenset({"tool_argument"}),
    "tool_result": frozenset({"tool_result"}),
    "mcp_result": frozenset({"mcp_result"}),
    "stdout": frozenset({"stdout_stderr"}),
    "stderr": frozenset({"stdout_stderr"}),
    "stdout_stderr": frozenset({"stdout_stderr"}),
    "reasoning": frozenset({"reasoning"}),
}

_SENSITIVE_DURABLE_FIELDS = frozenset(
    {
        "body",
        "content",
        "customstatus",
        "data",
        "details",
        "error",
        "exception",
        "input",
        "message",
        "output",
        "payload",
        "reason",
        "result",
        "state",
        "value",
    }
)
_DURABLE_ID_FIELDS = frozenset(
    {
        "executionid",
        "instanceid",
        "orchestrationinstance",
        "parentinstanceid",
        "partitionkey",
        "rowkey",
        "taskhub",
    }
)
_DURABLE_TIMESTAMP_FIELDS = frozenset(
    {
        "createdtime",
        "lastupdatedtime",
        "scheduledstarttime",
        "timestamp",
    }
)
_DURABLE_ENUM_FIELDS: Mapping[str, frozenset[str]] = {
    "runtimestatus": frozenset(
        {
            "Canceled",
            "Completed",
            "ContinuedAsNew",
            "Failed",
            "Pending",
            "Running",
            "Suspended",
            "Terminated",
        }
    ),
    "eventtype": frozenset(
        {
            "ContinueAsNew",
            "EventRaised",
            "EventSent",
            "ExecutionCompleted",
            "ExecutionFailed",
            "ExecutionStarted",
            "GenericEvent",
            "HistoryState",
            "OrchestratorStarted",
            "OrchestratorCompleted",
            "SubOrchestrationInstanceCompleted",
            "SubOrchestrationInstanceCreated",
            "SubOrchestrationInstanceFailed",
            "TaskCompleted",
            "TaskFailed",
            "TaskScheduled",
            "TimerCreated",
            "TimerFired",
        }
    ),
}
_DURABLE_SAFE_TEXT_FIELDS = frozenset(
    {
        "etag",
        "extensionversion",
        "hubname",
        "name",
        "version",
    }
)

_TASKHUB_TABLE_SUFFIXES: Mapping[str, str] = {
    "instances": "Durable/Instances",
    "history": "Durable/History",
    "entities": "Durable/Entities",
}
_TASKHUB_CONTAINER = re.compile(
    r"(?:^|[-_])(?:leases|largemessages|large-messages|messages)$",
    re.IGNORECASE,
)
_TASKHUB_QUEUE = re.compile(
    r"(?:-control-\d+|-workitems|-work-items)$",
    re.IGNORECASE,
)

_APP_INSIGHTS_TABLES = (
    "requests",
    "dependencies",
    "traces",
    "exceptions",
    "customEvents",
    "customMetrics",
)
_LOG_ANALYTICS_TABLES = (
    "AppRequests",
    "AppDependencies",
    "AppTraces",
    "AppExceptions",
    "AppEvents",
    "AppMetrics",
)
_APIM_LOG_TABLES = ("AzureDiagnostics", "ApiManagementGatewayLogs")

_SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "api-key",
        "cookie",
        "ocp-apim-subscription-key",
        "set-cookie",
        "subscription-key",
        "x-api-key",
        "x-functions-key",
    }
)
_SAFE_APIM_HEADERS = frozenset({"request-id", "traceparent", "x-af-operation-id"})
_OPTIONAL_SURFACES = frozenset({"Deployment/Activity"})


class PrivacyQualificationError(Exception):
    """A content-free scanner failure."""


class DuplicateKeyError(PrivacyQualificationError):
    """A JSON document contains duplicate object keys."""


class InputValidationError(PrivacyQualificationError):
    """The operator input violates the bounded schema."""


class ScanStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True, slots=True)
class ScanResult:
    surface: str
    scanned: int
    violations: int
    status: ScanStatus
    rule_counts: Mapping[str, int] = field(default_factory=dict)
    code: str | None = None


@dataclass(frozen=True, slots=True)
class ScanAggregate:
    results: tuple[ScanResult, ...]

    @property
    def status(self) -> ScanStatus:
        if any(result.status is ScanStatus.FAIL for result in self.results):
            return ScanStatus.FAIL
        if any(
            result.status is ScanStatus.INCONCLUSIVE and result.surface not in _OPTIONAL_SURFACES
            for result in self.results
        ):
            return ScanStatus.INCONCLUSIVE
        return ScanStatus.PASS

    @property
    def exit_code(self) -> int:
        if self.status is ScanStatus.FAIL:
            return 1
        if self.status is ScanStatus.INCONCLUSIVE:
            return 2
        return 0


@dataclass(frozen=True, slots=True)
class CanarySet:
    by_category: Mapping[str, tuple[str, ...]]
    exact_ids: tuple[str, ...]

    @property
    def all_exact(self) -> tuple[tuple[str, str], ...]:
        pairs = [
            (category, value) for category, values in self.by_category.items() for value in values
        ]
        pairs.extend(("exact_id", value) for value in self.exact_ids)
        return tuple(pairs)


@dataclass(frozen=True, slots=True)
class ResourceConfig:
    subscription_id: str
    resource_group: str = _DEFAULT_RESOURCE_GROUP
    function_name: str = _DEFAULT_FUNCTION_NAME
    app_insights_name: str = _DEFAULT_APP_INSIGHTS_NAME
    log_analytics_name: str = _DEFAULT_LOG_ANALYTICS_NAME
    storage_account: str = _DEFAULT_STORAGE_ACCOUNT
    content_container: str = _DEFAULT_CONTENT_CONTAINER
    sandbox_group: str = _DEFAULT_SANDBOX_GROUP
    sandbox_region: str = _DEFAULT_SANDBOX_REGION
    apim_resource_group: str = _DEFAULT_APIM_RESOURCE_GROUP
    apim_service: str = _DEFAULT_APIM_SERVICE
    apim_apis: tuple[str, ...] = _DEFAULT_APIM_APIS
    model_control_api: str = _MODEL_CONTROL_API


@dataclass(frozen=True, slots=True)
class OperatorInput:
    canaries: CanarySet
    resources: ResourceConfig | None
    sandbox_selector: Mapping[str, str]
    fixtures: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class TimingConfig:
    start: datetime
    end: datetime
    telemetry_lag_retries: int
    telemetry_lag_seconds: int
    timeout_seconds: int
    late_scan_seconds: int
    sandbox_wait_seconds: int
    sandbox_poll_seconds: int


@dataclass(frozen=True, slots=True)
class MatchSummary:
    scanned: int
    rule_counts: Mapping[str, int]

    @property
    def violations(self) -> int:
        return sum(self.rule_counts.values())


@dataclass(frozen=True, slots=True)
class Batch:
    items: tuple[Any, ...]
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class TableSnapshot:
    name: str
    entities: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class BlobSnapshot:
    container: str
    name: str
    metadata: Mapping[str, Any]
    tags: Mapping[str, Any]
    headers: Mapping[str, Any]
    body: bytes | None
    oversized: bool = False


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    name: str
    metadata: Mapping[str, Any]
    approximate_message_count: int


@dataclass(frozen=True, slots=True)
class AggregateObservation:
    scanned: int
    violations: int


@dataclass(frozen=True, slots=True)
class SandboxSnapshot:
    sandbox_id: str
    labels: Mapping[str, str]


class LiveScanAdapter(Protocol):
    def tables(self) -> Batch: ...

    def blobs(self) -> Batch: ...

    def queues(self) -> Batch: ...

    def telemetry(
        self,
        *,
        surface: str,
        query: str,
        start: datetime,
        end: datetime,
        timeout_seconds: int,
    ) -> AggregateObservation: ...

    def apim_diagnostics(self) -> Mapping[str, Sequence[Mapping[str, Any]]]: ...

    async def sandboxes(self, labels: Mapping[str, str]) -> Sequence[SandboxSnapshot]: ...

    def activity_records(self) -> Batch | None: ...

    def close(self) -> None: ...


class _DuplicateAwareObject(dict[str, Any]):
    pass


def _reject_duplicate_pairs(pairs: Sequence[tuple[str, Any]]) -> _DuplicateAwareObject:
    result: _DuplicateAwareObject = _DuplicateAwareObject()
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError("duplicate_json_key")
        result[key] = value
    return result


def load_bounded_json_bytes(content: bytes) -> Any:
    """Load bounded JSON while rejecting duplicate keys."""
    if len(content) > _MAX_OPERATOR_BYTES:
        raise InputValidationError("operator_input_too_large")
    try:
        return json.loads(content, object_pairs_hook=_reject_duplicate_pairs)
    except UnicodeDecodeError:
        raise InputValidationError("operator_input_invalid_utf8") from None
    except json.JSONDecodeError:
        raise InputValidationError("operator_input_invalid_json") from None


def _read_operator_file(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise InputValidationError("operator_file_invalid")
    mode = path.stat().st_mode
    if os.name != "nt" and mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise InputValidationError("operator_file_permissions")
    return path.read_bytes()


def read_operator_input(path: str | None, stream: Any = None) -> Any:
    """Read operator JSON from stdin or a protected regular file."""
    if path is not None:
        content = _read_operator_file(Path(path))
    else:
        source = sys.stdin.buffer if stream is None else stream
        content = source.read(_MAX_OPERATOR_BYTES + 1)
    return load_bounded_json_bytes(content)


def _require_mapping(value: Any, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InputValidationError(code)
    return value


def _strict_keys(value: Mapping[str, Any], allowed: frozenset[str], code: str) -> None:
    if not set(value).issubset(allowed):
        raise InputValidationError(code)


def _validated_name(value: Any, code: str) -> str:
    if not isinstance(value, str) or _SAFE_NAME.fullmatch(value) is None:
        raise InputValidationError(code)
    return value


def _validated_subscription(value: Any) -> str:
    if not isinstance(value, str) or _SAFE_SUBSCRIPTION.fullmatch(value) is None:
        raise InputValidationError("subscription_id_invalid")
    return value


def _validated_region(value: Any) -> str:
    if not isinstance(value, str) or _SAFE_REGION.fullmatch(value) is None:
        raise InputValidationError("sandbox_region_invalid")
    return value


def _validated_blob_name(value: Any) -> str:
    if not isinstance(value, str) or _SAFE_BLOB_NAME.fullmatch(value) is None:
        raise InputValidationError("fixture_blob_name_invalid")
    return value


def _parse_canaries(payload: Mapping[str, Any]) -> CanarySet:
    canary_payload = _require_mapping(payload.get("canaries"), "canaries_required")
    if set(canary_payload) != set(_CANARY_CATEGORIES):
        raise InputValidationError("canary_categories_invalid")
    by_category: dict[str, tuple[str, ...]] = {}
    observed: set[str] = set()
    for category in _CANARY_CATEGORIES:
        raw_values = canary_payload[category]
        if not isinstance(raw_values, list) or len(raw_values) > _MAX_CANARIES_PER_CATEGORY:
            raise InputValidationError("canary_list_invalid")
        values: list[str] = []
        for raw_value in raw_values:
            if (
                not isinstance(raw_value, str)
                or not raw_value
                or len(raw_value.encode("utf-8")) > _MAX_CANARY_BYTES
                or raw_value in observed
            ):
                raise InputValidationError("canary_value_invalid")
            observed.add(raw_value)
            values.append(raw_value)
        by_category[category] = tuple(values)

    exact_payload = payload.get("exact_ids", {})
    exact_mapping = _require_mapping(exact_payload, "exact_ids_invalid")
    _strict_keys(
        exact_mapping,
        frozenset({"provider", "call", "sandbox"}),
        "exact_ids_invalid",
    )
    exact_ids: list[str] = []
    for category in ("provider", "call", "sandbox"):
        raw_values = exact_mapping.get(category, [])
        if not isinstance(raw_values, list) or len(raw_values) > _MAX_CANARIES_PER_CATEGORY:
            raise InputValidationError("exact_ids_invalid")
        for raw_value in raw_values:
            if (
                not isinstance(raw_value, str)
                or not raw_value
                or len(raw_value.encode("utf-8")) > _MAX_CANARY_BYTES
                or raw_value in observed
            ):
                raise InputValidationError("exact_ids_invalid")
            observed.add(raw_value)
            exact_ids.append(raw_value)
    return CanarySet(by_category=by_category, exact_ids=tuple(exact_ids))


def _parse_resources(value: Any) -> ResourceConfig | None:
    if value is None:
        return None
    payload = _require_mapping(value, "resources_invalid")
    allowed = frozenset(
        {
            "subscription_id",
            "resource_group",
            "function_name",
            "app_insights_name",
            "log_analytics_name",
            "storage_account",
            "content_container",
            "sandbox_group",
            "sandbox_region",
            "apim_resource_group",
            "apim_service",
            "apim_apis",
            "model_control_api",
        }
    )
    _strict_keys(payload, allowed, "resources_invalid")
    subscription_id = _validated_subscription(payload.get("subscription_id"))
    apim_apis_raw = payload.get("apim_apis", list(_DEFAULT_APIM_APIS))
    if (
        not isinstance(apim_apis_raw, list)
        or len(apim_apis_raw) != 3
        or len(set(apim_apis_raw)) != 3
    ):
        raise InputValidationError("apim_apis_invalid")
    apim_apis = tuple(_validated_name(value, "apim_api_invalid") for value in apim_apis_raw)
    model_control_api = _validated_name(
        payload.get("model_control_api", _MODEL_CONTROL_API),
        "model_control_api_invalid",
    )
    if model_control_api not in apim_apis:
        raise InputValidationError("model_control_api_required")
    return ResourceConfig(
        subscription_id=subscription_id,
        resource_group=_validated_name(
            payload.get("resource_group", _DEFAULT_RESOURCE_GROUP),
            "resource_group_invalid",
        ),
        function_name=_validated_name(
            payload.get("function_name", _DEFAULT_FUNCTION_NAME),
            "function_name_invalid",
        ),
        app_insights_name=_validated_name(
            payload.get("app_insights_name", _DEFAULT_APP_INSIGHTS_NAME),
            "app_insights_name_invalid",
        ),
        log_analytics_name=_validated_name(
            payload.get("log_analytics_name", _DEFAULT_LOG_ANALYTICS_NAME),
            "log_analytics_name_invalid",
        ),
        storage_account=_validated_name(
            payload.get("storage_account", _DEFAULT_STORAGE_ACCOUNT),
            "storage_account_invalid",
        ),
        content_container=_validated_name(
            payload.get("content_container", _DEFAULT_CONTENT_CONTAINER),
            "content_container_invalid",
        ),
        sandbox_group=_validated_name(
            payload.get("sandbox_group", _DEFAULT_SANDBOX_GROUP),
            "sandbox_group_invalid",
        ),
        sandbox_region=_validated_region(payload.get("sandbox_region", _DEFAULT_SANDBOX_REGION)),
        apim_resource_group=_validated_name(
            payload.get("apim_resource_group", _DEFAULT_APIM_RESOURCE_GROUP),
            "apim_resource_group_invalid",
        ),
        apim_service=_validated_name(
            payload.get("apim_service", _DEFAULT_APIM_SERVICE),
            "apim_service_invalid",
        ),
        apim_apis=apim_apis,
        model_control_api=model_control_api,
    )


def _parse_sandbox_selector(value: Any) -> Mapping[str, str]:
    if value is None:
        return {}
    payload = _require_mapping(value, "sandbox_selector_invalid")
    if not 1 <= len(payload) <= 8:
        raise InputValidationError("sandbox_selector_invalid")
    parsed: dict[str, str] = {}
    for key, raw_value in payload.items():
        if (
            not isinstance(key, str)
            or _SAFE_LABEL_KEY.fullmatch(key) is None
            or not isinstance(raw_value, str)
            or _SAFE_LABEL_VALUE.fullmatch(raw_value) is None
        ):
            raise InputValidationError("sandbox_selector_invalid")
        parsed[key] = raw_value
    return parsed


def parse_operator_input(value: Any, *, require_resources: bool) -> OperatorInput:
    """Validate the complete operator document without retaining rejected values."""
    payload = _require_mapping(value, "operator_input_must_be_object")
    _strict_keys(
        payload,
        frozenset(
            {
                "canaries",
                "exact_ids",
                "resources",
                "sandbox_selector",
                "fixtures",
            }
        ),
        "operator_input_fields_invalid",
    )
    resources = _parse_resources(payload.get("resources"))
    if require_resources and resources is None:
        raise InputValidationError("resources_required")
    fixtures = payload.get("fixtures")
    if fixtures is not None:
        fixtures = _require_mapping(fixtures, "fixtures_invalid")
    return OperatorInput(
        canaries=_parse_canaries(payload),
        resources=resources,
        sandbox_selector=_parse_sandbox_selector(payload.get("sandbox_selector")),
        fixtures=fixtures,
    )


class PrivacyMatcher:
    """Count bounded exact and regex matches without retaining snippets."""

    _RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
        (
            "provider_response_id",
            re.compile(r"(?<![A-Za-z0-9])resp_[A-Za-z0-9]{8,160}(?![A-Za-z0-9])"),
        ),
        (
            "provider_call_id",
            re.compile(r"(?<![A-Za-z0-9])call_[A-Za-z0-9]{8,160}(?![A-Za-z0-9])"),
        ),
        (
            "bearer_or_jwt",
            re.compile(
                r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}|"
                r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
            ),
        ),
        (
            "api_or_subscription_key",
            re.compile(r"(?i)(?:api|subscription)[-_ ]?key\s*(?:=|:)\s*[A-Za-z0-9._~+/=-]{8,}"),
        ),
        (
            "storage_secret",
            re.compile(r"(?i)\b(?:AccountKey|SharedAccessSignature)\s*="),
        ),
        (
            "sas_parameter",
            re.compile(r"(?i)(?:\?|&)(?:sv|sig|se|sp|sr|spr|st)="),
        ),
        (
            "blob_url",
            re.compile(r"(?i)\bhttps://[a-z0-9-]{3,63}\.blob\.core\.windows\.net(?:/[^\s\"'<>]*)?"),
        ),
        (
            "protected_label",
            re.compile(r"(?i)\b(?:encrypted_content|protected_data)\b"),
        ),
        (
            "sandbox_id",
            re.compile(
                r"(?i)(?<![A-Za-z0-9])sbx_[A-Za-z0-9-]{8,160}(?![A-Za-z0-9])|"
                r"/sandboxes/[A-Za-z0-9-]{8,160}"
            ),
        ),
    )

    def __init__(self, canaries: CanarySet) -> None:
        self._canaries = canaries

    def scan_text(
        self,
        value: str,
        *,
        allowed_categories: frozenset[str] = frozenset(),
    ) -> Counter[str]:
        encoded = value.encode("utf-8", errors="replace")
        if len(encoded) > _MAX_STRING_BYTES:
            return Counter({"oversized_value": 1})
        counts: Counter[str] = Counter()
        for category, exact in self._canaries.all_exact:
            if exact not in value:
                continue
            if category in allowed_categories and category not in _ALWAYS_FORBIDDEN_CATEGORIES:
                continue
            counts[f"exact_{category}"] += value.count(exact)
        for rule, pattern in self._RULES:
            match_count = sum(1 for _ in pattern.finditer(value))
            if match_count:
                counts[rule] += match_count
        return counts

    @property
    def kusto_literals(self) -> tuple[str, ...]:
        return tuple(value for _, value in self._canaries.all_exact)

    @property
    def kusto_patterns(self) -> tuple[str, ...]:
        return tuple(pattern.pattern for _, pattern in self._RULES)


def _path_leaf(path: tuple[str, ...]) -> str:
    return path[-1].casefold() if path else ""


def _durable_string_is_allowed(path: tuple[str, ...], value: str) -> bool:
    leaf = _path_leaf(path)
    if leaf in _SENSITIVE_DURABLE_FIELDS:
        return True
    if leaf in _DURABLE_ID_FIELDS:
        return _SAFE_IDENTIFIER.fullmatch(value) is not None
    if leaf in _DURABLE_TIMESTAMP_FIELDS:
        return _ISO_TIMESTAMP.fullmatch(value) is not None
    if leaf in _DURABLE_ENUM_FIELDS:
        return value in _DURABLE_ENUM_FIELDS[leaf]
    if leaf in _DURABLE_SAFE_TEXT_FIELDS:
        return len(value.encode("utf-8")) <= 512 and "\r" not in value and "\n" not in value
    if leaf in {"hash", "sha256", "contenthash", "requesthash"}:
        return _SHA256.fullmatch(value) is not None
    if leaf in {"objectid", "object_id"} and "contentref" in {
        segment.casefold() for segment in path
    }:
        return _SAFE_IDENTIFIER.fullmatch(value) is not None
    return False


def scan_recursive(
    value: Any,
    matcher: PrivacyMatcher,
    *,
    durable_envelope: bool = False,
    allowed_categories: frozenset[str] = frozenset(),
    path: tuple[str, ...] = ("$",),
) -> MatchSummary:
    """Recursively scan one in-memory value and return aggregate counts only."""
    counts: Counter[str] = Counter()
    scanned = 0

    def visit(item: Any, current_path: tuple[str, ...]) -> None:
        nonlocal scanned
        if scanned >= _MAX_ITEMS:
            counts["item_limit"] += 1
            return
        scanned += 1
        if isinstance(item, Mapping):
            for key, child in item.items():
                key_text = str(key)
                counts.update(matcher.scan_text(key_text))
                visit(child, (*current_path, key_text))
            return
        if isinstance(item, list | tuple):
            for index, child in enumerate(item):
                visit(child, (*current_path, str(index)))
            return
        if isinstance(item, bytes):
            if len(item) > _MAX_STRING_BYTES:
                counts["oversized_value"] += 1
                return
            try:
                text = item.decode("utf-8")
            except UnicodeDecodeError:
                counts["binary_content"] += 1
                return
            counts.update(matcher.scan_text(text, allowed_categories=allowed_categories))
            if durable_envelope and not _durable_string_is_allowed(current_path, text):
                counts["unexpected_free_form"] += 1
            return
        if isinstance(item, str):
            counts.update(matcher.scan_text(item, allowed_categories=allowed_categories))
            if durable_envelope and not _durable_string_is_allowed(current_path, item):
                counts["unexpected_free_form"] += 1
            return
        if item is None or isinstance(item, bool | int | float):
            return
        counts["unsupported_type"] += 1

    visit(value, path)
    return MatchSummary(scanned=scanned, rule_counts=dict(counts))


def _merge_summaries(summaries: Iterable[MatchSummary]) -> MatchSummary:
    counts: Counter[str] = Counter()
    scanned = 0
    for summary in summaries:
        scanned += summary.scanned
        counts.update(summary.rule_counts)
    return MatchSummary(scanned=scanned, rule_counts=dict(counts))


def _result_from_summary(
    surface: str,
    summary: MatchSummary,
    *,
    required: bool = True,
    truncated: bool = False,
) -> ScanResult:
    counts = Counter(summary.rule_counts)
    if truncated:
        return ScanResult(
            surface=surface,
            scanned=summary.scanned,
            violations=summary.violations,
            status=ScanStatus.INCONCLUSIVE,
            rule_counts=dict(counts),
            code="item_limit",
        )
    if summary.violations:
        return ScanResult(
            surface=surface,
            scanned=summary.scanned,
            violations=summary.violations,
            status=ScanStatus.FAIL,
            rule_counts=dict(counts),
        )
    if summary.scanned == 0:
        return ScanResult(
            surface=surface,
            scanned=0,
            violations=0,
            status=(ScanStatus.INCONCLUSIVE if required else ScanStatus.NOT_APPLICABLE),
            code="expected_data_missing" if required else None,
        )
    return ScanResult(
        surface=surface,
        scanned=summary.scanned,
        violations=0,
        status=ScanStatus.PASS,
    )


def classify_taskhub_table(name: str) -> str | None:
    """Return the fixed durable surface for a task-hub table name."""
    normalized = name.casefold()
    for suffix, surface in _TASKHUB_TABLE_SUFFIXES.items():
        if normalized.endswith(suffix):
            return surface
    return None


def is_taskhub_container(name: str, content_container: str) -> bool:
    """Select task-hub containers while excluding secrets and content storage."""
    normalized = name.casefold()
    return (
        normalized != "azure-webjobs-secrets"
        and normalized != content_container.casefold()
        and _TASKHUB_CONTAINER.search(normalized) is not None
    )


def is_taskhub_queue(name: str) -> bool:
    """Select durable control and work-item queues."""
    return _TASKHUB_QUEUE.search(name) is not None


def scan_tables(batch: Batch, matcher: PrivacyMatcher) -> tuple[ScanResult, ...]:
    """Scan matching durable task-hub tables."""
    grouped: dict[str, list[MatchSummary]] = {
        surface: [] for surface in _TASKHUB_TABLE_SUFFIXES.values()
    }
    for item in batch.items:
        if not isinstance(item, TableSnapshot):
            continue
        surface = classify_taskhub_table(item.name)
        if surface is None:
            continue
        grouped[surface].extend(
            scan_recursive(entity, matcher, durable_envelope=True) for entity in item.entities
        )
    return tuple(
        _result_from_summary(
            surface,
            _merge_summaries(grouped[surface]),
            truncated=batch.truncated,
        )
        for surface in _TASKHUB_TABLE_SUFFIXES.values()
    )


def scan_taskhub_blobs(
    batch: Batch,
    matcher: PrivacyMatcher,
    *,
    content_container: str,
) -> ScanResult:
    """Scan matching task-hub blob names, metadata, headers, and bodies."""
    summaries: list[MatchSummary] = []
    oversize = 0
    for item in batch.items:
        if not isinstance(item, BlobSnapshot) or not is_taskhub_container(
            item.container, content_container
        ):
            continue
        summaries.append(scan_recursive(item.name, matcher))
        summaries.append(scan_recursive(item.metadata, matcher))
        summaries.append(scan_recursive(item.tags, matcher))
        summaries.append(scan_recursive(item.headers, matcher))
        if item.oversized:
            oversize += 1
        elif item.body is not None:
            if "message" in item.container.casefold():
                try:
                    envelope = load_bounded_json_bytes(item.body)
                except PrivacyQualificationError:
                    summaries.append(
                        MatchSummary(
                            scanned=1,
                            rule_counts={"durable_body_invalid_json": 1},
                        )
                    )
                    summaries.append(scan_recursive(item.body, matcher))
                else:
                    summaries.append(
                        scan_recursive(
                            envelope,
                            matcher,
                            durable_envelope=True,
                        )
                    )
            else:
                summaries.append(scan_recursive(item.body, matcher))
    summary = _merge_summaries(summaries)
    if oversize:
        counts = Counter(summary.rule_counts)
        counts["oversized_blob"] += oversize
        summary = MatchSummary(scanned=summary.scanned, rule_counts=dict(counts))
    return _result_from_summary(
        "Storage/TaskHubBlobs",
        summary,
        truncated=batch.truncated,
    )


def scan_queues(batch: Batch, matcher: PrivacyMatcher) -> ScanResult:
    """Scan durable queue metadata and require a non-destructive final drain."""
    summaries: list[MatchSummary] = []
    nonempty = 0
    matched = 0
    for item in batch.items:
        if not isinstance(item, QueueSnapshot) or not is_taskhub_queue(item.name):
            continue
        matched += 1
        summaries.append(scan_recursive(item.name, matcher))
        summaries.append(scan_recursive(item.metadata, matcher))
        if item.approximate_message_count > 0:
            nonempty += 1
    summary = _merge_summaries(summaries)
    if summary.violations:
        return _result_from_summary("Storage/Queues", summary)
    if batch.truncated:
        return ScanResult(
            surface="Storage/Queues",
            scanned=summary.scanned,
            violations=0,
            status=ScanStatus.INCONCLUSIVE,
            code="item_limit",
        )
    if matched == 0:
        return ScanResult(
            surface="Storage/Queues",
            scanned=0,
            violations=0,
            status=ScanStatus.INCONCLUSIVE,
            code="expected_data_missing",
        )
    if nonempty:
        return ScanResult(
            surface="Storage/Queues",
            scanned=summary.scanned,
            violations=0,
            status=ScanStatus.INCONCLUSIVE,
            rule_counts={"nonempty_queue": nonempty},
            code="queue_not_drained",
        )
    return ScanResult(
        surface="Storage/Queues",
        scanned=summary.scanned,
        violations=0,
        status=ScanStatus.PASS,
    )


def _casefold_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key).casefold(): item for key, item in value.items()}


def _blob_body_bytes(item: BlobSnapshot) -> bytes | None:
    if item.oversized or item.body is None:
        return None
    return item.body


def _content_digest_counts(item: BlobSnapshot, body: bytes | None) -> Counter[str]:
    counts: Counter[str] = Counter()
    name_match = _HASH_ADDRESSED_NAME.search(item.name)
    if name_match is None:
        counts["content_name_not_hash_addressed"] += 1
    if body is None:
        counts["content_body_unavailable"] += 1
        return counts
    digest = hashlib.sha256(body).hexdigest()
    if name_match is not None and name_match.group(1) != digest:
        counts["content_name_hash_mismatch"] += 1
    metadata = _casefold_mapping(item.metadata)
    raw_digest = metadata.get("sha256")
    if not isinstance(raw_digest, str):
        counts["content_sha256_missing"] += 1
    else:
        digest_match = _SHA256.fullmatch(raw_digest)
        if digest_match is None or digest_match.group(1) != digest:
            counts["content_sha256_mismatch"] += 1
    return counts


def _content_length_counts(item: BlobSnapshot, body: bytes | None) -> Counter[str]:
    counts: Counter[str] = Counter()
    if body is None:
        return counts
    metadata = _casefold_mapping(item.metadata)
    raw_count = metadata.get("byte_count")
    try:
        byte_count = int(raw_count)
    except (TypeError, ValueError):
        counts["content_byte_count_missing"] += 1
    else:
        if byte_count != len(body):
            counts["content_byte_count_mismatch"] += 1
    headers = _casefold_mapping(item.headers)
    content_length = headers.get("content_length")
    if content_length is not None:
        try:
            parsed_length = int(content_length)
        except (TypeError, ValueError):
            counts["content_length_invalid"] += 1
        else:
            if parsed_length != len(body):
                counts["content_length_mismatch"] += 1
    return counts


def _content_integrity_counts(item: BlobSnapshot, body: bytes | None) -> Counter[str]:
    counts = _content_digest_counts(item, body)
    counts.update(_content_length_counts(item, body))
    return counts


def scan_content_blobs(
    batch: Batch,
    matcher: PrivacyMatcher,
    *,
    content_container: str,
) -> ScanResult:
    """Validate protected content placement and hash-addressed integrity."""
    summaries: list[MatchSummary] = []
    integrity: Counter[str] = Counter()
    matched = 0
    for item in batch.items:
        if (
            not isinstance(item, BlobSnapshot)
            or item.container.casefold() != content_container.casefold()
        ):
            continue
        matched += 1
        summaries.append(scan_recursive(item.name, matcher))
        summaries.append(scan_recursive(item.metadata, matcher))
        summaries.append(scan_recursive(item.tags, matcher))
        summaries.append(scan_recursive(item.headers, matcher))
        body = _blob_body_bytes(item)
        integrity.update(_content_integrity_counts(item, body))
        metadata = _casefold_mapping(item.metadata)
        object_class = metadata.get("object_class", metadata.get("content_class"))
        if not isinstance(object_class, str) or object_class not in _CONTENT_CLASS_CATEGORIES:
            integrity["content_class_invalid"] += 1
            allowed_categories = frozenset()
        else:
            allowed_categories = _CONTENT_CLASS_CATEGORIES[object_class]
        if body is not None:
            summaries.append(
                scan_recursive(
                    body,
                    matcher,
                    allowed_categories=allowed_categories,
                )
            )
    summary = _merge_summaries(summaries)
    combined = Counter(summary.rule_counts)
    combined.update(integrity)
    combined_summary = MatchSummary(
        scanned=summary.scanned,
        rule_counts=dict(combined),
    )
    if matched == 0:
        return ScanResult(
            surface="Content/ProtectedBlobs",
            scanned=0,
            violations=0,
            status=ScanStatus.INCONCLUSIVE,
            code="expected_data_missing",
        )
    return _result_from_summary(
        "Content/ProtectedBlobs",
        combined_summary,
        truncated=batch.truncated,
    )


def _kusto_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def build_kusto_aggregate_query(
    *,
    tables: Sequence[str],
    timestamp_column: str,
    matcher: PrivacyMatcher,
    start: datetime,
    end: datetime,
) -> str:
    """Build a query whose only result columns are aggregate counts."""
    if not tables or not all(_SAFE_NAME.fullmatch(table) for table in tables):
        raise InputValidationError("kusto_tables_invalid")
    if timestamp_column not in {"timestamp", "TimeGenerated"}:
        raise InputValidationError("kusto_timestamp_invalid")
    predicates = [
        f"__privacy_text contains {_kusto_string(value)}" for value in matcher.kusto_literals
    ]
    predicates.extend(
        f"__privacy_text matches regex {_kusto_string(pattern)}"
        for pattern in matcher.kusto_patterns
    )
    violation_predicate = " or ".join(predicates) if predicates else "false"
    return (
        f"union isfuzzy=true {', '.join(tables)}\n"
        f"| where {timestamp_column} between "
        f"(datetime({start.astimezone(UTC).isoformat()}) .. "
        f"datetime({end.astimezone(UTC).isoformat()}))\n"
        "| extend __privacy_text = tostring(pack_all())\n"
        "| summarize scanned=count(), "
        f"violations=countif({violation_predicate})"
    )


def scan_aggregate_observation(
    surface: str,
    observation: AggregateObservation,
) -> ScanResult:
    """Convert a server-side aggregate into a fixed content-free result."""
    if observation.scanned < 0 or observation.violations < 0:
        return ScanResult(
            surface=surface,
            scanned=0,
            violations=0,
            status=ScanStatus.INCONCLUSIVE,
            code="aggregate_invalid",
        )
    if observation.violations > observation.scanned:
        return ScanResult(
            surface=surface,
            scanned=observation.scanned,
            violations=observation.violations,
            status=ScanStatus.INCONCLUSIVE,
            code="aggregate_invalid",
        )
    if observation.scanned == 0:
        return ScanResult(
            surface=surface,
            scanned=0,
            violations=0,
            status=ScanStatus.INCONCLUSIVE,
            code="expected_data_missing",
        )
    return ScanResult(
        surface=surface,
        scanned=observation.scanned,
        violations=observation.violations,
        status=ScanStatus.FAIL if observation.violations else ScanStatus.PASS,
        rule_counts=(
            {"server_aggregate": observation.violations} if observation.violations else {}
        ),
    )


def _body_capture_is_zero_or_absent(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, Mapping):
        return False
    return value.get("bytes") == 0 and set(value).issubset({"bytes"})


def _header_names(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return tuple(item.casefold() for item in value)


def _diagnostic_capture_counts(properties: Mapping[str, Any]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for side_name in ("frontend", "backend"):
        side = properties.get(side_name)
        if side is None:
            continue
        if not isinstance(side, Mapping):
            counts["diagnostic_shape_invalid"] += 1
            continue
        for direction_name in ("request", "response"):
            direction = side.get(direction_name)
            if direction is None:
                continue
            if not isinstance(direction, Mapping):
                counts["diagnostic_shape_invalid"] += 1
                continue
            if not _body_capture_is_zero_or_absent(direction.get("body")):
                counts["body_capture_enabled"] += 1
            headers = _header_names(direction.get("headers"))
            if headers is None:
                counts["diagnostic_headers_invalid"] += 1
                continue
            if any(header in _SENSITIVE_HEADERS for header in headers):
                counts["sensitive_header_capture"] += 1
            if any(header not in _SAFE_APIM_HEADERS for header in headers):
                counts["unexpected_header_capture"] += 1
    if properties.get("logClientIp") not in {False, None}:
        counts["client_ip_logging_enabled"] += 1
    return counts


def _model_control_diagnostic_counts(properties: Mapping[str, Any]) -> Counter[str]:
    counts: Counter[str] = Counter()
    sampling = properties.get("sampling")
    percentage = sampling.get("percentage") if isinstance(sampling, Mapping) else None
    if percentage != 0:
        counts["model_control_sampling_nonzero"] += 1
    for side_name in ("frontend", "backend"):
        side = properties.get(side_name)
        if not isinstance(side, Mapping):
            continue
        for direction_name in ("request", "response"):
            direction = side.get(direction_name)
            if direction is None or not isinstance(direction, Mapping):
                continue
            if direction.get("body") is not None:
                counts["model_control_body_configured"] += 1
            if direction.get("headers") is not None:
                counts["model_control_headers_configured"] += 1
    return counts


def _diagnostic_entry_counts(
    api_name: str,
    entry: Mapping[str, Any],
    *,
    model_control_api: str,
) -> Counter[str]:
    properties = entry.get("properties", entry)
    if not isinstance(properties, Mapping):
        return Counter({"diagnostic_shape_invalid": 1})
    counts = _diagnostic_capture_counts(properties)
    sampling = properties.get("sampling")
    percentage = sampling.get("percentage") if isinstance(sampling, Mapping) else None
    if api_name == model_control_api:
        counts.update(_model_control_diagnostic_counts(properties))
    elif percentage is None:
        counts["diagnostic_sampling_missing"] += 1
    return counts


def scan_apim_diagnostics(
    diagnostics: Mapping[str, Sequence[Mapping[str, Any]]],
    api_names: Sequence[str],
    *,
    model_control_api: str = _MODEL_CONTROL_API,
) -> ScanResult:
    """Verify body/header capture is disabled and control sampling is zero."""
    counts: Counter[str] = Counter()
    scanned = 0
    for api_name in api_names:
        entries = diagnostics.get(api_name, ())
        if not entries:
            counts["diagnostic_missing"] += 1
            continue
        for entry in entries:
            scanned += 1
            counts.update(
                _diagnostic_entry_counts(
                    api_name,
                    entry,
                    model_control_api=model_control_api,
                )
            )
    summary = MatchSummary(scanned=scanned, rule_counts=dict(counts))
    return _result_from_summary("APIM/Diagnostics", summary)


def _sandbox_owned(
    sandbox: SandboxSnapshot,
    selector: Mapping[str, str],
) -> bool:
    return bool(selector) and all(
        sandbox.labels.get(key) == value for key, value in selector.items()
    )


async def scan_sandbox_inventory(
    adapter: LiveScanAdapter,
    matcher: PrivacyMatcher,
    *,
    selector: Mapping[str, str],
    wait_seconds: int,
    poll_seconds: int,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> ScanResult:
    """Require app-owned inventory to reach zero within a bounded wait."""
    if not selector:
        return ScanResult(
            surface="Sandbox/Inventory",
            scanned=0,
            violations=0,
            status=ScanStatus.INCONCLUSIVE,
            code="sandbox_selector_missing",
        )
    deadline = time.monotonic() + wait_seconds
    scanned = 0
    last_owned = 0
    while True:
        inventory = await adapter.sandboxes(selector)
        owned = [sandbox for sandbox in inventory if _sandbox_owned(sandbox, selector)]
        scanned += len(inventory)
        last_owned = len(owned)
        identifier_summary = _merge_summaries(
            scan_recursive(sandbox.sandbox_id, matcher) for sandbox in inventory
        )
        label_summary = _merge_summaries(
            scan_recursive(sandbox.labels, matcher) for sandbox in inventory
        )
        combined = _merge_summaries((identifier_summary, label_summary))
        if combined.violations:
            return _result_from_summary("Sandbox/Inventory", combined)
        if not owned:
            return ScanResult(
                surface="Sandbox/Inventory",
                scanned=scanned,
                violations=0,
                status=ScanStatus.PASS,
            )
        if time.monotonic() >= deadline:
            return ScanResult(
                surface="Sandbox/Inventory",
                scanned=scanned,
                violations=last_owned,
                status=ScanStatus.FAIL,
                rule_counts={"remaining_owned_sandbox": last_owned},
            )
        await sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))


def _bounded_error_code(prefix: str, error: BaseException) -> str:
    class_name = type(error).__name__
    if _SAFE_ERROR_CLASS.fullmatch(class_name) is None:
        class_name = "Error"
    code = f"{prefix}_{class_name}"
    return code[:_MAX_ERROR_CODE_CHARS]


def inconclusive_from_exception(surface: str, error: BaseException) -> ScanResult:
    """Project an exception class into a bounded code without its message."""
    return ScanResult(
        surface=surface,
        scanned=0,
        violations=0,
        status=ScanStatus.INCONCLUSIVE,
        code=_bounded_error_code("access", error),
    )


def _safe_rule_counts(counts: Mapping[str, int]) -> dict[str, int]:
    result: dict[str, int] = {}
    for key, value in sorted(counts.items())[:_MAX_RULE_COUNTS]:
        if (
            isinstance(key, str)
            and _SAFE_LABEL_KEY.fullmatch(key) is not None
            and isinstance(value, int)
            and not isinstance(value, bool)
            and 0 < value <= _MAX_ITEMS
        ):
            result[key] = value
    return result


def render_result(result: ScanResult) -> str:
    """Render one fixed, content-free, single-line JSON result."""
    payload: dict[str, Any] = {
        "surface": result.surface,
        "scanned": max(0, min(result.scanned, _MAX_ITEMS * 100)),
        "violations": max(0, min(result.violations, _MAX_ITEMS * 100)),
        "status": result.status.value,
    }
    safe_counts = _safe_rule_counts(result.rule_counts)
    if safe_counts:
        payload["rule_counts"] = safe_counts
    if result.code is not None:
        payload["code"] = result.code[:_MAX_ERROR_CODE_CHARS]
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def render_aggregate(aggregate: ScanAggregate) -> str:
    """Render the final aggregate using status counts only."""
    status_counts = Counter(result.status.value for result in aggregate.results)
    payload = {
        "summary": {status.value: status_counts.get(status.value, 0) for status in ScanStatus},
        "status": aggregate.status.value,
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _fixture_mapping(fixtures: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = fixtures.get(key, {})
    return _require_mapping(value, f"fixture_{key}_invalid")


def _fixture_sequence(fixtures: Mapping[str, Any], key: str) -> Sequence[Any]:
    value = fixtures.get(key, [])
    if not isinstance(value, list):
        raise InputValidationError(f"fixture_{key}_invalid")
    if len(value) > _MAX_ITEMS:
        raise InputValidationError(f"fixture_{key}_too_large")
    return value


def _fixture_blob(item: Any) -> BlobSnapshot:
    payload = _require_mapping(item, "fixture_blob_invalid")
    body_value = payload.get("body")
    if body_value is None:
        body = None
    elif isinstance(body_value, str):
        body = body_value.encode("utf-8")
    elif isinstance(body_value, list) and all(
        isinstance(part, int) and not isinstance(part, bool) and 0 <= part <= 255
        for part in body_value
    ):
        body = bytes(body_value)
    else:
        raise InputValidationError("fixture_blob_body_invalid")
    return BlobSnapshot(
        container=_validated_name(payload.get("container"), "fixture_container_invalid"),
        name=_validated_blob_name(payload.get("name")),
        metadata=_require_mapping(payload.get("metadata", {}), "fixture_metadata_invalid"),
        tags=_require_mapping(payload.get("tags", {}), "fixture_tags_invalid"),
        headers=_require_mapping(payload.get("headers", {}), "fixture_headers_invalid"),
        body=body,
        oversized=payload.get("oversized", False) is True,
    )


def _fixture_batch(fixtures: Mapping[str, Any], key: str, items: Sequence[Any]) -> Batch:
    truncated = fixtures.get(f"{key}_truncated", False)
    if not isinstance(truncated, bool):
        raise InputValidationError(f"fixture_{key}_truncated_invalid")
    return Batch(items=tuple(items), truncated=truncated)


def _fixture_tables(fixtures: Mapping[str, Any]) -> Batch:
    snapshots: list[TableSnapshot] = []
    for item in _fixture_sequence(fixtures, "tables"):
        payload = _require_mapping(item, "fixture_table_invalid")
        entities = payload.get("entities")
        if not isinstance(entities, list) or not all(
            isinstance(entity, Mapping) for entity in entities
        ):
            raise InputValidationError("fixture_entities_invalid")
        snapshots.append(
            TableSnapshot(
                name=_validated_name(payload.get("name"), "fixture_table_name_invalid"),
                entities=tuple(entities),
            )
        )
    return _fixture_batch(fixtures, "tables", snapshots)


def _fixture_blobs(fixtures: Mapping[str, Any]) -> Batch:
    return _fixture_batch(
        fixtures,
        "blobs",
        tuple(_fixture_blob(item) for item in _fixture_sequence(fixtures, "blobs")),
    )


def _fixture_queues(fixtures: Mapping[str, Any]) -> Batch:
    snapshots: list[QueueSnapshot] = []
    for item in _fixture_sequence(fixtures, "queues"):
        payload = _require_mapping(item, "fixture_queue_invalid")
        message_count = payload.get("approximate_message_count")
        if (
            not isinstance(message_count, int)
            or isinstance(message_count, bool)
            or message_count < 0
            or message_count > _MAX_ITEMS
        ):
            raise InputValidationError("fixture_queue_count_invalid")
        snapshots.append(
            QueueSnapshot(
                name=_validated_name(payload.get("name"), "fixture_queue_name_invalid"),
                metadata=_require_mapping(
                    payload.get("metadata", {}),
                    "fixture_queue_metadata_invalid",
                ),
                approximate_message_count=message_count,
            )
        )
    return _fixture_batch(fixtures, "queues", snapshots)


def _fixture_observation(fixtures: Mapping[str, Any], key: str) -> AggregateObservation:
    payload = _fixture_mapping(fixtures, key)
    scanned = payload.get("scanned")
    violations = payload.get("violations")
    if (
        not isinstance(scanned, int)
        or isinstance(scanned, bool)
        or not isinstance(violations, int)
        or isinstance(violations, bool)
    ):
        raise InputValidationError(f"fixture_{key}_aggregate_invalid")
    return AggregateObservation(scanned=scanned, violations=violations)


class FixtureLiveAdapter:
    """Deterministic adapter used by scan-json and unit tests."""

    def __init__(self, fixtures: Mapping[str, Any]) -> None:
        self._fixtures = fixtures
        self.telemetry_queries: list[str] = []
        self._sandbox_calls = 0

    def tables(self) -> Batch:
        return _fixture_tables(self._fixtures)

    def blobs(self) -> Batch:
        return _fixture_blobs(self._fixtures)

    def queues(self) -> Batch:
        return _fixture_queues(self._fixtures)

    def telemetry(
        self,
        *,
        surface: str,
        query: str,
        start: datetime,
        end: datetime,
        timeout_seconds: int,
    ) -> AggregateObservation:
        del start, end, timeout_seconds
        self.telemetry_queries.append(query)
        key = {
            "Telemetry/AppInsights": "app_insights",
            "Telemetry/LogAnalytics": "log_analytics",
            "Telemetry/APIM": "apim_logs",
        }[surface]
        return _fixture_observation(self._fixtures, key)

    def apim_diagnostics(self) -> Mapping[str, Sequence[Mapping[str, Any]]]:
        payload = _fixture_mapping(self._fixtures, "apim_diagnostics")
        result: dict[str, Sequence[Mapping[str, Any]]] = {}
        for key, value in payload.items():
            if (
                not isinstance(key, str)
                or not isinstance(value, list)
                or not all(isinstance(item, Mapping) for item in value)
            ):
                raise InputValidationError("fixture_apim_diagnostics_invalid")
            result[key] = value
        return result

    async def sandboxes(self, labels: Mapping[str, str]) -> Sequence[SandboxSnapshot]:
        del labels
        sequences = self._fixtures.get("sandbox_inventory", [])
        if not isinstance(sequences, list):
            raise InputValidationError("fixture_sandbox_inventory_invalid")
        if sequences and isinstance(sequences[0], list):
            index = min(self._sandbox_calls, len(sequences) - 1)
            raw_items = sequences[index]
        else:
            raw_items = sequences
        self._sandbox_calls += 1
        if not isinstance(raw_items, list):
            raise InputValidationError("fixture_sandbox_inventory_invalid")
        snapshots: list[SandboxSnapshot] = []
        for item in raw_items:
            payload = _require_mapping(item, "fixture_sandbox_invalid")
            sandbox_id = payload.get("sandbox_id")
            if not isinstance(sandbox_id, str) or not sandbox_id:
                raise InputValidationError("fixture_sandbox_id_invalid")
            labels_payload = _require_mapping(
                payload.get("labels", {}),
                "fixture_sandbox_labels_invalid",
            )
            if not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in labels_payload.items()
            ):
                raise InputValidationError("fixture_sandbox_labels_invalid")
            snapshots.append(SandboxSnapshot(sandbox_id=sandbox_id, labels=dict(labels_payload)))
        return snapshots

    def activity_records(self) -> Batch | None:
        if "activity_logs" not in self._fixtures:
            return None
        return _fixture_batch(
            self._fixtures,
            "activity_logs",
            _fixture_sequence(self._fixtures, "activity_logs"),
        )

    def close(self) -> None:
        return None


def _telemetry_specs(
    matcher: PrivacyMatcher,
    timing: TimingConfig,
) -> tuple[tuple[str, str], ...]:
    return (
        (
            "Telemetry/AppInsights",
            build_kusto_aggregate_query(
                tables=_APP_INSIGHTS_TABLES,
                timestamp_column="timestamp",
                matcher=matcher,
                start=timing.start,
                end=timing.end,
            ),
        ),
        (
            "Telemetry/LogAnalytics",
            build_kusto_aggregate_query(
                tables=_LOG_ANALYTICS_TABLES,
                timestamp_column="TimeGenerated",
                matcher=matcher,
                start=timing.start,
                end=timing.end,
            ),
        ),
        (
            "Telemetry/APIM",
            build_kusto_aggregate_query(
                tables=_APIM_LOG_TABLES,
                timestamp_column="TimeGenerated",
                matcher=matcher,
                start=timing.start,
                end=timing.end,
            ),
        ),
    )


def _scan_telemetry_with_retries(
    adapter: LiveScanAdapter,
    *,
    surface: str,
    query: str,
    timing: TimingConfig,
    sleep: Callable[[float], None] = time.sleep,
) -> ScanResult:
    last = AggregateObservation(scanned=0, violations=0)
    for attempt in range(timing.telemetry_lag_retries + 1):
        last = adapter.telemetry(
            surface=surface,
            query=query,
            start=timing.start,
            end=timing.end,
            timeout_seconds=timing.timeout_seconds,
        )
        result = scan_aggregate_observation(surface, last)
        if result.status is not ScanStatus.INCONCLUSIVE:
            return result
        if attempt < timing.telemetry_lag_retries:
            sleep(timing.telemetry_lag_seconds)
    return scan_aggregate_observation(surface, last)


async def run_scan(
    adapter: LiveScanAdapter,
    operator: OperatorInput,
    timing: TimingConfig,
    *,
    sleep_sync: Callable[[float], None] = time.sleep,
    sleep_async: Callable[[float], Any] = asyncio.sleep,
) -> ScanAggregate:
    """Run all required scanners through an injected adapter."""
    matcher = PrivacyMatcher(operator.canaries)
    results: list[ScanResult] = []
    content_container = (
        operator.resources.content_container
        if operator.resources is not None
        else _DEFAULT_CONTENT_CONTAINER
    )
    api_names = (
        operator.resources.apim_apis if operator.resources is not None else _DEFAULT_APIM_APIS
    )
    model_control_api = (
        operator.resources.model_control_api
        if operator.resources is not None
        else _MODEL_CONTROL_API
    )

    try:
        results.extend(scan_tables(adapter.tables(), matcher))
    except Exception as error:
        results.extend(
            inconclusive_from_exception(surface, error)
            for surface in _TASKHUB_TABLE_SUFFIXES.values()
        )
    try:
        blob_batch = adapter.blobs()
    except Exception as error:
        results.append(inconclusive_from_exception("Storage/TaskHubBlobs", error))
        results.append(inconclusive_from_exception("Content/ProtectedBlobs", error))
    else:
        results.append(
            scan_taskhub_blobs(
                blob_batch,
                matcher,
                content_container=content_container,
            )
        )
        results.append(
            scan_content_blobs(
                blob_batch,
                matcher,
                content_container=content_container,
            )
        )
    try:
        results.append(scan_queues(adapter.queues(), matcher))
    except Exception as error:
        results.append(inconclusive_from_exception("Storage/Queues", error))

    if timing.late_scan_seconds:
        sleep_sync(timing.late_scan_seconds)
    for surface, query in _telemetry_specs(matcher, timing):
        try:
            results.append(
                _scan_telemetry_with_retries(
                    adapter,
                    surface=surface,
                    query=query,
                    timing=timing,
                    sleep=sleep_sync,
                )
            )
        except Exception as error:
            results.append(inconclusive_from_exception(surface, error))

    try:
        results.append(
            scan_apim_diagnostics(
                adapter.apim_diagnostics(),
                api_names,
                model_control_api=model_control_api,
            )
        )
    except Exception as error:
        results.append(inconclusive_from_exception("APIM/Diagnostics", error))

    try:
        results.append(
            await scan_sandbox_inventory(
                adapter,
                matcher,
                selector=operator.sandbox_selector,
                wait_seconds=timing.sandbox_wait_seconds,
                poll_seconds=timing.sandbox_poll_seconds,
                sleep=sleep_async,
            )
        )
    except Exception as error:
        results.append(inconclusive_from_exception("Sandbox/Inventory", error))

    try:
        activity = adapter.activity_records()
        if activity is None:
            results.append(
                ScanResult(
                    surface="Deployment/Activity",
                    scanned=0,
                    violations=0,
                    status=ScanStatus.NOT_APPLICABLE,
                )
            )
        else:
            summary = _merge_summaries(scan_recursive(item, matcher) for item in activity.items)
            results.append(
                _result_from_summary(
                    "Deployment/Activity",
                    summary,
                    required=False,
                    truncated=activity.truncated,
                )
            )
    except Exception as error:
        results.append(inconclusive_from_exception("Deployment/Activity", error))
    return ScanAggregate(results=tuple(results))


def _project_sdk_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Mapping):
        return {str(key): _project_sdk_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_project_sdk_value(item) for item in value]
    if value is None or isinstance(value, str | bytes | bool | int | float):
        return value
    return str(value)


class AzureLiveAdapter:
    """RBAC-only Azure adapter with lazy SDK imports and aggregate REST queries."""

    def __init__(self, resources: ResourceConfig) -> None:
        from azure.identity import DefaultAzureCredential

        self._resources = resources
        self._credential = DefaultAzureCredential()
        self._app_insights_id: str | None = None
        self._workspace_id: str | None = None

    @property
    def _storage_url(self) -> str:
        return f"https://{self._resources.storage_account}.blob.core.windows.net"

    @property
    def _table_url(self) -> str:
        return f"https://{self._resources.storage_account}.table.core.windows.net"

    @property
    def _queue_url(self) -> str:
        return f"https://{self._resources.storage_account}.queue.core.windows.net"

    def tables(self) -> Batch:
        from azure.data.tables import TableServiceClient

        service = TableServiceClient(
            endpoint=self._table_url,
            credential=self._credential,
        )
        snapshots: list[TableSnapshot] = []
        total = 0
        truncated = False
        try:
            for table_item in service.list_tables():
                name = str(table_item.name)
                if classify_taskhub_table(name) is None:
                    continue
                entities: list[Mapping[str, Any]] = []
                table = service.get_table_client(name)
                for entity in table.list_entities():
                    if total >= _MAX_ITEMS:
                        truncated = True
                        break
                    entities.append(_project_sdk_value(dict(entity)))
                    total += 1
                snapshots.append(TableSnapshot(name=name, entities=tuple(entities)))
                if truncated:
                    break
        finally:
            service.close()
        return Batch(items=tuple(snapshots), truncated=truncated)

    def blobs(self) -> Batch:
        from azure.storage.blob import BlobServiceClient

        service = BlobServiceClient(
            account_url=self._storage_url,
            credential=self._credential,
        )
        snapshots: list[BlobSnapshot] = []
        truncated = False
        try:
            for container_item in service.list_containers(include_metadata=True):
                container = str(container_item["name"])
                if not (
                    is_taskhub_container(container, self._resources.content_container)
                    or container.casefold() == self._resources.content_container.casefold()
                ):
                    continue
                client = service.get_container_client(container)
                for blob_item in client.list_blobs(include=["metadata", "tags"]):
                    if len(snapshots) >= _MAX_ITEMS:
                        truncated = True
                        break
                    blob_name = str(blob_item["name"])
                    blob_client = client.get_blob_client(blob_name)
                    size = int(blob_item.get("size", 0))
                    oversized = size > _MAX_BLOB_BYTES
                    body = (
                        None
                        if oversized
                        else blob_client.download_blob(max_concurrency=1).readall()
                    )
                    content_settings = blob_item.get("content_settings")
                    headers = {
                        "content_length": size,
                        "content_type": (
                            getattr(content_settings, "content_type", None)
                            if content_settings is not None
                            else None
                        ),
                    }
                    snapshots.append(
                        BlobSnapshot(
                            container=container,
                            name=blob_name,
                            metadata=dict(blob_item.get("metadata") or {}),
                            tags=dict(blob_item.get("tags") or {}),
                            headers=headers,
                            body=body,
                            oversized=oversized,
                        )
                    )
                if truncated:
                    break
        finally:
            service.close()
        return Batch(items=tuple(snapshots), truncated=truncated)

    def queues(self) -> Batch:
        from azure.storage.queue import QueueServiceClient

        service = QueueServiceClient(
            account_url=self._queue_url,
            credential=self._credential,
        )
        snapshots: list[QueueSnapshot] = []
        truncated = False
        try:
            for queue_item in service.list_queues(include_metadata=True):
                name = str(queue_item["name"])
                if not is_taskhub_queue(name):
                    continue
                if len(snapshots) >= _MAX_ITEMS:
                    truncated = True
                    break
                queue = service.get_queue_client(name)
                properties = queue.get_queue_properties()
                snapshots.append(
                    QueueSnapshot(
                        name=name,
                        metadata=dict(properties.metadata or {}),
                        approximate_message_count=int(properties.approximate_message_count or 0),
                    )
                )
        finally:
            service.close()
        return Batch(items=tuple(snapshots), truncated=truncated)

    def _token(self, scope: str) -> str:
        return self._credential.get_token(scope).token

    def _request_json(
        self,
        request: urllib.request.Request,
        *,
        timeout_seconds: int,
    ) -> Mapping[str, Any]:
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                content = response.read(_MAX_OPERATOR_BYTES + 1)
        except urllib.error.HTTPError:
            raise PrivacyQualificationError("azure_http_error") from None
        except (TimeoutError, urllib.error.URLError, OSError):
            raise PrivacyQualificationError("azure_transport_error") from None
        payload = load_bounded_json_bytes(content)
        return _require_mapping(payload, "azure_response_invalid")

    def _arm_get(self, path: str) -> Mapping[str, Any]:
        request = urllib.request.Request(
            f"https://management.azure.com{path}",
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer " + self._token("https://management.azure.com/.default"),
            },
            method="GET",
        )
        return self._request_json(request, timeout_seconds=_DEFAULT_TIMEOUT_SECONDS)

    def _monitoring_ids(self) -> tuple[str, str]:
        if self._app_insights_id is None:
            app = self._arm_get(
                f"/subscriptions/{self._resources.subscription_id}"
                f"/resourceGroups/{self._resources.resource_group}"
                f"/providers/Microsoft.Insights/components/"
                f"{self._resources.app_insights_name}?api-version=2020-02-02"
            )
            properties = _require_mapping(
                app.get("properties"),
                "app_insights_properties_invalid",
            )
            app_id = properties.get("AppId")
            if not isinstance(app_id, str) or not app_id:
                raise PrivacyQualificationError("app_insights_id_missing")
            self._app_insights_id = app_id
        if self._workspace_id is None:
            workspace = self._arm_get(
                f"/subscriptions/{self._resources.subscription_id}"
                f"/resourceGroups/{self._resources.resource_group}"
                f"/providers/Microsoft.OperationalInsights/workspaces/"
                f"{self._resources.log_analytics_name}?api-version=2023-09-01"
            )
            properties = _require_mapping(
                workspace.get("properties"),
                "workspace_properties_invalid",
            )
            workspace_id = properties.get("customerId")
            if not isinstance(workspace_id, str) or not workspace_id:
                raise PrivacyQualificationError("workspace_id_missing")
            self._workspace_id = workspace_id
        return self._app_insights_id, self._workspace_id

    def _query_aggregate(
        self,
        *,
        endpoint: str,
        scope: str,
        query: str,
        timeout_seconds: int,
    ) -> AggregateObservation:
        body = json.dumps({"query": query}, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer " + self._token(scope),
                "Content-Type": "application/json",
            },
            method="POST",
        )
        payload = self._request_json(request, timeout_seconds=timeout_seconds)
        tables = payload.get("tables")
        if not isinstance(tables, list) or len(tables) != 1:
            raise PrivacyQualificationError("aggregate_tables_invalid")
        table = _require_mapping(tables[0], "aggregate_table_invalid")
        columns = table.get("columns")
        rows = table.get("rows")
        if not isinstance(columns, list) or not isinstance(rows, list) or len(rows) != 1:
            raise PrivacyQualificationError("aggregate_shape_invalid")
        names = [column.get("name") if isinstance(column, Mapping) else None for column in columns]
        if names != ["scanned", "violations"]:
            raise PrivacyQualificationError("aggregate_columns_invalid")
        row = rows[0]
        if (
            not isinstance(row, list)
            or len(row) != 2
            or not all(isinstance(value, int) and not isinstance(value, bool) for value in row)
        ):
            raise PrivacyQualificationError("aggregate_row_invalid")
        return AggregateObservation(scanned=row[0], violations=row[1])

    def telemetry(
        self,
        *,
        surface: str,
        query: str,
        start: datetime,
        end: datetime,
        timeout_seconds: int,
    ) -> AggregateObservation:
        del start, end
        app_id, workspace_id = self._monitoring_ids()
        if surface == "Telemetry/AppInsights":
            return self._query_aggregate(
                endpoint=f"https://api.applicationinsights.io/v1/apps/{app_id}/query",
                scope="https://api.applicationinsights.io/.default",
                query=query,
                timeout_seconds=timeout_seconds,
            )
        return self._query_aggregate(
            endpoint=f"https://api.loganalytics.io/v1/workspaces/{workspace_id}/query",
            scope="https://api.loganalytics.io/.default",
            query=query,
            timeout_seconds=timeout_seconds,
        )

    def apim_diagnostics(self) -> Mapping[str, Sequence[Mapping[str, Any]]]:
        result: dict[str, Sequence[Mapping[str, Any]]] = {}
        for api_name in self._resources.apim_apis:
            payload = self._arm_get(
                f"/subscriptions/{self._resources.subscription_id}"
                f"/resourceGroups/{self._resources.apim_resource_group}"
                f"/providers/Microsoft.ApiManagement/service/"
                f"{self._resources.apim_service}/apis/{api_name}/diagnostics"
                "?api-version=2024-05-01"
            )
            entries = payload.get("value")
            if not isinstance(entries, list) or not all(
                isinstance(entry, Mapping) for entry in entries
            ):
                raise PrivacyQualificationError("apim_diagnostics_invalid")
            result[api_name] = entries
        return result

    async def sandboxes(self, labels: Mapping[str, str]) -> Sequence[SandboxSnapshot]:
        from azure_functions_agents.transport.aca_sdk import AcaSandboxAdapter

        resource_id = (
            f"/subscriptions/{self._resources.subscription_id}"
            f"/resourceGroups/{self._resources.resource_group}"
            f"/providers/Microsoft.App/sandboxGroups/"
            f"{self._resources.sandbox_group}"
        )
        adapter = await AcaSandboxAdapter.open(
            resource_id,
            region=self._resources.sandbox_region,
        )
        try:
            summaries = await adapter.list_sandboxes(labels=dict(labels))
            return tuple(
                SandboxSnapshot(
                    sandbox_id=summary.sandbox_id,
                    labels=dict(summary.labels),
                )
                for summary in summaries
            )
        finally:
            await adapter.close()

    def activity_records(self) -> Batch | None:
        return None

    def close(self) -> None:
        self._credential.close()


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise argparse.ArgumentTypeError("timestamp must be RFC 3339") from None
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include an offset")
    return parsed.astimezone(UTC)


def _bounded_integer(
    value: str,
    *,
    minimum: int,
    maximum: int,
    label: str,
) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{label} must be an integer") from None
    if not minimum <= parsed <= maximum:
        raise argparse.ArgumentTypeError(f"{label} must be between {minimum} and {maximum}")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    for command in ("scan-json", "live"):
        command_parser = subcommands.add_parser(command)
        command_parser.add_argument("--operator-file")
        command_parser.add_argument("--start-utc", type=_parse_datetime)
        command_parser.add_argument("--end-utc", type=_parse_datetime)
        command_parser.add_argument(
            "--telemetry-lag-retries",
            type=lambda value: _bounded_integer(
                value,
                minimum=0,
                maximum=_MAX_TELEMETRY_LAG_RETRIES,
                label="telemetry-lag-retries",
            ),
            default=_DEFAULT_TELEMETRY_LAG_RETRIES,
        )
        command_parser.add_argument(
            "--telemetry-lag-seconds",
            type=lambda value: _bounded_integer(
                value,
                minimum=0,
                maximum=_MAX_TELEMETRY_LAG_SECONDS,
                label="telemetry-lag-seconds",
            ),
            default=_DEFAULT_TELEMETRY_LAG_SECONDS,
        )
        command_parser.add_argument(
            "--timeout-seconds",
            type=lambda value: _bounded_integer(
                value,
                minimum=1,
                maximum=_MAX_TIMEOUT_SECONDS,
                label="timeout-seconds",
            ),
            default=_DEFAULT_TIMEOUT_SECONDS,
        )
        command_parser.add_argument(
            "--late-scan-seconds",
            type=lambda value: _bounded_integer(
                value,
                minimum=0,
                maximum=_MAX_LATE_SCAN_SECONDS,
                label="late-scan-seconds",
            ),
            default=_DEFAULT_LATE_SCAN_SECONDS,
        )
        command_parser.add_argument(
            "--sandbox-wait-seconds",
            type=lambda value: _bounded_integer(
                value,
                minimum=0,
                maximum=_MAX_SANDBOX_WAIT_SECONDS,
                label="sandbox-wait-seconds",
            ),
            default=_DEFAULT_SANDBOX_WAIT_SECONDS,
        )
        command_parser.add_argument(
            "--sandbox-poll-seconds",
            type=lambda value: _bounded_integer(
                value,
                minimum=1,
                maximum=60,
                label="sandbox-poll-seconds",
            ),
            default=_DEFAULT_SANDBOX_POLL_SECONDS,
        )
    return parser


def _timing(arguments: argparse.Namespace) -> TimingConfig:
    now = datetime.now(UTC)
    end = arguments.end_utc or now
    start = arguments.start_utc or end - timedelta(minutes=_DEFAULT_LOOKBACK_MINUTES)
    if start >= end or end - start > timedelta(days=7):
        raise InputValidationError("scan_window_invalid")
    return TimingConfig(
        start=start,
        end=end,
        telemetry_lag_retries=arguments.telemetry_lag_retries,
        telemetry_lag_seconds=arguments.telemetry_lag_seconds,
        timeout_seconds=arguments.timeout_seconds,
        late_scan_seconds=arguments.late_scan_seconds,
        sandbox_wait_seconds=arguments.sandbox_wait_seconds,
        sandbox_poll_seconds=arguments.sandbox_poll_seconds,
    )


def _emit_aggregate(aggregate: ScanAggregate) -> None:
    for result in aggregate.results:
        print(render_result(result))
    print(render_aggregate(aggregate))


def _input_failure(error: BaseException) -> ScanAggregate:
    return ScanAggregate(
        results=(
            ScanResult(
                surface="Scanner/Input",
                scanned=0,
                violations=0,
                status=ScanStatus.INCONCLUSIVE,
                code=_bounded_error_code("input", error),
            ),
        )
    )


def _build_adapter(
    command: str,
    operator: OperatorInput,
) -> LiveScanAdapter:
    if command == "scan-json":
        if operator.fixtures is None:
            raise InputValidationError("fixtures_required")
        return FixtureLiveAdapter(operator.fixtures)
    if operator.resources is None:
        raise InputValidationError("resources_required")
    return AzureLiveAdapter(operator.resources)


def main(arguments: Sequence[str] | None = None, stream: Any = None) -> int:
    """Run fixture or live qualification and emit JSON lines only."""
    args = _parser().parse_args(arguments)
    adapter: LiveScanAdapter | None = None
    try:
        raw_input = read_operator_input(args.operator_file, stream)
        operator = parse_operator_input(
            raw_input,
            require_resources=args.command == "live",
        )
        timing = _timing(args)
        adapter = _build_adapter(args.command, operator)
        aggregate = asyncio.run(run_scan(adapter, operator, timing))
    except Exception as error:
        aggregate = _input_failure(error)
    finally:
        if adapter is not None:
            close_error: BaseException | None = None
            try:
                adapter.close()
            except Exception as error:
                close_error = error
            if close_error is not None:
                aggregate = ScanAggregate(
                    results=(
                        *aggregate.results,
                        inconclusive_from_exception("Scanner/Close", close_error),
                    )
                )
    _emit_aggregate(aggregate)
    return aggregate.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
