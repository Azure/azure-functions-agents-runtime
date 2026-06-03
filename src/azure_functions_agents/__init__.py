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

# ---------------------------------------------------------------------------
# Global MAF ExperimentalWarning suppression
# ---------------------------------------------------------------------------
# The Microsoft Agent Framework emits ExperimentalWarning for experimental
# features like MemoryStore, SkillResource, etc. These warnings are
# informational — the runtime acknowledges the experimental status.
#
# Warnings are suppressed by default because they clutter cold-start logs
# without providing actionable guidance. Set ``maf_debug: true`` in an
# agent's front matter to allow warnings for that agent's execution.
#
# We suppress warnings by monkey-patching warnings.warn_explicit since that's
# what MAF uses to emit warnings. This ensures warnings are caught regardless
# of import order.
# ---------------------------------------------------------------------------

import warnings as _warnings

# Global flag to control MAF warning suppression (can be temporarily disabled)
_suppress_maf_warnings = True

# Store original functions
_original_warn_explicit = _warnings.warn_explicit
_original_warn = _warnings.warn


def _patched_warn_explicit(
    message,
    category,
    filename,
    lineno,
    module=None,
    registry=None,
    module_globals=None,
    source=None,
):
    """Patched warn_explicit that suppresses MAF ExperimentalWarning."""
    if _suppress_maf_warnings:
        # Check if this is a MAF experimental warning
        category_name = getattr(category, "__name__", "")
        if "ExperimentalWarning" in category_name:
            return  # Suppress
        if "FeatureStageWarning" in category_name:
            return  # Suppress
        # Also check message content for experimental warnings
        msg_str = str(message)
        if "experimental" in msg_str.lower() and "may change or be removed" in msg_str.lower():
            return  # Suppress

    return _original_warn_explicit(
        message, category, filename, lineno, module, registry, module_globals, source
    )


def _patched_warn(message, category=UserWarning, stacklevel=1, source=None):
    """Patched warn that suppresses MAF ExperimentalWarning."""
    if _suppress_maf_warnings:
        # Check if this is a MAF experimental warning
        category_name = getattr(category, "__name__", "")
        if "ExperimentalWarning" in category_name:
            return  # Suppress
        if "FeatureStageWarning" in category_name:
            return  # Suppress
        # Also check message content for experimental warnings
        msg_str = str(message)
        if "experimental" in msg_str.lower() and "may change or be removed" in msg_str.lower():
            return  # Suppress

    return _original_warn(message, category, stacklevel, source)


# Install patches immediately before any MAF imports
_warnings.warn_explicit = _patched_warn_explicit
_warnings.warn = _patched_warn

# Also set standard warning filters as additional backup
_warnings.filterwarnings("ignore", message=r".*experimental.*")

try:
    from agent_framework._feature_stage import ExperimentalWarning

    _warnings.filterwarnings("ignore", category=ExperimentalWarning)
except ImportError:
    pass


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
]
