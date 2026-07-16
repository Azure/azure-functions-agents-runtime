from __future__ import annotations

from pathlib import Path

from .._logger import logger
from .._slug import _function_name_from_source, _safe_function_name

__all__ = [
    "_function_name_from_source",
    "_safe_function_name",
    "allocate_unique_builtin_slug",
    "allocate_unique_function_name",
]


def _allocate_unique_name(base_name: str, registered_names: set[str]) -> str:
    """Reserve ``base_name`` in ``registered_names``.

    Slugs are the agent's stable, prompt-visible identity (function name,
    built-in endpoint route, and ``delegate_<slug>`` tool name all derive
    from it), so a collision must fail fast rather than silently register
    under a different name. See FRD 0006 Decision #17: this replaces the
    previous silent auto-suffix behavior, which is a breaking change.
    """
    if base_name not in registered_names:
        registered_names.add(base_name)
        return base_name

    raise ValueError(
        f"Duplicate agent slug {base_name!r}. Agent identity slugs must be "
        "globally unique across the app. Rename one of the colliding "
        "source files (e.g. its file stem) to resolve this."
    )


def allocate_unique_function_name(
    source_file: str | Path | None, name: str, registered_names: set[str]
) -> str:
    base_name = _function_name_from_source(source_file, name)
    try:
        return _allocate_unique_name(base_name, registered_names)
    except ValueError as exc:
        source_desc = f"{source_file!r}" if source_file else "<unknown source_file>"
        logger.error(
            "Function name collision: %s would register as %r but that name is already used.",
            source_desc,
            base_name,
        )
        raise ValueError(
            f"Function name collision: {source_desc} would register as {base_name!r} "
            "but that name is already used by another agent. Rename the source file "
            "to resolve this."
        ) from exc


def allocate_unique_builtin_slug(
    source_file: str | Path | None, name: str, registered_names: set[str]
) -> str:
    base_slug = _function_name_from_source(source_file, name)
    try:
        return _allocate_unique_name(base_slug, registered_names)
    except ValueError as exc:
        source_desc = Path(str(source_file)).name if source_file else "<unknown source_file>"
        logger.error(
            "Built-in endpoint slug collision: %r would register at '/agents/%s/' but "
            "that route is already used.",
            source_desc,
            base_slug,
        )
        raise ValueError(
            f"Built-in endpoint slug collision: {source_desc!r} would register at "
            f"'/agents/{base_slug}/' but that route is already used by another agent. "
            "Rename the source file to resolve this."
        ) from exc
