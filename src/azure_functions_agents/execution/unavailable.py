"""Fail-closed execution backend for ``session_runtime.aca_sandbox``.

The ACA Sandbox execution backend itself is not implemented yet — it lands in
a later phase of FRD 0008 (see ``docs/frds/0008-aca-sandbox-session-runtime.md``).
Application startup (``config.validation.validate_session_runtime`` plus the
composition root in ``app.py``) is expected to reject an ``aca_sandbox``
``session_runtime`` configuration before the app finishes starting, so in
practice nothing should ever construct :class:`UnavailableBackend`.

It exists as defense in depth: if that startup gate is ever bypassed (a
future refactor, a direct unit test, an unusual composition path), attempting
to use the ``aca_sandbox`` backend must still fail loudly and immediately —
never silently fall back to another backend, and never fabricate or simulate
a result.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from .backend import RunContext, RunEvent, RunHandle, RunStatus, StartRunRequest

_UNREACHABLE = (
    "UnavailableBackend method invoked; this should be unreachable because "
    "__init__ always raises before an instance can exist."
)


class BackendUnavailableError(Exception):
    """Raised when a requested execution backend capability is not available."""


def unavailable_backend_message(provider: str) -> str:
    """Build the shared, user-facing diagnostic for an unavailable execution provider."""
    return (
        f"{provider} backend not available in this build. See "
        "docs/frds/0008-aca-sandbox-session-runtime.md for the ACA Sandbox "
        "Session Runtime rollout phases."
    )


class UnavailableBackend:
    """Structurally implements :class:`AgentExecutionBackend` but never runs.

    Construction always raises :class:`BackendUnavailableError` from
    ``__init__``, before any method could be called. The four seam methods are
    still defined (rather than omitted) purely so this class satisfies the
    ``AgentExecutionBackend`` Protocol structurally under ``mypy --strict`` —
    matching how concrete backends in this package are never explicitly
    subclassed from the Protocol either (structural typing only). It is not a
    stub: it never returns, yields, or simulates a run result.
    """

    def __init__(self, *, provider: str) -> None:
        raise BackendUnavailableError(unavailable_backend_message(provider))

    async def start_run(self, request: StartRunRequest) -> RunHandle:
        raise BackendUnavailableError(_UNREACHABLE)

    async def get_run(self, context: RunContext) -> RunStatus:
        raise BackendUnavailableError(_UNREACHABLE)

    def read_events(
        self, context: RunContext, after_sequence: int
    ) -> AsyncIterator[RunEvent]:
        raise BackendUnavailableError(_UNREACHABLE)

    async def cancel_run(self, context: RunContext) -> RunStatus:
        raise BackendUnavailableError(_UNREACHABLE)
