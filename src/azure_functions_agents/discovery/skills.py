"""Discover skill metadata without loading resources or constructing SDK providers."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
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


@dataclass(frozen=True)
class SkillDescriptor:
    """Immutable skill metadata with a canonical individual skill root."""

    name: str
    description: str
    path: Path

    @classmethod
    def create(cls, *, name: str, description: str, path: Path) -> SkillDescriptor:
        """Normalize metadata and preserve the existing skill-name validation."""
        name = name.strip()
        path = path.resolve()
        skill_file = path / _SKILL_FILE_NAME
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
        return cls(name=name, description=description.strip(), path=path)


_DISCOVERED_SKILLS_CACHE: dict[Path, tuple[SkillDescriptor, ...]] = {}


@dataclass
class SkillDiscoveryResult:
    """Result of skill discovery including successes and failures."""

    skills: dict[str, Path]  # {skill_name: skill_directory}
    failed_loads: list[tuple[str, str]]  # [(skill_file, error_message), ...]
    descriptors: tuple[SkillDescriptor, ...] = ()


def clear_skills_cache() -> None:
    """Clear cached skill discovery results."""
    _DISCOVERED_SKILLS_CACHE.clear()


def _resolve_skills_dir(app_root: Path) -> Path | None:
    """Find ``{app_root}/skills`` (or ``Skills``) if it exists."""
    for name in ("skills", "Skills"):
        candidate = app_root / name
        if candidate.is_dir():
            return candidate
    return None


def _describe_skill_files(skill_files: Sequence[Path]) -> SkillDiscoveryResult:
    """Read only frontmatter, preserving parse skips and duplicate-name errors."""
    discovered: dict[str, Path] = {}
    descriptors: list[SkillDescriptor] = []
    failed_loads: list[tuple[str, str]] = []
    for skill_file in skill_files:
        try:
            post = frontmatter.load(skill_file)
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            failed_loads.append((str(skill_file), error_msg))
            logger.warning("Failed to parse skill frontmatter %s: %s", skill_file, exc)
            continue

        descriptor = SkillDescriptor.create(
            name=str(post.metadata.get("name") or ""),
            description=str(post.metadata.get("description") or ""),
            path=skill_file.parent,
        )
        if descriptor.name in discovered:
            raise ValueError(
                f"Duplicate skill name {descriptor.name!r}: defined at both "
                f"{discovered[descriptor.name]} and {skill_file.parent}."
            )
        discovered[descriptor.name] = descriptor.path
        descriptors.append(descriptor)

    return SkillDiscoveryResult(
        skills=discovered, failed_loads=failed_loads, descriptors=tuple(descriptors)
    )


def describe_skill_paths(paths: Sequence[Path]) -> tuple[SkillDescriptor, ...]:
    """Describe explicitly supplied individual roots without recursive loading."""
    result = _describe_skill_files([path / _SKILL_FILE_NAME for path in paths])
    if result.failed_loads:
        logger.warning("Failed to load %d skill file(s)", len(result.failed_loads))
    return result.descriptors


def describe_skill_catalog(paths: Sequence[Path]) -> tuple[SkillDescriptor, ...]:
    """Index ownership metadata under explicit roots without approving descendants."""
    files_by_root: dict[tuple[Path, Path], Path] = {}
    for path in paths:
        for skill_file in path.resolve().rglob(_SKILL_FILE_NAME):
            if skill_file.is_file():
                key = (skill_file.parent.resolve(), skill_file.resolve())
                files_by_root.setdefault(key, skill_file)
    skill_files = sorted(files_by_root.values(), key=lambda path: str(path).lower())
    result = _describe_skill_files(skill_files)
    if result.failed_loads:
        logger.warning("Failed to load %d skill file(s)", len(result.failed_loads))
    return result.descriptors


def discover_skills(app_root: Path) -> SkillDiscoveryResult:
    """Return discovered skills and any failed skill loads.

    Walks ``{app_root}/skills/`` for ``SKILL.md`` files. Each file is parsed
    for YAML frontmatter; the ``name`` field becomes the dictionary key and
    the containing directory becomes the value. Invalid names and duplicate
    names raise :class:`ValueError` so misconfiguration fails loudly at app
    startup rather than silently at request time.
    """
    resolved_root = Path(app_root).resolve()
    cached = _DISCOVERED_SKILLS_CACHE.get(resolved_root)
    if cached is not None:
        return SkillDiscoveryResult(
            skills={descriptor.name: descriptor.path for descriptor in cached},
            failed_loads=[],
            descriptors=cached,
        )

    skills_dir = _resolve_skills_dir(resolved_root)
    if skills_dir is None:
        _DISCOVERED_SKILLS_CACHE[resolved_root] = ()
        return SkillDiscoveryResult(skills={}, failed_loads=[])

    skill_files = sorted(
        (p for p in skills_dir.rglob(_SKILL_FILE_NAME) if p.is_file()),
        key=lambda p: str(p).lower(),
    )
    if not skill_files:
        logger.info("No %s files found in %s", _SKILL_FILE_NAME, skills_dir)
        _DISCOVERED_SKILLS_CACHE[resolved_root] = ()
        return SkillDiscoveryResult(skills={}, failed_loads=[])

    result = _describe_skill_files(skill_files)
    logger.info("Discovered %d skill(s) under %s", len(result.skills), skills_dir)
    if result.failed_loads:
        logger.warning("Failed to load %d skill file(s)", len(result.failed_loads))
    _DISCOVERED_SKILLS_CACHE[resolved_root] = result.descriptors
    return result
