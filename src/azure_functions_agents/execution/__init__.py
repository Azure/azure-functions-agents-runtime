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
from .factory import DEFAULT_EXECUTION_PROVIDER, create_execution_backend
from .local import LocalExecutionBackend

__all__ = [
    "DEFAULT_EXECUTION_PROVIDER",
    "AgentExecutionBackend",
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
    "create_execution_backend",
]
