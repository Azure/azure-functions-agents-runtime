"""Environment variable substitution helpers for config parsing."""

from __future__ import annotations

import os
import re
from typing import Any

_VAR_NAME_FRAGMENT = r"[A-Za-z_][A-Za-z0-9_]*"

_PERCENT_PATTERN = re.compile(rf"^%({_VAR_NAME_FRAGMENT})%$")
_DOLLAR_PATTERN = re.compile(rf"^\$({_VAR_NAME_FRAGMENT})$")

_INLINE_DOLLAR_PATTERN = re.compile(rf"\$({_VAR_NAME_FRAGMENT})")
_INLINE_PERCENT_PATTERN = re.compile(rf"%({_VAR_NAME_FRAGMENT})%")


def resolve_env_var(value: str) -> str:
    """Resolve a frontmatter value that is a single env-var reference."""
    stripped = value.strip()
    match = _PERCENT_PATTERN.match(stripped) or _DOLLAR_PATTERN.match(stripped)
    if match:
        return os.environ.get(match.group(1), value)
    return value


def substitute_env_vars_in_text(text: str) -> str:
    """Perform inline env-var substitution outside fenced code blocks."""

    def _dollar_replacer(match: re.Match[str]) -> str:
        return os.environ.get(match.group(1), match.group(0))

    def _percent_replacer(match: re.Match[str]) -> str:
        return os.environ.get(match.group(1), match.group(0))

    def _substitute(segment: str) -> str:
        segment = _INLINE_DOLLAR_PATTERN.sub(_dollar_replacer, segment)
        return _INLINE_PERCENT_PATTERN.sub(_percent_replacer, segment)

    parts = text.split("```")
    for index in range(0, len(parts), 2):
        parts[index] = _substitute(parts[index])
    return "```".join(parts)


def _to_bool(value: Any, default: bool = True) -> bool:
    """Coerce a config value to bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y"}:
            return True
        if lowered in {"false", "0", "no", "n"}:
            return False
    return default
