"""Azure Functions agent runtime — public API.

This package builds Azure Functions apps backed by a selected agent harness.
The most common entry points are:

* :func:`create_function_app` — top-level factory used in ``function_app.py``.
* :func:`run_agent` / :func:`run_agent_stream` — execute prompts directly
  (e.g. from custom code or tests).
* :func:`tool` — decorator for registering Python functions from ``tools/*.py``
  as agent tools.
* :func:`workflow_tool` — decorator for opting ``tools/*.py`` callables into
  Dynamic Workflow Activity execution.
"""

__version__ = "0.1.0b16"

from ._function_tool import tool, workflow_tool
from .app import create_function_app
from .config.paths import resolve_config_dir, set_app_root
from .harness._harness_lifecycle import _shutdown_harnesses
from .runner import (
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
    AgentResult,
    run_agent,
    run_agent_stream,
)
from .system_tools.sandbox import create_sandbox_tools
from .system_tools.web_request import create_web_request_tools
from .workflows.context import (
    WorkflowTaskContext,
    current_workflow_task_context,
)
from .workflows.schema import (
    WorkflowRetryableError,
    WorkflowRetryBackoff,
    WorkflowRetryPolicy,
    WorkflowTerminalError,
)


async def shutdown_runtime() -> None:
    """Close resources acquired by bound runtime harnesses."""
    await _shutdown_harnesses()


__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT",
    "AgentResult",
    "WorkflowRetryBackoff",
    "WorkflowRetryPolicy",
    "WorkflowRetryableError",
    "WorkflowTaskContext",
    "WorkflowTerminalError",
    "__version__",
    "create_function_app",
    "create_sandbox_tools",
    "create_web_request_tools",
    "current_workflow_task_context",
    "resolve_config_dir",
    "run_agent",
    "run_agent_stream",
    "set_app_root",
    "shutdown_runtime",
    "tool",
    "workflow_tool",
]
