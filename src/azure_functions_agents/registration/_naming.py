from __future__ import annotations

import re
from pathlib import Path

from .._logger import logger


def _safe_function_name(raw_name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_]", "_", raw_name).strip("_")
    if not name:
        return "agent_function"
    if name[0].isdigit():
        return f"fn_{name}"
    return name


def _function_name_from_source(source_file: str | Path | None, fallback_name: str) -> str:
    source_value = str(source_file).strip() if source_file is not None else ""
    if not source_value:
        logger.warning(
            "Resolved agent '%s' is missing source_file; falling back to sanitized display name for function registration.",
            fallback_name,
        )
        return _safe_function_name(fallback_name)

    source_name = Path(source_value).name
    base_name = source_name.removesuffix(".agent.md")
    if base_name == source_name:
        base_name = Path(source_name).stem
    return _safe_function_name(base_name)
