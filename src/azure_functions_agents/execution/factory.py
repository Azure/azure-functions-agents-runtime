"""Factory for the default in-process execution backend."""

from __future__ import annotations

from .local import LocalExecutionBackend, _RunAgent, _RunAgentStream

DEFAULT_EXECUTION_PROVIDER = "in_process"


def create_execution_backend(
    run_agent: _RunAgent, run_agent_stream: _RunAgentStream
) -> LocalExecutionBackend:
    """Create the sole execution backend reachable during P1."""
    return LocalExecutionBackend(run_agent, run_agent_stream)
