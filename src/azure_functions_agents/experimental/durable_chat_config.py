"""Private configuration and credential-free diagnostics for durable chat."""

from __future__ import annotations

import base64
import gzip
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

from .._observability import is_observability_enabled
from ..config.paths import get_app_root
from .durable_chat_protocol import (
    DurableChatDiagnosticLinkV1,
    DurableChatFrozenDiagnosticsV1,
    DurableChatIntegrationAvailabilityV1,
    DurableChatIntegrationKind,
    DurableChatIntegrationMetadataV1,
    durable_chat_run_correlation,
)
from .durable_loop_config import DurableLoopSettings
from .hybrid_config import HYBRID_SANDBOX_GROUP_ENV

DTS_TASK_HUB_DASHBOARD_URL_ENV = "DTS_TASK_HUB_DASHBOARD_URL"
APPLICATIONINSIGHTS_RESOURCE_ID_ENV = "APPLICATIONINSIGHTS_RESOURCE_ID"
TASK_HUB_NAME_ENV = "TASKHUB_NAME"
_DTS_CONNECTION_NAME_DEFAULT = "DURABLE_TASK_SCHEDULER_CONNECTION_STRING"
_HOST_DURABLE_PROVIDER_TYPE_ENV = (
    "AzureFunctionsJobHost__extensions__durableTask__storageProvider__type"
)
_HOST_DURABLE_CONNECTION_NAME_ENV = (
    "AzureFunctionsJobHost__extensions__durableTask__storageProvider__connectionStringName"
)
_HOST_DURABLE_HUB_NAME_ENV = "AzureFunctionsJobHost__extensions__durableTask__hubName"
_HOST_HTTP_ROUTE_PREFIX_ENV = "AzureFunctionsJobHost__extensions__http__routePrefix"
_TASK_HUB_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_RESOURCE_ID_PATTERN = re.compile(
    r"^/subscriptions/[0-9A-Fa-f-]{36}/resourceGroups/"
    r"[A-Za-z0-9._()-]{1,90}/providers/Microsoft\.Insights/components/"
    r"[A-Za-z0-9._()-]{1,128}$",
    re.IGNORECASE,
)
_SANDBOX_GROUP_RESOURCE_ID_PATTERN = re.compile(
    r"^/subscriptions/[0-9A-Fa-f-]{36}/resourceGroups/"
    r"[A-Za-z0-9._()-]{1,90}/providers/Microsoft\.App/sandboxGroups/"
    r"[A-Za-z0-9._()-]{1,128}$",
    re.IGNORECASE,
)
_ROUTE_PREFIX_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class DurableChatConfigurationError(RuntimeError):
    """The private durable-chat configuration is invalid."""


@dataclass(frozen=True, slots=True)
class DurableChatDtsSettings:
    """One safe, frozen Durable Task Scheduler dashboard capability."""

    dashboard_url: str | None
    unavailable_reason: str | None

    @property
    def configured(self) -> bool:
        """Return whether an owner-safe dashboard link is available."""
        return self.dashboard_url is not None


@dataclass(frozen=True, slots=True)
class DurableChatApplicationInsightsSettings:
    """One safe Application Insights Logs capability."""

    resource_id: str | None
    tracing_enabled: bool
    unavailable_reason: str | None

    @property
    def configured(self) -> bool:
        """Return whether a resource-scoped query link can be produced."""
        return self.resource_id is not None and self.tracing_enabled


@dataclass(frozen=True, slots=True)
class DurableChatSettings:
    """Validated process settings hosted under the durable-loop gate."""

    enabled: bool
    dts: DurableChatDtsSettings
    application_insights: DurableChatApplicationInsightsSettings
    sandbox_group_resource_id: str | None = None

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        app_root: Path | None = None,
        observability_enabled: bool | None = None,
    ) -> DurableChatSettings:
        """Resolve the optional shell and its display-only integrations."""
        source = os.environ if environment is None else environment
        enabled = DurableLoopSettings.from_environment(source) is not None
        host = _load_host_configuration(app_root)
        dts = _resolve_dts_settings(source, host)
        application_insights = _resolve_application_insights_settings(
            source,
            is_observability_enabled()
            if observability_enabled is None
            else observability_enabled,
        )
        return cls(
            enabled=enabled,
            sandbox_group_resource_id=_resolve_sandbox_group_resource_id(source),
            dts=dts,
            application_insights=application_insights,
        )

    def integration_metadata(self) -> DurableChatIntegrationMetadataV1:
        """Return the shell-safe availability projection."""
        return DurableChatIntegrationMetadataV1(
            durable_task_scheduler=DurableChatIntegrationAvailabilityV1(
                configured=self.dts.configured,
                unavailable_reason=self.dts.unavailable_reason,
            ),
            application_insights=DurableChatIntegrationAvailabilityV1(
                configured=self.application_insights.configured,
                unavailable_reason=self.application_insights.unavailable_reason,
            ),
        )

    def freeze_diagnostics(
        self,
        *,
        request_started_at: datetime,
        request_ends_at: datetime,
    ) -> DurableChatFrozenDiagnosticsV1:
        """Freeze safe request-link inputs before durable execution starts."""
        return DurableChatFrozenDiagnosticsV1(
            sandbox_group_resource_id=self.sandbox_group_resource_id,
            durable_task_dashboard_url=self.dts.dashboard_url,
            durable_task_unavailable_reason=self.dts.unavailable_reason,
            application_insights_resource_id=self.application_insights.resource_id,
            application_insights_tracing_enabled=(
                self.application_insights.tracing_enabled
            ),
            application_insights_unavailable_reason=(
                self.application_insights.unavailable_reason
            ),
            request_started_at=request_started_at,
            request_ends_at=request_ends_at,
        )


def resolve_durable_chat_route_prefix(
    environment: Mapping[str, str] | None = None,
    *,
    app_root: Path | None = None,
) -> str:
    """Return the configured Functions route prefix as a safe path fragment."""
    source = os.environ if environment is None else environment
    configured = _configured_environment_value(source, _HOST_HTTP_ROUTE_PREFIX_ENV)
    if configured is None:
        host = _load_host_configuration(app_root or get_app_root())
        configured = _nested_configured_string(
            host,
            "extensions",
            "http",
            "routePrefix",
        )
        if configured is None:
            return "/api"
    raw = configured.strip("/")
    if not raw:
        return ""
    segments = raw.split("/")
    if any(
        segment in {".", ".."}
        or _ROUTE_PREFIX_SEGMENT_PATTERN.fullmatch(segment) is None
        for segment in segments
    ):
        raise DurableChatConfigurationError(
            f"{_HOST_HTTP_ROUTE_PREFIX_ENV} must contain only safe route segments"
        )
    return "/" + "/".join(segments)


def durable_chat_route(
    path: str,
    *,
    environment: Mapping[str, str] | None = None,
    app_root: Path | None = None,
) -> str:
    """Build a safe same-origin public path from the effective Functions prefix."""
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        raise ValueError("durable-chat route path is invalid")
    return resolve_durable_chat_route_prefix(environment, app_root=app_root) + path


def build_durable_chat_diagnostic_links(
    metadata: DurableChatFrozenDiagnosticsV1 | None,
    *,
    run_id: str,
) -> tuple[DurableChatDiagnosticLinkV1, ...]:
    """Build only frozen, credential-free request diagnostics links."""
    if metadata is None:
        reason = "Run diagnostic metadata is unavailable."
        return (
            _unavailable_link(DurableChatIntegrationKind.DURABLE_TASK_SCHEDULER, reason),
            _unavailable_link(DurableChatIntegrationKind.APPLICATION_INSIGHTS, reason),
        )

    dts = (
        DurableChatDiagnosticLinkV1(
            kind=DurableChatIntegrationKind.DURABLE_TASK_SCHEDULER,
            available=True,
            href=metadata.durable_task_dashboard_url,
        )
        if metadata.durable_task_dashboard_url is not None
        else _unavailable_link(
            DurableChatIntegrationKind.DURABLE_TASK_SCHEDULER,
            metadata.durable_task_unavailable_reason
            or "Durable Task Scheduler diagnostics are unavailable.",
        )
    )
    if (
        metadata.application_insights_resource_id is None
        or not metadata.application_insights_tracing_enabled
    ):
        application_insights = _unavailable_link(
            DurableChatIntegrationKind.APPLICATION_INSIGHTS,
            metadata.application_insights_unavailable_reason
            or "Application Insights diagnostics are unavailable.",
        )
    else:
        application_insights = DurableChatDiagnosticLinkV1(
            kind=DurableChatIntegrationKind.APPLICATION_INSIGHTS,
            available=True,
            href=build_application_insights_logs_link(
                resource_id=metadata.application_insights_resource_id,
                run_correlation=durable_chat_run_correlation(run_id),
                start_at=metadata.request_started_at,
                end_at=metadata.request_ends_at,
            ),
        )
    return dts, application_insights


def build_application_insights_logs_link(
    *,
    resource_id: str,
    run_correlation: str,
    start_at: datetime,
    end_at: datetime,
) -> str:
    """Build a resource-scoped, request-correlated Application Insights Logs link."""
    if _RESOURCE_ID_PATTERN.fullmatch(resource_id) is None:
        raise DurableChatConfigurationError(
            f"{APPLICATIONINSIGHTS_RESOURCE_ID_ENV} must be an Application Insights resource ID"
        )
    if re.fullmatch(r"[0-9a-f]{64}", run_correlation) is None:
        raise ValueError("durable-chat run correlation is invalid")
    start = _iso8601_utc(start_at)
    end = _iso8601_utc(end_at)
    if end < start:
        raise ValueError("durable-chat diagnostics time range is invalid")
    query = "\n".join(
        (
            "dependencies",
            f"| where timestamp between (datetime({start}) .. datetime({end}))",
            (
                "| where tostring(customDimensions"
                '["af.durable_loop.run_correlation"]) '
                f"== '{run_correlation}'"
            ),
            (
                "| project timestamp, name, target, resultCode, duration, success, "
                "customDimensions"
            ),
            "| order by timestamp asc",
        )
    )
    encoded_query = quote(
        base64.b64encode(gzip.compress(query.encode("utf-8"), mtime=0)).decode("ascii"),
        safe="",
    )
    timespan = quote(f"{start}/{end}", safe="")
    return (
        "https://portal.azure.com/#blade/Microsoft_Azure_Monitoring_Logs/LogsBlade/"
        f"resourceId/{quote(resource_id, safe='')}/"
        f"source/LogsBlade.AnalyticsShareLinkToQuery/q/{encoded_query}/timespan/{timespan}"
    )


def _resolve_dts_settings(
    environment: Mapping[str, str],
    host: Mapping[str, object],
) -> DurableChatDtsSettings:
    provider_type = _environment_value(environment, _HOST_DURABLE_PROVIDER_TYPE_ENV)
    if not provider_type:
        provider_type = _nested_string(
            host,
            "extensions",
            "durableTask",
            "storageProvider",
            "type",
        )
    if provider_type.casefold() != "azuremanaged":
        return DurableChatDtsSettings(
            dashboard_url=None,
            unavailable_reason="Durable Task Scheduler storage provider is not active.",
        )

    connection_name = _environment_value(
        environment,
        _HOST_DURABLE_CONNECTION_NAME_ENV,
    )
    if not connection_name:
        connection_name = _nested_string(
            host,
            "extensions",
            "durableTask",
            "storageProvider",
            "connectionStringName",
        )
    connection_name = _expand_setting_name(
        connection_name or _DTS_CONNECTION_NAME_DEFAULT,
        environment,
    )
    connection = _environment_value(environment, connection_name)
    endpoint, connection_hub = _dts_endpoint_and_hub(connection)
    configured_hub = _configured_environment_value(
        environment,
        _HOST_DURABLE_HUB_NAME_ENV,
    )
    if configured_hub is None:
        configured_hub = _nested_configured_string(
            host,
            "extensions",
            "durableTask",
            "hubName",
        )
    if configured_hub is None:
        configured_hub = _configured_environment_value(environment, TASK_HUB_NAME_ENV)
    task_hub = _expand_setting_name(
        connection_hub if configured_hub is None else configured_hub,
        environment,
    )
    if endpoint is None:
        return DurableChatDtsSettings(
            dashboard_url=None,
            unavailable_reason="Durable Task Scheduler endpoint is not configured.",
        )
    if _TASK_HUB_PATTERN.fullmatch(task_hub) is None:
        return DurableChatDtsSettings(
            dashboard_url=None,
            unavailable_reason="Durable Task Scheduler task hub is not configured.",
        )

    override = _environment_value(environment, DTS_TASK_HUB_DASHBOARD_URL_ENV)
    if override:
        if not _is_safe_dashboard_url(override):
            return DurableChatDtsSettings(
                dashboard_url=None,
                unavailable_reason="Durable Task Scheduler dashboard override is invalid.",
            )
        return DurableChatDtsSettings(dashboard_url=override, unavailable_reason=None)
    if _is_local_endpoint(endpoint):
        return DurableChatDtsSettings(
            dashboard_url="http://localhost:8082",
            unavailable_reason=None,
        )
    return DurableChatDtsSettings(
        dashboard_url=(
            "https://dashboard.durabletask.io/?"
            f"endpoint={quote(endpoint, safe='')}&taskhub={quote(task_hub, safe='')}"
        ),
        unavailable_reason=None,
    )


def _resolve_application_insights_settings(
    environment: Mapping[str, str],
    observability_enabled: bool,
) -> DurableChatApplicationInsightsSettings:
    resource_id = _environment_value(
        environment,
        APPLICATIONINSIGHTS_RESOURCE_ID_ENV,
    )
    if not resource_id:
        return DurableChatApplicationInsightsSettings(
            resource_id=None,
            tracing_enabled=False,
            unavailable_reason="Application Insights resource metadata is not configured.",
        )
    if _RESOURCE_ID_PATTERN.fullmatch(resource_id) is None:
        return DurableChatApplicationInsightsSettings(
            resource_id=None,
            tracing_enabled=False,
            unavailable_reason="Application Insights resource metadata is invalid.",
        )
    if not observability_enabled:
        return DurableChatApplicationInsightsSettings(
            resource_id=None,
            tracing_enabled=False,
            unavailable_reason="Runtime tracing is not active.",
        )
    return DurableChatApplicationInsightsSettings(
        resource_id=resource_id,
        tracing_enabled=True,
        unavailable_reason=None,
    )


def _resolve_sandbox_group_resource_id(
    environment: Mapping[str, str],
) -> str | None:
    """Return the validated configured sandbox-group resource ID."""
    resource_id = _environment_value(environment, HYBRID_SANDBOX_GROUP_ENV)
    if _SANDBOX_GROUP_RESOURCE_ID_PATTERN.fullmatch(resource_id) is None:
        return None
    return resource_id


def _load_host_configuration(app_root: Path | None) -> Mapping[str, object]:
    if app_root is None:
        return {}
    path = app_root / "host.json"
    if not path.is_file():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DurableChatConfigurationError("host.json is invalid") from exc
    if not isinstance(parsed, Mapping):
        raise DurableChatConfigurationError("host.json must contain a JSON object")
    return parsed


def _environment_value(environment: Mapping[str, str], name: str) -> str:
    configured = _configured_environment_value(environment, name)
    return configured or ""


def _configured_environment_value(
    environment: Mapping[str, str],
    name: str,
) -> str | None:
    for key, value in environment.items():
        if key.casefold() == name.casefold():
            if not isinstance(value, str):
                raise DurableChatConfigurationError(f"{name} must be a string.")
            return value.strip()
    return None


def _nested_string(source: Mapping[str, object], *path: str) -> str:
    current: object = source
    for key in path:
        if not isinstance(current, Mapping):
            return ""
        current = next(
            (
                value
                for candidate, value in current.items()
                if isinstance(candidate, str) and candidate.casefold() == key.casefold()
            ),
            None,
        )
    return current.strip() if isinstance(current, str) else ""


def _nested_configured_string(
    source: Mapping[str, object],
    *path: str,
) -> str | None:
    current: object = source
    for key in path:
        if not isinstance(current, Mapping):
            return None
        found = next(
            (
                value
                for candidate, value in current.items()
                if isinstance(candidate, str) and candidate.casefold() == key.casefold()
            ),
            None,
        )
        if found is None:
            return None
        current = found
    return current.strip() if isinstance(current, str) else None


def _expand_setting_name(value: str, environment: Mapping[str, str]) -> str:
    match = re.fullmatch(r"%([A-Za-z_][A-Za-z0-9_]*)%", value.strip())
    return _environment_value(environment, match.group(1)) if match is not None else value


def _dts_endpoint_and_hub(connection: str) -> tuple[str | None, str]:
    if not connection:
        return None, ""
    values: dict[str, str] = {}
    for fragment in connection.split(";"):
        if not fragment.strip():
            continue
        key, separator, value = fragment.partition("=")
        normalized = key.strip().casefold()
        if not separator or not normalized or normalized in values:
            return None, ""
        values[normalized] = value.strip()
    endpoint = values.get("endpoint", "")
    if not _is_safe_dts_endpoint(endpoint):
        return None, ""
    return endpoint.rstrip("/"), values.get("taskhub", "")


def _is_safe_dts_endpoint(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        return False
    return parsed.scheme == "https" or _is_local_endpoint(value)


def _is_local_endpoint(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.hostname in {"localhost", "127.0.0.1", "::1"}


def _is_safe_dashboard_url(value: str) -> bool:
    try:
        DurableChatDiagnosticLinkV1(
            kind=DurableChatIntegrationKind.DURABLE_TASK_SCHEDULER,
            available=True,
            href=value,
        )
    except ValueError:
        return False
    return True


def _unavailable_link(
    kind: DurableChatIntegrationKind,
    reason: str,
) -> DurableChatDiagnosticLinkV1:
    return DurableChatDiagnosticLinkV1(
        kind=kind,
        available=False,
        unavailable_reason=reason,
    )


def _iso8601_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("durable-chat diagnostics timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
