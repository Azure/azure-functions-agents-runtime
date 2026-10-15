"""App-owned lazy Copilot stdio client and acquired resource lifetime."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import partial
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, cast

from ..._credential import build_async_credential
from ..._logger import logger
from ...config.env import EnvVar, raw_env_value
from .. import _harness_lifecycle
from .._agent_runner import get_agent_runner
from .._harness_binding import AppHarness, HarnessKind, UnsupportedCapabilityError
from .._provider_config import resolve_inference_target

if TYPE_CHECKING:
    from azure.core.credentials_async import AsyncTokenCredential
    from copilot import CopilotClient
    from copilot.session import ProviderTokenArgs

    from ._copilot_runner import CopilotRunner


SDK_DISTRIBUTION = "github-copilot-sdk"
PROVIDER_ENV = EnvVar.PROVIDER


class CopilotPreviewError(RuntimeError):
    """A safe, actionable Copilot SDK error without underlying SDK details."""


def check_sdk_dependency() -> None:
    try:
        version(SDK_DISTRIBUTION)
    except PackageNotFoundError:
        raise CopilotPreviewError(
            "Copilot preview requires azurefunctions-agents-runtime[copilot]. "
            f"Install {SDK_DISTRIBUTION} through the optional extra."
        ) from None


def select_copilot_harness(root: Path) -> AppHarness:
    """Freeze provider and storage settings without acquiring runtime resources."""
    from ._copilot_providers import _PROVIDERS
    from ._copilot_session_identity import resolve_route

    check_sdk_dependency()
    try:
        target = resolve_inference_target(None)
        provider, model = target.require()
    except RuntimeError:
        raise UnsupportedCapabilityError(
            f"Copilot preview supports {PROVIDER_ENV}=openai, azure_openai, or foundry "
            "with the existing explicit-or-autodetected provider settings."
        ) from None
    copilot_provider = _PROVIDERS[provider].from_environment()
    for name in (EnvVar.REASONING_EFFORT, EnvVar.REASONING_SUMMARY):
        if raw_env_value(name):
            raise UnsupportedCapabilityError(f"Copilot preview does not support {name}.")
    route = resolve_route(root)
    from ._copilot_runner import CopilotRunner

    return AppHarness(
        HarnessKind.COPILOT,
        root,
        partial(
            CopilotRunner,
            storage_root=route.local_dir / "copilot-preview",
            default_model=model,
            provider=copilot_provider,
            session_storage=route,
        ),
    )

_START_TIMEOUT_SECONDS = 30
_EMPTY_CLIENT_MODE: Final[Literal["empty"]] = "empty"


class CopilotRuntime:
    """One SDK-managed stdio client owned by one identity-distinct app binding."""

    def __init__(self, harness: AppHarness, runner: CopilotRunner) -> None:
        self._harness = harness
        self._runner = runner
        self.native_root = runner.storage_root / "native"
        self.workspace = self.native_root / "workspace"
        self._client: CopilotClient | None = None
        self._start_lock = asyncio.Lock()
        self._credential: AsyncTokenCredential | None = None
        self._close_callback = self.close

    def _register_resources(self) -> None:
        _harness_lifecycle._register_shutdown(self._close_callback)

    async def _stop_client(self, client: CopilotClient, timeout: float = 10) -> None:
        try:
            await asyncio.wait_for(client.stop(), timeout=timeout)
        except Exception:
            logger.warning("Copilot graceful shutdown failed; forcing SDK shutdown.")
            try:
                await asyncio.wait_for(client.force_stop(), timeout=5)
            except Exception:
                raise CopilotPreviewError("Copilot forced SDK shutdown failed.") from None

    async def client(self) -> CopilotClient:
        async with self._start_lock:
            if self._client is not None:
                return self._client
            self.workspace.mkdir(parents=True, exist_ok=True)
            from copilot import CopilotClient, RuntimeConnection

            from ._copilot_session_paths import HOST_PATH_CONVENTIONS, SESSION_STATE_ROOT

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
            self._register_resources()
            try:
                await asyncio.wait_for(client.start(), timeout=_START_TIMEOUT_SECONDS)
            except BaseException:
                try:
                    await self._stop_client(client, timeout=5)
                except BaseException:
                    logger.error("Copilot native startup cleanup failed.")
                raise
            self._client = client
            logger.info("Agent harness ready: harness=copilot transport=stdio")
            return client

    async def _close_credential(self, credential: AsyncTokenCredential) -> None:
        await asyncio.wait_for(credential.close(), timeout=5)

    def _release_runtime(self) -> None:
        with self._harness._resources.guard:
            if self._runner.runtime is self:
                self._runner.runtime = None
        _harness_lifecycle._unregister_shutdown(self._close_callback)

    async def close(self) -> None:
        """Close acquired handles once, then discard them regardless of cleanup outcome."""
        failure = False
        try:
            async with self._start_lock:
                client = self._client
                credential = self._credential
                self._client = None
                self._credential = None
            if client is not None:
                try:
                    await self._stop_client(client)
                except CopilotPreviewError:
                    failure = True
                    logger.error("Copilot runtime client cleanup failed.")
            if credential is not None:
                try:
                    await self._close_credential(credential)
                except Exception:
                    failure = True
                    logger.error("Copilot runtime credential cleanup failed.")
        finally:
            self._release_runtime()
        if failure:
            raise CopilotPreviewError("Copilot preview shutdown did not complete cleanly.") from None

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


def get_runtime(harness: AppHarness) -> CopilotRuntime:
    """Construct one resource-free owner under the app binding's synchronous guard."""
    if harness.name is not HarnessKind.COPILOT:
        raise CopilotPreviewError("Copilot was not selected for this app.")
    runner = cast("CopilotRunner", get_agent_runner(harness))
    with harness._resources.guard:
        owner = runner.runtime
        if owner is None:
            owner = CopilotRuntime(harness, runner)
            runner.runtime = owner
        return owner
