"""Opt-in live coverage for the ACA harness module entrypoint."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from tests.live.aca_smoke_support import (
    AcaSmokeConfig,
    aca_smoke_config_from_environment,
    provision_aca_smoke_sandbox,
)

from azure_functions_agents.execution.run_control import _JOURNAL_ENTRYPOINT
from azure_functions_agents.transport.ports import SandboxSessionHandle

if os.environ.get("AZURE_FUNCTIONS_AGENTS_RUN_ACA_SMOKE") != "1":
    pytest.skip(
        "Set AZURE_FUNCTIONS_AGENTS_RUN_ACA_SMOKE=1 after human authorization to run live ACA.",
        allow_module_level=True,
    )


@pytest.fixture
def aca_smoke_config() -> AcaSmokeConfig:
    return aca_smoke_config_from_environment()


@pytest_asyncio.fixture
async def aca_harness_smoke_handle(
    aca_smoke_config: AcaSmokeConfig,
) -> AsyncIterator[SandboxSessionHandle]:
    async with provision_aca_smoke_sandbox(
        aca_smoke_config,
        session_prefix="aca-harness-smoke",
    ) as handle:
        yield handle


@pytest.mark.live_aca
@pytest.mark.asyncio
async def test_live_aca_harness_entrypoint_smoke(
    aca_harness_smoke_handle: SandboxSessionHandle,
) -> None:
    command = _JOURNAL_ENTRYPOINT.removeprefix("setsid nohup ")
    result = await aca_harness_smoke_handle.exec(f"{command} --help", timeout_seconds=60)
    assert result.exit_code == 0, result.stderr
