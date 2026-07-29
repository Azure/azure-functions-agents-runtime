"""Factory for the execution backend selected by whether ``session_runtime.aca_sandbox`` is configured."""

from __future__ import annotations

from .backend import AgentExecutionBackend
from .binding import AgentBinding
from .in_lang_worker import LanguageWorkerExecutionBackend
from .unavailable import UnavailableBackend

DEFAULT_EXECUTION_PROVIDER = "in_lang_worker"
ACA_SANDBOX_EXECUTION_PROVIDER = "aca_sandbox"


def create_execution_backend(
    *,
    binding: AgentBinding,
    provider: str = DEFAULT_EXECUTION_PROVIDER,
    stream_events: bool = False,
) -> AgentExecutionBackend:
    """Create the execution backend for ``provider``.

    ``aca_sandbox`` is recognized but not implemented yet: it resolves to
    :class:`UnavailableBackend`, which raises immediately on construction.
    Application startup is expected to reject an ``aca_sandbox``
    ``session_runtime`` configuration before this factory is ever reached in
    that mode (see ``config.validation.validate_session_runtime`` and its
    ``app.py`` call site); this branch is defense in depth, not the primary
    gate.
    """
    if provider == DEFAULT_EXECUTION_PROVIDER:
        return LanguageWorkerExecutionBackend(binding, stream_events=stream_events)
    if provider == ACA_SANDBOX_EXECUTION_PROVIDER:
        return UnavailableBackend(provider=provider)
    raise ValueError(f"Unsupported execution provider: {provider}")
