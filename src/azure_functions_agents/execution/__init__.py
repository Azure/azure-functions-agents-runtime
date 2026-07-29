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
from .factory import (
    ACA_SANDBOX_EXECUTION_PROVIDER,
    DEFAULT_EXECUTION_PROVIDER,
    create_execution_backend,
)
from .local import LocalExecutionBackend
from .result import AgentResult
from .unavailable import BackendUnavailableError, UnavailableBackend, unavailable_backend_message

__all__ = [
    "ACA_SANDBOX_EXECUTION_PROVIDER",
    "DEFAULT_EXECUTION_PROVIDER",
    "AgentBinding",
    "AgentExecutionBackend",
    "AgentResult",
    "BackendUnavailableError",
    "EventCursorExpiredError",
    "LocalExecutionBackend",
    "RunContext",
    "RunError",
    "RunEvent",
    "RunHandle",
    "RunResult",
    "RunState",
    "RunStatus",
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
