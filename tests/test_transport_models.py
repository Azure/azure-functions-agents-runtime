"""Validation tests for provider-neutral transport models."""

from __future__ import annotations

import pytest

from azure_functions_agents.transport.transport_models import (
    SandboxProvisioningError,
    SandboxSummary,
)


@pytest.mark.parametrize("state", ("Running", "x" * 32, None))
def test_sandbox_summary_accepts_valid_or_absent_state(state: str | None) -> None:
    summary = SandboxSummary.create(
        sandbox_id="sandbox-1",
        labels={"app_hash": "app-1"},
        state=state,
    )

    assert summary.state == state


@pytest.mark.parametrize("state", ("", "x" * 33, "😀" * 9))
def test_sandbox_summary_rejects_empty_or_overlong_state(state: str) -> None:
    with pytest.raises(SandboxProvisioningError):
        SandboxSummary.create(
            sandbox_id="sandbox-1",
            labels={"app_hash": "app-1"},
            state=state,
        )
