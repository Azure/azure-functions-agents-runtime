"""Skill discovery — locate MAF-compatible SKILL.md files under ``{app_root}/skills/``.

Each skill lives in its own directory and is declared by a ``SKILL.md`` file
with YAML frontmatter providing at least a ``name`` and a ``description``.
The runtime hands the resolved skill directories to
:class:`agent_framework.SkillsProvider`, which exposes per-skill load /
resource / script tooling to the agent.

Skills can include reference material in ``references/`` and ``assets/``
subdirectories. MAF's ``read_skill_resource`` tool allows the agent to
read these files on demand at runtime (progressive disclosure).
"""

from __future__ import annotations

import re
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
_DISCOVERED_SKILLS_CACHE: dict[Path, dict[str, Path]] = {}


@dataclass
class SkillDiscoveryResult:
    """Result of skill discovery including successes and failures."""

    skills: dict[str, Path]  # {skill_name: skill_directory}
    failed_loads: list[tuple[str, str]]  # [(skill_file, error_message), ...]


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
        return SkillDiscoveryResult(skills=dict(cached), failed_loads=[])

    skills_dir = _resolve_skills_dir(resolved_root)
    if skills_dir is None:
        _DISCOVERED_SKILLS_CACHE[resolved_root] = {}
        return SkillDiscoveryResult(skills={}, failed_loads=[])

    skill_files = sorted(
        (p for p in skills_dir.rglob(_SKILL_FILE_NAME) if p.is_file()),
        key=lambda p: str(p).lower(),
    )
    if not skill_files:
        logger.info("No %s files found in %s", _SKILL_FILE_NAME, skills_dir)
        _DISCOVERED_SKILLS_CACHE[resolved_root] = {}
        return SkillDiscoveryResult(skills={}, failed_loads=[])

    discovered: dict[str, Path] = {}
    failed_loads: list[tuple[str, str]] = []
    for skill_file in skill_files:
        try:
            post = frontmatter.load(skill_file)
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            failed_loads.append((str(skill_file), error_msg))
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
    if failed_loads:
        logger.warning("Failed to load %d skill file(s)", len(failed_loads))
    _DISCOVERED_SKILLS_CACHE[resolved_root] = discovered
    return SkillDiscoveryResult(skills=dict(discovered), failed_loads=failed_loads)
