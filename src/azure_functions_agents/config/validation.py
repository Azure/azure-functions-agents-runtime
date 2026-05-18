"""Validation helpers for configuration translation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from azure_functions_agents._logger import logger as _logger

LEGACY_FIELDS_AGENT = {
    "runtime": (
        "Removed in 1.0.0. Only the Microsoft Agent Framework is supported. Remove this field.",
        "",
    ),
    "execution_sandbox": (
        "Moved to the global agents.config.yaml under `system_tools.execute_in_sessions`.",
        "#system_tools",
    ),
    "tools_from_connections": (
        "Moved to the global agents.config.yaml under `system_tools.tools_from_connections`.",
        "#system_tools",
    ),
}

LEGACY_FIELDS_GLOBAL = {
    "execution_sandbox": (
        "Renamed to `system_tools.execute_in_sessions`.",
        "#system_tools",
    ),
    "tools_from_connections": (
        "Moved under `system_tools.tools_from_connections`.",
        "#system_tools",
    ),
}

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


def validate_agent_frontmatter(metadata: dict[str, Any], source_file: str | Path) -> None:
    """Raise ValueError with a clear message if metadata contains a legacy field."""
    for field, (message, spec_anchor) in LEGACY_FIELDS_AGENT.items():
        if field in metadata:
            raise ValueError(_format_error(source_file, field, message, spec_anchor))


def validate_global_config_dict(data: dict[str, Any], source_file: str | Path) -> None:
    """Raise ValueError if agents.config.yaml uses legacy field names."""
    for field, (message, spec_anchor) in LEGACY_FIELDS_GLOBAL.items():
        if field in data:
            raise ValueError(_format_error(source_file, field, message, spec_anchor))


def validate_global_mcp_references(
    global_mcp: list[str],
    discovered_mcp_names: list[str],
    *,
    source_file: str | Path | None = None,
) -> None:
    """Raise ValueError if global MCP names are not defined in mcp.json."""
    missing = sorted(set(global_mcp) - set(discovered_mcp_names))
    if not missing:
        return

    source = Path(source_file) if source_file is not None else "<unknown>"
    names = ", ".join(missing)
    raise ValueError(
        f"{source}: agents.config.yaml#mcp references undefined server(s): {names}. "
        "Define them in mcp.json or .vscode/mcp.json. See docs/front-matter-spec.md#mcp."
    )


def validate_resolved_agent(
    resolved: Any,
    *,
    all_global_mcp: list[str],
    discovered_skills: list[str],
) -> None:
    """Run post-merge sanity checks for a resolved agent."""
    source_file = resolved.source_file or "<unknown>"

    if not resolved.is_main and resolved.trigger is None:
        raise ValueError(
            _format_error(
                source_file,
                "trigger",
                "Required for non-main agents.",
                "#trigger",
            )
        )

    requested_mcp = set(all_global_mcp)
    requested_excludes = list(getattr(resolved, "mcp_exclude_names", []) or [])
    if not requested_excludes:
        requested_excludes = list(getattr(resolved, "enabled_mcp_names", []) or [])
    for name in requested_excludes:
        if name not in requested_mcp:
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
