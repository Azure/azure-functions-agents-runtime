"""Discover skill directory candidates without parsing SDK-owned content."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .._logger import logger

_SKILL_FILE_NAME = "SKILL.md"
_SKILL_ROOT_SEARCH_DEPTH = 2


@dataclass(frozen=True)
class SkillDescriptor:
    """Immutable directory identity with a canonical individual skill root."""

    name: str
    path: Path

    @classmethod
    def create(cls, *, name: str, path: Path) -> SkillDescriptor:
        """Freeze supplied identity without interpreting SDK-owned metadata."""
        return cls(name=name, path=path.resolve())


_DISCOVERED_SKILLS_CACHE: dict[Path, tuple[SkillDescriptor, ...]] = {}


@dataclass
class SkillDiscoveryResult:
    """Directory inventory with legacy name-map and failed-load result fields."""

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
    """Identify canonical directory candidates without reading their contents."""
    discovered: dict[str, Path] = {}
    descriptors: list[SkillDescriptor] = []
    identities: set[tuple[str, Path]] = set()
    for skill_file in skill_files:
        path = skill_file.parent.resolve()
        identity = (path.name, path)
        if identity in identities:
            continue
        identities.add(identity)
        descriptor = SkillDescriptor(name=path.name, path=path)
        discovered.setdefault(descriptor.name, descriptor.path)
        descriptors.append(descriptor)

    return SkillDiscoveryResult(skills=discovered, failed_loads=[], descriptors=tuple(descriptors))


def describe_skill_paths(paths: Sequence[Path]) -> tuple[SkillDescriptor, ...]:
    """Identify explicitly supplied individual roots without reading SKILL.md."""
    return _describe_skill_files(
        [path / _SKILL_FILE_NAME for path in paths if (path / _SKILL_FILE_NAME).is_file()]
    ).descriptors


def describe_skill_catalog(paths: Sequence[Path]) -> tuple[SkillDescriptor, ...]:
    """Identify MAF-compatible roots beneath explicitly supplied search paths."""
    result = _describe_skill_files(_find_skill_files(paths))
    return result.descriptors


def _find_skill_files(paths: Sequence[Path]) -> list[Path]:
    """Find roots through two child levels, without descending through a skill."""
    files: list[Path] = []

    def visit(directory: Path, depth: int) -> None:
        skill_file = directory / _SKILL_FILE_NAME
        if skill_file.is_file():
            files.append(skill_file)
            return
        if depth == _SKILL_ROOT_SEARCH_DEPTH:
            return
        try:
            children = list(directory.iterdir())
        except OSError as exc:
            logger.warning("Failed to scan skill directory %s: %s", directory, exc)
            return
        for child in children:
            if child.is_dir():
                visit(child, depth + 1)

    for path in paths:
        if path.is_dir():
            visit(path, 0)
    return files


def discover_skills(app_root: Path) -> SkillDiscoveryResult:
    """Return candidates without performing SDK-owned skill content validation.

    Search ``skills/`` through two child levels, stopping at each skill root.
    Directory names identify candidates; the selected SDK validates their contents.
    """
    resolved_root = Path(app_root).resolve()
    cached = _DISCOVERED_SKILLS_CACHE.get(resolved_root)
    if cached is not None:
        cached_skills: dict[str, Path] = {}
        for descriptor in cached:
            cached_skills.setdefault(descriptor.name, descriptor.path)
        return SkillDiscoveryResult(
            skills=cached_skills,
            failed_loads=[],
            descriptors=cached,
        )

    skills_dir = _resolve_skills_dir(resolved_root)
    if skills_dir is None:
        _DISCOVERED_SKILLS_CACHE[resolved_root] = ()
        return SkillDiscoveryResult(skills={}, failed_loads=[])

    skill_files = _find_skill_files((skills_dir,))
    if not skill_files:
        logger.info("No %s files found in %s", _SKILL_FILE_NAME, skills_dir)
        _DISCOVERED_SKILLS_CACHE[resolved_root] = ()
        return SkillDiscoveryResult(skills={}, failed_loads=[])

    result = _describe_skill_files(skill_files)
    logger.info("Discovered %d skill candidate(s) under %s", len(result.descriptors), skills_dir)
    _DISCOVERED_SKILLS_CACHE[resolved_root] = result.descriptors
    return result
