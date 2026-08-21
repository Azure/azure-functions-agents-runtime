"""Explicit factory for in-worker, ACA Sandbox, and Foundry Responses backends."""

from __future__ import annotations

from ..session_state import OwnerPrincipal, resolve_owner_context
from .aca_sandbox import AcaSandboxExecutionBackend
from .backend import AgentExecutionBackend
from .binding import AgentBinding
from .foundry_responses_execution_backend import FoundryResponsesExecutionBackend
from .foundry_responses_runtime import FoundryResponsesRuntime
from .in_lang_worker import LanguageWorkerExecutionBackend
from .session_runtime import SessionExecutionRuntime
from .setup_budget import SetupBudget
from .unavailable import UnavailableBackend

DEFAULT_EXECUTION_PROVIDER = "in_lang_worker"
ACA_SANDBOX_EXECUTION_PROVIDER = "aca_sandbox"
FOUNDRY_RESPONSES_EXECUTION_PROVIDER = "foundry_responses"


def create_execution_backend(
    *,
    binding: AgentBinding,
    provider: str = DEFAULT_EXECUTION_PROVIDER,
    stream_events: bool = False,
    session_runtime: SessionExecutionRuntime | None = None,
    foundry_runtime: FoundryResponsesRuntime | None = None,
    owner: OwnerPrincipal | None = None,
    setup_budget: SetupBudget | None = None,
) -> AgentExecutionBackend:
    """Create the execution backend for ``provider``.

    A configured session-runtime binding is the authoritative backend-selection
    input. Owner resolution remains construction-time state, preserving the
    serializable four-method lifecycle seam.
    """
    if session_runtime is not None and foundry_runtime is not None:
        raise ValueError("ACA Sandbox and Foundry Responses runtimes cannot coexist")
    selected_runtime = foundry_runtime or session_runtime
    if isinstance(selected_runtime, FoundryResponsesRuntime):
        agent_name = binding.agent_name
        if not agent_name:
            raise ValueError("Foundry Responses execution requires an agent identity slug")
        return FoundryResponsesExecutionBackend(
            binding,
            runtime=selected_runtime,
            owner=resolve_owner_context(selected_runtime.app_identity, agent_name, owner),
            stream_events=stream_events,
        )
    if selected_runtime is not None:
        agent_name = binding.agent_name
        if not agent_name:
            raise ValueError("ACA Sandbox execution requires an agent identity slug")
        return AcaSandboxExecutionBackend(
            binding,
            runtime=selected_runtime,
            owner=resolve_owner_context(selected_runtime.app_identity, agent_name, owner),
            setup_budget=setup_budget,
        )
    if provider == DEFAULT_EXECUTION_PROVIDER:
        return LanguageWorkerExecutionBackend(binding, stream_events=stream_events)
    if provider in {ACA_SANDBOX_EXECUTION_PROVIDER, FOUNDRY_RESPONSES_EXECUTION_PROVIDER}:
        return UnavailableBackend(provider=provider)
    raise ValueError(f"Unsupported execution provider: {provider}")
