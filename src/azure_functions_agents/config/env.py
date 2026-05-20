"""Environment variable substitution helpers for config parsing."""

from __future__ import annotations

import os
import re
from typing import Any

_VAR_NAME_FRAGMENT = r"[A-Za-z_][A-Za-z0-9_]*"

_ESCAPED_DOLLAR_PATTERN = re.compile(rf"\$\$({_VAR_NAME_FRAGMENT})")
_ESCAPED_PERCENT_PATTERN = re.compile(rf"%%({_VAR_NAME_FRAGMENT})%%")
_INLINE_DOLLAR_PATTERN = re.compile(rf"\$({_VAR_NAME_FRAGMENT})")
_INLINE_PERCENT_PATTERN = re.compile(rf"%({_VAR_NAME_FRAGMENT})%")

_LITERAL_DOLLAR_SENTINEL = "\x00AF_LITERAL_DOLLAR:"
_LITERAL_PERCENT_SENTINEL = "\x00AF_LITERAL_PERCENT:"
_LITERAL_SENTINEL_SUFFIX = "\x00"


def _escaped_dollar_replacer(match: re.Match[str]) -> str:
    return (
        f"{_LITERAL_DOLLAR_SENTINEL}{match.group(1)}{_LITERAL_SENTINEL_SUFFIX}"
    )


def _escaped_percent_replacer(match: re.Match[str]) -> str:
    return (
        f"{_LITERAL_PERCENT_SENTINEL}{match.group(1)}{_LITERAL_SENTINEL_SUFFIX}"
    )


def _dollar_replacer(match: re.Match[str]) -> str:
    return os.environ.get(match.group(1), match.group(0))


def _percent_replacer(match: re.Match[str]) -> str:
    return os.environ.get(match.group(1), match.group(0))


def _restore_escaped_literals(value: str) -> str:
    value = re.sub(
        rf"{re.escape(_LITERAL_DOLLAR_SENTINEL)}({_VAR_NAME_FRAGMENT}){re.escape(_LITERAL_SENTINEL_SUFFIX)}",
        lambda match: f"${match.group(1)}",
        value,
    )
    return re.sub(
        rf"{re.escape(_LITERAL_PERCENT_SENTINEL)}({_VAR_NAME_FRAGMENT}){re.escape(_LITERAL_SENTINEL_SUFFIX)}",
        lambda match: f"%{match.group(1)}%",
        value,
    )


def substitute_env_vars_in_value(value: str) -> str:
    """Perform inline env-var substitution across a single string value."""
    value = _ESCAPED_DOLLAR_PATTERN.sub(_escaped_dollar_replacer, value)
    value = _ESCAPED_PERCENT_PATTERN.sub(_escaped_percent_replacer, value)
    value = _INLINE_DOLLAR_PATTERN.sub(_dollar_replacer, value)
    value = _INLINE_PERCENT_PATTERN.sub(_percent_replacer, value)
    return _restore_escaped_literals(value)


def has_unresolved_placeholders(value: str) -> bool:
    """Return True if the string still contains $VAR or %VAR% placeholders after substitution."""
    return bool(_INLINE_DOLLAR_PATTERN.search(value) or _INLINE_PERCENT_PATTERN.search(value))


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
