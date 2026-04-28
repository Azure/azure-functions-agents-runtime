import asyncio
import logging
import os
from typing import Optional

from copilot import CopilotClient, SubprocessConfig

from .config import get_app_root, resolve_config_dir


def _is_byok_mode() -> bool:
    """Check if BYO key (Microsoft Foundry) environment variables are configured."""
    return bool(
        os.environ.get("AZURE_AI_FOUNDRY_ENDPOINT")
        and os.environ.get("AZURE_AI_FOUNDRY_API_KEY")
    )


class CopilotClientManager:
    """
    Singleton manager for the CopilotClient.
    """

    _instance: Optional["CopilotClientManager"] = None
    _client: Optional[CopilotClient] = None
    _lock: asyncio.Lock = None
    _started: bool = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._lock = asyncio.Lock()
        return cls._instance

    @classmethod
    async def get_client(cls) -> CopilotClient:
        manager = cls()
        async with manager._lock:
            if manager._client is None or not manager._started:
                app_root = str(get_app_root())
                config_dir = resolve_config_dir()

                # Pass --config-dir as a CLI startup flag so the subprocess
                # writes events.jsonl and can load sessions from the shared
                # mount, enabling cross-instance session resume.
                cli_args: list[str] = []
                if config_dir:
                    cli_args = ["--config-dir", config_dir]
                    logging.info(f"CLI config-dir: {config_dir}")

                if _is_byok_mode():
                    logging.info("BYOK mode: using Microsoft Foundry (no GitHub token)")
                    manager._client = CopilotClient(
                        SubprocessConfig(cwd=app_root, cli_args=cli_args)
                    )
                else:
                    github_token = os.environ.get("GITHUB_TOKEN")
                    manager._client = CopilotClient(
                        SubprocessConfig(
                            github_token=github_token,
                            cwd=app_root,
                            cli_args=cli_args,
                        )
                    )

                await manager._client.start()
                manager._started = True
                logging.info(f"CopilotClient singleton started (BYOK: {_is_byok_mode()})")
        return manager._client

    @classmethod
    async def shutdown(cls):
        manager = cls()
        async with manager._lock:
            if manager._client and manager._started:
                await manager._client.stop()
                manager._started = False
                manager._client = None
                logging.info("CopilotClient singleton stopped")

    @classmethod
    def is_running(cls) -> bool:
        manager = cls()
        return manager._started
