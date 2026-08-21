from __future__ import annotations

from pathlib import Path

import pytest

from azure_functions_agents.controller.readiness import StateStoreBinding
from azure_functions_agents.execution.foundry_application_content import (
    build_application_content_manifest,
    compute_application_content_digest,
)
from azure_functions_agents.execution.foundry_responses_binding import (
    FoundryResponsesRuntimeBinding,
    compute_foundry_responses_binding_fingerprint,
)
from azure_functions_agents.execution.foundry_responses_runtime import FoundryResponsesRuntime
from azure_functions_agents.session_state import AppIdentity

_APP = AppIdentity.create(
    subscription_id="11111111-2222-3333-4444-555555555555",
    site_name="agent-app",
)
_PROJECT_RESOURCE_ID = (
    "/subscriptions/11111111-2222-3333-4444-555555555555"
    "/resourceGroups/agents-rg/providers/Microsoft.CognitiveServices/accounts/project/projects/demo"
)


def _binding(root: Path) -> FoundryResponsesRuntimeBinding:
    (root / "main.agent.md").write_text("---\nname: Main\n---\n", encoding="utf-8")
    manifest = build_application_content_manifest(root)
    digest = compute_application_content_digest(root, manifest)
    values = {
        "project_endpoint": "https://project.services.ai.azure.com/api/projects/demo",
        "project_resource_id": _PROJECT_RESOURCE_ID,
        "managed_agent_name": "hosted-agent",
        "managed_agent_version": "v1",
        "application_content_manifest": manifest,
        "application_content_digest": digest,
        "wrapper_digest": "sha256:" + ("a" * 64),
    }
    return FoundryResponsesRuntimeBinding.create(
        **values,
        binding_fingerprint=compute_foundry_responses_binding_fingerprint(
            app_identity=_APP,
            **values,
        ),
    )


@pytest.mark.asyncio
async def test_runtime_opens_transport_and_state_store_only_on_first_use(tmp_path: Path) -> None:
    opened: list[str] = []
    transport = object()
    state_store = object()

    async def transport_factory() -> object:
        opened.append("transport")
        return transport

    async def state_store_factory() -> StateStoreBinding:
        opened.append("state")
        return StateStoreBinding.create(
            store=state_store,  # type: ignore[arg-type]
            state_store_fingerprint="s1-" + ("a" * 52),
        )

    runtime = FoundryResponsesRuntime.create(
        binding=_binding(tmp_path),
        app_identity=_APP,
        transport_factory=transport_factory,  # type: ignore[arg-type]
        state_store_factory=state_store_factory,
    )

    assert opened == []
    assert await runtime.get_transport() is transport
    assert await runtime.get_transport() is transport
    assert (await runtime.get_state_store()).store is state_store
    assert opened == ["transport", "state"]
