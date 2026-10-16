"""Ordinary Python tool-result conversion used by the Copilot adapter."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import cast

from pydantic import BaseModel


def tool_result_text(result: object) -> str:
    """Use supported Python serialization without classifying value origins."""

    def ordinary(value: object) -> object:
        if isinstance(value, list):
            return [ordinary(item) for item in value]
        if isinstance(value, dict):
            return {key: ordinary(item) for key, item in value.items()}
        if isinstance(value, BaseModel):
            return value.model_dump()
        if hasattr(value, "to_dict"):
            to_dict = cast(Callable[[], object], value.to_dict)
            return to_dict()
        text = getattr(value, "text", None)
        return text if isinstance(text, str) else value

    if result is None or isinstance(result, str):
        return "" if result is None else result
    converted = ordinary(result)
    return converted if isinstance(converted, str) else json.dumps(converted, default=str)
