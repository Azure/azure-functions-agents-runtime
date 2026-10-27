"""Packaged, private smart-retry advisor configuration."""

from __future__ import annotations

from importlib.resources import files
from typing import cast

import frontmatter

from .config.merge import compose
from .config.schema import AgentSpec, GlobalConfig
from .registration.capabilities import AgentCapabilities
from .registration.catalog import CatalogEntry

DEFAULT_ADVISOR_SLUG = "_smart_retry_advisor"
_ADVISOR_RESOURCE = "resources/smart-retry-advisor.agent.md"


def build_default_advisor(global_config: GlobalConfig) -> CatalogEntry:
    """Compose bundled guidance with application defaults, without granting capabilities."""
    document = frontmatter.loads(
        files("azure_functions_agents").joinpath(_ADVISOR_RESOURCE).read_text(encoding="utf-8")
    )
    metadata = cast(dict[str, object], document.metadata)
    spec = AgentSpec.model_validate({
        **metadata,
        "instructions": document.content,
        "tools": False,
        "mcp": False,
        "skills": False,
        "substitute_variables": False,
        "system_tools": {"dynamic_sessions_code_interpreter": False, "web_request": False},
    })
    resolved = compose(spec, global_config).model_copy(update={"slug": DEFAULT_ADVISOR_SLUG})
    return CatalogEntry(resolved, AgentCapabilities())