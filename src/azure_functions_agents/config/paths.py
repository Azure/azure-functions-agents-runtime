"""Application root and config directory resolution helpers."""

from __future__ import annotations

import os
from pathlib import Path

from azure_functions_agents._logger import logger
from azure_functions_agents.config.env import runtime_env_value

_app_root: Path | None = None


def set_app_root(path: Path) -> None:
    """Explicitly set the application root directory."""
    global _app_root
    _app_root = Path(path).resolve()


def get_app_root() -> Path:
    """Return the root directory of the user's agent project."""
    if _app_root is not None:
        return _app_root
    explicit = os.environ.get("AZURE_FUNCTIONS_AGENTS_APP_ROOT")
    if explicit:
        return Path(explicit).resolve()
    script_root = os.environ.get("AzureWebJobsScriptRoot")  # noqa: SIM112 - Azure Functions uses this name.
    if script_root:
        return Path(script_root).resolve()
    return Path.cwd().resolve()


def resolve_config_dir() -> str:
    """Resolve the config directory used to persist agent-session history files."""
    explicit_path = runtime_env_value("AZURE_FUNCTIONS_AGENTS_SESSION_DIR")
    if explicit_path:
        logger.info("Using session dir override: %s", explicit_path)
        return explicit_path

    fallback_path = str(Path(os.path.expanduser("~/.azure-functions-agents")).resolve())
    logger.debug("Using local config dir fallback: %s", fallback_path)
    return fallback_path
