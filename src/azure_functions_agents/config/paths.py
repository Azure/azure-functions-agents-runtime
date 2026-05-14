"""Application root and config directory resolution helpers."""

from __future__ import annotations

import os
from pathlib import Path

from azure_functions_agents._logger import logger

_app_root: Path | None = None

_REMOTE_CONFIG_DIR = "/code-assistant-session"


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


def resolve_config_dir() -> str | None:
    """Resolve the config directory used to persist agent-session history files."""
    explicit_path = os.environ.get("AZURE_FUNCTIONS_AGENTS_CONFIG_DIR") or os.environ.get(
        "CODE_ASSISTANT_CONFIG_PATH"
    )
    if explicit_path:
        logger.info("Using config dir override: %s", explicit_path)
        return explicit_path

    container_name = os.environ.get("CONTAINER_NAME")
    if container_name:
        logger.info(
            "Remote mode detected (CONTAINER_NAME=%s), using %s",
            container_name,
            _REMOTE_CONFIG_DIR,
        )
        return _REMOTE_CONFIG_DIR

    return None
