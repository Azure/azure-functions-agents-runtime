"""Composition of the private APIM, MCP, and ACA durable execution planes."""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..client_manager import ClientManager
from ..config.paths import get_app_root
from ..discovery.mcp import discover_mcp_servers
from .durable_loop_catalog import (
    freeze_durable_tool_catalog,
    load_durable_tool_policy,
)
from .durable_loop_config import (
    DURABLE_LOOP_APIM_MCP_BASE_URL_ENV,
    DURABLE_LOOP_APIM_MODEL_CONTROL_BASE_URL_ENV,
    DurableLoopConfigurationError,
    DurableLoopSettings,
)
from .durable_loop_mcp import DurableRemoteMcpLane
from .durable_loop_protocol import (
    DurableFaultProfile,
    SandboxExecutionProfile,
    ToolProvenance,
    ToolRequestV1,
    ToolResultV1,
    canonical_hash,
)
from .durable_loop_receipts import DurableKeyedDocumentStore
from .durable_loop_sandbox import DurableAcaSandboxLane
from .durable_loop_tools import (
    DurableRetainedSandboxInspection,
    DurableRetainedSandboxInspectionPort,
    DurableToolCatalogSnapshot,
    DurableToolCleanupPort,
    DurableToolDispatchPort,
)
from .hybrid_apim import HybridApimClientManager
from .hybrid_config import HybridSandboxSettings


@dataclass(frozen=True, slots=True)
class DurableLoopExecutionBinding:
    """App-scoped private capability filters for the default runtime."""

    enabled_mcp_names: tuple[str, ...]
    local_tools_enabled: bool


class DurableExecutionPlaneRouter(
    DurableToolDispatchPort,
    DurableToolCleanupPort,
    DurableRetainedSandboxInspectionPort,
):
    """Route privileged remote calls to MCP and customer code to ACA."""

    def __init__(
        self,
        *,
        remote: DurableRemoteMcpLane,
        local: DurableAcaSandboxLane,
    ) -> None:
        self._remote = remote
        self._local = local
        self._snapshot: DurableToolCatalogSnapshot | None = None

    async def freeze_catalog(
        self,
        *,
        policy_hash: str,
        sandbox_profile: SandboxExecutionProfile,
    ) -> DurableToolCatalogSnapshot:
        """Discover both planes and bind every tool to explicit policy."""
        del sandbox_profile
        remote = await self._remote.discover()
        local, local_package_hash = await self._local.discover()
        package_hash = canonical_hash(
            {
                "local_package_hash": local_package_hash,
                "remote_tools": [descriptor.name for descriptor in remote],
            }
        )
        policy = load_durable_tool_policy(
            get_app_root(),
            required=bool(remote or local),
        )
        catalog = freeze_durable_tool_catalog(
            (*remote, *local),
            policy,
            package_hash=package_hash,
            base_policy_hash=policy_hash,
        )
        snapshot = DurableToolCatalogSnapshot(
            catalog=catalog,
            package_hash=package_hash,
        )
        self._snapshot = snapshot
        return snapshot

    async def dispatch(self, request: ToolRequestV1) -> ToolResultV1:
        """Route only by the frozen provenance value."""
        snapshot = self._snapshot
        if snapshot is None:
            remote = await self._remote.discover()
            local, local_package_hash = await self._local.discover()
            package_hash = canonical_hash(
                {
                    "local_package_hash": local_package_hash,
                    "remote_tools": [descriptor.name for descriptor in remote],
                }
            )
            policy = load_durable_tool_policy(
                get_app_root(),
                required=bool(remote or local),
            )
            catalog = freeze_durable_tool_catalog(
                (*remote, *local),
                policy,
                package_hash=package_hash,
                base_policy_hash="0" * 64,
            ).model_copy(update={"policy_hash": request.policy_hash})
            snapshot = DurableToolCatalogSnapshot(
                catalog=catalog,
                package_hash=package_hash,
            )
            self._snapshot = snapshot
        if (
            request.catalog_hash != snapshot.catalog.catalog_hash
            or request.policy_hash != snapshot.catalog.policy_hash
            or request.package_hash != snapshot.package_hash
        ):
            raise RuntimeError("durable execution catalog binding changed")
        descriptor = snapshot.catalog.by_name().get(request.tool_name)
        if (
            descriptor is None
            or descriptor.provenance is not request.provenance
            or descriptor.behavior is not request.behavior
        ):
            raise RuntimeError("durable tool request classification changed")
        if request.provenance is ToolProvenance.REMOTE:
            return await self._remote.dispatch(request)
        if request.provenance is ToolProvenance.LOCAL:
            return await self._local.dispatch(request)
        raise RuntimeError("runtime control tools must not reach a transport lane")

    async def cleanup(
        self,
        *,
        run_id: str,
        session_id: str,
        sandbox_profile: SandboxExecutionProfile,
        fault_profile: DurableFaultProfile,
    ) -> None:
        """Explicitly clean retained ACA state; MCP stays worker-scoped."""
        await self._local.cleanup(
            run_id=run_id,
            session_id=session_id,
            profile=sandbox_profile,
            fault_profile=fault_profile,
        )

    async def inspect_retained_sandbox(
        self,
        *,
        run_id: str,
        session_id: str,
    ) -> DurableRetainedSandboxInspection | None:
        """Return the safe retained ACA projection for one durable run."""
        return await self._local.inspect_retained_sandbox(
            run_id=run_id,
            session_id=session_id,
        )


def build_durable_execution_plane(
    *,
    client_manager: ClientManager,
    settings: DurableLoopSettings,
    content: object,
    receipts: DurableKeyedDocumentStore,
    binding: DurableLoopExecutionBinding,
) -> DurableExecutionPlaneRouter:
    """Construct real private execution planes from existing settings."""
    if not isinstance(client_manager, HybridApimClientManager):
        raise DurableLoopConfigurationError(
            "durable execution planes require the APIM client manager"
        )
    mcp_base_url = os.environ.get(DURABLE_LOOP_APIM_MCP_BASE_URL_ENV, "").strip()
    if not mcp_base_url:
        raise DurableLoopConfigurationError(
            f"{DURABLE_LOOP_APIM_MCP_BASE_URL_ENV} is required"
        )
    configured = discover_mcp_servers(get_app_root())
    selected = tuple(
        configured.servers[name]
        for name in binding.enabled_mcp_names
        if name in configured.servers
    )
    sandbox_settings = HybridSandboxSettings.from_environment()
    if sandbox_settings is None:
        raise DurableLoopConfigurationError(
            "durable local tools require the private ACA Sandbox Group settings"
        )
    from .durable_loop_activities import DurableContentStore

    if not isinstance(content, DurableContentStore):
        raise TypeError("durable content store has an invalid type")
    return DurableExecutionPlaneRouter(
        remote=DurableRemoteMcpLane(
            manager=client_manager,
            base_url=mcp_base_url,
            configured=selected,
            content=content,
            receipts=receipts,
        ),
        local=DurableAcaSandboxLane(
            settings=sandbox_settings,
            loop_settings=settings,
            content=content,
            receipts=receipts,
            local_tools_enabled=binding.local_tools_enabled,
        ),
    )


def durable_model_control_base_url() -> str:
    """Return the required private APIM background-control frontend."""
    value = os.environ.get(
        DURABLE_LOOP_APIM_MODEL_CONTROL_BASE_URL_ENV,
        "",
    ).strip()
    if not value:
        raise DurableLoopConfigurationError(
            f"{DURABLE_LOOP_APIM_MODEL_CONTROL_BASE_URL_ENV} is required"
        )
    return value
