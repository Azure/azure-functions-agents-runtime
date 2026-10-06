"""SDK-free ordinary Python tool-result conversion compatible with MAF defaults."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import cast

from pydantic import BaseModel


def tool_result_text(
    result: object, *, validate_result: Callable[[object], None] | None = None
) -> str:
    """Convert ordinary values while letting the adapter reject unsupported rich results."""
    def check(value: object) -> None:
        if validate_result is not None:
            validate_result(value)

    def ordinary(value: object) -> object:
        check(value)
        if isinstance(value, list):
            return [ordinary(item) for item in value]
        if isinstance(value, dict):
            return {key: ordinary(item) for key, item in value.items()}
        if isinstance(value, BaseModel):
            return value.model_dump()
        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            return cast(object, to_dict())
        text = getattr(value, "text", None)
        return text if isinstance(text, str) else value

    def fallback(value: object) -> str:
        check(value)
        return str(value)

    if result is None or isinstance(result, str):
        check(result)
        return "" if result is None else result
    converted = ordinary(result)
    return converted if isinstance(converted, str) else json.dumps(converted, default=fallback)
