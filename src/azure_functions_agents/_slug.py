"""Slug derivation helpers shared by naming, config composition, and delegation.

Extracted from ``registration/_naming.py`` so ``config/merge.py`` can compute a
``ResolvedAgent``'s identity slug without importing the ``registration``
package (which imports ``config`` and would otherwise create a cycle).
"""

from __future__ import annotations

import re
from pathlib import Path


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


def _function_name_from_source(source_file: str | Path) -> str:
    """Derive a sanitized base name from ``source_file``'s stem.

    The caller must supply the source filename; presentation metadata is never
    used as a machine-identity fallback.
    """
    source_name = Path(source_file).name
    lower_name = source_name.lower()

    # Single-agent files (bare agent.md or CLAUDE.md, any casing) → alias for main.agent.md
    if lower_name in ("agent.md", "claude.md"):
        return "main"

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
