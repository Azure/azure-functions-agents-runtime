"""Sandbox-only harness components."""

from __future__ import annotations

import os

SANDBOX_MARKER_ENV_VAR = "AZURE_FUNCTIONS_AGENTS_SANDBOX"


def _ensure_sandbox() -> None:
    """Reject harness activation outside a sandbox process."""
    if SANDBOX_MARKER_ENV_VAR not in os.environ:
        raise RuntimeError("Sandbox harness activation requires a sandbox process")
