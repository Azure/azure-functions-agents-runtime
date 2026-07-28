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
from .factory import DEFAULT_EXECUTION_PROVIDER, create_execution_backend
from .local import LocalExecutionBackend
from .result import AgentResult

__all__ = [
    "DEFAULT_EXECUTION_PROVIDER",
    "AgentBinding",
    "AgentExecutionBackend",
    "AgentResult",
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
    "collect_terminal_run",
    "create_execution_backend",
    "render_sse_event",
    "run_to_agent_result",
    "split_runner_call",
    "status_to_agent_result",
]
