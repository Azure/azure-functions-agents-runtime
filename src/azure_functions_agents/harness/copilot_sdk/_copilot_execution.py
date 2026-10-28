"""Per-request Copilot invocation, custom tools, results and failure diagnostics."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager, nullcontext
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, NotRequired, TypedDict

from ..._agent_run_diagnostics import _set_run_summary_attributes
from ..._logger import logger
from ..._observability import (
    ATTR_TOOL_OUTCOME,
    FaultDomain,
    RuntimeSpan,
    SpanOutcome,
    capture_current_context,
)
from ..._skill_policy import SkillPolicy
from ..._tool_descriptor import ToolDescriptor
from .. import _harness_execution
from .._harness_binding import AppHarness, HarnessRequest
from .._provider_config import InferenceTarget
from .._stream_events import ContentEvent, SessionEvent, StreamEvent
from ._copilot_capabilities import (
    available_tools,
    mcp_configuration,
    permission_handler,
)
from ._copilot_observability import CopilotEventTelemetry, invocation_span
from ._copilot_runtime import (
    _CLEANUP_TIMEOUT_SECONDS,
    CopilotPreviewError,
    CopilotRuntime,
    get_runtime,
)
from ._copilot_session_fs import open_session_fs
from ._copilot_session_identity import CopilotSessionError, StorageRoute, session_prefix
from ._copilot_session_local import LocalSessionFileBackend
from ._copilot_tool_calls import CopilotToolCalls, tool_result_text

if TYPE_CHECKING:
    from copilot import CopilotClient
    from copilot.session import (
        CopilotSession,
        CreateSessionFsHandler,
        InfiniteSessionConfig,
        MCPServerConfig,
        PermissionInvocation,
        PermissionRequestResult,
        ProviderConfig,
        ProviderTokenArgs,
        ReasoningEffort,
        ReasoningSummary,
        SystemMessageConfig,
        ToolSearchConfig,
    )
    from copilot.session_events import PermissionRequest
    from copilot.session_events import SessionEvent as CopilotSessionEvent
    from copilot.tools import Tool, ToolInvocation, ToolResult

    from ...runner import AgentResult, _DelegateErrorTracker
    from ._copilot_providers import ProviderTokenSource
    from ._copilot_session_fs import CopilotSessionFs


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
    working_directory: str
    mcp_servers: dict[str, MCPServerConfig]
    enable_skills: bool
    included_builtin_skills: list[str]
    skill_directories: list[str]
    disabled_skills: list[str]
    create_session_fs_handler: CreateSessionFsHandler
    infinite_sessions: NotRequired[InfiniteSessionConfig]


type _EventSink = Callable[[StreamEvent], None]

_SAFE_CUSTOM_TOOL_FAILURE_MESSAGE = "Custom tool failed or returned unsupported content."
_SAFE_CUSTOM_TOOL_FAILURE = f'{{"error":"{_SAFE_CUSTOM_TOOL_FAILURE_MESSAGE}"}}'


class _ExecutionOperation(StrEnum):
    PROVIDER_SETUP = "provider_setup"
    SESSION_STORAGE = "session_storage"
    STARTUP = "startup"
    SESSION_STATE = "session_state"
    CREATE_SESSION = "create_session"
    RESUME_SESSION = "resume_session"
    TOOL_CATALOG = "tool_catalog"
    SEND = "send"
    DISCONNECT = "disconnect"
    SESSION_CLEANUP = "session_cleanup"


def _emit_event(event_sink: _EventSink | None, event: StreamEvent | None) -> None:
    if event_sink is not None and event is not None:
        event_sink(event)


def _copilot_session_id(agent_slug: str, session_id: str) -> str:
    return f"{agent_slug}-{session_id}"


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


@dataclass(frozen=True)
class _ExecutionLifecycle:
    copilot_id: str
    session_storage: StorageRoute | None
    session_storage_id: str
    leaf: bool

    @classmethod
    def create(
        cls, owner: CopilotRuntime, request: HarnessRequest
    ) -> _ExecutionLifecycle:
        leaf = request.execution_role != "primary"
        if leaf and not request.new_session:
            raise CopilotPreviewError("Copilot specialist sessions cannot be resumed.")
        copilot_id = (
            str(uuid.uuid4())
            if leaf
            else _copilot_session_id(request.agent_slug, request.session_id)
        )
        return cls(
            copilot_id=copilot_id,
            session_storage=StorageRoute(owner.native_root / "ephemeral")
            if leaf
            else owner._runner.session_storage,
            session_storage_id=copilot_id if leaf else request.session_id,
            leaf=leaf,
        )

    def session_lock(self, request: HarnessRequest) -> AbstractAsyncContextManager[None]:
        if self.leaf:
            return nullcontext()
        return _harness_execution._session_lock_bounded_by(
            request.session_id, request.deadline, agent_slug=request.agent_slug
        )

    def leaf_cleanup(
        self, route: StorageRoute, agent_slug: str
    ) -> AbstractAsyncContextManager[None]:
        if not self.leaf:
            return nullcontext()
        return _bounded_cleanup(
            lambda: _delete_leaf(self.copilot_id, route, agent_slug),
            "Copilot could not delete ephemeral specialist state.",
        )

    def apply_session_options(self, options: _SessionOptions) -> None:
        if self.leaf:
            from copilot.session import InfiniteSessionConfig

            options["infinite_sessions"] = InfiniteSessionConfig(enabled=False)

    def timeout_error(self) -> TimeoutError | CopilotPreviewError:
        if self.leaf:
            return TimeoutError("Copilot specialist timed out.")
        return CopilotPreviewError("Copilot request exceeded its deadline.")


def _tool(
    descriptor: ToolDescriptor,
    calls: CopilotToolCalls,
    event_sink: _EventSink | None = None,
) -> Tool:
    from copilot.tools import Tool, ToolResult

    async def invoke(invocation: ToolInvocation) -> ToolResult:
        claimed = calls.start_custom(invocation)
        start_event = calls.start_event(invocation.tool_call_id)
        _emit_event(event_sink, start_event)
        with calls.custom_span(invocation, claimed=claimed) as span:
            try:
                result = await descriptor.invoke(
                    arguments=invocation.arguments,
                    tool_call_id=invocation.tool_call_id,
                )
                text = tool_result_text(result)
            except asyncio.CancelledError:
                span.set_attribute(ATTR_TOOL_OUTCOME, SpanOutcome.CANCELLED)
                raise
            except Exception as exc:
                logger.warning("Copilot custom tool failed: tool=%s", descriptor.name)
                text = _SAFE_CUSTOM_TOOL_FAILURE
                span.set_attribute(ATTR_TOOL_OUTCOME, SpanOutcome.ERROR)
                span.record_exception(
                    exc,
                    fault_domain=FaultDomain.APP,
                    infer_error_type=True,
                )
                calls.complete_custom(invocation.tool_call_id, text, success=False)
                _emit_event(event_sink, calls.end_event(invocation.tool_call_id))
                return ToolResult(text_result_for_llm=text, result_type="failure")
            span.set_attribute(ATTR_TOOL_OUTCOME, SpanOutcome.SUCCESS)
            calls.complete_custom(invocation.tool_call_id, text, success=True)
            _emit_event(event_sink, calls.end_event(invocation.tool_call_id))
            return ToolResult(text_result_for_llm=text, result_type="success")

    return Tool(
        name=descriptor.name,
        description=descriptor.description,
        parameters=descriptor.parameters(),
        handler=invoke,
        skip_permission=True,
        defer="never",
    )


def _provider(
    harness: AppHarness,
    tokens: ProviderTokenSource,
    model: str,
    *,
    max_output_tokens: int | None = None,
) -> ProviderConfig:
    provider = get_runtime(harness)._runner.provider
    try:
        config = provider.sdk_config(
            model,
            tokens,
            max_output_tokens=max_output_tokens,
        )
    except CopilotPreviewError:
        raise
    except Exception:
        logger.error(
            "Copilot provider setup failed: provider=%s authentication=%s; "
            "underlying details were not logged.",
            provider.kind,
            provider.auth_label,
        )
        raise CopilotPreviewError(provider.setup_diagnostic()) from None
    return config


async def _abort(session: CopilotSession) -> None:
    try:
        await asyncio.wait_for(session.abort(), timeout=_CLEANUP_TIMEOUT_SECONDS)
    except BaseException:
        logger.exception("Copilot session abort failed.")


async def _close_storage(storage: CopilotSessionFs) -> None:
    await asyncio.wait_for(storage.close(), timeout=_CLEANUP_TIMEOUT_SECONDS)


async def _delete_leaf(
    session_id: str, route: StorageRoute, agent_slug: str
) -> None:
    # SDK session.delete does not use the SessionFs callback route.
    parent = LocalSessionFileBackend(route.local_dir)
    await parent.rm(session_prefix(route, agent_slug, session_id), recursive=True, force=True)


@asynccontextmanager
async def _bounded_cleanup(
    operation: Callable[[], Awaitable[None]], diagnostic: str
) -> AsyncIterator[None]:
    """Keep an active failure or cancellation authoritative over cleanup failures."""
    try:
        yield
    except BaseException:
        try:
            await asyncio.wait_for(operation(), timeout=_CLEANUP_TIMEOUT_SECONDS)
        except BaseException:
            logger.exception("%s", diagnostic)
        raise
    else:
        try:
            await asyncio.wait_for(operation(), timeout=_CLEANUP_TIMEOUT_SECONDS)
        except TimeoutError as error:
            raise CopilotPreviewError(diagnostic) from error


@contextmanager
def _event_subscription(
    session: CopilotSession, on_event: Callable[[CopilotSessionEvent], None]
) -> Iterator[None]:
    unsubscribe = session.on(on_event)
    try:
        yield
    except BaseException:
        try:
            unsubscribe()
        except BaseException:
            logger.exception("Copilot session observer cleanup failed.")
        raise
    else:
        unsubscribe()


async def _open_session(
    client: CopilotClient,
    session_id: str,
    *,
    new_session: bool,
    options: _SessionOptions,
    reasoning_effort: ReasoningEffort | None,
    reasoning_summary: ReasoningSummary | None,
    on_permission_request: Callable[
        [PermissionRequest, PermissionInvocation], PermissionRequestResult
    ],
) -> CopilotSession:
    if new_session:
        return await client.create_session(
            session_id=session_id,
            on_permission_request=on_permission_request,
            reasoning_effort=reasoning_effort,
            reasoning_summary=reasoning_summary,
            **options,
        )
    return await client.resume_session(
        session_id,
        on_permission_request=on_permission_request,
        continue_pending_work=False,
        reasoning_effort=reasoning_effort,
        reasoning_summary=reasoning_summary,
        **options,
    )


async def _acquire_client(owner: CopilotRuntime, deadline: float) -> CopilotClient:
    try:
        return await owner.client()
    except TimeoutError:
        if asyncio.get_running_loop().time() >= deadline:
            raise
        raise CopilotPreviewError("Copilot native runtime startup timed out.") from None


async def _verify_tool_catalog(
    session: CopilotSession, functions: list[Tool]
) -> None:
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
) -> CopilotSessionEvent | None:
    try:
        return await session.send_and_wait(
            prompt=prompt,
            timeout=max(0.0, deadline - asyncio.get_running_loop().time()),
        )
    except BaseException:
        await _abort(session)
        raise


def _final_reply_content(response: CopilotSessionEvent | None) -> str:
    from copilot.session_events import AssistantMessageData

    match response.data if response is not None else None:
        case AssistantMessageData(content=content) if content.strip():
            return content
        case _:
            raise CopilotPreviewError(
                "Copilot returned no final model reply. Check provider "
                "authentication and model/deployment access."
            )


async def _finalize_turn_response(
    session: CopilotSession,
    response: CopilotSessionEvent | None,
    *,
    interrupted: bool,
    token_source: _RequestTokenSource,
) -> str:
    try:
        token_error = token_source.take_error()
        if token_error is not None:
            raise token_error
        if interrupted:
            raise CopilotPreviewError(
                "Copilot interrupted the turn before producing a usable final reply."
            )
        return _final_reply_content(response)
    except BaseException:
        await _abort(session)
        raise


async def _run_session_turn(
    session: CopilotSession,
    *,
    prompt: str,
    deadline: float,
    on_event: Callable[[CopilotSessionEvent], None],
    recorder: _harness_execution._AgentUsageRecorder,
    get_usage: Callable[[], tuple[int | None, int | None]],
    token_source: _RequestTokenSource,
) -> str:
    from copilot.session_events import AbortData, AgentInterruptedData, SessionIdleData

    interrupted = False

    def observe(event: CopilotSessionEvent) -> None:
        nonlocal interrupted
        match event.data:
            case AbortData() | AgentInterruptedData():
                interrupted = True
            case SessionIdleData(aborted=True):
                interrupted = True
        on_event(event)

    with _event_subscription(session, observe):
        try:
            response = await _send_turn(session, prompt=prompt, deadline=deadline)
            return await _finalize_turn_response(
                session,
                response,
                interrupted=interrupted,
                token_source=token_source,
            )
        finally:
            input_tokens, output_tokens = get_usage()
            recorder.emit_counts(input_tokens=input_tokens, output_tokens=output_tokens)


async def run(
    harness: AppHarness,
    request: HarnessRequest,
    *,
    delegate_tracker: _DelegateErrorTracker | None = None,
) -> AgentResult:
    with invocation_span(request) as run_span:
        result = await _run(harness, request, run_span)
        result.delegate_error_count = delegate_tracker.count if delegate_tracker else 0
        _set_run_summary_attributes(run_span, result)
        return result


async def _run(
    harness: AppHarness, request: HarnessRequest, run_span: RuntimeSpan
) -> AgentResult:
    from copilot.session import SystemMessageReplaceConfig, ToolSearchConfig
    from copilot.session_events import (
        AssistantMessageData,
        AssistantMessageDeltaData,
        AssistantReasoningDeltaData,
        AssistantUsageData,
        SkillInvokedData,
        ToolExecutionCompleteData,
        ToolExecutionStartData,
    )

    from ...runner import AgentResult

    owner = get_runtime(harness)
    lifecycle = _ExecutionLifecycle.create(owner, request)
    calls = CopilotToolCalls(
        parent_context=capture_current_context(),
        run_span=run_span,
        custom_tool_names=tuple(function.name for function in request.tools),
        agent_name=request.agent_slug,
        session_id=request.session_id,
    )
    telemetry = CopilotEventTelemetry(run_span)
    messages: list[str] = []
    input_tokens: int | None = None
    output_tokens: int | None = None
    recorder = _harness_execution._AgentUsageRecorder(
        agent_name=request.agent_slug,
        execution_role=request.execution_role,
        inference_target=InferenceTarget(
            owner._runner.provider.kind,
            request.model,
        ),
    )

    def on_event(event: CopilotSessionEvent) -> None:
        nonlocal input_tokens, output_tokens
        match event.data:
            case AssistantMessageDeltaData(delta_content=delta, parent_tool_call_id=None) if delta:
                _emit_event(request.event_sink, ContentEvent(type="delta", content=delta))
            case AssistantReasoningDeltaData(delta_content=delta) if delta:
                _emit_event(
                    request.event_sink,
                    ContentEvent(type="intermediate", content=delta),
                )
        match event.data:
            case AssistantMessageData(content=content, parent_tool_call_id=None) if content:
                messages.append(content)
            case AssistantUsageData(input_tokens=used_input, output_tokens=used_output):
                telemetry.observe_usage(event.id, event.data)
                if used_input is not None:
                    input_tokens = (input_tokens or 0) + used_input
                if used_output is not None:
                    output_tokens = (output_tokens or 0) + used_output
            case SkillInvokedData(name=name):
                telemetry.observe_skill(name)
            case ToolExecutionStartData() as started:
                calls.start_native(
                    started,
                    event_id=event.id,
                    timestamp=event.timestamp,
                )
                if not calls.is_custom(started.tool_call_id):
                    start_event = calls.start_event(started.tool_call_id)
                    _emit_event(request.event_sink, start_event)
            case ToolExecutionCompleteData() as completed:
                calls.complete_native(
                    completed,
                    event_id=event.id,
                    timestamp=event.timestamp,
                )
                if not calls.is_custom(completed.tool_call_id):
                    start_event = calls.start_event(
                        completed.tool_call_id, synthesize_arguments=True
                    )
                    _emit_event(request.event_sink, start_event)
                    _emit_event(request.event_sink, calls.end_event(completed.tool_call_id))

    sdk_tools = [_tool(function, calls, request.event_sink) for function in request.tools]
    token_source = _RequestTokenSource(owner)
    operation = _ExecutionOperation.PROVIDER_SETUP
    try:
        provider = _provider(
            harness,
            token_source,
            request.model,
            max_output_tokens=request.max_output_tokens,
        )
        async with lifecycle.session_lock(request), asyncio.timeout_at(request.deadline):
            route = lifecycle.session_storage
            if route is None:
                raise CopilotPreviewError("Native session storage is not configured.")
            skill_policy = SkillPolicy.create(
                approved=request.skills,
                discovered=request.skill_catalog,
                working_directory=owner.workspace,
            )
            operation = _ExecutionOperation.SESSION_STORAGE
            storage = await open_session_fs(
                route,
                request.agent_slug,
                lifecycle.session_storage_id,
                workspace_path=str(owner.workspace),
                deadline=request.deadline,
                skill_policy=skill_policy,
            )
            storage_cleanup = _bounded_cleanup(
                lambda: _close_storage(storage),
                "Copilot session storage could not be closed.",
            )
            leaf_cleanup = lifecycle.leaf_cleanup(route, request.agent_slug)
            async with storage_cleanup, leaf_cleanup:
                mcp_servers = await mcp_configuration(
                    request.mcp_servers, protect_headers=calls.protect_headers
                )
                on_permission_request = permission_handler(skill_policy)
                operation = _ExecutionOperation.STARTUP
                client = await _acquire_client(owner, request.deadline)
                options = _SessionOptions(
                    model=request.model,
                    tools=sdk_tools,
                    available_tools=available_tools(request),
                    system_message=SystemMessageReplaceConfig(
                        mode="replace", content=request.instructions or ""
                    ),
                    provider=provider,
                    streaming=request.event_sink is not None,
                    enable_config_discovery=False,
                    enable_session_telemetry=False,
                    request_extensions=False,
                    tool_search=ToolSearchConfig(enabled=False),
                    working_directory=str(owner.workspace),
                    mcp_servers=mcp_servers,
                    enable_skills=bool(request.skills),
                    included_builtin_skills=[],
                    skill_directories=[str(skill.path) for skill in request.skills],
                    disabled_skills=list(skill_policy.disabled_names),
                    create_session_fs_handler=lambda _session: storage,
                )
                lifecycle.apply_session_options(options)
                new_session = request.new_session
                if not new_session and not lifecycle.leaf:
                    operation = _ExecutionOperation.SESSION_STATE
                    new_session = not await storage.has_persisted_state()
                operation = (
                    _ExecutionOperation.CREATE_SESSION
                    if new_session
                    else _ExecutionOperation.RESUME_SESSION
                )
                session = await _open_session(
                    client,
                    lifecycle.copilot_id,
                    new_session=new_session,
                    options=options,
                    on_permission_request=on_permission_request,
                    reasoning_effort=owner._runner.reasoning_effort,
                    reasoning_summary=owner._runner.reasoning_summary,
                )
                request.session_state.resumable = True
                async with _bounded_cleanup(
                    session.disconnect, "Copilot native session could not be disconnected."
                ):
                    operation = _ExecutionOperation.TOOL_CATALOG
                    if not request.mcp_servers and not request.skills:
                        await _verify_tool_catalog(session, sdk_tools)
                    _emit_event(
                        request.event_sink,
                        SessionEvent(type="session", session_id=request.session_id),
                    )
                    logger.info(
                        "Copilot request target: provider=%s model=%s",
                        owner._runner.provider.kind,
                        request.model,
                    )
                    operation = _ExecutionOperation.SEND
                    content = await _run_session_turn(
                        session,
                        prompt=request.prompt,
                        deadline=request.deadline,
                        on_event=on_event,
                        recorder=recorder,
                        get_usage=lambda: (input_tokens, output_tokens),
                        token_source=token_source,
                    )
                    operation = _ExecutionOperation.DISCONNECT
                operation = _ExecutionOperation.SESSION_CLEANUP
                return AgentResult(
                    session_id=request.session_id,
                    content=content,
                    content_intermediate=messages[:-1],
                    tool_calls=calls.calls,
                    model=request.model,
                )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        token_error = token_source.take_error()
        if token_error is not None:
            raise token_error from None
        raise lifecycle.timeout_error() from None
    except (CopilotPreviewError, CopilotSessionError):
        raise
    except Exception:
        token_error = token_source.take_error()
        if token_error is not None:
            raise token_error from None
        logger.exception("Copilot execution failed: operation=%s", operation.value)
        raise
    finally:
        calls.finish_telemetry()
