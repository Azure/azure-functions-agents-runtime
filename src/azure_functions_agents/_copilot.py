"""Lazy Copilot SDK adapter for the explicitly limited local preview."""

from __future__ import annotations

import asyncio
import atexit
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

from ._copilot_session_fs import (
    HOST_PATH_CONVENTIONS,
    SESSION_STATE_ROOT,
    NativeSessionFs,
    open_session_fs,
)
from ._credential import build_async_credential
from ._harness import (
    AppHarness,
    CopilotPreviewError,
    HarnessKind,
    HarnessRequest,
    UnsupportedCapabilityError,
    _register_shutdown,
    _unregister_shutdown,
    validate_copilot_client_manager,
)
from ._logger import logger
from ._native_session_identity import NativeSessionError
from .client_manager import InferenceTarget

if TYPE_CHECKING:
    from azure.core.credentials_async import AsyncTokenCredential
    from copilot import CopilotClient
    from copilot.generated.rpc import PermissionDecision
    from copilot.session import (
        CopilotSession,
        CreateSessionFsHandler,
        PermissionInvocation,
        ProviderConfig,
        ProviderTokenArgs,
        SystemMessageConfig,
        ToolSearchConfig,
    )
    from copilot.session_events import PermissionRequest, SessionEvent
    from copilot.tools import Tool, ToolInvocation, ToolResult

    from ._copilot_providers import ProviderTokenSource
    from ._function_tool import FunctionTool
    from .runner import AgentResult


class _SessionOptions(TypedDict):
    model: str
    tools: list[Tool]
    available_tools: list[str]
    system_message: SystemMessageConfig
    provider: ProviderConfig
    streaming: bool
    enable_config_discovery: bool
    enable_session_telemetry: bool
    request_extensions: bool
    tool_search: ToolSearchConfig
    on_event: Callable[[SessionEvent], None]
    create_session_fs_handler: CreateSessionFsHandler


def _native_id(agent_slug: str, session_id: str) -> str:
    return f"{agent_slug}.{session_id}"


class _NativeRuntime:
    """One SDK-managed stdio client per app worker."""

    def __init__(self, root: Path) -> None:
        self.native_root = root / "native"
        self.workspace = self.native_root / "workspace"
        self._client: CopilotClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._start_lock = asyncio.Lock()
        self._credential: AsyncTokenCredential | None = None
        self._filesystems: set[NativeSessionFs] = set()

    async def release_filesystem(self, provider: NativeSessionFs) -> None:
        await asyncio.wait_for(provider.close(), timeout=5)
        self._filesystems.discard(provider)

    async def client(self) -> CopilotClient:
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise CopilotPreviewError(
                "Copilot preview requires one event loop per worker. "
                "Await shutdown_client_manager() before closing a standalone event loop."
            )
        async with self._start_lock:
            if self._client is not None:
                return self._client
            self.workspace.mkdir(parents=True, exist_ok=True)
            from copilot import CopilotClient, RuntimeConnection

            client = CopilotClient(
                connection=RuntimeConnection.for_stdio(),
                mode="empty",
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
            try:
                await client.start()
            except BaseException:
                try:
                    await asyncio.wait_for(client.stop(), timeout=5)
                except Exception:
                    try:
                        await asyncio.wait_for(client.force_stop(), timeout=5)
                    except Exception:
                        logger.error("Copilot native startup cleanup failed.")
                raise
            self._client = client
            self._loop = loop
            atexit.register(self._exit)
            logger.info("Agent harness ready: harness=copilot transport=stdio")
            return client

    async def close(self) -> None:
        stopped = self._client is None
        failed = False
        try:
            if self._client is not None:
                try:
                    await asyncio.wait_for(self._client.stop(), timeout=10)
                    stopped = True
                except Exception:
                    logger.warning("Copilot graceful shutdown failed; forcing SDK shutdown.")
                    failed = True
                    try:
                        await asyncio.wait_for(self._client.force_stop(), timeout=5)
                        stopped = True
                    except Exception:
                        logger.error("Copilot forced SDK shutdown failed.")
        finally:
            if stopped:
                self._client = None
                self._loop = None
                atexit.unregister(self._exit)
            for provider in tuple(self._filesystems):
                try:
                    await self.release_filesystem(provider)
                except OSError:
                    logger.error("Copilot session filesystem cleanup failed.")
                    failed = True
            if self._credential is not None:
                try:
                    await self._credential.close()
                    self._credential = None
                except Exception:
                    logger.error("Copilot credential cleanup failed.")
                    failed = True
        if failed:
            raise CopilotPreviewError("Copilot preview shutdown did not complete cleanly.")

    def credential(self) -> AsyncTokenCredential:
        if self._credential is None:
            self._credential = build_async_credential()
        return self._credential

    async def _entra_token(self, scope: str, diagnostic: str) -> str:
        try:
            token = await self.credential().get_token(scope)
        except Exception:
            raise CopilotPreviewError(diagnostic) from None
        return token.token

    def bearer_token_provider(self, scope: str, diagnostic: str) -> Callable[[ProviderTokenArgs], Awaitable[str]]:
        self.credential()

        async def token(_args: ProviderTokenArgs) -> str:
            return await self._entra_token(scope, diagnostic)

        return token

    def _exit(self) -> None:
        """Best-effort SDK fallback when the host does not await shutdown."""
        if self._client is not None:
            try:
                asyncio.run(asyncio.wait_for(self._client.force_stop(), timeout=2))
            except Exception:
                logger.error("Copilot native process-exit cleanup failed.")


class _RequestTokenSource:
    def __init__(self, owner: _NativeRuntime) -> None:
        self._owner = owner
        self._error: CopilotPreviewError | None = None

    def bearer_token_provider(
        self, scope: str, diagnostic: str
    ) -> Callable[[ProviderTokenArgs], Awaitable[str]]:
        callback = self._owner.bearer_token_provider(scope, diagnostic)

        async def token(args: ProviderTokenArgs) -> str:
            try:
                return await callback(args)
            except CopilotPreviewError as error:
                self._error = error
                raise

        return token

    def take_error(self) -> CopilotPreviewError | None:
        error = self._error
        self._error = None
        return error


_RUNTIMES: dict[AppHarness, _NativeRuntime] = {}


def _runtime(harness: AppHarness) -> _NativeRuntime:
    if harness.name != HarnessKind.COPILOT or harness.storage_root is None:
        raise CopilotPreviewError("Copilot was not selected for this app.")
    owner = _RUNTIMES.get(harness)
    if owner is None:
        owner = _NativeRuntime(harness.storage_root)
        _RUNTIMES[harness] = owner
        _register_shutdown(shutdown)
    return owner


async def shutdown() -> None:
    failed = False
    for key, owner in list(_RUNTIMES.items()):
        try:
            await owner.close()
        except (CopilotPreviewError, OSError, RuntimeError):
            logger.error("Copilot runtime owner cleanup failed.")
            failed = True
        if owner._client is None and owner._credential is None and not owner._filesystems:
            _RUNTIMES.pop(key, None)
    if not _RUNTIMES:
        _unregister_shutdown(shutdown)
    if failed:
        raise CopilotPreviewError("Copilot preview shutdown failed for one or more workers.")


def _deny_permission(
    _request: PermissionRequest, _invocation: PermissionInvocation
) -> PermissionDecision:
    from copilot.generated.rpc import PermissionDecisionDeniedByRules

    return PermissionDecisionDeniedByRules(rules=[])


def _tool(function: FunctionTool, calls: list[dict[str, Any]]) -> Tool:
    from copilot.tools import Tool, ToolResult

    async def invoke(invocation: ToolInvocation) -> ToolResult:
        record: dict[str, Any] = {
            "type": "tool_start",
            "tool_call_id": invocation.tool_call_id,
            "tool_name": function.name,
            "arguments": invocation.arguments,
        }
        calls.append(record)
        try:
            contents = await function.invoke(
                arguments=invocation.arguments,
                tool_call_id=invocation.tool_call_id,
            )
            if not isinstance(contents, list) or any(
                getattr(item, "type", None) != "text" for item in contents
            ):
                raise CopilotPreviewError("Copilot preview tools must return text or JSON.")
            text = "\n".join(item.text or "" for item in contents)
        except Exception:
            logger.warning("Copilot custom tool failed: tool=%s", function.name)
            text = '{"error":"Custom tool failed or returned unsupported content."}'
            record["result"] = text
            return ToolResult(text_result_for_llm=text, result_type="failure")
        record["result"] = text
        return ToolResult(text_result_for_llm=text, result_type="success")

    return Tool(
        name=function.name,
        description=function.description,
        parameters=function.parameters(),
        handler=invoke,
        skip_permission=True,
        defer="never",
    )


def _provider(harness: AppHarness, tokens: ProviderTokenSource, model: str) -> ProviderConfig:
    if harness.provider is None:
        raise CopilotPreviewError("Copilot preview has no valid provider target.")
    try:
        return harness.provider.sdk_config(model, tokens)
    except CopilotPreviewError:
        raise
    except Exception:
        logger.error(
            "Copilot provider setup failed: provider=%s authentication=%s; "
            "underlying details were not logged.",
            harness.provider.kind,
            harness.provider.auth_label,
        )
        raise CopilotPreviewError(harness.provider.setup_diagnostic()) from None


async def _abort(session: CopilotSession) -> None:
    try:
        await asyncio.wait_for(session.abort(), timeout=5)
    except Exception:
        logger.error("Copilot session abort failed.")


@asynccontextmanager
async def _session_context(
    session: CopilotSession,
) -> AsyncIterator[CopilotSession]:
    """Bound SDK session detachment without stopping the shared client."""
    try:
        yield session
    finally:
        turn_failed = sys.exception() is not None
        try:
            await asyncio.wait_for(session.disconnect(), timeout=5)
        except Exception:
            logger.error("Copilot session detach failed.")
            if not turn_failed:
                raise CopilotPreviewError(
                    "Copilot native session could not be disconnected."
                ) from None


async def run(harness: AppHarness, request: HarnessRequest) -> AgentResult:
    validate_copilot_client_manager()
    if request.max_output_tokens is not None:
        raise UnsupportedCapabilityError(
            "Copilot preview cannot enforce max_output_tokens with the pinned SDK/runtime. "
            "Remove the cap or use the MAF harness."
        )
    from copilot.session import SystemMessageReplaceConfig, ToolSearchConfig
    from copilot.session_events import AssistantMessageData, AssistantUsageData

    from .runner import AgentResult, _AgentUsageRecorder, _session_lock_bounded_by

    owner = _runtime(harness)
    native_id = _native_id(request.agent_slug, request.session_id)
    calls: list[dict[str, Any]] = []
    messages: list[str] = []
    input_tokens: int | None = None
    output_tokens: int | None = None
    recorder = _AgentUsageRecorder(
        agent_name=request.agent_slug,
        execution_role="primary",
        inference_target=InferenceTarget(
            harness.provider.kind if harness.provider is not None else None,
            request.model,
        ),
    )
    invocation_started = False

    def on_event(event: SessionEvent) -> None:
        nonlocal input_tokens, output_tokens
        if not invocation_started:
            return
        match event.data:
            case AssistantMessageData(content=content) if content:
                messages.append(content)
            case AssistantUsageData(input_tokens=used_input, output_tokens=used_output):
                if used_input is not None:
                    input_tokens = (input_tokens or 0) + used_input
                if used_output is not None:
                    output_tokens = (output_tokens or 0) + used_output

    token_source = _RequestTokenSource(owner)
    try:
        provider = _provider(harness, token_source, request.model)
        async with _session_lock_bounded_by(
            request.session_id, request.deadline, agent_slug=request.agent_slug
        ), asyncio.timeout_at(request.deadline), AsyncExitStack() as cleanup:
            if harness.session_storage is None:
                raise CopilotPreviewError("Native session storage is not configured.")
            storage = await open_session_fs(
                harness.session_storage,
                request.agent_slug,
                request.session_id,
                workspace_path=str(owner.workspace),
                deadline=request.deadline,
            )
            owner._filesystems.add(storage)
            cleanup.push_async_callback(owner.release_filesystem, storage)
            client = await owner.client()
            options = _SessionOptions(
                model=request.model,
                tools=[_tool(function, calls) for function in request.tools],
                available_tools=[f"custom:{function.name}" for function in request.tools],
                system_message=SystemMessageReplaceConfig(
                    mode="replace", content=request.instructions or ""
                ),
                provider=provider,
                streaming=False,
                enable_config_discovery=False,
                enable_session_telemetry=False,
                request_extensions=False,
                tool_search=ToolSearchConfig(enabled=False),
                on_event=on_event,
                create_session_fs_handler=lambda _session: storage,
            )
            if request.new_session:
                session = await client.create_session(
                    session_id=native_id, on_permission_request=_deny_permission, **options
                )
            else:
                session = await client.resume_session(
                    native_id, on_permission_request=_deny_permission, **options
                )
            async with _session_context(session):
                metadata = await session.rpc.tools.get_current_metadata()
                if metadata.tools is None:
                    raise CopilotPreviewError("Copilot did not report its model-visible tool catalog.")
                actual_names = sorted(item.name for item in metadata.tools)
                expected_names = sorted(function.name for function in request.tools)
                if actual_names != expected_names:
                    raise CopilotPreviewError(
                        "Copilot model-visible tool catalog differs from the configured custom tools. "
                        "No prompt was sent."
                    )
                logger.info("Copilot tool catalog verified: custom_tool_count=%d", len(expected_names))
                logger.info(
                    "Copilot request target: provider=%s model=%s",
                    harness.provider.kind if harness.provider is not None else None,
                    request.model,
                )
                invocation_started = True
                replied = False
                try:
                    response = await session.send_and_wait(
                        request.prompt,
                        timeout=max(0.0, request.deadline - asyncio.get_running_loop().time()),
                    )
                    replied = True
                finally:
                    if not replied:
                        await _abort(session)
                token_error = token_source.take_error()
                if token_error is not None:
                    raise token_error
                match response.data if response is not None else None:
                    case AssistantMessageData(content=content) if content.strip():
                        pass
                    case _:
                        raise CopilotPreviewError(
                            "Copilot returned no final model reply. Check provider "
                            "authentication and model/deployment access."
                        )
            return AgentResult(
                session_id=request.session_id,
                content=content,
                content_intermediate=messages[:-1],
                tool_calls=calls,
            )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        raise CopilotPreviewError("Copilot request exceeded its deadline.") from None
    except (CopilotPreviewError, NativeSessionError):
        raise
    except Exception:
        token_error = token_source.take_error()
        if token_error is not None:
            raise token_error from None
        logger.error("Copilot preview execution failed; native details were not logged.")
        raise CopilotPreviewError(
            "Copilot preview failed. Check the SDK runtime, provider authentication, and "
            "session storage. No fallback was attempted."
        ) from None
    finally:
        if invocation_started:
            recorder.emit_counts(input_tokens=input_tokens, output_tokens=output_tokens)
