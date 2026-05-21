"""Validation helpers for configuration translation."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from azure_functions_agents._logger import logger as _logger

if TYPE_CHECKING:
    from azure_functions_agents.config.schema import AgentConfiguration

_SPEC_LINK_DEFAULT = "docs/front-matter-spec.md"


def _format_error(
    source_file: str | Path,
    field: str,
    message: str,
    spec_anchor: str = "",
) -> str:
    spec_link = f"{_SPEC_LINK_DEFAULT}{spec_anchor}"
    normalized_message = message if message.endswith(".") else f"{message}."
    suffix = "" if "See " in normalized_message else f" See {spec_link}."
    return f"{Path(source_file)}: field `{field}`: {normalized_message}{suffix}"


def _agent_label(agent_name: str | None) -> str:
    return f"Agent `{agent_name}`" if agent_name else "Agent"


def _provider_sub_blocks() -> tuple[str, ...]:
    from azure_functions_agents.client_manager.providers import PROVIDER_REGISTRY

    return tuple(sorted(PROVIDER_REGISTRY))


def _present_provider_sub_blocks(agent_configuration: AgentConfiguration) -> list[str]:
    return [
        name
        for name in _provider_sub_blocks()
        if getattr(agent_configuration, name, None) is not None
    ]


def validate_agent_configuration(
    agent_configuration: AgentConfiguration,
    *,
    source_file: str | Path,
    agent_name: str | None = None,
) -> None:
    """Run structural validation for provider selection after config composition."""
    from azure_functions_agents.client_manager.providers import (
        PROVIDER_REGISTRY,
        UnknownProviderError,
        get_provider,
    )

    agent = _agent_label(agent_name)
    provider = agent_configuration.provider

    try:
        get_provider(provider)
    except UnknownProviderError as exc:
        known = ", ".join(sorted(PROVIDER_REGISTRY))
        raise ValueError(
            _format_error(
                source_file,
                "agent_configuration.provider",
                f"{agent} declares unknown provider {provider!r}. "
                f"Expected one of: {known}. Update `agent_configuration.provider` "
                "to a registered provider name and keep only the matching provider sub-block",
                "#agent_configuration",
            )
        ) from exc

    present_provider_blocks = _present_provider_sub_blocks(agent_configuration)
    if len(present_provider_blocks) > 1:
        blocks = ", ".join(f"`{name}`" for name in present_provider_blocks)
        raise ValueError(
            _format_error(
                source_file,
                "agent_configuration",
                f"{agent} declares multiple provider sub-blocks ({blocks}). "
                f"Expected exactly one sub-block matching provider {provider!r}. "
                f"Remove the extra sub-blocks so only `agent_configuration.{provider}` remains",
                "#agent_configuration",
            )
        )

    if getattr(agent_configuration, provider, None) is None:
        if present_provider_blocks:
            actual = present_provider_blocks[0]
            message = (
                f"{agent} declares provider {provider!r}, which requires the matching "
                f"`{provider}` sub-block; got `{actual}` instead. Add "
                f"`agent_configuration.{provider}` and remove the mismatched `{actual}` "
                f"sub-block, or change `provider` to {actual!r}"
            )
        else:
            message = (
                f"{agent} declares provider {provider!r}, which requires the matching "
                f"`{provider}` sub-block. Add `agent_configuration.{provider}` with the "
                "provider-specific settings for this agent"
            )
        raise ValueError(
            _format_error(
                source_file,
                "agent_configuration",
                message,
                "#agent_configuration",
            )
        )

def validate_resolved_agent(
    resolved: Any,
    *,
    discovered_mcp_names: list[str],
    discovered_skills: list[str],
) -> None:
    """Run post-merge sanity checks for a resolved agent."""
    source_file = resolved.source_file or "<unknown>"
    if getattr(resolved, "agent_configuration", None) is not None:
        validate_agent_configuration(
            resolved.agent_configuration,
            source_file=source_file,
            agent_name=getattr(resolved, "name", None),
        )

    if not resolved.is_main and resolved.trigger is None:
        raise ValueError(
            _format_error(
                source_file,
                "trigger",
                "Required for non-main agents.",
                "#trigger",
            )
        )

    known_mcp = set(discovered_mcp_names)
    for name in getattr(resolved, "mcp_exclude_names", []) or []:
        if name not in known_mcp:
            raise ValueError(
                _format_error(
                    source_file,
                    "mcp.exclude",
                    f"Unknown MCP server reference `{name}`.",
                    "#mcp",
                )
            )

    known_skills = set(discovered_skills)
    for name in getattr(resolved, "skills_exclude_names", []):
        if name not in known_skills:
            _logger.warning(
                "%s: field `skills.exclude`: Unknown skill reference `%s`. See docs/front-matter-spec.md#skills",
                source_file,
                name,
            )

    for name in getattr(resolved, "tool_exclude_names", []):
        _logger.warning(
            "%s: field `tools.exclude`: Could not verify tool reference `%s` during config validation. See docs/front-matter-spec.md#tools",
            source_file,
            name,
        )
