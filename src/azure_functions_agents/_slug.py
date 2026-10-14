"""Slug derivation helpers shared by naming, config composition, and delegation.

Extracted from ``registration/_naming.py`` so ``config/merge.py`` can compute a
``ResolvedAgent``'s identity slug without importing the ``registration``
package (which imports ``config`` and would otherwise create a cycle).
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path

from ._logger import logger


class AgentFileSuffix(StrEnum):
    """Recognized agent-markdown suffixes."""

    AGENT = ".agent.md"
    CLAUDE = ".claude.md"


_PREFIXED_AGENT_FILE_SUFFIXES = (
    AgentFileSuffix.AGENT,
    AgentFileSuffix.CLAUDE,
)


def _prefixed_agent_file_suffix(filename: str) -> AgentFileSuffix | None:
    """Return the recognized case-insensitive suffix for a prefixed agent file."""
    lower_name = filename.lower()
    return next(
        (suffix for suffix in _PREFIXED_AGENT_FILE_SUFFIXES if lower_name.endswith(suffix)),
        None,
    )


def _agent_file_suffix(filename: str) -> AgentFileSuffix | None:
    """Classify bare and prefixed agent-markdown filenames by suffix family."""
    lower_name = filename.lower()
    if lower_name == "agent.md":
        return AgentFileSuffix.AGENT
    if lower_name == "claude.md":
        return AgentFileSuffix.CLAUDE
    return _prefixed_agent_file_suffix(lower_name)


def _safe_function_name(raw_name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_]", "_", raw_name).strip("_")
    if not name:
        return "agent_function"
    if name[0].isdigit():
        return f"fn_{name}"
    return name


def _is_bare_agent_md(filename: str) -> bool:
    """Return True if filename is the bare agent.md single-agent alias (case-insensitive)."""
    return filename.lower() == "agent.md"


def _is_claude_md(filename: str) -> bool:
    """Return True if filename is the CLAUDE.md single-agent alias (case-insensitive)."""
    return filename.lower() == "claude.md"


def _is_single_agent_file(filename: str) -> bool:
    """Return True if filename is a bare single-agent file (agent.md or CLAUDE.md, case-insensitive)."""
    return _is_bare_agent_md(filename) or _is_claude_md(filename)


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

    # Single-agent files (bare agent.md or CLAUDE.md, any casing) → alias for main.agent.md
    if lower_name in ("agent.md", "claude.md"):
        return "main"

    suffix = _prefixed_agent_file_suffix(source_name)
    if suffix is not None:
        prefix = source_name[: -len(suffix)]
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
