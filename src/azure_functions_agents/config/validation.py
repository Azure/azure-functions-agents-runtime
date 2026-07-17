"""Validation helpers for configuration translation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from azure_functions_agents._logger import logger as _logger

from .schema import ResolvedAgent

_SPEC_LINK_DEFAULT = "docs/front-matter-spec.md"

_UNSUPPORTED_TRIGGER_TYPES: dict[str, str] = {
    "activity_trigger": "Durable Functions triggers are not supported as agent triggers.",
    "assistant_skill_trigger": "Assistant skill triggers are not supported as agent triggers; use agent tools or MCP surfaces instead.",
    "entity_trigger": "Durable Functions triggers are not supported as agent triggers.",
    "mcp_prompt_trigger": "MCP prompt triggers are registered by built-in endpoints, not agent trigger front matter.",
    "mcp_resource_trigger": "MCP resource triggers are registered by built-in endpoints, not agent trigger front matter.",
    "mcp_tool_trigger": "MCP tool triggers are registered by built-in endpoints, not agent trigger front matter.",
    "orchestration_trigger": "Durable Functions triggers are not supported as agent triggers.",
    "route": "Use `http_trigger` instead of the Azure Functions `route` decorator name.",
    "schedule": "Use `timer_trigger` instead of the Azure Functions `schedule` decorator alias.",
    "warm_up_trigger": "Warm-up triggers are host lifecycle hooks and are not supported as agent triggers.",
}


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


def validate_resolved_agent(
    resolved: Any,
    *,
    discovered_mcp_names: list[str],
    discovered_skills: list[str],
    is_referenced_as_subagent: bool = False,
) -> None:
    """Run post-merge sanity checks for a resolved agent.

    ``is_referenced_as_subagent`` relaxes the trigger/``builtin_endpoints``
    requirement below: an agent reachable only as another agent's
    delegation target (via that agent's ``subagents:``) doesn't need its
    own external entry point (Decision #18).
    """
    source_file = resolved.source_file or "<unknown>"

    builtin_endpoints = resolved.builtin_endpoints
    has_builtin_endpoints = bool(
        builtin_endpoints.debug_chat_ui or builtin_endpoints.chat_api or builtin_endpoints.mcp
    )
    if (
        resolved.trigger is None
        and not has_builtin_endpoints
        and not is_referenced_as_subagent
    ):
        raise ValueError(
            _format_error(
                source_file,
                "trigger",
                "Required when no builtin_endpoints are enabled.",
                "#trigger",
            )
        )

    if resolved.trigger is not None:
        trigger_type = str(resolved.trigger.type or "").strip()
        unsupported_message = _UNSUPPORTED_TRIGGER_TYPES.get(trigger_type)
        if unsupported_message:
            raise ValueError(
                _format_error(
                    source_file,
                    "trigger.type",
                    unsupported_message,
                    "#trigger",
                )
            )
        if "." in trigger_type:
            raise ValueError(
                _format_error(
                    source_file,
                    "trigger.type",
                    "Dotted connector trigger types are not supported. Use `connector_trigger` instead.",
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


def validate_subagent_references(
    resolved: ResolvedAgent,
    *,
    known_slugs: set[str],
) -> None:
    """Reject self, unknown, and duplicate ``subagents:`` references.

    Must run only after the app-wide identity-slug index (``known_slugs``)
    is built and de-duplicated (see ``app.py``'s two-pass composition
    root) — these are fail-fast configuration errors, never silently
    dropped.
    """
    source_file = resolved.source_file or "<unknown>"
    seen: set[str] = set()
    for ref in resolved.subagents:
        if ref.agent == resolved.slug:
            raise ValueError(
                _format_error(
                    source_file,
                    "subagents",
                    f"An agent cannot delegate to itself (`agent: {ref.agent}`).",
                    "#subagents",
                )
            )
        if ref.agent not in known_slugs:
            raise ValueError(
                _format_error(
                    source_file,
                    "subagents",
                    f"Unknown agent reference `{ref.agent}`. No agent with that "
                    "identity slug (file stem) was discovered in this app.",
                    "#subagents",
                )
            )
        if ref.agent in seen:
            raise ValueError(
                _format_error(
                    source_file,
                    "subagents",
                    f"Duplicate reference to agent `{ref.agent}` in `subagents`.",
                    "#subagents",
                )
            )
        seen.add(ref.agent)
