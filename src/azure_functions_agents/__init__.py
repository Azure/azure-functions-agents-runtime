"""Azure Functions agent runtime — public API.

This package builds Azure Functions apps backed by the Microsoft Agent
Framework. The most common entry points are:

* :func:`create_function_app` — top-level factory used in ``function_app.py``.
* :func:`run_agent` / :func:`run_agent_stream` — execute prompts directly
  (e.g. from custom code or tests).
* :func:`tool` — decorator for registering Python functions from ``tools/*.py``
  as agent tools.
"""

from ._function_tool import tool
from .app import create_function_app
from .config.paths import resolve_config_dir, set_app_root
from .runner import (
    AgentResult,
    run_agent,
    run_agent_stream,
    run_copilot_agent,
    run_copilot_agent_stream,
)
from .system_tools.connectors.cache import configure_connector_tools, get_connector_tools
from .system_tools.sandbox import create_sandbox_tools

__all__ = [
    "AgentResult",
    "configure_connector_tools",
    "create_function_app",
    "create_sandbox_tools",
    "get_connector_tools",
    "resolve_config_dir",
    "run_agent",
    "run_agent_stream",
    "run_copilot_agent",
    "run_copilot_agent_stream",
    "set_app_root",
    "tool",
]
