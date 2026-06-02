"""Azure Functions agent runtime — public API.

This package builds Azure Functions apps backed by the Microsoft Agent
Framework. The most common entry points are:

* :func:`create_function_app` — top-level factory used in ``function_app.py``.
* :func:`run_agent` / :func:`run_agent_stream` — execute prompts directly
  (e.g. from custom code or tests).
* :class:`ClientManager` — extension point for plugging in alternate chat
  client providers. The default implementation is :class:`MAFClientManager`
  (auto-detects OpenAI, Azure OpenAI, or Foundry from environment variables).
* :func:`tool` — decorator for registering Python functions from ``tools/*.py``
  as agent tools.
"""

__version__ = "0.0.0a2"

from ._function_tool import tool
from .app import create_function_app
from .client_manager import (
    ClientManager,
    MAFClientManager,
    get_client_manager,
    set_client_manager,
    shutdown_client_manager,
)
from .config.paths import resolve_config_dir, set_app_root
from .runner import (
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
    AgentResult,
    run_agent,
    run_agent_stream,
)
from .system_tools.sandbox import create_sandbox_tools

__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT",
    "AgentResult",
    "ClientManager",
    "MAFClientManager",
    "create_function_app",
    "create_sandbox_tools",
    "get_client_manager",
    "resolve_config_dir",
    "run_agent",
    "run_agent_stream",
    "set_app_root",
    "set_client_manager",
    "shutdown_client_manager",
    "tool",
    "__version__",
]
