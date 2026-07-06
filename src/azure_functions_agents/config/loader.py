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
        f"{source_file}: field `{location}`: {message}. See docs/front-matter-spec.md"
    )


def _load_agent_spec(source_file: Path) -> AgentSpec:
    try:
        post = frontmatter.load(str(source_file))
    except yaml.YAMLError as exc:
        raise ValueError(f"{source_file}: invalid YAML frontmatter: {exc}") from exc

    metadata = dict(post.metadata or {})
    substitute_variables = _to_bool(metadata.pop("substitute_variables", True), default=True)

    normalized = dict(metadata)
    if substitute_variables:
        normalized = _normalize_agent_metadata(normalized)
        instructions = substitute_env_vars_in_text(post.content)
    else:
        instructions = post.content

    normalized["substitute_variables"] = substitute_variables
    normalized["instructions"] = instructions
    normalized["source_file"] = str(source_file.resolve())
    normalized["is_main"] = source_file.name == "main.agent.md"

    try:
        return AgentSpec.model_validate(normalized)
    except ValidationError as exc:
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
        raise ValueError(f"{source_file}: invalid YAML in agents.config.yaml: {exc}") from exc

    if data is None:
        return GlobalConfig()
    if not isinstance(data, dict):
        raise ValueError(
            f"{source_file}: field `<root>`: expected a YAML mapping. See docs/front-matter-spec.md"
        )

    normalized = _normalize_global_config_dict(data)
    try:
        return GlobalConfig.model_validate(normalized)
    except ValidationError as exc:
        raise _format_validation_error(source_file, exc) from exc


def _resolve_agents_dir(app_root: Path) -> Path | None:
    """Find ``{app_root}/agents`` (or ``Agents``) if it exists."""
    for name in ("agents", "Agents"):
        candidate = app_root / name
        if candidate.is_dir():
            return candidate
    return None


def load_agent_specs(app_root: Path, strict: bool = False) -> list[AgentSpec]:
    """Read every *.agent.md in app_root and agents/ folder, return AgentSpec values.

    Searches for agent markdown files in two locations:
    1. Top-level: ``{app_root}/*.agent.md``
    2. Agents folder: ``{app_root}/agents/*.agent.md`` (case-insensitive)

    Files from both locations are combined and sorted by path for deterministic
    ordering. This allows customers to organize agents in a dedicated folder
    while maintaining backward compatibility with top-level agents.
    """
    root = Path(app_root).resolve()
    specs: list[AgentSpec] = []

    # Collect agent files from both top-level and agents/ folder
    agent_files: list[Path] = list(root.glob("*.agent.md"))
    agents_dir = _resolve_agents_dir(root)
    if agents_dir is not None:
        agent_files.extend(agents_dir.glob("*.agent.md"))

    for source_file in sorted(agent_files):
        try:
            specs.append(_load_agent_spec(source_file))
        except Exception as exc:
            if strict:
                raise
            logger.warning("Skipping malformed agent file %s: %s", source_file, exc)
            continue

    return specs
