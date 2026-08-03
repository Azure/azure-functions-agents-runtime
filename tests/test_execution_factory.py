from __future__ import annotations

from pathlib import Path

from azure_functions_agents.controller.readiness import SessionRuntimeBinding, StateStoreBinding
from azure_functions_agents.execution.aca_sandbox import AcaSandboxExecutionBackend
from azure_functions_agents.execution.binding import AgentBinding
from azure_functions_agents.execution.factory import create_execution_backend
from azure_functions_agents.session_state import AppIdentity, FunctionAppPrincipal
from tests.doubles.fake_session_runtime import (
    DEFAULT_GROUP_RESOURCE_ID,
    FakeSandboxSessionHandle,
    FakeSandboxSessionProvider,
    FakeSessionStateStore,
)


def test_factory_selects_the_real_sandbox_backend_for_a_runtime_binding(
    tmp_path: Path,
) -> None:
    (tmp_path / "function_app.py").write_text("app = object()\n", encoding="utf-8")
    app_identity = AppIdentity.create(
        subscription_id="11111111-2222-3333-4444-555555555555",
        site_name="agent-app",
    )
    provider = FakeSandboxSessionProvider(FakeSandboxSessionHandle())
    store = FakeSessionStateStore()

    async def provider_factory() -> FakeSandboxSessionProvider:
        return provider

    async def store_factory() -> StateStoreBinding:
        return StateStoreBinding.create(
            store=store,
            state_store_fingerprint="s1-" + ("a" * 52),
        )

    runtime = SessionRuntimeBinding.create(
        app_identity=app_identity,
        sandbox_group_resource_id=DEFAULT_GROUP_RESOURCE_ID,
        script_root=tmp_path,
        provider_factory=provider_factory,
        state_store_factory=store_factory,
    )

    backend = create_execution_backend(
        binding=AgentBinding(agent_name="main"),
        session_runtime=runtime,
        owner=FunctionAppPrincipal(),
    )

    assert isinstance(backend, AcaSandboxExecutionBackend)
