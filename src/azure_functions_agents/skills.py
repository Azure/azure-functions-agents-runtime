"""Skill discovery — read project markdown files into a single instructions block.

The runtime supports an optional ``skills/`` directory under the app root. Any
markdown files inside (recursively) are loaded once at app startup, sorted by
path, and concatenated. The combined text is intended to be appended to the
agent's ``instructions`` so that every turn sees the same skill content
without per-call file I/O.

Skills are an opt-in convention — if no skills directory exists, this module
returns an empty string and contributes nothing.
"""

from __future__ import annotations

<<<<<<< HEAD
import logging
=======
import os
>>>>>>> b60b0883341572b030f5452ffe40d103d3c24b77
from pathlib import Path
from typing import List, Optional

from ._logger import logger
from .config import get_app_root


def _resolve_skills_dir() -> Optional[Path]:
    """Find ``{app_root}/skills`` (or ``Skills``) if it exists."""
    app_root = Path(get_app_root())
    for name in ("skills", "Skills"):
        candidate = app_root / name
        if candidate.is_dir():
            return candidate
    return None


def _collect_skill_files(skills_dir: Path) -> List[Path]:
    """Return a deterministic, sorted list of every ``*.md`` file under ``skills_dir``."""
    files = [p for p in skills_dir.rglob("*.md") if p.is_file()]
    files.sort(key=lambda p: str(p).lower())
    return files


def load_skills_text() -> str:
    """Return the concatenated text of every skill markdown file, sorted by path.

    Each file is preceded by a small header so the agent can see where the
    content came from. If the skills directory does not exist, returns an
    empty string.
    """
    skills_dir = _resolve_skills_dir()
    if skills_dir is None:
        return ""

    files = _collect_skill_files(skills_dir)
    if not files:
        logger.info("No skill markdown files found in %s", skills_dir)
        return ""

    parts: List[str] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to read skill file %s: %s", path, exc)
            continue
        rel = path.relative_to(skills_dir)
        parts.append(f"## skill: {rel.as_posix()}\n\n{text.rstrip()}")

    logger.info("Loaded %d skill file(s) from %s", len(files), skills_dir)
    return "\n\n".join(parts)


# Cached at module load — skills are static for the life of the process.
_SKILLS_TEXT_CACHE: str = load_skills_text()


def get_cached_skills_text() -> str:
    """Return the cached skills text computed at app startup."""
    return _SKILLS_TEXT_CACHE

