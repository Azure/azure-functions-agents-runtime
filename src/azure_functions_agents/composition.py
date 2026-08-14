"""Binding-only project composition for smart agent injection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import frontmatter
import yaml  # type: ignore[import-untyped]
from agent_framework import FunctionTool

from ._function_tool import WorkflowTool
from ._slug import _function_name_from_source
from .config.env import _to_bool, substitute_env_vars_in_text
from .config.loader import (
    _collect_agent_files,
    _resolve_agents_dir,
    load_global_config,
)
from .config.paths import get_app_root
from .config.schema import GlobalConfig
from .discovery.mcp import MCPServerDefinition, discover_mcp_server_definitions
from .discovery.skills import discover_skills
from .discovery.tools import discover_project_tools


@dataclass(frozen=True)
class BindingAgentDefinition:
    """Minimal agent authoring surface recognized by ``agent_input``."""

    name: str
    description: str
    instructions: str
    source_file: Path
    filename_stem: str
    slug: str


@dataclass(frozen=True)
class BindingAgentSource:
    """Definition identity available without parsing its front matter."""

    source_file: Path
    filename_stem: str
    slug: str


@dataclass(frozen=True)
class DiscoveryInventory:
    """Immutable projection of the existing root-keyed discovery results."""

    user_tools: tuple[FunctionTool, ...]
    workflow_tools: tuple[WorkflowTool, ...]
    skills: tuple[tuple[str, Path], ...]
    mcp_servers: tuple[tuple[str, MCPServerDefinition], ...]
    failed_loads: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ProjectSnapshot:
    """Binding composition state owned by one FunctionApp instance."""

    app_root: Path
    config: GlobalConfig
    sources: tuple[BindingAgentSource, ...]
    discovery: DiscoveryInventory


@dataclass(frozen=True)
class BindingAgentEntry:
    """A resolved binding target and the app-level assets used to hydrate it."""

    definition: BindingAgentDefinition
    config: GlobalConfig
    discovery: DiscoveryInventory


def _filename_stem(source_file: Path) -> str:
    name = source_file.name
    lower_name = name.lower()
    for suffix in (".agent.md", ".claude.md"):
        if lower_name.endswith(suffix):
            return name[: -len(suffix)]
    return source_file.stem


def _binding_agent_files(app_root: Path) -> list[Path]:
    files = _collect_agent_files(app_root)
    agents_dir = _resolve_agents_dir(app_root)
    if agents_dir is not None:
        files.extend(_collect_agent_files(agents_dir))
    return sorted(files)


def _required_string(metadata: dict[str, object], field: str, source_file: Path) -> str:
    value = metadata.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{source_file}: field `{field}`: expected a non-empty string")
    return value.strip()


def load_binding_definition(source_file: Path) -> BindingAgentDefinition:
    """Load only name, description, and markdown instructions from an agent file."""
    resolved_source = source_file.resolve()
    try:
        post = frontmatter.load(str(resolved_source))
    except yaml.YAMLError as exc:
        raise ValueError(f"{resolved_source}: invalid YAML frontmatter: {exc}") from exc
    except Exception as exc:
        raise ValueError(f"{resolved_source}: failed to parse frontmatter: {exc}") from exc

    metadata: dict[str, object] = dict(post.metadata or {})
    name = _required_string(metadata, "name", resolved_source)
    description = _required_string(metadata, "description", resolved_source)
    substitute_variables = _to_bool(metadata.get("substitute_variables", True), default=True)
    instructions = str(post.content)
    if substitute_variables:
        instructions = substitute_env_vars_in_text(instructions)
    filename_stem = _filename_stem(resolved_source)
    slug = _function_name_from_source(resolved_source, name, warn_on_missing=False)
    return BindingAgentDefinition(
        name=name,
        description=description,
        instructions=instructions,
        source_file=resolved_source,
        filename_stem=filename_stem,
        slug=slug,
    )


def _build_discovery_inventory(app_root: Path) -> DiscoveryInventory:
    tools = discover_project_tools(app_root)
    skills = discover_skills(app_root)
    mcp = discover_mcp_server_definitions(app_root)
    failed_loads = sorted([*tools.failed_loads, *skills.failed_loads, *mcp.failed_loads])
    return DiscoveryInventory(
        user_tools=tuple(tools.user_tools),
        workflow_tools=tuple(tools.workflow_tools),
        skills=tuple(sorted(skills.skills.items())),
        mcp_servers=tuple(sorted(mcp.definitions.items())),
        failed_loads=tuple(failed_loads),
    )


def _binding_source(source_file: Path) -> BindingAgentSource:
    resolved_source = source_file.resolve()
    filename_stem = _filename_stem(resolved_source)
    return BindingAgentSource(
        source_file=resolved_source,
        filename_stem=filename_stem,
        slug=_function_name_from_source(
            resolved_source,
            filename_stem,
            warn_on_missing=False,
        ),
    )


def _fail_on_duplicate_binding_slugs(sources: tuple[BindingAgentSource, ...]) -> None:
    sources_by_slug: dict[str, list[Path]] = {}
    for source in sources:
        sources_by_slug.setdefault(source.slug, []).append(source.source_file)
    for slug, colliding_paths in sorted(sources_by_slug.items()):
        if len(colliding_paths) > 1:
            listed = ", ".join(str(source) for source in sorted(colliding_paths))
            raise ValueError(
                f"Duplicate agent slug {slug!r} is used by {len(colliding_paths)} source files: "
                f"{listed}. Rename one of the colliding source files."
            )


def load_project_snapshot(app_root: Path | None = None) -> ProjectSnapshot:
    """Build one binding-only snapshot without invoking declarative validation."""
    resolved_root = Path(app_root).resolve() if app_root is not None else get_app_root()
    sources = tuple(_binding_source(source_file) for source_file in _binding_agent_files(resolved_root))
    _fail_on_duplicate_binding_slugs(sources)
    return ProjectSnapshot(
        app_root=resolved_root,
        config=load_global_config(resolved_root),
        sources=sources,
        discovery=_build_discovery_inventory(resolved_root),
    )


def compose_binding_target(
    snapshot: ProjectSnapshot,
    agent_name: str,
) -> BindingAgentEntry:
    """Resolve a filename stem first, then its normalized identity slug."""
    requested = agent_name.strip()
    if not requested:
        raise ValueError("agent_name must be a non-empty filename stem or normalized slug")

    exact = [source for source in snapshot.sources if source.filename_stem == requested]
    if len(exact) == 1:
        definition = load_binding_definition(exact[0].source_file)
        return BindingAgentEntry(definition, snapshot.config, snapshot.discovery)

    normalized = _function_name_from_source(
        f"{requested}.agent.md",
        requested,
        warn_on_missing=False,
    )
    by_slug = [source for source in snapshot.sources if source.slug == normalized]
    if len(by_slug) == 1:
        definition = load_binding_definition(by_slug[0].source_file)
        return BindingAgentEntry(definition, snapshot.config, snapshot.discovery)

    available = ", ".join(
        f"{source.filename_stem} ({source.slug})" for source in snapshot.sources
    )
    raise ValueError(
        f"Agent definition {agent_name!r} was not found under {snapshot.app_root}. "
        "agent_name must be a filename stem or normalized slug, not the front-matter "
        f"display name. Available definitions: {available or '<none>'}"
    )