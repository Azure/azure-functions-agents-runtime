"""Opt-in live coverage for a controller-to-harness journal round trip."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from tests.live.aca_smoke_support import (
    AcaSmokeConfig,
    aca_smoke_config_from_environment,
    prepare_journal_root,
    provision_aca_smoke_sandbox,
)

from azure_functions_agents.execution.run_control import RunEnvelope, SandboxRunControl
from azure_functions_agents.transport.ports import SandboxSessionHandle

_ACCEPTANCE_TIMEOUT_SECONDS = 120.0

if os.environ.get("AZURE_FUNCTIONS_AGENTS_RUN_ACA_SMOKE") != "1":
    pytest.skip(
        "Set AZURE_FUNCTIONS_AGENTS_RUN_ACA_SMOKE=1 after human authorization to run live ACA.",
        allow_module_level=True,
    )


@pytest.fixture
def aca_smoke_config() -> AcaSmokeConfig:
    return aca_smoke_config_from_environment()


@pytest_asyncio.fixture
async def aca_run_journal_handle(
    aca_smoke_config: AcaSmokeConfig,
) -> AsyncIterator[SandboxSessionHandle]:
    async with provision_aca_smoke_sandbox(
        aca_smoke_config,
        session_prefix="aca-run-journal",
        before_yield=prepare_journal_root,
    ) as handle:
        yield handle


@pytest.mark.live_aca
@pytest.mark.asyncio
async def test_live_aca_run_journal_acceptance(
    aca_run_journal_handle: SandboxSessionHandle,
) -> None:
    run_id = uuid.uuid4().hex
    session_id = uuid.uuid4().hex
    envelope = RunEnvelope.create(
        run_id=run_id,
        session_id=session_id,
        agent_name=f"aca-smoke-missing-{uuid.uuid4().hex}",
        prompt="Acceptance-only smoke request.",
        timeout=30.0,
    )

    # 120 seconds allows cold sandbox process startup and closure imports while keeping acceptance
    # loss bounded.
    status = await SandboxRunControl().submit(
        aca_run_journal_handle,
        run_id,
        envelope,
        timeout_seconds=_ACCEPTANCE_TIMEOUT_SECONDS,
    )

    assert status.run_id == run_id
    assert status.session_id == session_id
    assert status.result_available is False
    # The unmatched name avoids a model call. The harness publishes accepted before catalog lookup,
    # so the controller can observe that state or its immediate expected terminal failure.
    assert status.state in {"accepted", "failed"}
    if status.state == "failed":
        assert status.error is not None
        assert status.error.code == "sandbox_storage_failure"
