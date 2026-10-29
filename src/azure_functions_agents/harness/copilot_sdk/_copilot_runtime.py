"""App-owned lazy Copilot stdio client and acquired resource lifetime."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from enum import StrEnum
from functools import cache, partial
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Final, Literal, cast, get_args
from uuid import uuid4

from ..._credential import build_async_credential
from ..._logger import logger
from ..._observability import current_operation_id
from ...config.env import EnvVar, runtime_env_value
from .. import _harness_lifecycle
from .._agent_runner import get_agent_runner
from .._harness_binding import AppHarness, HarnessKind, UnsupportedCapabilityError
from .._provider_config import resolve_inference_target

if TYPE_CHECKING:
    from azure.core.credentials_async import AsyncTokenCredential
    from copilot import CopilotClient
    from copilot.session import (
        ProviderTokenArgs,
        ReasoningEffort,
        ReasoningSummary,
    )

    from ._copilot_runner import CopilotRunner
else:
    type ReasoningEffort = str
    type ReasoningSummary = str

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


def _literal_values(name: str, values: tuple[object, ...]) -> frozenset[str]:
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise CopilotPreviewError(
                f"Copilot preview requires {name} to export string Literal values."
            )
        normalized.add(value)
    return frozenset(normalized)


@cache
def _reasoning_effort_values() -> frozenset[str]:
    from copilot.session import ReasoningEffort

    return _literal_values("ReasoningEffort", get_args(ReasoningEffort))


@cache
def _reasoning_summary_values() -> frozenset[str]:
    from copilot.session import ReasoningSummary

    return _literal_values("ReasoningSummary", get_args(ReasoningSummary))


def _validate_reasoning_setting(
    name: EnvVar, *, allowed: Callable[[], frozenset[str]]
) -> str | None:
    value = runtime_env_value(name)
    if not value:
        return None
    allowed_values_set = allowed()
    if value not in allowed_values_set:
        allowed_values = ", ".join(sorted(allowed_values_set))
        logger.warning(
            "Ignoring invalid Copilot reasoning setting %s=%r; expected one of: %s. "
            "Omitting the override so the SDK/provider default applies.",
            name,
            value,
            allowed_values,
        )
        return None
    return value


def _reasoning_effort_from_environment() -> ReasoningEffort | None:
    return cast(
        ReasoningEffort | None,
        _validate_reasoning_setting(
            EnvVar.REASONING_EFFORT,
            allowed=_reasoning_effort_values,
        )
    )


def _reasoning_summary_from_environment() -> ReasoningSummary | None:
    return cast(
        ReasoningSummary | None,
        _validate_reasoning_setting(
            EnvVar.REASONING_SUMMARY,
            allowed=_reasoning_summary_values,
        )
    )


def select_copilot_harness(root: Path) -> AppHarness:
    """Freeze provider and storage settings without acquiring runtime resources."""
    from ._copilot_providers import _PROVIDERS
    from ._copilot_session_identity import resolve_route

    check_sdk_dependency()
    supported_providers = [provider.value for provider in _PROVIDERS]
    provider_list = (
        supported_providers[0]
        if len(supported_providers) == 1
        else ", ".join(supported_providers[:-1]) + f", or {supported_providers[-1]}"
    )
    try:
        target = resolve_inference_target(None)
        provider, model = target.require()
    except RuntimeError:
        raise UnsupportedCapabilityError(
            f"Copilot preview supports {PROVIDER_ENV}={provider_list} "
            "with the existing explicit-or-autodetected provider settings."
        ) from None
    copilot_provider = _PROVIDERS[provider].from_environment()
    reasoning_effort = _reasoning_effort_from_environment()
    reasoning_summary = _reasoning_summary_from_environment()
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
            reasoning_effort=reasoning_effort,
            reasoning_summary=reasoning_summary,
            session_storage=route,
        ),
    )


_START_TIMEOUT_SECONDS = 30
_CLEANUP_TIMEOUT_SECONDS = 5
_EMPTY_CLIENT_MODE: Final[Literal["empty"]] = "empty"


class _StartupStage(StrEnum):
    ACQUIRE = "client_acquire"
    LOCK_WAIT = "owner_lock_wait"
    CONSTRUCT = "client_construct"
    START = "client_start"


class _ClientSelection(StrEnum):
    NEW = "new"
    JOIN = "join"
    REUSE = "reuse"


class _TimingOutcome(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    CANCELLED = "cancelled"


class CopilotRuntime:
    """One SDK-managed stdio client owned by one identity-distinct app binding."""

    def __init__(self, harness: AppHarness, runner: CopilotRunner) -> None:
        self._harness = harness
        self._runner = runner
        self.native_root = runner.storage_root / "native"
        self.workspace = self.native_root / "workspace"
        self._client: CopilotClient | None = None
        self._client_task: asyncio.Task[CopilotClient] | None = None
        self._start_lock = asyncio.Lock()
        self._credential: AsyncTokenCredential | None = None
        self._close_callback = self.close
        self._close_requested = False
        self._timing_id = uuid4().hex

    @contextmanager
    def _timing(self, stage: _StartupStage) -> Iterator[None]:
        """Measure owner boundaries without recording SDK inputs or exception details."""
        started = perf_counter()
        operation_id = current_operation_id() or ""
        fields = {
            "stage": stage.value,
            "runtime_id": self._timing_id,
            "process_id": os.getpid(),
            "operation_id": operation_id,
        }
        logger.info(
            "Copilot startup stage started: stage=%s runtime_id=%s process_id=%s operation_id=%s",
            stage.value, self._timing_id, fields["process_id"], operation_id,
            extra=fields,
        )
        outcome = _TimingOutcome.SUCCESS
        try:
            yield
        except asyncio.CancelledError:
            outcome = _TimingOutcome.CANCELLED
            raise
        except BaseException:
            outcome = _TimingOutcome.ERROR
            raise
        finally:
            elapsed_ms = (perf_counter() - started) * 1000
            logger.info(
                "Copilot startup stage finished: stage=%s outcome=%s elapsed_ms=%.3f "
                "runtime_id=%s process_id=%s operation_id=%s",
                stage.value, outcome.value, elapsed_ms,
                self._timing_id, fields["process_id"], operation_id,
                extra={**fields, "outcome": outcome.value, "elapsed_ms": elapsed_ms},
            )

    def _log_client_selection(self, selection: _ClientSelection) -> None:
        logger.info(
            "Copilot startup client selection: selection=%s runtime_id=%s process_id=%s "
            "operation_id=%s",
            selection.value, self._timing_id, os.getpid(), current_operation_id() or "",
            extra={"selection": selection.value, "runtime_id": self._timing_id},
        )

    def _register_resources(self) -> None:
        _harness_lifecycle._register_shutdown(self._close_callback)

    def _should_release_runtime(self) -> bool:
        return (
            self._close_requested
            and self._client_task is None
            and self._client is None
            and self._credential is None
        )

    async def _stop_client(self, client: CopilotClient, timeout: float = 10) -> None:
        try:
            await asyncio.wait_for(client.stop(), timeout=timeout)
        except Exception:
            logger.warning("Copilot graceful shutdown failed; forcing SDK shutdown.")
            try:
                await asyncio.wait_for(
                    client.force_stop(), timeout=_CLEANUP_TIMEOUT_SECONDS
                )
            except Exception:
                raise CopilotPreviewError("Copilot forced SDK shutdown failed.") from None

    def _build_client(self) -> CopilotClient:
        self.workspace.mkdir(parents=True, exist_ok=True)
        from copilot import CopilotClient, RuntimeConnection

        from ._copilot_session_paths import HOST_PATH_CONVENTIONS, SESSION_STATE_ROOT

        return CopilotClient(
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

    async def _construct_and_start_client(self) -> CopilotClient:
        with self._timing(_StartupStage.CONSTRUCT):
            client = await asyncio.to_thread(self._build_client)
        try:
            async with self._start_lock:
                if self._close_requested:
                    raise asyncio.CancelledError
            with self._timing(_StartupStage.START):
                await asyncio.wait_for(client.start(), timeout=_START_TIMEOUT_SECONDS)
            async with self._start_lock:
                if self._close_requested:
                    raise asyncio.CancelledError
                self._client = client
            logger.info("Agent harness ready: harness=copilot transport=stdio")
            return client
        except BaseException:
            try:
                await self._stop_client(client, timeout=_CLEANUP_TIMEOUT_SECONDS)
            except BaseException:
                logger.error("Copilot native startup cleanup failed.")
            raise
        finally:
            release_runtime = False
            async with self._start_lock:
                if self._client_task is asyncio.current_task():
                    self._client_task = None
                release_runtime = self._should_release_runtime()
            if release_runtime:
                self._release_runtime()

    async def client(self) -> CopilotClient:
        with self._timing(_StartupStage.ACQUIRE):
            with self._timing(_StartupStage.LOCK_WAIT):
                await self._start_lock.acquire()
            try:
                if self._client is not None:
                    self._log_client_selection(_ClientSelection.REUSE)
                    return self._client
                task = self._client_task
                if task is None:
                    self._log_client_selection(_ClientSelection.NEW)
                    self._close_requested = False
                    self._register_resources()
                    task = asyncio.create_task(self._construct_and_start_client())
                    self._client_task = task
                else:
                    self._log_client_selection(_ClientSelection.JOIN)
            finally:
                self._start_lock.release()
            return await asyncio.shield(task)

    async def _close_credential(self, credential: AsyncTokenCredential) -> None:
        await asyncio.wait_for(credential.close(), timeout=_CLEANUP_TIMEOUT_SECONDS)

    def _release_runtime(self) -> None:
        with self._harness._resources.guard:
            if self._runner.runtime is self:
                self._runner.runtime = None
        _harness_lifecycle._unregister_shutdown(self._close_callback)

    async def close(self) -> None:
        """Close acquired handles once, then discard them regardless of cleanup outcome."""
        failure = False
        release_runtime = False
        try:
            async with self._start_lock:
                self._close_requested = True
                client_task = self._client_task
                client = self._client
                credential = self._credential
                self._client = None
                self._credential = None
                release_runtime = client_task is None
            if client is None and client_task is not None:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(client_task),
                        timeout=_CLEANUP_TIMEOUT_SECONDS,
                    )
                except asyncio.CancelledError:
                    pass
                except TimeoutError:
                    failure = True
                    logger.error("Copilot runtime client startup cleanup timed out.")
                except Exception:
                    pass
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
            if release_runtime:
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
