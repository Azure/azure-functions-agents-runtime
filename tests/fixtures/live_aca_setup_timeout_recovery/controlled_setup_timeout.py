"""Fixture-only provider wrapper that delays create after durable reservation."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Final

import azure.functions as func

from azure_functions_agents import app as app_module
from azure_functions_agents.config.loader import load_global_config
from azure_functions_agents.execution.aca_composition import AcaExecutionComposition
from azure_functions_agents.execution.setup_budget import SETUP_BUDGET_SECONDS
from azure_functions_agents.transport.aca_sdk import AcaSandboxAdapter
from azure_functions_agents.transport.manifest import ExpectedSandboxManifestBinding
from azure_functions_agents.transport.ports import SandboxSessionHandle, SandboxSessionProvider
from azure_functions_agents.transport.transport_models import (
    PersistedSandboxBinding,
    SandboxCreateRequest,
    SandboxGroupBinding,
    SandboxGroupIdentity,
    SandboxSnapshot,
    SandboxSummary,
)

POST_RESERVATION_DELAY_SECONDS: Final = SETUP_BUDGET_SECONDS + 5.0
_RECONCILER_CADENCE_ENV: Final = "AZURE_FUNCTIONS_AGENTS_RECONCILER_CADENCE_SECONDS"
_RECONCILER_CADENCE_SECONDS: Final = "60"


class _DelayedCreateSandboxProvider:
    """Delegate the real provider while delaying only its create operation."""

    def __init__(self, inner: SandboxSessionProvider) -> None:
        self._inner = inner

    @property
    def group(self) -> SandboxGroupIdentity:
        return self._inner.group

    async def create(
        self,
        request: SandboxCreateRequest,
        *,
        persisted_group: SandboxGroupBinding,
    ) -> SandboxSessionHandle:
        if not request.reconcile_only:
            await asyncio.sleep(POST_RESERVATION_DELAY_SECONDS)
        return await self._inner.create(request, persisted_group=persisted_group)

    async def attach(
        self,
        persisted: PersistedSandboxBinding,
        expected: ExpectedSandboxManifestBinding,
        *,
        readiness_timeout_seconds: float,
    ) -> SandboxSessionHandle:
        return await self._inner.attach(
            persisted,
            expected,
            readiness_timeout_seconds=readiness_timeout_seconds,
        )

    async def resume(
        self,
        persisted: PersistedSandboxBinding,
        expected: ExpectedSandboxManifestBinding,
        *,
        readiness_timeout_seconds: float,
    ) -> SandboxSessionHandle:
        return await self._inner.resume(
            persisted,
            expected,
            readiness_timeout_seconds=readiness_timeout_seconds,
        )

    async def list_sandboxes(self, *, labels: dict[str, str]) -> tuple[SandboxSummary, ...]:
        return await self._inner.list_sandboxes(labels=labels)

    async def delete_sandbox(self, sandbox_id: str) -> None:
        await self._inner.delete_sandbox(sandbox_id)

    async def list_snapshots(self) -> tuple[SandboxSnapshot, ...]:
        return await self._inner.list_snapshots()

    async def delete_snapshot(self, snapshot_id: str) -> None:
        await self._inner.delete_snapshot(snapshot_id)

    async def close(self) -> None:
        await self._inner.close()


async def _open_delayed_provider(app_root: Path) -> SandboxSessionProvider:
    global_config = load_global_config(app_root)
    session_runtime = global_config.session_runtime
    if session_runtime is None or session_runtime.aca_sandbox is None:
        raise RuntimeError("The controlled timeout fixture requires ACA Sandbox configuration.")
    inner = await AcaSandboxAdapter.open(session_runtime.aca_sandbox.sandbox_group_resource_id)
    return _DelayedCreateSandboxProvider(inner)


def create_controlled_function_app() -> func.FunctionApp:
    """Compose this fixture with the real provider wrapped before only provider.create."""
    os.environ[_RECONCILER_CADENCE_ENV] = _RECONCILER_CADENCE_SECONDS
    original_composer = app_module.compose_aca_application

    def compose_with_delayed_provider(app_root: Path) -> AcaExecutionComposition:
        async def provider_factory() -> SandboxSessionProvider:
            return await _open_delayed_provider(app_root)

        return original_composer(app_root, provider_factory=provider_factory)

    app_module.compose_aca_application = compose_with_delayed_provider
    try:
        return app_module.create_function_app()
    finally:
        app_module.compose_aca_application = original_composer
