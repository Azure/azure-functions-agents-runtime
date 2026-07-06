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
* :func:`workflow_tool` — decorator for opting ``tools/*.py`` callables into
  Dynamic Workflow Activity execution.
"""

__version__ = "0.1.0b4"

# ---------------------------------------------------------------------------
# Global MAF ExperimentalWarning suppression
# ---------------------------------------------------------------------------
# The Microsoft Agent Framework emits ExperimentalWarning for experimental
# features like MemoryStore, SkillResource, etc. These warnings are
# informational — the runtime acknowledges the experimental status.
#
# Warnings are suppressed because they clutter cold-start logs without
# providing actionable guidance.
#
# We suppress warnings by monkey-patching warnings.warn_explicit since that's
# what MAF uses to emit warnings. This ensures warnings are caught regardless
# of import order.
# ---------------------------------------------------------------------------

import warnings as _warnings
from typing import Any

# Global flag to control MAF warning suppression (can be temporarily disabled)
_suppress_maf_warnings = True

# Store original functions
_original_warn_explicit = _warnings.warn_explicit
_original_warn = _warnings.warn


def _patched_warn_explicit(
    message: str | Warning,
    category: type[Warning],
    filename: str,
    lineno: int,
    module: str | None = None,
    registry: Any = None,
    module_globals: Any = None,
    source: Any = None,
) -> None:
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


def _patched_warn(
    message: str | Warning,
    category: type[Warning] = UserWarning,
    stacklevel: int = 1,
    source: Any = None,
) -> None:
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
_warnings.warn = _patched_warn  # type: ignore[assignment]

# Also set standard warning filters as additional backup
_warnings.filterwarnings("ignore", message=r".*experimental.*")

try:
    from agent_framework._feature_stage import ExperimentalWarning

    _warnings.filterwarnings("ignore", category=ExperimentalWarning)
except ImportError:
    pass


from ._function_tool import tool, workflow_tool  # noqa: E402
from .app import create_function_app  # noqa: E402
from .client_manager import (  # noqa: E402
    ClientManager,
    MAFClientManager,
    get_client_manager,
    set_client_manager,
    shutdown_client_manager,
)
from .config.paths import resolve_config_dir, set_app_root  # noqa: E402
from .runner import (  # noqa: E402
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
    AgentResult,
    run_agent,
    run_agent_stream,
)
from .system_tools.sandbox import create_sandbox_tools  # noqa: E402

__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT",
    "AgentResult",
    "ClientManager",
    "MAFClientManager",
    "__version__",
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
    "workflow_tool",
]
