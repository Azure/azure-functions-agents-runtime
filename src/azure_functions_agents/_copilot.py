"""Lazy Copilot SDK adapter for the explicitly limited local preview."""

from __future__ import annotations

import asyncio
import atexit
import os
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

from ._credential import build_async_credential
from ._harness import (
    AppHarness,
    CopilotPreviewError,
    HarnessKind,
    HarnessRequest,
    ProviderKind,
    UnsupportedCapabilityError,
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

    async def foundry_token(self, _args: ProviderTokenArgs) -> str:
        if self._credential is None:
            self._credential = build_async_credential()
        try:
            token = await self._credential.get_token("https://ai.azure.com/.default")
        except Exception:
            raise CopilotPreviewError(
                "Copilot Foundry preview could not acquire an Entra token. "
                "Check the existing Azure sign-in and project access."
            ) from None
        return token.token

    def _exit(self) -> None:
        """Best-effort SDK fallback when the host does not await shutdown."""
        if self._client is not None:
            try:
                asyncio.run(asyncio.wait_for(self._client.force_stop(), timeout=2))
            except Exception:
                logger.error("Copilot native process-exit cleanup failed.")


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


def _token(_args: ProviderTokenArgs) -> str:
    token = os.environ.get("OPENAI_API_KEY", "").strip()
    if not token:
        raise CopilotPreviewError("Copilot OpenAI preview requires OPENAI_API_KEY in the host environment.")
    return token


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
            record["success"] = False
            return ToolResult(text_result_for_llm=text, result_type="failure")
        record["result"] = text
        record["success"] = True
        return ToolResult(text_result_for_llm=text, result_type="success")

    return Tool(
        name=function.name,
        description=function.description,
        parameters=function.parameters(),
        handler=invoke,
        skip_permission=True,
        defer="never",
    )


def _provider(harness: AppHarness, owner: _NativeRuntime) -> ProviderConfig:
    from copilot.session import ProviderConfig

    if harness.provider == ProviderKind.OPENAI:
        return ProviderConfig(
            type="openai",
            wire_api="completions",
            base_url="https://api.openai.com/v1",
            bearer_token_provider=_token,
        )
    if harness.provider == ProviderKind.FOUNDRY and harness.endpoint is not None:
        return ProviderConfig(
            type="openai",
            wire_api="responses",
            base_url=f"{harness.endpoint}/openai/v1",
            bearer_token_provider=owner.foundry_token,
        )
    raise CopilotPreviewError("Copilot preview has no valid provider target.")


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


async def run(harness: AppHarness, request: HarnessRequest) -> AgentResult:
    if request.max_output_tokens is not None:
        raise UnsupportedCapabilityError(
            "Copilot preview cannot enforce max_output_tokens with the pinned SDK/runtime. "
            "Remove the cap or use the MAF harness."
        )
    from copilot.session import InfiniteSessionConfig, SystemMessageReplaceConfig, ToolSearchConfig
    from copilot.session_events import AssistantMessageData, AssistantUsageData

    from .runner import AgentResult, _AgentUsageRecorder, _session_lock_bounded_by

    owner = _runtime(harness)
    provider = _provider(harness, owner)
    native_id = _native_id(request.agent_slug, request.session_id)
    calls: list[dict[str, Any]] = []
    messages: list[str] = []
    input_tokens: int | None = None
    output_tokens: int | None = None
    recorder = _AgentUsageRecorder(
        agent_name=request.agent_slug,
        execution_role="primary",
        inference_target=InferenceTarget(harness.provider, request.model),
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

    try:
        async with _session_lock_bounded_by(
            native_id, request.deadline, agent_slug=request.agent_slug
        ), asyncio.timeout_at(request.deadline):
            client = await owner.client()
            tools = [_tool(function, calls) for function in request.tools]
            options = _SessionOptions(
                model=request.model,
                tools=tools,
                available_tools=[f"custom:{function.name}" for function in request.tools],
                system_message=SystemMessageReplaceConfig(
                    mode="replace", content=request.instructions or ""
                ),
                provider=provider,
                streaming=False,
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
                logger.info("Copilot tool catalog verified: custom_tool_count=%d", len(expected_names))
                logger.info("Copilot request target: provider=%s model=%s", harness.provider, request.model)
                try:
                    invocation_started = True
                    response = await session.send_and_wait(
                        request.prompt,
                        timeout=max(0.0, request.deadline - asyncio.get_running_loop().time()),
                    )
                    match response.data if response is not None else None:
                        case AssistantMessageData(content=content) if content.strip():
                            await _verify_completed_turn(session)
                        case _:
                            raise CopilotPreviewError("Copilot returned no final model reply.")
                except BaseException:
                    if invocation_started:
                        await _abort(session)
                    raise
            return AgentResult(
                session_id=request.session_id,
                content=content,
                content_intermediate=messages[:-1],
                tool_calls=calls,
                model=request.model,
            )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
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
