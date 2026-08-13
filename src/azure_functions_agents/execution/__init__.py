"""Execution backend contracts."""

from .backend import (
    AgentExecutionBackend,
    EventCursorExpiredError,
    RunContext,
    RunError,
    RunEvent,
    RunHandle,
    RunResult,
    RunState,
    RunStatus,
    StartRunRequest,
)
from .binding import AgentBinding
from .compat import (
    collect_terminal_run,
    render_sse_event,
    run_to_agent_result,
    split_runner_call,
    status_to_agent_result,
)
from .in_lang_worker import LanguageWorkerExecutionBackend
from .result import AgentResult
from .run_control import (
    RunControlError,
    RunEnvelope,
    RunSubmissionDefinitiveFailureError,
    RunSubmissionIndeterminateError,
    SandboxRunControl,
)
from .setup_budget import SetupBudget, SetupBudgetExpiredError
from .unavailable import BackendUnavailableError, UnavailableBackend, unavailable_backend_message

__all__ = [
    "ACA_SANDBOX_EXECUTION_PROVIDER",
    "DEFAULT_EXECUTION_PROVIDER",
    "AcaSandboxExecutionBackend",
    "AgentBinding",
    "AgentExecutionBackend",
    "AgentResult",
    "BackendUnavailableError",
    "EventCursorExpiredError",
    "LanguageWorkerExecutionBackend",
    "RunContext",
    "RunControlError",
    "RunEnvelope",
    "RunError",
    "RunEvent",
    "RunHandle",
    "RunResult",
    "RunState",
    "RunStatus",
    "RunSubmissionDefinitiveFailureError",
    "RunSubmissionIndeterminateError",
    "SandboxRunControl",
    "SetupBudget",
    "SetupBudgetExpiredError",
    "StartRunRequest",
    "UnavailableBackend",
    "collect_terminal_run",
    "create_execution_backend",
    "render_sse_event",
    "run_to_agent_result",
    "split_runner_call",
    "status_to_agent_result",
    "unavailable_backend_message",
]


def __getattr__(name: str) -> object:
    """Delay the ACA implementation import until controller contracts are initialized."""
    if name == "AcaSandboxExecutionBackend":
        from .aca_sandbox import AcaSandboxExecutionBackend

        return AcaSandboxExecutionBackend
    if name in {
        "ACA_SANDBOX_EXECUTION_PROVIDER",
        "DEFAULT_EXECUTION_PROVIDER",
        "create_execution_backend",
    }:
        from . import factory

        return getattr(factory, name)
    raise AttributeError(name)
