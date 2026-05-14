"""Skill discovery — read project markdown files into a single instructions block.

The runtime supports an optional ``skills/`` directory under the app root. Any
markdown files inside (recursively) are loaded when requested, sorted by
path, and concatenated.
"""

from __future__ import annotations

from pathlib import Path

from .._logger import logger


def _resolve_skills_dir(app_root: Path) -> Path | None:
    """Find ``{app_root}/skills`` (or ``Skills``) if it exists."""
    for name in ("skills", "Skills"):
        candidate = app_root / name
        if candidate.is_dir():
            return candidate
    return None


def _collect_skill_files(skills_dir: Path) -> list[Path]:
    """Return a deterministic, sorted list of every ``*.md`` file under ``skills_dir``."""
    files = [p for p in skills_dir.rglob("*.md") if p.is_file()]
    files.sort(key=lambda path: str(path).lower())
    return files


def discover_skill_names(app_root: Path) -> list[str]:
    skills_dir = _resolve_skills_dir(app_root)
    if skills_dir is None:
        return []
    return [path.relative_to(skills_dir).as_posix() for path in _collect_skill_files(skills_dir)]


def discover_skill_texts(app_root: Path) -> dict[str, str]:
    """Return per-skill markdown text keyed by relative path."""
    skills_dir = _resolve_skills_dir(app_root)
    if skills_dir is None:
        return {}

    files = _collect_skill_files(skills_dir)
    if not files:
        logger.info("No skill markdown files found in %s", skills_dir)
        return {}

    skills: dict[str, str] = {}
    for path in files:
        try:
            skills[path.relative_to(skills_dir).as_posix()] = path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to read skill file %s: %s", path, exc)
    logger.info("Loaded %d skill file(s) from %s", len(skills), skills_dir)
    return skills


def discover_skills(app_root: Path) -> str:
    """Return the concatenated text of every skill markdown file, sorted by path."""
    parts: list[str] = [
        f"## skill: {name}\n\n{text.rstrip()}"
        for name, text in discover_skill_texts(app_root).items()
    ]
    return "\n\n".join(parts)
