"""Slug derivation helpers shared by naming, config composition, and delegation.

Extracted from ``registration/_naming.py`` so ``config/merge.py`` can compute a
``ResolvedAgent``'s identity slug without importing the ``registration``
package (which imports ``config`` and would otherwise create a cycle).
"""

from __future__ import annotations

import re
from pathlib import Path

from ._logger import logger


def _safe_function_name(raw_name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_]", "_", raw_name).strip("_")
    if not name:
        return "agent_function"
    if name[0].isdigit():
        return f"fn_{name}"
    return name


def _function_name_from_source(
    source_file: str | Path | None, fallback_name: str, *, warn_on_missing: bool = True
) -> str:
    """Derive a sanitized base name from ``source_file``'s stem.

    ``warn_on_missing`` gates the "no source_file" warning: registration
    call sites (``registration/_naming.py``) want it (a missing
    ``source_file`` there means something real is misconfigured), but
    ``config/merge.py``'s ``compose()`` must stay warning-free — see
    ``test_compose_defers_warning_only_validation`` — since directly
    constructed ``AgentSpec``s (common in unit tests) often omit
    ``source_file`` and that is not itself a validation concern.
    """
    source_value = str(source_file).strip() if source_file is not None else ""
    if not source_value:
        if warn_on_missing:
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


def delegate_tool_name(slug: str) -> str:
    """Return the auto-derived tool name for delegating to the agent ``slug``.

    Always ``delegate_<slug>`` — no user-configurable ``tool_name`` override
    exists (FRD 0007 §4.8, §5 Decision #16). Centralized here so the
    tool-name-collision check (``registration/capabilities.py``) and the
    actual tool construction (``runner.py``) can never drift apart.
    """
    return f"delegate_{slug}"
