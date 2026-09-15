from __future__ import annotations

import base64
import gzip
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

import pytest

from azure_functions_agents.experimental import durable_chat_config
from azure_functions_agents.experimental.durable_chat_config import (
    _HOST_DURABLE_HUB_NAME_ENV,
    _HOST_DURABLE_PROVIDER_TYPE_ENV,
    _HOST_HTTP_ROUTE_PREFIX_ENV,
    APPLICATIONINSIGHTS_RESOURCE_ID_ENV,
    DTS_TASK_HUB_DASHBOARD_URL_ENV,
    TASK_HUB_NAME_ENV,
    DurableChatConfigurationError,
    DurableChatSettings,
    build_application_insights_logs_link,
    build_durable_chat_diagnostic_links,
    durable_chat_route,
    resolve_durable_chat_route_prefix,
)
from azure_functions_agents.experimental.durable_loop_config import (
    DURABLE_LOOP_ENABLED_ENV,
)
from azure_functions_agents.experimental.hybrid_config import HYBRID_SANDBOX_GROUP_ENV

_RESOURCE_ID = (
    "/subscriptions/00000000-0000-0000-0000-000000000000/"
    "resourceGroups/demo/providers/Microsoft.Insights/components/demo-ai"
)
_SANDBOX_GROUP_RESOURCE_ID = (
    "/subscriptions/00000000-0000-0000-0000-000000000000/"
    "resourceGroups/demo/providers/Microsoft.App/sandboxGroups/demo-group"
)
_NOW = datetime(2026, 9, 14, tzinfo=UTC)


def _dts_environment() -> dict[str, str]:
    return {
        _HOST_DURABLE_PROVIDER_TYPE_ENV: "azureManaged",
        "DURABLE_TASK_SCHEDULER_CONNECTION_STRING": (
            "Endpoint=https://scheduler.example.test;TaskHub=demo_hub"
        ),
    }


def test_durable_chat_uses_the_existing_durable_loop_gate() -> None:
    assert not DurableChatSettings.from_environment(
        {},
        observability_enabled=False,
    ).enabled
    assert DurableChatSettings.from_environment(
        {DURABLE_LOOP_ENABLED_ENV: "true"},
        observability_enabled=False,
    ).enabled
    assert not DurableChatSettings.from_environment(
        {"AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_CHAT_ENABLED": "true"},
        observability_enabled=False,
    ).enabled


def test_sandbox_group_metadata_is_optional_and_validated() -> None:
    configured = DurableChatSettings.from_environment(
        {HYBRID_SANDBOX_GROUP_ENV: _SANDBOX_GROUP_RESOURCE_ID},
        observability_enabled=False,
    )
    missing = DurableChatSettings.from_environment({}, observability_enabled=False)
    invalid = DurableChatSettings.from_environment(
        {HYBRID_SANDBOX_GROUP_ENV: "not-a-sandbox-group-resource-id"},
        observability_enabled=False,
    )
    frozen = configured.freeze_diagnostics(
        request_started_at=_NOW,
        request_ends_at=_NOW + timedelta(minutes=5),
    )

    assert configured.sandbox_group_resource_id == _SANDBOX_GROUP_RESOURCE_ID
    assert frozen.sandbox_group_resource_id == _SANDBOX_GROUP_RESOURCE_ID
    assert missing.sandbox_group_resource_id is None
    assert invalid.sandbox_group_resource_id is None


def test_route_prefix_defaults_to_api_and_honors_explicit_safe_overrides() -> None:
    assert (
        resolve_durable_chat_route_prefix(
            {},
            app_root=Path("missing-durable-chat-host-root"),
        )
        == "/api"
    )
    assert (
        durable_chat_route(
            "/experimental/durable-agent-runs",
            environment={_HOST_HTTP_ROUTE_PREFIX_ENV: "internal-api"},
        )
        == "/internal-api/experimental/durable-agent-runs"
    )
    assert (
        resolve_durable_chat_route_prefix(
            {_HOST_HTTP_ROUTE_PREFIX_ENV: "v1/agents"}
        )
        == "/v1/agents"
    )
    assert resolve_durable_chat_route_prefix({_HOST_HTTP_ROUTE_PREFIX_ENV: ""}) == ""

    with pytest.raises(DurableChatConfigurationError):
        resolve_durable_chat_route_prefix({_HOST_HTTP_ROUTE_PREFIX_ENV: "../api"})
    with pytest.raises(DurableChatConfigurationError):
        resolve_durable_chat_route_prefix(
            {_HOST_HTTP_ROUTE_PREFIX_ENV: "v1/../agents"}
        )


def test_route_prefix_uses_host_configuration_when_no_environment_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        durable_chat_config,
        "_load_host_configuration",
        lambda _app_root: {"extensions": {"http": {"routePrefix": "chat-e2e"}}},
    )

    assert resolve_durable_chat_route_prefix({}, app_root=Path(".")) == "/chat-e2e"
    assert (
        resolve_durable_chat_route_prefix(
            {_HOST_HTTP_ROUTE_PREFIX_ENV: "private-api"},
            app_root=Path("."),
        )
        == "/private-api"
    )
    monkeypatch.setattr(
        durable_chat_config,
        "_load_host_configuration",
        lambda _app_root: {"extensions": {"http": {"routePrefix": "v1/agents"}}},
    )
    assert resolve_durable_chat_route_prefix({}, app_root=Path(".")) == "/v1/agents"
    monkeypatch.setattr(
        durable_chat_config,
        "_load_host_configuration",
        lambda _app_root: {"extensions": {"http": {"routePrefix": ""}}},
    )
    assert resolve_durable_chat_route_prefix({}, app_root=Path(".")) == ""


def test_dts_requires_the_effective_azure_managed_provider_and_safe_override() -> None:
    connection_only = DurableChatSettings.from_environment(
        {
            "DURABLE_TASK_SCHEDULER_CONNECTION_STRING": (
                "Endpoint=https://scheduler.example.test;TaskHub=demo_hub"
            )
        },
        observability_enabled=False,
    )
    managed = DurableChatSettings.from_environment(
        _dts_environment(),
        observability_enabled=False,
    )
    unsafe = DurableChatSettings.from_environment(
        {
            **_dts_environment(),
            DTS_TASK_HUB_DASHBOARD_URL_ENV: (
                "https://dashboard.example.test/#view?x-functions-key=secret"
            ),
        },
        observability_enabled=False,
    )
    local = DurableChatSettings.from_environment(
        {
            _HOST_DURABLE_PROVIDER_TYPE_ENV: "azureManaged",
            "DURABLE_TASK_SCHEDULER_CONNECTION_STRING": (
                "Endpoint=http://localhost:7071;TaskHub=demo_hub"
            ),
        },
        observability_enabled=False,
    )

    assert not connection_only.dts.configured
    assert managed.dts.dashboard_url == (
        "https://dashboard.durabletask.io/?"
        "endpoint=https%3A%2F%2Fscheduler.example.test&taskhub=demo_hub"
    )
    assert not unsafe.dts.configured
    assert local.dts.dashboard_url == "http://localhost:8082"


def test_dts_uses_explicit_host_hub_before_a_stale_taskhub_environment_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        durable_chat_config,
        "_load_host_configuration",
        lambda _app_root: {
            "extensions": {"durableTask": {"hubName": "%EFFECTIVE_TASK_HUB%"}}
        },
    )
    settings = DurableChatSettings.from_environment(
        {
            **_dts_environment(),
            "EFFECTIVE_TASK_HUB": "effective_hub",
            TASK_HUB_NAME_ENV: "stale_hub",
        },
        app_root=Path("."),
        observability_enabled=False,
    )

    assert settings.dts.dashboard_url is not None
    assert "taskhub=effective_hub" in settings.dts.dashboard_url


def test_dts_does_not_fall_back_from_an_explicit_empty_host_hub() -> None:
    settings = DurableChatSettings.from_environment(
        {
            **_dts_environment(),
            _HOST_DURABLE_HUB_NAME_ENV: "",
            TASK_HUB_NAME_ENV: "stale_hub",
        },
        observability_enabled=False,
    )

    assert not settings.dts.configured


def test_application_insights_link_is_resource_scoped_and_gzip_encoded() -> None:
    settings = DurableChatSettings.from_environment(
        {
            **_dts_environment(),
            APPLICATIONINSIGHTS_RESOURCE_ID_ENV: _RESOURCE_ID,
        },
        observability_enabled=True,
    )
    metadata = settings.freeze_diagnostics(
        request_started_at=_NOW,
        request_ends_at=_NOW + timedelta(minutes=5),
    )
    links = settings.integration_metadata()
    diagnostic_links = build_durable_chat_diagnostic_links(
        metadata,
        run_id="run-1",
    )
    link = build_application_insights_logs_link(
        resource_id=_RESOURCE_ID,
        run_correlation="a" * 64,
        start_at=_NOW,
        end_at=_NOW + timedelta(minutes=5),
    )
    encoded_query = link.split("/q/", 1)[1].split("/timespan/", 1)[0]
    query = gzip.decompress(base64.b64decode(unquote(encoded_query))).decode("utf-8")

    assert metadata.application_insights_resource_id == _RESOURCE_ID
    assert links.application_insights is not None
    assert links.application_insights.configured
    assert diagnostic_links[1].available
    assert "af.durable_loop.run_correlation" in query
    assert "dependencies" in query
