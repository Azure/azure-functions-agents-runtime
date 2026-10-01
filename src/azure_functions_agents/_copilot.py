"""Lazy Copilot SDK adapter for the explicitly limited local preview."""

from __future__ import annotations

import asyncio
import atexit
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

from ._credential import build_async_credential
from ._harness import (
    AppHarness,
    CopilotPreviewError,
    HarnessKind,
    HarnessRequest,
    UnsupportedCapabilityError,
    validate_copilot_client_manager,
)
from ._logger import logger
from .client_manager import InferenceTarget

if TYPE_CHECKING:
    from azure.core.credentials_async import AsyncTokenCredential
    from copilot import CopilotClient
    from copilot.generated.rpc import PermissionDecision
    from copilot.session import (
        CopilotSession,
        InfiniteSessionConfig,
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
    infinite_sessions: InfiniteSessionConfig
    tool_search: ToolSearchConfig
    on_event: Callable[[SessionEvent], None]


def _native_id(agent_slug: str, session_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"af-copilot:{agent_slug}:{session_id}"))


class _NativeRuntime:
    """One SDK-managed stdio client per app worker."""

    def __init__(self, root: Path) -> None:
        self.native_root = root / "native"
        self._client: CopilotClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._start_lock = asyncio.Lock()
        self._credential: AsyncTokenCredential | None = None

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
            self.native_root.mkdir(parents=True, exist_ok=True)
            from copilot import CopilotClient, RuntimeConnection

            client = CopilotClient(
                connection=RuntimeConnection.for_stdio(),
                mode="empty",
                base_directory=str(self.native_root),
                working_directory=str(self.native_root),
                use_logged_in_user=False,
                log_level="none",
                telemetry=None,
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


_RUNTIMES: dict[Path, _NativeRuntime] = {}


def _runtime(harness: AppHarness) -> _NativeRuntime:
    if harness.name != HarnessKind.COPILOT or harness.storage_root is None:
        raise CopilotPreviewError("Copilot was not selected for this app.")
    owner = _RUNTIMES.get(harness.storage_root)
    if owner is None:
        owner = _NativeRuntime(harness.storage_root)
        _RUNTIMES[harness.storage_root] = owner
    return owner


async def shutdown() -> None:
    failed = False
    for root, owner in list(_RUNTIMES.items()):
        try:
            await owner.close()
        except Exception:
            logger.error("Copilot runtime owner cleanup failed.")
            failed = True
        if owner._client is None and owner._credential is None:
            _RUNTIMES.pop(root, None)
    if failed:
        raise CopilotPreviewError("Copilot preview shutdown failed for one or more workers.")


def _deny_permission(
    _request: PermissionRequest, _invocation: PermissionInvocation
) -> PermissionDecision:
    from copilot.generated.rpc import PermissionDecisionDeniedByRules

    return PermissionDecisionDeniedByRules(rules=[])


def _tool(
    function: FunctionTool,
    calls: list[dict[str, Any]],
    event_sink: Callable[[dict[str, Any]], None] | None = None,
) -> Tool:
    from copilot.tools import Tool, ToolResult

    async def invoke(invocation: ToolInvocation) -> ToolResult:
        record: dict[str, Any] = {
            "type": "tool_start",
            "tool_call_id": invocation.tool_call_id,
            "tool_name": function.name,
            "arguments": invocation.arguments,
        }
        calls.append(record)
        if event_sink is not None:
            event_sink(dict(record))
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
            if event_sink is not None:
                event_sink({
                    "type": "tool_end", "tool_call_id": invocation.tool_call_id,
                    "tool_name": function.name, "result": text,
                })
            return ToolResult(text_result_for_llm=text, result_type="failure")
        record["result"] = text
        if event_sink is not None:
            event_sink({
                "type": "tool_end", "tool_call_id": invocation.tool_call_id,
                "tool_name": function.name, "result": text,
            })
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


def _completed_turn(events: list[SessionEvent]) -> bool:
    from copilot.session_events import SessionEventType, SessionIdleData

    has_user_message = False
    completed = False
    interrupted = False
    for event in events:
        if event.type == SessionEventType.USER_MESSAGE:
            has_user_message = True
            completed = False
            interrupted = False
        elif event.type == SessionEventType.ASSISTANT_TURN_START and has_user_message:
            completed = False
        elif event.type in {
            SessionEventType.ABORT,
            SessionEventType.AGENT_INTERRUPTED,
            SessionEventType.SESSION_ERROR,
        }:
            interrupted = True
        elif event.type == SessionEventType.SESSION_IDLE:
            match event.data:
                case SessionIdleData(aborted=True):
                    interrupted = True
        elif event.type == SessionEventType.ASSISTANT_TURN_END and has_user_message:
            completed = not interrupted
    return has_user_message and completed and not interrupted


async def _verify_completed_turn(session: CopilotSession) -> None:
    try:
        events = await session.get_events()
    except Exception:
        raise CopilotPreviewError(
            "Copilot native session history is missing, corrupt, or unavailable; "
            "no conversation was reset."
        ) from None
    if not _completed_turn(events):
        raise CopilotPreviewError(
            "Copilot native session has no verifiable completed turn. "
            "Interrupted or empty history cannot be continued; start a new conversation."
        )


async def _abort(session: CopilotSession) -> None:
    try:
        await asyncio.wait_for(session.abort(), timeout=5)
    except Exception:
        logger.error("Copilot session abort failed.")


@asynccontextmanager
async def _session_context(session: CopilotSession) -> AsyncIterator[CopilotSession]:
    """Bound SDK session detachment without stopping the shared client."""
    await session.__aenter__()
    turn_succeeded = False
    try:
        yield session
        turn_succeeded = True
    finally:
        try:
            await asyncio.wait_for(session.__aexit__(None, None, None), timeout=5)
        except Exception:
            logger.error("Copilot session detach failed.")
            if turn_succeeded:
                raise CopilotPreviewError(
                    "Copilot native session could not be detached; its state may be unfinished."
                ) from None


@asynccontextmanager
async def _leaf_cleanup(
    owner: _NativeRuntime, native_id: str, *, enabled: bool
) -> AsyncIterator[None]:
    """Remove SDK-owned leaf history even when its turn fails."""
    try:
        yield
    finally:
        if enabled and owner._client is not None:
            try:
                await asyncio.wait_for(owner._client.delete_session(native_id), timeout=5)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("Copilot leaf session deletion failed.")
                raise CopilotPreviewError(
                    "Copilot could not delete ephemeral specialist state."
                ) from None


async def run(harness: AppHarness, request: HarnessRequest) -> AgentResult:
    validate_copilot_client_manager()
    if request.max_output_tokens is not None:
        raise UnsupportedCapabilityError(
            "Copilot preview cannot enforce max_output_tokens with the pinned SDK/runtime. "
            "Remove the cap or use the MAF harness."
        )
    from copilot.session import InfiniteSessionConfig, SystemMessageReplaceConfig, ToolSearchConfig
    from copilot.session_events import (
        AssistantMessageData,
        AssistantMessageDeltaData,
        AssistantReasoningDeltaData,
        AssistantUsageData,
    )

    from .runner import AgentResult, _AgentUsageRecorder, _session_lock_bounded_by

    owner = _runtime(harness)
    leaf = request.execution_role != "primary"
    if leaf and not request.new_session:
        raise CopilotPreviewError("Copilot specialist sessions cannot be resumed.")
    # Leaf sessions are never resumed: unique native IDs need no persistent per-session lock.
    native_id = str(uuid.uuid4()) if leaf else _native_id(request.agent_slug, request.session_id)
    session_lock = (
        nullcontext()
        if leaf
        else _session_lock_bounded_by(native_id, request.deadline, agent_slug=request.agent_slug)
    )
    calls: list[dict[str, Any]] = []
    messages: list[str] = []
    input_tokens: int | None = None
    output_tokens: int | None = None
    recorder = _AgentUsageRecorder(
        agent_name=request.agent_slug,
        execution_role=request.execution_role,
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
        if request.event_sink is not None:
            match event.data:
                case AssistantMessageDeltaData(delta_content=delta, parent_tool_call_id=None):
                    if delta:
                        request.event_sink({"type": "delta", "content": delta})
                case AssistantReasoningDeltaData(delta_content=delta):
                    if delta:
                        request.event_sink({"type": "intermediate", "content": delta})
        match event.data:
            case AssistantMessageData(content=content, parent_tool_call_id=None) if content:
                messages.append(content)
            case AssistantUsageData(input_tokens=used_input, output_tokens=used_output):
                if used_input is not None:
                    input_tokens = (input_tokens or 0) + used_input
                if used_output is not None:
                    output_tokens = (output_tokens or 0) + used_output

    try:
        token_source = _RequestTokenSource(owner)
        provider = _provider(harness, token_source, request.model)
        async with session_lock, asyncio.timeout_at(request.deadline), _leaf_cleanup(
            owner, native_id, enabled=leaf
        ):
            client = await owner.client()
            tools = [_tool(function, calls, request.event_sink) for function in request.tools]
            options = _SessionOptions(
                model=request.model,
                tools=tools,
                available_tools=[f"custom:{function.name}" for function in request.tools],
                system_message=SystemMessageReplaceConfig(
                    mode="replace", content=request.instructions or ""
                ),
                provider=provider,
                streaming=request.event_sink is not None,
                enable_config_discovery=False,
                enable_session_telemetry=False,
                request_extensions=False,
                infinite_sessions=InfiniteSessionConfig(enabled=False),
                tool_search=ToolSearchConfig(enabled=False),
                on_event=on_event,
            )
            if request.new_session:
                try:
                    existing = await client.get_session_metadata(native_id)
                except Exception:
                    raise CopilotPreviewError(
                        "Copilot could not check native session state; refusing to reset it."
                    ) from None
                if existing is not None:
                    raise CopilotPreviewError(
                        "Copilot native session already exists; refusing to reset it."
                    )
                session = await client.create_session(
                    session_id=native_id, on_permission_request=_deny_permission, **options
                )
            else:
                try:
                    session = await client.resume_session(
                        native_id,
                        on_permission_request=_deny_permission,
                        continue_pending_work=False,
                        **options,
                    )
                except Exception:
                    raise CopilotPreviewError(
                        "Copilot could not resume this session. Native state may be missing, corrupt, "
                        "or unavailable; no replacement session was created."
                    ) from None
            async with _session_context(session):
                if not request.new_session:
                    await _verify_completed_turn(session)
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
                if request.event_sink is not None:
                    request.event_sink({"type": "session", "session_id": request.session_id})
                logger.info("Copilot tool catalog verified: custom_tool_count=%d", len(expected_names))
                logger.info(
                    "Copilot request target: provider=%s model=%s",
                    harness.provider.kind if harness.provider is not None else None,
                    request.model,
                )
                try:
                    invocation_started = True
                    try:
                        response = await session.send_and_wait(
                            request.prompt,
                            timeout=max(0.0, request.deadline - asyncio.get_running_loop().time()),
                        )
                    except BaseException:
                        token_error = token_source.take_error()
                        if token_error is not None:
                            raise token_error from None
                        raise
                    token_error = token_source.take_error()
                    if token_error is not None:
                        raise token_error
                    match response.data if response is not None else None:
                        case AssistantMessageData(content=content) if content.strip():
                            await _verify_completed_turn(session)
                        case _:
                            raise CopilotPreviewError(
                                "Copilot returned no final model reply. Check provider "
                                "authentication and model/deployment access."
                            )
                except BaseException:
                    if invocation_started:
                        await _abort(session)
                    raise
            return AgentResult(
                session_id=request.session_id,
                content=content,
                content_intermediate=messages[:-1],
                tool_calls=calls,
            )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        if leaf:
            raise TimeoutError("Copilot specialist timed out.") from None
        raise CopilotPreviewError(
            "Copilot preview request timed out during a turn. This session may be unfinished."
            if invocation_started
            else "Copilot preview timed out before starting this turn. "
            "Retry after the active session operation completes."
        ) from None
    except CopilotPreviewError:
        raise
    except Exception:
        logger.error("Copilot preview execution failed; native details were not logged.")
        raise CopilotPreviewError(
            "Copilot preview failed. Check the SDK runtime, provider authentication, and "
            "local storage. This conversation was not silently restarted."
        ) from None
    finally:
        if invocation_started:
            recorder.emit_counts(input_tokens=input_tokens, output_tokens=output_tokens)
