"""Factory for the default in-process execution backend."""

from __future__ import annotations

from .backend import AgentExecutionBackend
from .local import LocalExecutionBackend

DEFAULT_EXECUTION_PROVIDER = "in_process"


def create_execution_backend(
    provider: str = DEFAULT_EXECUTION_PROVIDER,
) -> AgentExecutionBackend:
    """Create the sole execution backend reachable during P1."""
    if provider != DEFAULT_EXECUTION_PROVIDER:
        raise ValueError(f"Unsupported execution provider: {provider}")
    return LocalExecutionBackend()
