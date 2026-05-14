"""Azure Functions agent runtime — public API.

This package builds Azure Functions apps backed by the Microsoft Agent
Framework. The most common entry points are:

* :func:`create_function_app` — top-level factory used in ``function_app.py``.
* :func:`run_agent` / :func:`run_agent_stream` — execute prompts directly
  (e.g. from custom code or tests).
* :class:`ClientManager` — extension point for plugging in alternate chat
  client providers. The default implementation is :class:`MAFClientManager`
  (auto-detects OpenAI, Azure OpenAI, or Foundry from environment variables).
* :func:`tool` — re-exported from :mod:`agent_framework`. Use this decorator
  in ``tools/*.py`` to register Python functions as agent tools.
"""

from agent_framework import tool  # re-export the canonical tool decorator

from .app import create_function_app
from .client_manager import (
    ClientManager,
    MAFClientManager,
    get_client_manager,
    set_client_manager,
    shutdown_client_manager,
)
from .config import resolve_config_dir, set_app_root
from .connector_tool_cache import configure_connector_tools, get_connector_tools
from .runner import (
    AgentResult,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
    run_agent,
    run_agent_stream,
    run_copilot_agent,
    run_copilot_agent_stream,
)
from .sandbox import create_sandbox_tools

__all__ = [
    "AgentResult",
    "ClientManager",
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT",
    "MAFClientManager",
    "configure_connector_tools",
    "create_function_app",
    "create_sandbox_tools",
    "get_client_manager",
    "get_connector_tools",
    "resolve_config_dir",
    "run_agent",
    "run_agent_stream",
    "run_copilot_agent",
    "run_copilot_agent_stream",
    "set_app_root",
    "set_client_manager",
    "shutdown_client_manager",
    "tool",
]
