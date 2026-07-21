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
            "Resolved agent is missing source_file; falling back to sanitized default for function registration.",
        )
        return _safe_function_name(fallback_name)

    source_name = Path(source_value).name
    lower_name = source_name.lower()

    # Single-agent files (bare agent.md or CLAUDE.md, any casing) → "default"
    if lower_name in ("agent.md", "claude.md"):
        return "default"

    # *.claude.md → strip the suffix to get the prefix
    if lower_name.endswith(".claude.md"):
        prefix = source_name[: -len(".claude.md")]
        return _safe_function_name(prefix)

    # *.agent.md (case-insensitive suffix)
    if lower_name.endswith(".agent.md"):
        prefix = source_name[: -len(".agent.md")]
        return _safe_function_name(prefix)

    # Fallback: use the stem
    base_name = Path(source_name).stem
    return _safe_function_name(base_name)


def _allocate_unique_name(base_name: str, registered_names: set[str]) -> tuple[str, bool]:
    if base_name not in registered_names:
        registered_names.add(base_name)
        return base_name, False

    suffix = 2
    allocated_name = f"{base_name}_{suffix}"
    while allocated_name in registered_names:
        suffix += 1
        allocated_name = f"{base_name}_{suffix}"

    registered_names.add(allocated_name)
    return allocated_name, True


def allocate_unique_function_name(
    source_file: str | Path | None, name: str, registered_names: set[str]
) -> str:
    base_name = _function_name_from_source(source_file, name)
    function_name, collided = _allocate_unique_name(base_name, registered_names)
    if not collided:
        return function_name

    source_desc = f"{source_file!r}" if source_file else "<unknown source_file>"
    logger.warning(
        "Function name collision: %s would register as %r but that name is already used. "
        "Registering as %r. Rename the source file to avoid the suffix.",
        source_desc,
        base_name,
        function_name,
    )
    return function_name


def allocate_unique_builtin_slug(
    source_file: str | Path | None, name: str, registered_names: set[str]
) -> str:
    base_slug = _function_name_from_source(source_file, name)
    slug, collided = _allocate_unique_name(base_slug, registered_names)
    if not collided:
        return slug

    source_desc = Path(str(source_file)).name if source_file else "<unknown source_file>"
    logger.warning(
        "Built-in endpoint slug collision: %r would register at '/agents/%s/' but that route is already used. "
        "Registering at '/agents/%s/'. Rename the source file to avoid the suffix.",
        source_desc,
        base_slug,
        slug,
    )
    return slug
