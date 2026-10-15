"""Shared Copilot preview warnings for ignored settings."""

from __future__ import annotations

from ..._logger import logger
from ...config.env import EnvVar, runtime_env_value

_OUTPUT_LIMIT_WARNING = (
    "Ignoring %s for Copilot preview: the pinned SDK/runtime does not expose a verified "
    "provider generation-cap mapping, so this cap is not enforced and the provider default "
    "remains in effect."
)

_REASONING_SETTING_WARNING = (
    "Ignoring %s for Copilot preview: the pinned SDK/runtime does not expose this as a "
    "supported native-session setting, so the SDK/provider default remains in effect."
)


def warn_ignored_output_limit(setting_name: str, value: int | None) -> None:
    """Warn once at the current boundary when Copilot cannot enforce the cap."""
    if value is None:
        return
    logger.warning(_OUTPUT_LIMIT_WARNING, setting_name)


def warn_ignored_reasoning_setting(name: EnvVar) -> None:
    """Warn only when a non-empty reasoning override would otherwise take effect."""
    if not runtime_env_value(name):
        return
    logger.warning(_REASONING_SETTING_WARNING, name)
