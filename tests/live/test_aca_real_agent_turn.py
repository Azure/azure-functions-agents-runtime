"""Lower-level opt-in qualification of one real MAF turn through the ACA harness."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from tests.live.aca_smoke_support import (
    AcaSmokeConfig,
    aca_smoke_config_from_environment,
    production_smoke_app_identity,
    reap_current_production_smoke_sandboxes,
)

from azure_functions_agents.execution.aca_composition import compose_aca_application
from azure_functions_agents.execution.aca_sandbox import AcaSandboxExecutionBackend
from azure_functions_agents.execution.backend import RunContext, StartRunRequest
from azure_functions_agents.session_state import FunctionAppPrincipal

_AGENT_NAME = "model_turn"

if os.environ.get("AZURE_FUNCTIONS_AGENTS_RUN_ACA_SMOKE") != "1":
    pytest.skip(
        "Set AZURE_FUNCTIONS_AGENTS_RUN_ACA_SMOKE=1 after human authorization to run live ACA.",
        allow_module_level=True,
    )


@pytest.fixture(scope="session")
def aca_real_agent_backend(aca_materialized_app_root: Path) -> AcaSandboxExecutionBackend:
    application = compose_aca_application(
        aca_materialized_app_root,
        app_identity=production_smoke_app_identity(),
    )
    return application.backend_for(_AGENT_NAME, owner=FunctionAppPrincipal())


@pytest_asyncio.fixture(autouse=True)
async def reap_current_production_smoke(
    aca_smoke_config: AcaSmokeConfig,
) -> AsyncIterator[None]:
    """Observe and reap the production backend's current Function App label family."""

    try:
        yield
    finally:
        await reap_current_production_smoke_sandboxes(aca_smoke_config)


@pytest.fixture
def aca_smoke_config() -> AcaSmokeConfig:
    return aca_smoke_config_from_environment()


@pytest.mark.live_aca
@pytest.mark.asyncio
async def test_live_aca_real_agent_turn(
    aca_real_agent_backend: AcaSandboxExecutionBackend,
) -> None:
    """Require a captured catalog, ordered journal, terminal result, and real model turn."""

    handle = await aca_real_agent_backend.start_run(
        StartRunRequest(
            prompt="Provide a short acknowledgement.",
            timeout=120.0,
        )
    )
    context = RunContext(run_id=handle.run_id, session_id=handle.session_id)
    events = [
        event
        async for event in aca_real_agent_backend.read_events(context, after_sequence=0)
    ]
    status = await aca_real_agent_backend.get_run(context)

    assert status.state == "succeeded"
    assert status.error is None
    assert status.result_available is True
    assert status.result is not None
    assert bool(status.result.content.strip())
    assert status.result.tool_calls == []
    assert events
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert events[-1].type == "done"
    assert status.last_sequence == events[-1].sequence
