"""Azure Functions agent runtime app factory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import azure.functions as func

from ._logger import logger
from .config.loader import load_agent_specs, load_global_config
from .config.merge import compose
from .config.paths import get_app_root, set_app_root
from .config.validation import validate_resolved_agent
from .discovery.mcp import discover_mcp_servers
from .discovery.skills import discover_skills
from .discovery.tools import discover_user_tools
from .registration._naming import allocate_unique_function_name
from .registration.capabilities import build_capabilities
from .registration.endpoints import register_builtin_endpoints
from .registration.triggers import register_agent


def _builtin_endpoints_enabled(builtin_endpoints: Any) -> bool:
    return bool(
        builtin_endpoints.debug_chat_ui or builtin_endpoints.chat_api or builtin_endpoints.mcp
    )


def create_function_app(app_root: Path | None = None) -> func.FunctionApp:
    """Build and return a fully-configured Azure Functions app.

    Pipeline:
      1. Resolve app root (explicit > AZURE_FUNCTIONS_AGENTS_APP_ROOT > AzureWebJobsScriptRoot > cwd).
      2. Load global agents.config.yaml (optional).
      3. Load all *.agent.md frontmatter into AgentSpec objects.
      4. Discover user tools, skills, and MCP servers from disk.
      5. Compose a ResolvedAgent per spec (apply global defaults + agent overrides).
      6. Validate each ResolvedAgent (required fields, MCP exclude references, etc.).
      7. Build AgentCapabilities per agent (apply mcp/skills/tools filters).
      8. Create the FunctionApp.
      9. Register each agent's trigger (if any) and built-in endpoints (if any).
    """
    if app_root is not None:
        set_app_root(app_root)
    resolved_root = get_app_root()

    global_config = load_global_config(resolved_root)
    agent_specs = load_agent_specs(resolved_root)
    user_tools = discover_user_tools(resolved_root)
    mcp_tools = discover_mcp_servers(resolved_root)
    skills = discover_skills(resolved_root)
    skill_names = list(skills)
    mcp_names = list(mcp_tools)

    app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)
    registered_names: set[str] = set()

    # Collect indexing summary for structured logging
    agents_summary: list[dict[str, Any]] = []
    system_tools_used: set[str] = set()

    # Track global system tools configuration
    if global_config.system_tools:
        if global_config.system_tools.dynamic_sessions_code_interpreter:
            system_tools_used.add("dynamic_sessions_code_interpreter")

    for spec in agent_specs:
        resolved = compose(
            spec,
            global_config,
            discovered_mcp_names=mcp_names,
            discovered_skill_names=skill_names,
        )
        # Validation is owned by the app factory; compose() stays a pure translation step.
        validate_resolved_agent(
            resolved,
            discovered_mcp_names=mcp_names,
            discovered_skills=skill_names,
        )
        capabilities = build_capabilities(
            resolved,
            discovered_user_tools=user_tools,
            discovered_mcp_tools=mcp_tools,
            discovered_skills=skills,
        )
        allocated_name: str | None = None
        if resolved.trigger is not None or _builtin_endpoints_enabled(resolved.builtin_endpoints):
            allocated_name = allocate_unique_function_name(
                resolved.source_file,
                resolved.name,
                registered_names,
            )
        if resolved.trigger is not None:
            register_agent(
                app,
                resolved,
                capabilities,
                registered_names=registered_names if allocated_name is None else None,
                function_name=allocated_name,
            )
        if _builtin_endpoints_enabled(resolved.builtin_endpoints):
            register_builtin_endpoints(app, resolved, capabilities, slug=allocated_name)

        # Collect agent summary info
        agent_info: dict[str, Any] = {
            "name": resolved.name,
            "source_file": resolved.source_file,
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

        # Track per-agent system tools (if not opted out)
        if resolved.sandbox_config:
            system_tools_used.add("dynamic_sessions_code_interpreter")

        agents_summary.append(agent_info)

    # Emit structured indexing summary log
    indexing_summary = {
        "event": "agent_runtime_indexed",
        "agent_count": len(agent_specs),
        "agents": agents_summary,
        "system_tools": sorted(system_tools_used),
        "discovered_capabilities": {
            "mcp_servers": len(mcp_names),
            "skills": len(skill_names),
            "user_tools": len(user_tools),
        },
    }
    logger.info(
        "Agent runtime indexing completed: %s",
        json.dumps(indexing_summary, ensure_ascii=False, default=str),
    )

    return app
