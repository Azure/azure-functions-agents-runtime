"""Factory for the execution backend selected by whether ``session_runtime.aca_sandbox`` is configured."""

from __future__ import annotations

from ..controller.readiness import SessionRuntimeBinding
from ..session_state import OwnerPrincipal, resolve_owner_context
from .aca_sandbox import AcaSandboxExecutionBackend
from .backend import AgentExecutionBackend
from .binding import AgentBinding
from .in_lang_worker import LanguageWorkerExecutionBackend
from .setup_budget import SetupBudget
from .unavailable import UnavailableBackend

DEFAULT_EXECUTION_PROVIDER = "in_lang_worker"
ACA_SANDBOX_EXECUTION_PROVIDER = "aca_sandbox"


def create_execution_backend(
    *,
    binding: AgentBinding,
    provider: str = DEFAULT_EXECUTION_PROVIDER,
    stream_events: bool = False,
    session_runtime: SessionRuntimeBinding | None = None,
    owner: OwnerPrincipal | None = None,
    setup_budget: SetupBudget | None = None,
) -> AgentExecutionBackend:
    """Create the execution backend for ``provider``.

    A configured session-runtime binding is the authoritative backend-selection
    input. Owner resolution remains construction-time state, preserving the
    serializable four-method lifecycle seam.
    """
    if session_runtime is not None:
        agent_name = binding.agent_name
        if not agent_name:
            raise ValueError("ACA Sandbox execution requires an agent identity slug")
        return AcaSandboxExecutionBackend(
            binding,
            runtime=session_runtime,
            owner=resolve_owner_context(session_runtime.app_identity, agent_name, owner),
            setup_budget=setup_budget,
        )
    if provider == DEFAULT_EXECUTION_PROVIDER:
        return LanguageWorkerExecutionBackend(binding, stream_events=stream_events)
    if provider == ACA_SANDBOX_EXECUTION_PROVIDER:
        return UnavailableBackend(provider=provider)
    raise ValueError(f"Unsupported execution provider: {provider}")
