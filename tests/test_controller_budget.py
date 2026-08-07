from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from azure_functions_agents.controller.budget import (
    CLEANUP_HEADROOM_SECONDS,
    RequestBudget,
    RunCleanupDeadlineExceededError,
    RunDeadlineExceededError,
)
from azure_functions_agents.controller.readiness import (
    SessionRuntimeBinding,
    StateStoreBinding,
    activate_session,
)
from azure_functions_agents.execution.setup_budget import SetupBudget
from azure_functions_agents.session_state import AppIdentity, FunctionAppOwnerContext
from azure_functions_agents.transport.transport_models import DiskSource
from tests.doubles.fake_session_runtime import (
    DEFAULT_GROUP_RESOURCE_ID,
    FakeSandboxSessionHandle,
    FakeSandboxSessionProvider,
    FakeSessionStateStore,
)

pytestmark = pytest.mark.usefixtures("deterministic_content_package")


def test_request_budget_uses_one_anchor_for_setup_and_wall_deadlines() -> None:
    def clock() -> float:
        return 100.0

    budget = RequestBudget.start(authored_timeout=10.0, clock=clock)

    assert budget.wall_deadline == 110.0
    assert budget.setup.deadline == 110.0


def test_request_budget_rejects_an_elapsed_wall_deadline() -> None:
    clock_value = 5.0
    budget = RequestBudget(
        wall_deadline=clock_value,
        setup=SetupBudget.create(deadline=clock_value + 1, clock=lambda: clock_value),
        _clock=lambda: clock_value,
    )

    with pytest.raises(RunDeadlineExceededError):
        budget.remaining_wall_seconds()


def test_request_budget_reserves_bounded_cleanup_headroom_from_the_same_anchor() -> None:
    budget = RequestBudget(
        wall_deadline=180.0,
        setup=SetupBudget.create(deadline=30.0, clock=lambda: 0.0),
        _clock=lambda: 180.0,
    )

    assert budget.remaining_cleanup_seconds() == CLEANUP_HEADROOM_SECONDS


@pytest.mark.asyncio
async def test_request_budget_closes_cleanup_coroutine_after_headroom_expires() -> None:
    budget = RequestBudget(
        wall_deadline=0.0,
        setup=SetupBudget.create(deadline=1.0, clock=lambda: 0.0),
        _clock=lambda: CLEANUP_HEADROOM_SECONDS + 1.0,
    )

    async def pending() -> None:
        await asyncio.Event().wait()

    operation = pending()
    with pytest.raises(RunCleanupDeadlineExceededError):
        await budget.wait_for_cleanup(operation)

    assert operation.cr_frame is None


@pytest.mark.asyncio
async def test_create_receives_the_remaining_shared_setup_budget(tmp_path: Path) -> None:
    script_root = tmp_path
    (script_root / "function_app.py").write_text("app = object()\n", encoding="utf-8")
    clock = [0.0]
    owner = FunctionAppOwnerContext.create(
        AppIdentity.create(
            subscription_id="11111111-2222-3333-4444-555555555555",
            site_name="agent-app",
        ),
        "main",
    )
    handle = FakeSandboxSessionHandle("new-sandbox")
    provider = FakeSandboxSessionProvider(handle)
    store = FakeSessionStateStore()

    async def provider_factory() -> FakeSandboxSessionProvider:
        clock[0] = 10.0
        return provider

    async def state_store_factory() -> StateStoreBinding:
        return StateStoreBinding.create(
            store=store,
            state_store_fingerprint="s1-" + ("a" * 52),
        )

    runtime = SessionRuntimeBinding.create(
        app_identity=owner.app_identity,
        sandbox_group_resource_id=DEFAULT_GROUP_RESOURCE_ID,
        script_root=script_root,
        provider_factory=provider_factory,
        state_store_factory=state_store_factory,
        creation_source=DiskSource.create("test-harness"),
    )
    setup_budget = SetupBudget.create(deadline=30.0, clock=lambda: clock[0])

    activated = await activate_session(
        runtime,
        owner,
        "new-session",
        setup_budget,
        allow_create=True,
    )

    request = provider.create_calls[0]
    assert request.remaining_setup_budget_seconds == 20.0
    assert request.provisioning_timeout_seconds == 20.0
    await activated.handle.close()
