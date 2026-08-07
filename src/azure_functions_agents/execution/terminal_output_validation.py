"""Controller-side validation of terminal sandbox output."""

from __future__ import annotations

from .backend import RunError, RunStatus
from .binding import AgentBinding


def validate_terminal_output(binding: AgentBinding, status: RunStatus) -> RunStatus:
    """Turn an invalid terminal success into a durable app-domain failure."""
    if status.state != "succeeded" or status.result is None or binding.output_validator is None:
        return status
    error = binding.output_validator(status.result)
    if error is None:
        return status
    return RunStatus(
        run_id=status.run_id,
        session_id=status.session_id,
        state="failed",
        last_sequence=status.last_sequence,
        result_available=False,
        error=RunError(
            code=error.code,
            message=error.message,
            fault_domain=error.fault_domain,
        ),
    )
