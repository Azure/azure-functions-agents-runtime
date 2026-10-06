"""App-owned lazy Copilot stdio client and acquired resource lifetime."""

from __future__ import annotations

import asyncio
import atexit
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Final, Literal

from ..._credential import build_async_credential
from ..._logger import logger
from .. import _harness_lifecycle
from .._harness_binding import AppHarness, HarnessKind
from ._copilot_preview import CopilotPreviewError
from ._copilot_session_paths import HOST_PATH_CONVENTIONS, SESSION_STATE_ROOT

if TYPE_CHECKING:
    from azure.core.credentials_async import AsyncTokenCredential
    from copilot import CopilotClient
    from copilot.session import ProviderTokenArgs

    from ._copilot_session_fs import CopilotSessionFs

_START_TIMEOUT_SECONDS = 30
_EMPTY_CLIENT_MODE: Final[Literal["empty"]] = "empty"


class CopilotRuntime:
    """One SDK-managed stdio client owned by one identity-distinct app binding."""

    def __init__(self, harness: AppHarness) -> None:
        if harness.storage_root is None:
            raise CopilotPreviewError("Copilot was not selected for this app.")
        self._harness = harness
        self.native_root = harness.storage_root / "native"
        self.workspace = self.native_root / "workspace"
        self._client: CopilotClient | None = None
        self._failed_client: CopilotClient | None = None
        self._start_lock = asyncio.Lock()
        self._credential: AsyncTokenCredential | None = None
        self._filesystems: set[CopilotSessionFs] = set()
        self._close_callback = self.close
        self._exit_callback = self._exit

    def _register_resources(self) -> None:
        _harness_lifecycle._register_shutdown(self._close_callback)
        atexit.unregister(self._exit_callback)
        atexit.register(self._exit_callback)

    def own_filesystem(self, provider: CopilotSessionFs) -> None:
        self._filesystems.add(provider)
        self._register_resources()

    async def release_filesystem(self, provider: CopilotSessionFs) -> None:
        await asyncio.wait_for(provider.close(), timeout=5)
        self._filesystems.discard(provider)

    def _forget_client(self, client: CopilotClient) -> None:
        if self._client is client:
            self._client = None
        if self._failed_client is client:
            self._failed_client = None

    async def _stop_client(self, client: CopilotClient, timeout: float = 10) -> None:
        try:
            await asyncio.wait_for(client.stop(), timeout=timeout)
        except Exception:
            logger.warning("Copilot graceful shutdown failed; forcing SDK shutdown.")
            try:
                await asyncio.wait_for(client.force_stop(), timeout=5)
            except Exception:
                raise CopilotPreviewError("Copilot forced SDK shutdown failed.") from None
            self._forget_client(client)
            raise CopilotPreviewError("Copilot graceful shutdown failed.") from None
        self._forget_client(client)

    async def client(self) -> CopilotClient:
        async with self._start_lock:
            if self._client is not None:
                return self._client
            if self._failed_client is not None:
                await self._stop_client(self._failed_client, timeout=5)
            self.workspace.mkdir(parents=True, exist_ok=True)
            from copilot import CopilotClient, RuntimeConnection

            client = CopilotClient(
                connection=RuntimeConnection.for_stdio(),
                # Let this runtime own provider, tool, and session-fs wiring.
                mode=_EMPTY_CLIENT_MODE,
                base_directory=str(self.native_root),
                working_directory=str(self.native_root),
                use_logged_in_user=False,
                log_level="none",
                telemetry=None,
                session_fs={
                    "initial_working_directory": str(self.workspace),
                    "session_state_path": SESSION_STATE_ROOT,
                    "conventions": HOST_PATH_CONVENTIONS,
                },
            )
            self._failed_client = client
            self._register_resources()
            try:
                await asyncio.wait_for(client.start(), timeout=_START_TIMEOUT_SECONDS)
            except BaseException:
                try:
                    await self._stop_client(client, timeout=5)
                except BaseException:
                    logger.error("Copilot native startup cleanup failed.")
                raise
            self._failed_client = None
            self._client = client
            logger.info("Agent harness ready: harness=copilot transport=stdio")
            return client

    async def _close_credential(self) -> None:
        if self._credential is not None:
            await asyncio.wait_for(self._credential.close(), timeout=5)
            self._credential = None

    def _forget_if_released(self) -> None:
        if (
            self._client is not None
            or self._failed_client is not None
            or self._credential is not None
            or self._filesystems
        ):
            return
        with self._harness._resources.guard:
            if self._harness._resources.runtime is self:
                self._harness._resources.runtime = None
        _harness_lifecycle._unregister_shutdown(self._close_callback)
        atexit.unregister(self._exit_callback)

    async def close(self) -> None:
        """Visit all acquired handles, retaining each one until its close succeeds."""
        try:
            async with self._start_lock, AsyncExitStack() as cleanup:
                cleanup.push_async_callback(self._close_credential)
                for provider in tuple(self._filesystems):
                    cleanup.push_async_callback(self.release_filesystem, provider)
                for client in (self._client, self._failed_client):
                    if client is not None:
                        cleanup.push_async_callback(self._stop_client, client)
        except Exception:
            logger.error("Copilot runtime owner cleanup failed.")
            raise CopilotPreviewError("Copilot preview shutdown did not complete cleanly.") from None
        finally:
            self._forget_if_released()

    def credential(self) -> AsyncTokenCredential:
        if self._credential is None:
            self._credential = build_async_credential()
            self._register_resources()
        return self._credential

    async def _entra_token(self, scope: str, diagnostic: str) -> str:
        try:
            token = await self.credential().get_token(scope)
        except Exception:
            raise CopilotPreviewError(diagnostic) from None
        return token.token

    def bearer_token_provider(
        self, scope: str, diagnostic: str
    ) -> Callable[[ProviderTokenArgs], Awaitable[str]]:
        self.credential()

        async def token(_args: ProviderTokenArgs) -> str:
            return await self._entra_token(scope, diagnostic)

        return token

    def _exit(self) -> None:
        """Best-effort SDK fallback when the host does not await shutdown."""
        for client in (self._client, self._failed_client):
            if client is not None:
                try:
                    asyncio.run(asyncio.wait_for(client.force_stop(), timeout=2))
                except Exception:
                    logger.error("Copilot native process-exit cleanup failed.")


def get_runtime(harness: AppHarness) -> CopilotRuntime:
    """Construct one resource-free owner under the app binding's synchronous guard."""
    if harness.name is not HarnessKind.COPILOT or harness.storage_root is None:
        raise CopilotPreviewError("Copilot was not selected for this app.")
    with harness._resources.guard:
        owner = harness._resources.runtime
        if owner is None:
            owner = CopilotRuntime(harness)
            harness._resources.runtime = owner
        return owner
