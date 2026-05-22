"""Validation helpers for configuration translation."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from azure_functions_agents._logger import logger as _logger

if TYPE_CHECKING:
    from azure_functions_agents.config.schema import AgentConfiguration, ResolvedAgent

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


def validate_agent_configuration(
    agent_configuration: AgentConfiguration,
    *,
    source_file: str | Path,
    agent_name: str | None = None,
) -> None:
    """Run post-merge required-field completeness checks for the active provider.

    Structural invariants (unknown provider, multiple sub-blocks, mismatched
    sub-block, model presence) are enforced by ``AgentConfiguration``'s pydantic
    validators on every ``model_validate`` call. This helper only enforces the
    additional per-provider field requirements that are too provider-specific
    to express on the shared base model (``azure_endpoint``/``api_version`` for
    azure_openai; ``project_endpoint`` for foundry).
    """
    del agent_name  # reserved for future error enrichment

    provider = agent_configuration.provider
    provider_config = getattr(agent_configuration, provider, None)

    required_fields: tuple[str, ...] = ()
    if provider == "azure_openai":
        required_fields = ("azure_endpoint", "api_version")
    elif provider == "foundry":
        required_fields = ("project_endpoint",)

    for field_name in required_fields:
        value = (
            getattr(provider_config, field_name, None)
            if provider_config is not None
            else None
        )
        if value:
            continue
        raise ValueError(
            _format_error(
                source_file,
                f"agent_configuration.{provider}.{field_name}",
                f"agent_configuration.{provider}.{field_name} must be set",
                "#agent_configuration",
            )
        )


def validate_resolved_agent(
    resolved: ResolvedAgent,
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
