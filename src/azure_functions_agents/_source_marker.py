"""Shared utility for creating concise source file markers in logs."""

from __future__ import annotations

from pathlib import Path


def source_marker(source_file: str | None) -> str:
    """Extract a concise identifier from a source file path for logging.

    Prefers filename over full path to reduce log verbosity and PII risk.
    For files under agents/ directory, prefixes with 'agents_' for clarity.

    Args:
        source_file: Full path to the source file, or None.

    Returns:
        A concise marker: 'agents_<filename>' for files in agents/ folder,
        '<filename>' otherwise, or '<unknown>' if source_file is None.
    """
    if not source_file:
        return "<unknown>"

    path = Path(str(source_file))
    filename = path.name

    # Check if file is in agents/ directory
    if len(path.parts) >= 2 and path.parts[-2].lower() in ("agents", "Agents"):
        return f"agents_{filename}"

    return filename
