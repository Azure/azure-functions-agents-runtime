"""Reusable ACA execution composition without Function registration side effects."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config.loader import load_agent_specs, load_global_config
from ..config.merge import compose
from ..config.schema import GlobalConfig, ResolvedAgent
from ..config.validation import (
    validate_resolved_agent,
    validate_session_runtime,
    validate_subagent_references,
    validate_workflow_subagent_references,
)
from ..controller.readiness import SessionRuntimeBinding
from ..controller.sandbox_config import SandboxCreateProfile
from ..discovery.mcp import discover_mcp_servers
from ..discovery.skills import discover_skills
from ..discovery.tools import discover_project_tools
from ..harness.delegation import validate_delegation_graph
from ..registration._handlers import build_output_validator
from ..registration.capabilities import build_capabilities, validate_subagent_tool_names
from ..registration.catalog import AgentCatalog, CatalogEntry, build_catalog
from ..session_state import AppIdentity, OwnerPrincipal
from ..transport.ports import SandboxSessionProvider
from .aca_sandbox import AcaSandboxExecutionBackend
from .binding import AgentBinding
from .factory import create_execution_backend


@dataclass(frozen=True, slots=True)
class AcaExecutionComposition:
    """Registration-free, fully resolved application inputs and ACA runtime."""

    app_root: Path
    global_config: GlobalConfig
    agent_specs: tuple[Any, ...]
    resolved_agents: tuple[ResolvedAgent, ...]
    catalog: AgentCatalog
    bindings: dict[str, AgentBinding]
    tool_result: Any
    mcp_result: Any
    skill_result: Any
    create_profile: SandboxCreateProfile | None
    session_runtime: SessionRuntimeBinding | None

    def backend_for(
        self,
        agent_slug: str,
        *,
        owner: OwnerPrincipal,
    ) -> AcaSandboxExecutionBackend:
        """Build an ACA backend bound to an explicit authenticated owner."""

        runtime = self.session_runtime
        if runtime is None:
            raise ValueError("ACA Sandbox is not configured for this application")
        backend = create_execution_backend(
            binding=self.bindings[agent_slug],
            session_runtime=runtime,
            owner=owner,
        )
        if not isinstance(backend, AcaSandboxExecutionBackend):
            raise RuntimeError("ACA composition did not produce an ACA backend")
        return backend


def compose_aca_execution(
    *,
    app_root: Path,
    global_config: GlobalConfig,
    resolved_agents: list[ResolvedAgent],
    catalog: AgentCatalog,
    bindings: dict[str, AgentBinding],
    mcp_result: Any,
    agent_specs: tuple[Any, ...] = (),
    tool_result: Any = None,
    skill_result: Any = None,
    app_identity: AppIdentity | None = None,
    provider_factory: Callable[[], Awaitable[SandboxSessionProvider]] | None = None,
) -> AcaExecutionComposition:
    """Compose ACA runtime inputs once for registration and non-HTTP callers."""

    # These helpers remain in app.py because their later reconciler callbacks are
    # application registration concerns. Importing here avoids a module cycle at
    # import time while keeping this facade free of Azure Functions registration.
    from ..app import _build_sandbox_create_profile, _build_session_runtime_binding

    create_profile = _build_sandbox_create_profile(global_config, resolved_agents, mcp_result)
    session_runtime = _build_session_runtime_binding(
        global_config,
        app_root,
        terminal_bindings=bindings,
        create_profile=create_profile,
        app_identity=app_identity,
        provider_factory=provider_factory,
    )
    return AcaExecutionComposition(
        app_root=app_root,
        global_config=global_config,
        agent_specs=agent_specs,
        resolved_agents=tuple(resolved_agents),
        catalog=catalog,
        bindings=bindings,
        tool_result=tool_result,
        mcp_result=mcp_result,
        skill_result=skill_result,
        create_profile=create_profile,
        session_runtime=session_runtime,
    )


def compose_aca_application(
    app_root: Path,
    *,
    app_identity: AppIdentity | None = None,
    provider_factory: Callable[[], Awaitable[SandboxSessionProvider]] | None = None,
) -> AcaExecutionComposition:
    """Resolve ACA application execution without registering Azure Functions routes."""

    global_config = load_global_config(app_root)
    agent_specs = load_agent_specs(app_root)
    tool_result = discover_project_tools(app_root)
    mcp_result = discover_mcp_servers(app_root)
    skill_result = discover_skills(app_root)
    mcp_names = list(mcp_result.servers)
    skill_names = list(skill_result.skills)
    resolved_agents = [
        compose(
            spec,
            global_config,
            discovered_mcp_names=mcp_names,
            discovered_skill_names=skill_names,
        )
        for spec in agent_specs
    ]
    validate_session_runtime(global_config, resolved_agents)
    from ..app import _fail_on_duplicate_slugs

    known_slugs = _fail_on_duplicate_slugs(resolved_agents)
    referenced_slugs: set[str] = set()
    for resolved in resolved_agents:
        validate_subagent_references(resolved, known_slugs=known_slugs)
        validate_workflow_subagent_references(resolved, known_slugs=known_slugs)
        referenced_slugs.update(ref.agent for ref in resolved.subagents)
        if resolved.workflows is not None:
            referenced_slugs.update(ref.agent for ref in resolved.workflows.subagents)
    if global_config.session_runtime is not None and global_config.session_runtime.aca_sandbox is not None:
        validate_delegation_graph(resolved_agents)

    entries: dict[str, CatalogEntry] = {}
    bindings: dict[str, AgentBinding] = {}
    for resolved in resolved_agents:
        validate_resolved_agent(
            resolved,
            discovered_mcp_names=mcp_names,
            discovered_skills=skill_names,
            is_referenced_as_subagent=resolved.slug in referenced_slugs,
        )
        capabilities = build_capabilities(
            resolved,
            discovered_user_tools=tool_result.user_tools,
            discovered_workflow_tools=tool_result.workflow_tools,
            discovered_mcp_tools=mcp_result.servers,
            discovered_skills=skill_result.skills,
        )
        validate_subagent_tool_names(resolved, capabilities)
        entries[resolved.slug] = CatalogEntry(resolved, capabilities)

    catalog = build_catalog(entries)
    for resolved in resolved_agents:
        capabilities = catalog[resolved.slug].capabilities
        bindings[resolved.slug] = AgentBinding(
            instructions=resolved.instructions,
            tools=capabilities.filtered_user_tools,
            mcp_tools=capabilities.filtered_mcp_tools,
            skill_paths=capabilities.enabled_skill_paths,
            model=resolved.model,
            agent_name=resolved.slug,
            display_name=resolved.name,
            web_request_tools=capabilities.web_request_tools,
            subagents=resolved.subagents,
            catalog=catalog,
            output_validator=build_output_validator(resolved),
        )
    return compose_aca_execution(
        app_root=app_root,
        global_config=global_config,
        resolved_agents=resolved_agents,
        catalog=catalog,
        bindings=bindings,
        mcp_result=mcp_result,
        agent_specs=tuple(agent_specs),
        tool_result=tool_result,
        skill_result=skill_result,
        app_identity=app_identity,
        provider_factory=provider_factory,
    )
