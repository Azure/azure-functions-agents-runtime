"""Public configuration package surface."""

from __future__ import annotations

from azure_functions_agents.config.env import (
    _INLINE_DOLLAR_PATTERN,
    _INLINE_PERCENT_PATTERN,
    _to_bool,
    resolve_env_vars_in_data,
    substitute_env_vars_in_text,
    substitute_env_vars_in_value,
)
from azure_functions_agents.config.loader import load_agent_specs, load_global_config
from azure_functions_agents.config.merge import (
    DEFAULT_TIMEOUT,
    apply_mcp_filter,
    apply_skills_filter,
    apply_tools_filter,
    compose,
)
from azure_functions_agents.config.paths import (
    _REMOTE_CONFIG_DIR,
    _app_root,
    get_app_root,
    resolve_config_dir,
    set_app_root,
)
from azure_functions_agents.config.schema import (
    AgentSpec,
    DebugConfig,
    ExecuteInSessionsConfig,
    GlobalConfig,
    McpFilter,
    ResolvedAgent,
    SkillsFilter,
    SystemToolsAgentOverride,
    SystemToolsConfig,
    ToolsFilter,
    ToolsFromConnectionEntry,
    TriggerSpec,
)
from azure_functions_agents.config.validation import (
    validate_resolved_agent,
)

__all__ = [
    "DEFAULT_TIMEOUT",
    "_INLINE_DOLLAR_PATTERN",
    "_INLINE_PERCENT_PATTERN",
    "_REMOTE_CONFIG_DIR",
    "AgentSpec",
    "DebugConfig",
    "ExecuteInSessionsConfig",
    "GlobalConfig",
    "McpFilter",
    "ResolvedAgent",
    "SkillsFilter",
    "SystemToolsAgentOverride",
    "SystemToolsConfig",
    "ToolsFilter",
    "ToolsFromConnectionEntry",
    "TriggerSpec",
    "_app_root",
    "_to_bool",
    "apply_mcp_filter",
    "apply_skills_filter",
    "apply_tools_filter",
    "compose",
    "get_app_root",
    "load_agent_specs",
    "load_global_config",
    "resolve_config_dir",
    "resolve_env_vars_in_data",
    "set_app_root",
    "substitute_env_vars_in_text",
    "substitute_env_vars_in_value",
    "validate_resolved_agent",
]
