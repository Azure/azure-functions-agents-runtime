"""Azure Functions agent runtime — public API."""

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
from .runner import DEFAULT_MODEL, DEFAULT_TIMEOUT, AgentResult, run_agent, run_agent_stream
from .system_tools.sandbox import create_sandbox_tools

__all__ = [
    'DEFAULT_MODEL',
    'DEFAULT_TIMEOUT',
    'AgentResult',
    'ClientManager',
    'MAFClientManager',
    'create_function_app',
    'create_sandbox_tools',
    'get_client_manager',
    'resolve_config_dir',
    'run_agent',
    'run_agent_stream',
    'set_app_root',
    'set_client_manager',
    'shutdown_client_manager',
    'tool',
]
