"""Public registration helpers."""

from .capabilities import (
    AgentCapabilities,
    build_capabilities,
    existing_tool_names,
    validate_subagent_tool_names,
)
from .catalog import AgentCatalog, CatalogEntry, build_catalog
from .endpoints import register_builtin_endpoints, reset_builtin_slug_registry
from .triggers import register_agent

__all__ = [
    "AgentCapabilities",
    "AgentCatalog",
    "CatalogEntry",
    "build_capabilities",
    "build_catalog",
    "existing_tool_names",
    "register_agent",
    "register_builtin_endpoints",
    "reset_builtin_slug_registry",
    "validate_subagent_tool_names",
]
