"""Lower-level opt-in qualification of one real MAF turn through the ACA harness."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from tests.live.aca_smoke_support import (
    AcaSmokeConfig,
    AcaSmokeModelConfig,
    DependencyClosureArchive,
    aca_smoke_config_from_environment,
    aca_smoke_model_config_from_environment,
    prepare_real_agent_project,
    provision_aca_smoke_sandbox,
)

from azure_functions_agents.execution.backend import RunContext
from azure_functions_agents.execution.run_control import RunEnvelope, SandboxRunControl
from azure_functions_agents.transport.ports import SandboxSessionHandle

_TURN_TIMEOUT_SECONDS = 180.0
_AGENT_NAME = "model_turn"

if os.environ.get("AZURE_FUNCTIONS_AGENTS_RUN_ACA_SMOKE") != "1":
    pytest.skip(
        "Set AZURE_FUNCTIONS_AGENTS_RUN_ACA_SMOKE=1 after human authorization to run live ACA.",
        allow_module_level=True,
    )


@pytest.fixture
def aca_smoke_config() -> AcaSmokeConfig:
    return aca_smoke_config_from_environment()


@pytest.fixture
def aca_smoke_model_config() -> AcaSmokeModelConfig:
    return aca_smoke_model_config_from_environment()


@pytest_asyncio.fixture
async def aca_real_agent_handle(
    aca_smoke_config: AcaSmokeConfig,
    aca_smoke_model_config: AcaSmokeModelConfig,
) -> AsyncIterator[SandboxSessionHandle]:
    async def prepare(
        handle: SandboxSessionHandle,
        dependency_closure: DependencyClosureArchive,
    ) -> None:
        await prepare_real_agent_project(
            handle,
            config=aca_smoke_config,
            dependency_closure=dependency_closure,
        )

    async with provision_aca_smoke_sandbox(
        aca_smoke_config,
        session_prefix="aca-real-agent-turn",
        before_yield_with_closure=prepare,
        model_config=aca_smoke_model_config,
    ) as handle:
        yield handle


@pytest.mark.live_aca
@pytest.mark.asyncio
async def test_live_aca_lower_level_real_agent_turn(
    aca_real_agent_handle: SandboxSessionHandle,
) -> None:
    """Require a captured catalog, ordered journal, terminal result, and real model turn."""

    run_id = uuid.uuid4().hex
    session_id = uuid.uuid4().hex
    envelope = RunEnvelope.create(
        run_id=run_id,
        session_id=session_id,
        agent_name=_AGENT_NAME,
        prompt="Provide a short acknowledgement.",
        timeout=120.0,
    )
    run_control = SandboxRunControl()
    await run_control.submit(
        aca_real_agent_handle,
        run_id,
        envelope,
        timeout_seconds=_TURN_TIMEOUT_SECONDS,
    )

    context = RunContext(run_id=run_id, session_id=session_id)
    events = [
        event
        async for event in run_control.read_events(
            aca_real_agent_handle,
            context,
            after_sequence=0,
        )
    ]
    status = await run_control.get_status(aca_real_agent_handle, context)

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
