from __future__ import annotations

from collections.abc import Iterator

import pytest

from azure_functions_agents.config.schema import DynamicSessionsCodeInterpreterConfig
from azure_functions_agents.system_tools._sandbox_endpoint import (
    SandboxEndpointStatus,
    resolve_sandbox_endpoint,
)


@pytest.fixture(autouse=True)
def unset_dynamic_sessions(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("UNSET_DYNAMIC_SESSIONS", raising=False)
    yield


@pytest.mark.parametrize(
    ("config", "expected_status", "expected_eligible", "expected_endpoint"),
    [
        (None, SandboxEndpointStatus.MISSING, False, None),
        ({}, SandboxEndpointStatus.MISSING, False, None),
        (
            DynamicSessionsCodeInterpreterConfig(endpoint=""),
            SandboxEndpointStatus.MISSING,
            False,
            None,
        ),
        (
            {"endpoint": "https://$UNSET_DYNAMIC_SESSIONS/api"},
            SandboxEndpointStatus.UNRESOLVED,
            False,
            None,
        ),
        (
            {"endpoint": "https://collector.example/ingest"},
            SandboxEndpointStatus.INVALID,
            False,
            "https://collector.example/ingest",
        ),
        (
            {"endpoint": "https://eastus.dynamicsessions.io/api"},
            SandboxEndpointStatus.ELIGIBLE,
            True,
            "https://eastus.dynamicsessions.io/api",
        ),
    ],
)
def test_resolve_sandbox_endpoint_statuses(
    config: DynamicSessionsCodeInterpreterConfig | dict[str, str] | None,
    expected_status: SandboxEndpointStatus,
    expected_eligible: bool,
    expected_endpoint: str | None,
) -> None:
    resolution = resolve_sandbox_endpoint(config)

    assert resolution.status is expected_status
    assert resolution.status.value == expected_status.value
    assert resolution.eligible is expected_eligible
    assert resolution.endpoint == expected_endpoint
