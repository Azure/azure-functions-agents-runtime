"""Environment variable substitution helpers for config parsing."""

from __future__ import annotations

import os
import re
from typing import Any

_VAR_NAME_FRAGMENT = r"[A-Za-z_][A-Za-z0-9_]*"

_INLINE_DOLLAR_PATTERN = re.compile(rf"\$({_VAR_NAME_FRAGMENT})")
_INLINE_PERCENT_PATTERN = re.compile(rf"%({_VAR_NAME_FRAGMENT})%")


def _dollar_replacer(match: re.Match[str]) -> str:
    return os.environ.get(match.group(1), match.group(0))


def _percent_replacer(match: re.Match[str]) -> str:
    return os.environ.get(match.group(1), match.group(0))


def substitute_env_vars_in_value(value: str) -> str:
    """Perform inline env-var substitution across a single string value."""
    value = _INLINE_DOLLAR_PATTERN.sub(_dollar_replacer, value)
    return _INLINE_PERCENT_PATTERN.sub(_percent_replacer, value)


def substitute_env_vars_in_text(text: str) -> str:
    """Perform inline env-var substitution outside fenced code blocks."""
    parts = text.split("```")
    for index in range(0, len(parts), 2):
        parts[index] = substitute_env_vars_in_value(parts[index])
    return "```".join(parts)


def resolve_env_vars_in_data(value: Any) -> Any:
    """Recursively substitute env vars in string values within nested data."""
    if isinstance(value, str):
        return substitute_env_vars_in_value(value)
    if isinstance(value, list):
        return [resolve_env_vars_in_data(item) for item in value]
    if isinstance(value, dict):
        return {key: resolve_env_vars_in_data(item) for key, item in value.items()}
    return value


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
