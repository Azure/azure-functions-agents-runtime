"""Load global and agent configuration files into typed schema models."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import frontmatter
import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from azure_functions_agents._logger import logger
from azure_functions_agents.config.env import (
    _to_bool,
    resolve_env_vars_in_data,
    substitute_env_vars_in_text,
)
from azure_functions_agents.config.schema import AgentSpec, GlobalConfig

_FRONTMATTER_SCHEMA_LINK = "aka.ms/agents-front-matter-schema"


_FRONTMATTER_ACTION_ITEMS = (
    "Fix YAML syntax between leading and trailing '---' delimiters.",
    f"Validate required fields like `name`, `description`, and `trigger` against {_FRONTMATTER_SCHEMA_LINK}.",
    "Re-run startup in strict mode to fail fast (load_agent_specs(..., strict=True)).",
)


def _is_bare_agent_md(filename: str) -> bool:
    """Check if filename is bare agent.md (case-insensitive)."""
    return filename.lower() == "agent.md"


def _is_claude_md(filename: str) -> bool:
    """Check if filename is CLAUDE.md (case-insensitive)."""
    return filename.lower() == "claude.md"


def _is_single_agent_file(filename: str) -> bool:
    """Check if filename is a recognized single-agent file (agent.md or CLAUDE.md)."""
    return _is_bare_agent_md(filename) or _is_claude_md(filename)


def _find_agent_files_with_suffix(directory: Path) -> list[Path]:
    """Find all *.agent.md and *.claude.md files (case-insensitive) in directory.
    
    Excludes bare agent.md and CLAUDE.md variants as those are handled separately.
    """
    agent_files: list[Path] = []
    for md_file in directory.iterdir():
        if not md_file.is_file():
            continue
        lower_name = md_file.name.lower()
        if not lower_name.endswith(".md"):
            continue
        # Check if it ends with .agent.md or .claude.md (case-insensitive)
        # and exclude bare single-agent files (handled separately)
        if lower_name.endswith((".agent.md", ".claude.md")) and not _is_single_agent_file(md_file.name):
            agent_files.append(md_file)
    return agent_files


def _normalize_agent_filename(source_file: Path) -> Path:
    """Normalize single-agent and claude-prefixed files for internal processing.
    
    - Bare agent.md and CLAUDE.md (case-insensitive) become default.agent.md
      internally, generating function name 'default'.
    - Files matching *.claude.md (e.g., report.claude.md) become *.agent.md
      (e.g., report.agent.md), preserving the prefix for function naming.
    """
    filename = source_file.name
    lower_filename = filename.lower()
    
    # Handle single-agent files (bare agent.md or CLAUDE.md)
    if _is_single_agent_file(filename):
        return source_file.with_name("default.agent.md")
    
    # Handle *.claude.md pattern (e.g., report.claude.md → report.agent.md)
    if lower_filename.endswith(".claude.md"):
        # Preserve the prefix, just change .claude.md to .agent.md
        prefix = filename[:-len(".claude.md")]
        return source_file.with_name(f"{prefix}.agent.md")

    # Normalize mixed-case *.agent.md suffix (e.g., report.AGENT.md → report.agent.md)
    if lower_filename.endswith(".agent.md") and not filename.endswith(".agent.md"):
        prefix = filename[:-len(".agent.md")]
        return source_file.with_name(f"{prefix}.agent.md")

    return source_file


def _format_action_items(items: tuple[str, ...]) -> str:
    return " | ".join(f"{index + 1}) {item}" for index, item in enumerate(items))


def _log_frontmatter_indexing_error(source_file: Path, exc: Exception) -> None:
    logger.error(
        "frontmatter_indexing_error: file=%s reason=%s action_items=%s",
        source_file,
        exc,
        _format_action_items(_FRONTMATTER_ACTION_ITEMS),
    )


def _normalize_global_config_dict(data: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(data)
    return cast(dict[str, Any], resolve_env_vars_in_data(normalized))


def _normalize_agent_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(metadata)
    return cast(dict[str, Any], resolve_env_vars_in_data(normalized))


def _format_validation_error(source_file: Path, exc: ValidationError) -> ValueError:
    error = exc.errors()[0]
    location = (
        ".".join(str(part) for part in error.get("loc", ()) if part != "__root__") or "<root>"
    )
    message = error.get("msg", str(exc))
    return ValueError(
        f"{source_file}: field `{location}`: {message}. See {_FRONTMATTER_SCHEMA_LINK}"
    )


def _first_validation_issue(exc: ValidationError) -> tuple[str, str]:
    error = exc.errors()[0]
    location = ".".join(str(part) for part in error.get("loc", ()) if part != "__root__") or "<root>"
    reason = str(error.get("msg", str(exc))).strip()
    return location, reason


def _load_agent_spec(source_file: Path) -> AgentSpec:
    try:
        post = frontmatter.load(str(source_file))
    except yaml.YAMLError as exc:
        _log_frontmatter_indexing_error(source_file, exc)
        raise ValueError(f"{source_file}: invalid YAML frontmatter: {exc}") from exc
    except Exception as exc:
        _log_frontmatter_indexing_error(source_file, exc)
        raise ValueError(f"{source_file}: failed to parse frontmatter: {exc}") from exc

    metadata = dict(post.metadata or {})
    substitute_variables = _to_bool(metadata.pop("substitute_variables", True), default=True)

    normalized = dict(metadata)
    if substitute_variables:
        normalized = _normalize_agent_metadata(normalized)
        instructions = substitute_env_vars_in_text(post.content)
    else:
        instructions = post.content

    # Normalize bare agent.md → default.agent.md for internal processing
    normalized_file = _normalize_agent_filename(source_file.resolve())

    normalized["substitute_variables"] = substitute_variables
    normalized["instructions"] = instructions
    # Keep the real on-disk path so diagnostics reference the file the user can actually edit
    normalized["source_file"] = str(source_file.resolve())
    # agent.md and CLAUDE.md (and their case variants) map to default.agent.md internally;
    # check the normalized name to determine main-agent status
    normalized["is_main"] = normalized_file.name.lower() in ("main.agent.md", "default.agent.md")

    try:
        return AgentSpec.model_validate(normalized)
    except ValidationError as exc:
        field, reason = _first_validation_issue(exc)
        logger.error(
            "frontmatter_validation_error: file=%s field=%s reason=%s schema=%s",
            source_file,
            field,
            reason,
            _FRONTMATTER_SCHEMA_LINK,
        )
        raise _format_validation_error(source_file, exc) from exc


def load_global_config(app_root: Path) -> GlobalConfig:
    """Read agents.config.yaml from app_root. Returns empty GlobalConfig() if missing."""
    source_file = Path(app_root).resolve() / "agents.config.yaml"
    if not source_file.exists():
        return GlobalConfig()

    try:
        with source_file.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        logger.error(
            "global_config_yaml_error: file=%s reason=%s action_items=%s",
            source_file,
            exc,
            f"1) Fix YAML syntax in agents.config.yaml. | 2) Validate fields against {_FRONTMATTER_SCHEMA_LINK}.",
        )
        raise ValueError(f"{source_file}: invalid YAML in agents.config.yaml: {exc}") from exc

    if data is None:
        return GlobalConfig()
    if not isinstance(data, dict):
        logger.error(
            "global_config_validation_error: file=%s reason=expected YAML mapping action_items=%s",
            source_file,
            "1) Ensure the YAML root is a mapping/object. | 2) Move list/scalar values under supported fields.",
        )
        raise ValueError(
            f"{source_file}: field `<root>`: expected a YAML mapping. See {_FRONTMATTER_SCHEMA_LINK}"
        )

    normalized = _normalize_global_config_dict(data)
    try:
        return GlobalConfig.model_validate(normalized)
    except ValidationError as exc:
        logger.error(
            "global_config_validation_error: file=%s reason=%s action_items=%s",
            source_file,
            exc,
            f"1) Fix invalid field types/values in agents.config.yaml. | 2) Compare fields to {_FRONTMATTER_SCHEMA_LINK}.",
        )
        raise _format_validation_error(source_file, exc) from exc


def _resolve_agents_dir(app_root: Path) -> Path | None:
    """Find ``{app_root}/agents`` (or ``Agents``) if it exists."""
    for name in ("agents", "Agents"):
        candidate = app_root / name
        if candidate.is_dir():
            return candidate
    return None


def load_agent_specs(app_root: Path, strict: bool = False) -> list[AgentSpec]:
    """Read every *.agent.md and *.claude.md in app_root and agents/ folder, return AgentSpec values.

    Searches for agent markdown files in these locations:
    1. Top-level: ``{app_root}/*.agent.md`` and ``{app_root}/*.claude.md`` (case-insensitive)
    2. Top-level single-agent: ``{app_root}/agent.md`` or ``{app_root}/CLAUDE.md`` (case-insensitive)
    3. Agents folder: ``{app_root}/agents/*.agent.md`` and ``{app_root}/agents/*.claude.md`` (case-insensitive)
    4. Agents folder single-agent: ``{app_root}/agents/agent.md`` or ``{app_root}/agents/CLAUDE.md``

    Single-agent files (agent.md and CLAUDE.md, case-insensitive) are internally
    normalized to default.agent.md for function name generation, producing 'default'
    as the function name.
    
    Files matching *.claude.md pattern are normalized to *.agent.md, preserving the
    prefix for function naming (e.g., report.claude.md → report.agent.md → 'report').
    
    Suffix matching (.agent.md and .claude.md) is case-insensitive, supporting files
    like report.AGENT.md or summary.CLAUDE.md.

    Files from all locations are combined and sorted by path for deterministic
    ordering. This allows customers to organize agents in a dedicated folder
    while maintaining backward compatibility with top-level agents.
    """
    root = Path(app_root).resolve()
    specs: list[AgentSpec] = []

    # Collect agent files from both top-level and agents/ folder (case-insensitive)
    agent_files: list[Path] = _find_agent_files_with_suffix(root)
    
    # Also check for single-agent files: agent.md and CLAUDE.md (case-insensitive)
    for candidate in sorted(root.iterdir()):
        if candidate.is_file() and _is_bare_agent_md(candidate.name):
            agent_files.append(candidate)
            break  # Only add one agent.md variant

    for candidate in sorted(root.iterdir()):
        if candidate.is_file() and _is_claude_md(candidate.name):
            agent_files.append(candidate)
            break  # Only add one CLAUDE.md variant
    
    agents_dir = _resolve_agents_dir(root)
    if agents_dir is not None:
        # Find prefixed agent files with case-insensitive suffix matching
        agent_files.extend(_find_agent_files_with_suffix(agents_dir))
        # Also check for single-agent files in agents folder (case-insensitive)
        for candidate in sorted(agents_dir.iterdir()):
            if candidate.is_file() and _is_bare_agent_md(candidate.name):
                agent_files.append(candidate)
                break  # Only add one agent.md variant

        for candidate in sorted(agents_dir.iterdir()):
            if candidate.is_file() and _is_claude_md(candidate.name):
                agent_files.append(candidate)
                break  # Only add one CLAUDE.md variant

    for source_file in sorted(agent_files):
        try:
            specs.append(_load_agent_spec(source_file))
        except Exception as exc:
            if strict:
                raise
            logger.warning(
                "Skipping agent during indexing: file=%s reason=%s",
                source_file,
                exc,
            )
            continue

    return specs
