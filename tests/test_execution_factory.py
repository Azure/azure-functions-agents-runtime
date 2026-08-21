from __future__ import annotations

from pathlib import Path

from azure_functions_agents.controller.readiness import SessionRuntimeBinding, StateStoreBinding
from azure_functions_agents.execution.aca_sandbox import AcaSandboxExecutionBackend
from azure_functions_agents.execution.binding import AgentBinding
from azure_functions_agents.execution.factory import create_execution_backend
from azure_functions_agents.execution.foundry_application_content import (
    build_application_content_manifest,
    compute_application_content_digest,
)
from azure_functions_agents.execution.foundry_responses_binding import (
    FoundryResponsesRuntimeBinding,
    compute_foundry_responses_binding_fingerprint,
)
from azure_functions_agents.execution.foundry_responses_execution_backend import (
    FoundryResponsesExecutionBackend,
)
from azure_functions_agents.execution.foundry_responses_runtime import FoundryResponsesRuntime
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


def test_factory_selects_foundry_responses_only_for_its_explicit_runtime(
    tmp_path: Path,
) -> None:
    (tmp_path / "main.agent.md").write_text("---\nname: Main\n---\n", encoding="utf-8")
    app_identity = AppIdentity.create(
        subscription_id="11111111-2222-3333-4444-555555555555",
        site_name="agent-app",
    )
    manifest = build_application_content_manifest(tmp_path)
    digest = compute_application_content_digest(tmp_path, manifest)
    binding = FoundryResponsesRuntimeBinding.create(
        project_endpoint="https://project.services.ai.azure.com/api/projects/demo",
        project_resource_id=(
            "/subscriptions/11111111-2222-3333-4444-555555555555"
            "/resourceGroups/agents-rg/providers/Microsoft.CognitiveServices/accounts/project/projects/demo"
        ),
        managed_agent_name="hosted-agent",
        managed_agent_version="v1",
        application_content_manifest=manifest,
        application_content_digest=digest,
        wrapper_digest="sha256:" + ("a" * 64),
        binding_fingerprint="fha1-" + ("a" * 52),
    )
    runtime = FoundryResponsesRuntime.create(
        binding=FoundryResponsesRuntimeBinding.create(
            project_endpoint=binding.project_endpoint,
            project_resource_id=binding.project_resource_id,
            managed_agent_name=binding.managed_agent_name,
            managed_agent_version=binding.managed_agent_version,
            application_content_manifest=binding.application_content_manifest,
            application_content_digest=binding.application_content_digest,
            wrapper_digest=binding.wrapper_digest,
            binding_fingerprint=compute_foundry_responses_binding_fingerprint(
                app_identity=app_identity,
                project_endpoint=binding.project_endpoint,
                project_resource_id=binding.project_resource_id,
                managed_agent_name=binding.managed_agent_name,
                managed_agent_version=binding.managed_agent_version,
                application_content_manifest=binding.application_content_manifest,
                application_content_digest=binding.application_content_digest,
                wrapper_digest=binding.wrapper_digest,
            ),
        ),
        app_identity=app_identity,
    )

    backend = create_execution_backend(
        binding=AgentBinding(agent_name="main"),
        stream_events=True,
        session_runtime=runtime,
        owner=FunctionAppPrincipal(),
    )

    assert isinstance(backend, FoundryResponsesExecutionBackend)
    assert backend._stream_events is True
