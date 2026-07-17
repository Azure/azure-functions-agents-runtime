"""Azure Functions agent runtime app factory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import azure.durable_functions as df
import azure.functions as func

from ._logger import logger
from ._observability import configure_observability
from ._source_marker import source_marker
from .config.loader import load_agent_specs, load_global_config
from .config.merge import compose
from .config.paths import get_app_root, set_app_root
from .config.schema import ResolvedAgent
from .config.validation import validate_resolved_agent, validate_subagent_references
from .discovery.mcp import discover_mcp_servers
from .discovery.skills import discover_skills
from .discovery.tools import discover_project_tools
from .registration.capabilities import build_capabilities, validate_subagent_tool_names
from .registration.catalog import AgentCatalog, CatalogEntry, build_catalog
from .registration.endpoints import register_builtin_endpoints
from .registration.triggers import register_agent
from .workflows import build_workflow_integration


def _tool_name(tool: object) -> str:
    name = getattr(tool, "name", "") or ""
    return str(name)


def _serialize_capabilities_for_log(
    *,
    user_tools: list[Any] | None,
    mcp_tools: list[Any] | None,
    skill_paths: list[Path],
    skill_name_by_path: dict[str, str],
) -> dict[str, list[str]]:
    return {
        "user_tools": sorted(_tool_name(tool) for tool in (user_tools or [])),
        "mcp_servers": sorted(_tool_name(tool) for tool in (mcp_tools or [])),
        "skills": sorted(
            skill_name_by_path.get(str(path.resolve()), path.name) for path in skill_paths
        ),
    }


def _builtin_endpoints_enabled(builtin_endpoints: Any) -> bool:
    return bool(
        builtin_endpoints.debug_chat_ui or builtin_endpoints.chat_api or builtin_endpoints.mcp
    )


def _workflows_requested(workflows: dict[str, Any] | None) -> bool:
    return isinstance(workflows, dict) and workflows.get("enabled") is True


def _fail_on_duplicate_slugs(resolved_agents: list[ResolvedAgent]) -> set[str]:
    """Fail fast on colliding agent identity slugs and return the known-slug set.

    A slug (sanitized file stem) doubles as the function name, the
    ``/agents/<slug>/`` route, and the ``delegate_<slug>`` tool name, so a
    collision is a hard startup error (Decision #17), not the old silent
    auto-suffix behavior. Must run first (two-pass composition, pass 1) so
    ``known_slugs`` can be handed to ``validate_subagent_references``.
    """
    sources_by_slug: dict[str, list[str]] = {}
    for resolved in resolved_agents:
        sources_by_slug.setdefault(resolved.slug, []).append(source_marker(resolved.source_file))

    for slug, sources in sorted(sources_by_slug.items()):
        if len(sources) > 1:
            listed = ", ".join(sorted(sources))
            raise ValueError(
                f"Duplicate agent slug {slug!r} is used by {len(sources)} source "
                f"files: {listed}. Agent identity slugs must be globally unique "
                "across the app (a slug doubles as the registered function "
                "name, the `/agents/<slug>/` built-in endpoint route, and the "
                "`delegate_<slug>` tool name). Rename one of the colliding "
                "source files (e.g. its file stem) to resolve this. See "
                "docs/front-matter-spec.md#subagents."
            )

    return set(sources_by_slug)


def create_function_app(app_root: Path | None = None) -> func.FunctionApp:
    """Build and return a fully-configured Azure Functions app.

    Two-pass composition: resolve, validate, and freeze every agent into a
    read-only ``AgentCatalog`` (pass 1) before registering any trigger or
    endpoint (pass 2), so `subagents:` references always see the full,
    already-validated app. See FRD 0006 §4.2 for the full pipeline stages.
    """
    if app_root is not None:
        set_app_root(app_root)
    resolved_root = get_app_root()

    global_config = load_global_config(resolved_root)

    # Bootstrap observability before anything runs so MAF gen_ai spans + runtime spans/metrics
    # flow to Application Insights with zero app code. No-op unless a telemetry provider is active.
    configure_observability()

    agent_specs = load_agent_specs(resolved_root)
    tool_result = discover_project_tools(resolved_root)
    mcp_result = discover_mcp_servers(resolved_root)
    skill_result = discover_skills(resolved_root)

    user_tools = tool_result.user_tools
    workflow_tools = tool_result.workflow_tools
    mcp_tools = mcp_result.servers
    skills = skill_result.skills
    skill_names = list(skills)
    mcp_names = list(mcp_tools)
    skill_name_by_path = {str(path.resolve()): name for name, path in skills.items()}
    discovered_user_tool_names = sorted(_tool_name(tool) for tool in user_tools)

    logger.info(
        "discovery_summary: mcp_servers=%s skills=%s user_tools=%s",
        sorted(mcp_names),
        sorted(skill_names),
        discovered_user_tool_names,
    )

    resolved_agents = [
        compose(
            spec,
            global_config,
            discovered_mcp_names=mcp_names,
            discovered_skill_names=skill_names,
        )
        for spec in agent_specs
    ]

    # --- Two-pass composition, pass 1a: app-wide identity index (FRD 0006 §4.2). ---
    # Must run before any other cross-agent validation: `validate_subagent_references`
    # needs a collision-free slug set, and nothing below may assume slugs are unique
    # until this has actually verified it.
    known_slugs = _fail_on_duplicate_slugs(resolved_agents)

    referenced_slugs: set[str] = set()
    for resolved in resolved_agents:
        validate_subagent_references(resolved, known_slugs=known_slugs)
        referenced_slugs.update(ref.agent for ref in resolved.subagents)

    workflows_requested = any(
        resolved.is_main and _workflows_requested(resolved.workflows)
        for resolved in resolved_agents
    )
    app: func.FunctionApp = (
        df.DFApp(http_auth_level=func.AuthLevel.FUNCTION)
        if workflows_requested
        else func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)
    )

    # Collect indexing summary for structured logging
    agents_summary: list[dict[str, Any]] = []
    system_tools_used: set[str] = set()

    # Track global system tools configuration
    if (
        global_config.system_tools
        and global_config.system_tools.dynamic_sessions_code_interpreter
    ):
        system_tools_used.add("dynamic_sessions_code_interpreter")
    if not (global_config.system_tools and global_config.system_tools.web_request is False):
        system_tools_used.add("web_request")

    # --- Two-pass composition, pass 1b (FRD 0006 §4.2): validate + build capabilities ---
    # for every agent and freeze the result into a read-only AgentCatalog. Nothing here
    # touches `app` — a coordinator's `delegate_<slug>` tools must be able to resolve
    # *any* specialist by slug at request time, including ones registered later in
    # `resolved_agents` than the coordinator itself, so the full catalog has to exist
    # before pass 2 (FunctionApp registration) begins.
    catalog_entries: dict[str, CatalogEntry] = {}
    for resolved in resolved_agents:
        # Validation is owned by the app factory; compose() stays a pure translation step.
        validate_resolved_agent(
            resolved,
            discovered_mcp_names=mcp_names,
            discovered_skills=skill_names,
            is_referenced_as_subagent=resolved.slug in referenced_slugs,
        )
        capabilities = build_capabilities(
            resolved,
            discovered_user_tools=user_tools,
            discovered_workflow_tools=workflow_tools,
            discovered_mcp_tools=mcp_tools,
            discovered_skills=skills,
        )
        validate_subagent_tool_names(resolved, capabilities)
        catalog_entries[resolved.slug] = CatalogEntry(resolved, capabilities)

    catalog: AgentCatalog = build_catalog(catalog_entries)

    # --- Two-pass composition, pass 2 (FRD 0006 §4.2): mutate `app` --------------------
    for resolved in resolved_agents:
        capabilities = catalog[resolved.slug].capabilities

        workflows_enabled = False
        workflow_system_addendum: str | None = None
        if resolved.is_main:
            _, workflow_system_addendum = build_workflow_integration(
                app,
                resolved.metadata,
                workflow_tools=capabilities.filtered_workflow_tools,
            )
            workflows_enabled = workflow_system_addendum is not None
        elif _workflows_requested(resolved.workflows):
            logger.warning(
                "workflows.enabled is only honored on main.agent.md; ignoring "
                "workflows for agent %s",
                resolved.name,
            )

        capability_names = _serialize_capabilities_for_log(
            user_tools=capabilities.filtered_user_tools,
            mcp_tools=capabilities.filtered_mcp_tools,
            skill_paths=capabilities.enabled_skill_paths,
            skill_name_by_path=skill_name_by_path,
        )
        logger.info(
            "agent_capabilities_registered: source_file=%s user_tools=%s mcp_servers=%s skills=%s",
            source_marker(resolved.source_file),
            capability_names["user_tools"],
            capability_names["mcp_servers"],
            capability_names["skills"],
        )
        # The identity slug (pass 1a) is already guaranteed globally unique, so it is
        # used directly as the registered function name / built-in endpoint slug —
        # no allocator or de-duplication pass is needed here anymore.
        if resolved.trigger is not None:
            register_agent(
                app,
                resolved,
                capabilities,
                function_name=resolved.slug,
                catalog=catalog,
            )
        if _builtin_endpoints_enabled(resolved.builtin_endpoints):
            register_builtin_endpoints(
                app,
                resolved,
                capabilities,
                slug=resolved.slug,
                workflows_enabled=workflows_enabled,
                workflow_system_addendum=workflow_system_addendum,
                catalog=catalog,
            )

        # Collect agent summary info
        agent_info: dict[str, Any] = {
            "source_file": source_marker(resolved.source_file),
            "registered_capabilities": capability_names,
        }
        if resolved.trigger:
            agent_info["trigger_type"] = resolved.trigger.type
        else:
            agent_info["trigger_type"] = None
        if _builtin_endpoints_enabled(resolved.builtin_endpoints):
            endpoints = []
            if resolved.builtin_endpoints.debug_chat_ui:
                endpoints.append("debug_chat_ui")
            if resolved.builtin_endpoints.chat_api:
                endpoints.append("chat_api")
            if resolved.builtin_endpoints.mcp:
                endpoints.append("mcp")
            agent_info["builtin_endpoints"] = endpoints
        if workflows_enabled:
            agent_info["workflows"] = "enabled"

        # Track per-agent system tools (if not opted out)
        if resolved.sandbox_config:
            system_tools_used.add("dynamic_sessions_code_interpreter")
        if resolved.web_request_config:
            system_tools_used.add("web_request")

        agents_summary.append(agent_info)

    # Emit structured indexing summary log
    indexing_summary = {
        "event": "agent_runtime_indexed",
        "agent_count": len(agent_specs),
        "agents": agents_summary,
        "system_tools": list(system_tools_used),
        "discovered_capabilities": {
            "mcp_servers": len(mcp_names),
            "skills": len(skill_names),
            "user_tools": len(user_tools),
        },
        "discovered_capability_names": {
            "mcp_servers": sorted(mcp_names),
            "skills": sorted(skill_names),
            "user_tools": discovered_user_tool_names,
        },
        "failed_loads": {
            "mcp_servers": [f"{name}: {error}" for name, error in mcp_result.failed_loads],
            "skills": [f"{path}: {error}" for path, error in skill_result.failed_loads],
            "user_tools": [f"{file}: {error}" for file, error in tool_result.failed_loads],
        },
    }
    logger.info(
        "Agent runtime indexing completed: %s",
        json.dumps(indexing_summary, ensure_ascii=False, default=str),
    )

    return app
