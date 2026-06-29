"""Skill discovery — locate MAF-compatible SKILL.md files under ``{app_root}/skills/``.

Each skill lives in its own directory and is declared by a ``SKILL.md`` file
with YAML frontmatter providing at least a ``name`` and a ``description``.
The runtime hands the resolved skill directories to
:class:`agent_framework.SkillsProvider`, which exposes per-skill load /
resource / script tooling to the agent.

Skills support markdown link includes: a standalone line containing only a
markdown link with a relative path (e.g., ``[api.md](./references/api.md)``)
will inline the referenced file's content at discovery time.
"""

from __future__ import annotations

import atexit
import re
import shutil
import tempfile
from pathlib import Path

import frontmatter

from .._logger import logger

# Mirrors :data:`agent_framework._skills.VALID_NAME_RE` and ``MAX_NAME_LENGTH``.
# We pre-validate here because :class:`SkillsProvider` does *not* raise on
# invalid names — it logs a warning and silently drops the skill, which gives
# users an agent that mysteriously lacks the skill with no startup error.
# By failing loud here we turn that into a clear configuration error.
# If MAF tightens or loosens these rules, update both constants below to
# match ``agent_framework._skills``.
_VALID_SKILL_NAME = re.compile(r"^[a-z0-9]([a-z0-9]*-[a-z0-9])*[a-z0-9]*$")
_MAX_SKILL_NAME_LENGTH = 64
_SKILL_FILE_NAME = "SKILL.md"
_DISCOVERED_SKILLS_CACHE: dict[Path, dict[str, Path]] = {}

# Include directive pattern: markdown links on their own line with relative paths.
# Matches [any text](./relative/path) where the line contains only the link.
# The path must start with ./ to distinguish includes from regular links.
# Captures the path after ./ (e.g., "references/api.md" from "./references/api.md").
_INCLUDE_PATTERN = re.compile(r"^\s*\[.*?\]\(\.\/(.*?)\)\s*$", re.MULTILINE)

# Global temp directory for resolved skills (cleaned up on process exit).
_RESOLVED_SKILLS_TEMP_DIR: Path | None = None


def _get_resolved_skills_temp_dir() -> Path:
    """Return (creating if needed) the temp directory for resolved skills."""
    global _RESOLVED_SKILLS_TEMP_DIR
    if _RESOLVED_SKILLS_TEMP_DIR is None:
        _RESOLVED_SKILLS_TEMP_DIR = Path(tempfile.mkdtemp(prefix="agents_skills_"))
        atexit.register(_cleanup_resolved_skills_temp_dir)
        logger.debug("Created temp directory for resolved skills: %s", _RESOLVED_SKILLS_TEMP_DIR)
    return _RESOLVED_SKILLS_TEMP_DIR


def _cleanup_resolved_skills_temp_dir() -> None:
    """Remove the temp directory for resolved skills (called via atexit)."""
    global _RESOLVED_SKILLS_TEMP_DIR
    if _RESOLVED_SKILLS_TEMP_DIR is not None and _RESOLVED_SKILLS_TEMP_DIR.exists():
        try:
            shutil.rmtree(_RESOLVED_SKILLS_TEMP_DIR)
            logger.debug("Cleaned up resolved skills temp directory: %s", _RESOLVED_SKILLS_TEMP_DIR)
        except OSError as exc:
            logger.warning("Failed to clean up temp directory %s: %s", _RESOLVED_SKILLS_TEMP_DIR, exc)
        _RESOLVED_SKILLS_TEMP_DIR = None


def _has_includes(content: str) -> bool:
    """Return True if the content contains any markdown link includes."""
    return _INCLUDE_PATTERN.search(content) is not None


def _resolve_includes(
    content: str,
    skill_dir: Path,
    *,
    visited: set[Path] | None = None,
) -> str:
    """Resolve all markdown link includes in the content.

    Markdown links on their own line with paths starting with ``./`` are treated
    as includes. For example, ``[api.md](./references/api.md)`` will inline the
    content of ``references/api.md``.

    Args:
        content: The skill file content (body, not frontmatter).
        skill_dir: The skill directory (where SKILL.md lives).
        visited: Paths already visited in this resolution chain (for cycle detection).

    Returns:
        The content with all includes resolved (file contents inlined).

    Raises:
        ValueError: If an include path is invalid, escapes the skill directory,
            points to a missing file, or creates a circular include.
    """
    if visited is None:
        visited = set()

    def replace_include(match: re.Match[str]) -> str:
        rel_path = match.group(1).strip()
        if not rel_path:
            raise ValueError("Empty include path in markdown link")

        # Reject absolute paths
        if rel_path.startswith("/") or (len(rel_path) > 1 and rel_path[1] == ":"):
            raise ValueError(f"Include path must be relative, got: {rel_path}")

        # Resolve the path relative to skill directory
        include_path = (skill_dir / rel_path).resolve()

        # Security: ensure the path is within the skill directory
        try:
            include_path.relative_to(skill_dir.resolve())
        except ValueError:
            raise ValueError(
                f"Include path {rel_path!r} escapes the skill directory {skill_dir}"
            ) from None

        # Check for circular includes
        if include_path in visited:
            raise ValueError(
                f"Circular include detected: {include_path} is already in the include chain"
            )

        # Read the included file
        if not include_path.is_file():
            raise ValueError(f"Include file not found: {include_path}")

        try:
            included_content = include_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Include file {include_path} is not valid UTF-8: {exc}") from None

        # Recursively resolve includes in the included content
        visited.add(include_path)
        try:
            return _resolve_includes(included_content, skill_dir, visited=visited)
        finally:
            visited.discard(include_path)

    return _INCLUDE_PATTERN.sub(replace_include, content)


def prepare_resolved_skills(skills: dict[str, Path]) -> dict[str, Path]:
    """Prepare skill directories with resolved include directives.

    For skills with ``{{include:...}}`` directives in their SKILL.md, this
    function copies the skill directory to a temp location and writes the
    resolved SKILL.md content. Skills without includes are returned as-is.

    Args:
        skills: Mapping of skill name to skill directory path.

    Returns:
        Mapping of skill name to (possibly temp) directory path with resolved content.

    Raises:
        ValueError: If include resolution fails for any skill.
    """
    if not skills:
        return {}

    resolved: dict[str, Path] = {}

    for name, skill_dir in skills.items():
        skill_file = skill_dir / _SKILL_FILE_NAME
        if not skill_file.is_file():
            # Shouldn't happen if discover_skills() was used, but be defensive
            resolved[name] = skill_dir
            continue

        content = skill_file.read_text(encoding="utf-8")
        if not _has_includes(content):
            # No includes — use original path
            resolved[name] = skill_dir
            continue

        # Parse frontmatter to separate it from body
        try:
            post = frontmatter.load(skill_file)
        except Exception as exc:
            raise ValueError(f"Failed to parse skill frontmatter {skill_file}: {exc}") from exc

        # Resolve includes in the body content
        resolved_body = _resolve_includes(post.content, skill_dir)

        # Create temp directory for this skill
        temp_dir = _get_resolved_skills_temp_dir()
        skill_temp_dir = temp_dir / name
        if skill_temp_dir.exists():
            shutil.rmtree(skill_temp_dir)

        # Copy the entire skill directory to temp
        shutil.copytree(skill_dir, skill_temp_dir)

        # Write resolved SKILL.md
        resolved_skill_file = skill_temp_dir / _SKILL_FILE_NAME
        resolved_post = frontmatter.Post(resolved_body, **post.metadata)
        resolved_skill_file.write_text(frontmatter.dumps(resolved_post), encoding="utf-8")

        logger.info("Resolved includes for skill %r → %s", name, skill_temp_dir)
        resolved[name] = skill_temp_dir

    return resolved


def clear_skills_cache() -> None:
    """Clear cached skill discovery results and clean up resolved skills temp directory."""
    _DISCOVERED_SKILLS_CACHE.clear()
    _cleanup_resolved_skills_temp_dir()


def _resolve_skills_dir(app_root: Path) -> Path | None:
    """Find ``{app_root}/skills`` (or ``Skills``) if it exists."""
    for name in ("skills", "Skills"):
        candidate = app_root / name
        if candidate.is_dir():
            return candidate
    return None


def discover_skills(app_root: Path) -> dict[str, Path]:
    """Return ``{skill_name: skill_directory}`` for every valid skill found.

    Walks ``{app_root}/skills/`` for ``SKILL.md`` files. Each file is parsed
    for YAML frontmatter; the ``name`` field becomes the dictionary key and
    the containing directory becomes the value. Invalid names and duplicate
    names raise :class:`ValueError` so misconfiguration fails loudly at app
    startup rather than silently at request time.
    """
    resolved_root = Path(app_root).resolve()
    cached = _DISCOVERED_SKILLS_CACHE.get(resolved_root)
    if cached is not None:
        return dict(cached)

    skills_dir = _resolve_skills_dir(resolved_root)
    if skills_dir is None:
        _DISCOVERED_SKILLS_CACHE[resolved_root] = {}
        return {}

    skill_files = sorted(
        (p for p in skills_dir.rglob(_SKILL_FILE_NAME) if p.is_file()),
        key=lambda p: str(p).lower(),
    )
    if not skill_files:
        logger.info("No %s files found in %s", _SKILL_FILE_NAME, skills_dir)
        _DISCOVERED_SKILLS_CACHE[resolved_root] = {}
        return {}

    discovered: dict[str, Path] = {}
    for skill_file in skill_files:
        try:
            post = frontmatter.load(skill_file)
        except Exception as exc:
            logger.warning("Failed to parse skill frontmatter %s: %s", skill_file, exc)
            continue

        name = str(post.metadata.get("name") or "").strip()
        if not name:
            raise ValueError(
                f"Skill at {skill_file} is missing a 'name' field in its frontmatter."
            )
        if not _VALID_SKILL_NAME.match(name) or len(name) > _MAX_SKILL_NAME_LENGTH:
            raise ValueError(
                f"Skill name {name!r} at {skill_file} is invalid. Names must match "
                f"{_VALID_SKILL_NAME.pattern} (lowercase letters, digits, and single "
                f"hyphens) and be at most {_MAX_SKILL_NAME_LENGTH} characters."
            )
        if name in discovered:
            raise ValueError(
                f"Duplicate skill name {name!r}: defined at both {discovered[name]} and "
                f"{skill_file.parent}."
            )
        discovered[name] = skill_file.parent

    logger.info("Discovered %d skill(s) under %s", len(discovered), skills_dir)
    _DISCOVERED_SKILLS_CACHE[resolved_root] = discovered
    return dict(discovered)
