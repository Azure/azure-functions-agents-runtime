"""Per-request Copilot invocation, custom tools, results and safe errors."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import TYPE_CHECKING, TypedDict

from ..._logger import logger
from ...client_manager import InferenceTarget
from .. import _harness_execution
from .._harness_binding import AppHarness, HarnessRequest, UnsupportedCapabilityError
from ._copilot_preview import CopilotPreviewError, validate_copilot_client_manager
from ._copilot_runtime import CopilotRuntime, get_runtime
from ._copilot_session_fs import open_session_fs
from ._copilot_session_identity import CopilotSessionError

if TYPE_CHECKING:
    from agent_framework import Content
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

    from ..._function_tool import FunctionTool
    from ...runner import AgentResult, ToolCallEvidence
    from ._copilot_providers import ProviderTokenSource


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
    create_session_fs_handler: CreateSessionFsHandler


def _copilot_session_id(agent_slug: str, session_id: str) -> str:
    return f"{agent_slug}.{session_id}"


class _RequestTokenSource:
    def __init__(self, owner: CopilotRuntime) -> None:
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


def _deny_permission(
    _request: PermissionRequest, _invocation: PermissionInvocation
) -> PermissionDecision:
    from copilot.generated.rpc import PermissionDecisionDeniedByRules

    return PermissionDecisionDeniedByRules(rules=[])


def _tool(function: FunctionTool, calls: list[ToolCallEvidence]) -> Tool:
    from copilot.tools import Tool, ToolResult

    async def invoke(invocation: ToolInvocation) -> ToolResult:
        record: ToolCallEvidence = {
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
            text = _tool_result_text(contents)
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


def _tool_result_text(contents: list[Content]) -> str:
    if any(item.type != "text" for item in contents):
        raise CopilotPreviewError("Copilot preview tools must return text or JSON.")
    return "\n".join(item.text or "" for item in contents)


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
    except BaseException:
        logger.error("Copilot session abort failed.")


@asynccontextmanager
async def _bounded_cleanup(
    operation: Callable[[], Awaitable[None]], diagnostic: str
) -> AsyncIterator[None]:
    """Keep an active failure or cancellation authoritative over cleanup failures."""
    try:
        yield
    except BaseException:
        try:
            await asyncio.wait_for(operation(), timeout=5)
        except BaseException:
            logger.error("%s", diagnostic)
        raise
    else:
        try:
            await asyncio.wait_for(operation(), timeout=5)
        except Exception:
            logger.error("%s", diagnostic)
            raise CopilotPreviewError(diagnostic) from None


@contextmanager
def _event_subscription(
    session: CopilotSession, on_event: Callable[[SessionEvent], None]
) -> Iterator[None]:
    unsubscribe = session.on(on_event)
    try:
        yield
    except BaseException:
        try:
            unsubscribe()
        except BaseException:
            logger.error("Copilot session observer cleanup failed.")
        raise
    else:
        unsubscribe()


async def _open_session(
    client: CopilotClient,
    session_id: str,
    *,
    new_session: bool,
    options: _SessionOptions,
) -> CopilotSession:
    if new_session:
        return await client.create_session(
            session_id=session_id, on_permission_request=_deny_permission, **options
        )
    return await client.resume_session(
        session_id, on_permission_request=_deny_permission, **options
    )


async def _verify_tool_catalog(session: CopilotSession, functions: list[FunctionTool]) -> None:
    metadata = await session.rpc.tools.get_current_metadata()
    if metadata.tools is None:
        raise CopilotPreviewError("Copilot did not report its model-visible tool catalog.")
    actual_names = sorted(item.name for item in metadata.tools)
    expected_names = sorted(function.name for function in functions)
    if actual_names != expected_names:
        raise CopilotPreviewError(
            "Copilot model-visible tool catalog differs from the configured custom tools. "
            "No prompt was sent."
        )
    logger.info("Copilot tool catalog verified: custom_tool_count=%d", len(expected_names))


async def _send_turn(
    session: CopilotSession,
    *,
    prompt: str,
    deadline: float,
) -> SessionEvent | None:
    try:
        return await session.send_and_wait(
            prompt,
            timeout=max(0.0, deadline - asyncio.get_running_loop().time()),
        )
    except BaseException:
        await _abort(session)
        raise


def _final_reply_content(response: SessionEvent | None) -> str:
    from copilot.session_events import AssistantMessageData

    match response.data if response is not None else None:
        case AssistantMessageData(content=content) if content.strip():
            return content
        case _:
            raise CopilotPreviewError(
                "Copilot returned no final model reply. Check provider "
                "authentication and model/deployment access."
            )


async def _run_session_turn(
    session: CopilotSession,
    *,
    prompt: str,
    deadline: float,
    on_event: Callable[[SessionEvent], None],
    recorder: _harness_execution._AgentUsageRecorder,
    get_usage: Callable[[], tuple[int | None, int | None]],
    token_source: _RequestTokenSource,
) -> str:
    with _event_subscription(session, on_event):
        try:
            response = await _send_turn(session, prompt=prompt, deadline=deadline)
            token_error = token_source.take_error()
            if token_error is not None:
                raise token_error
            return _final_reply_content(response)
        finally:
            input_tokens, output_tokens = get_usage()
            recorder.emit_counts(input_tokens=input_tokens, output_tokens=output_tokens)


async def run(harness: AppHarness, request: HarnessRequest) -> AgentResult:
    validate_copilot_client_manager()
    if request.max_output_tokens is not None:
        raise UnsupportedCapabilityError(
            "Copilot preview cannot enforce max_output_tokens with the pinned SDK/runtime. "
            "Remove the cap or use the MAF harness."
        )
    from copilot.session import SystemMessageReplaceConfig, ToolSearchConfig
    from copilot.session_events import AssistantMessageData, AssistantUsageData

    from ...runner import AgentResult

    owner = get_runtime(harness)
    copilot_id = _copilot_session_id(request.agent_slug, request.session_id)
    calls: list[ToolCallEvidence] = []
    messages: list[str] = []
    input_tokens: int | None = None
    output_tokens: int | None = None
    recorder = _harness_execution._AgentUsageRecorder(
        agent_name=request.agent_slug,
        execution_role="primary",
        inference_target=InferenceTarget(
            harness.provider.kind if harness.provider is not None else None,
            request.model,
        ),
    )

    def on_event(event: SessionEvent) -> None:
        nonlocal input_tokens, output_tokens
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
        async with _harness_execution._session_lock_bounded_by(
            request.session_id, request.deadline, agent_slug=request.agent_slug
        ), asyncio.timeout_at(request.deadline):
            if harness.session_storage is None:
                raise CopilotPreviewError("Native session storage is not configured.")
            storage = await open_session_fs(
                harness.session_storage,
                request.agent_slug,
                request.session_id,
                workspace_path=str(owner.workspace),
                deadline=request.deadline,
            )
            owner.own_filesystem(storage)
            async with _bounded_cleanup(
                lambda: owner.release_filesystem(storage),
                "Copilot session storage could not be closed.",
            ):
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
                    create_session_fs_handler=lambda _session: storage,
                )
                session = await _open_session(
                    client, copilot_id, new_session=request.new_session, options=options
                )
                async with _bounded_cleanup(
                    session.disconnect, "Copilot native session could not be disconnected."
                ):
                    await _verify_tool_catalog(session, request.tools)
                    logger.info(
                        "Copilot request target: provider=%s model=%s",
                        harness.provider.kind if harness.provider is not None else None,
                        request.model,
                    )
                    content = await _run_session_turn(
                        session,
                        prompt=request.prompt,
                        deadline=request.deadline,
                        on_event=on_event,
                        recorder=recorder,
                        get_usage=lambda: (input_tokens, output_tokens),
                        token_source=token_source,
                    )
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
        token_error = token_source.take_error()
        if token_error is not None:
            raise token_error from None
        raise CopilotPreviewError("Copilot request exceeded its deadline.") from None
    except (CopilotPreviewError, CopilotSessionError):
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
