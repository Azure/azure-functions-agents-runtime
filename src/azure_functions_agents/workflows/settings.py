"""Operational settings for the Dynamic Workflows runtime."""

from azure_functions_agents.config.env import runtime_env_value

WORKFLOW_DRAIN_MODE_ENV = "AZURE_FUNCTIONS_AGENTS_WORKFLOW_DRAIN_MODE"

_TRUE_VALUES = frozenset({"true", "1", "yes", "y"})
_FALSE_VALUES = frozenset({"false", "0", "no", "n"})


def workflow_drain_mode_enabled() -> bool:
    """Return whether the app should retain Durable runtime for workflow draining."""
    raw = runtime_env_value(WORKFLOW_DRAIN_MODE_ENV)
    if not raw:
        return False
    normalized = raw.lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ValueError(
        f"{WORKFLOW_DRAIN_MODE_ENV} must be a boolean "
        "(true/false, 1/0, yes/no, or y/n)"
    )


__all__ = ["WORKFLOW_DRAIN_MODE_ENV", "workflow_drain_mode_enabled"]
