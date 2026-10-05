"""Microsoft Agent Framework construction, invocation, and event interpretation."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

from azure_functions_agents import runner as _runner

from ..._agent_identity import agent_id
from ..._logger import logger
from ..._observability import FaultDomain, LifecycleStage
from ..._tool_descriptor import ToolDescriptor, ToolInput, describe_tools
from ...client_manager import InferenceTarget, get_client_manager
from ...config import ResolvedAgent, SubagentRef
from ...config.env import EnvVar, runtime_env_value
from ...config.paths import get_app_root
from ...config.paths import resolve_config_dir as resolve_config_dir
from ...config.schema import AgentConfiguration
from ...discovery.mcp import MCPServerDescriptor
from ...registration._handlers import _looks_like_tool_error
from ...registration.capabilities import AgentCapabilities
from ...registration.catalog import AgentCatalog
from .. import _harness_execution
from .._harness_binding import AppHarness, HarnessRequest
from .._history_identity import validate_agent_slug
from ._maf_tools import FunctionTool, build_maf_tools

if TYPE_CHECKING:
    from agent_framework import (
        Agent,
        AgentResponse,
        Content,
        HistoryProvider,
        MCPStreamableHTTPTool,
        Message,
        RoleLiteral,
        SupportsChatGetResponse,
    )

    from azure_functions_agents.runner import ToolCallEvidence

    from ...workflows.schema import WorkflowPlanPolicy

type AgentFunctionTool = ToolInput
type AgentTool = FunctionTool | MCPStreamableHTTPTool

_FINAL_USAGE_TIMEOUT_SECONDS = 1.0
_ASSISTANT_ROLE: Final[RoleLiteral] = "assistant"


def _response_usage_details(response: Any) -> Any:
    try:
        return getattr(response, "usage_details", None)
    except Exception:
        return None


async def _stream_usage_details(stream: Any, *, remaining_timeout: float) -> Any:
    try:
        get_final_response = getattr(stream, "get_final_response", None)
        if not callable(get_final_response) or remaining_timeout <= 0:
            return None
        response = await asyncio.wait_for(
            get_final_response(),
            timeout=min(remaining_timeout, _FINAL_USAGE_TIMEOUT_SECONDS),
        )
        return _response_usage_details(response)
    except Exception:
        return None


def _resolve_sessions_dir(agent_slug: str) -> Path:
    """Resolve and create the existing agent-scoped local history directory."""
    slug = validate_agent_slug(agent_slug)
    base = Path(_runner.resolve_config_dir()).resolve() / "agent-sessions" / slug
    base.mkdir(parents=True, exist_ok=True)
    return base


def _build_history_provider(agent_slug: str) -> Any:
    """Choose MAF Blob history when configured, otherwise scoped local JSONL."""
    from ._maf_blob_history import build_blob_provider_from_environment

    blob_provider = build_blob_provider_from_environment(agent_slug=agent_slug)
    if blob_provider is not None:
        return blob_provider

    from ._maf_file_history import ScopedFileHistoryProvider

    scoped_dir = _resolve_sessions_dir(agent_slug)
    return ScopedFileHistoryProvider(
        storage_root=scoped_dir.parent,
        agent_slug=agent_slug,
    )


def _build_chat_options_from_environment() -> dict[str, Any] | None:
    """Build provider chat options from supported runtime environment variables."""
    reasoning: dict[str, str] = {}
    effort = runtime_env_value(EnvVar.REASONING_EFFORT)
    if effort:
        reasoning["effort"] = effort
    summary = runtime_env_value(EnvVar.REASONING_SUMMARY)
    if summary:
        reasoning["summary"] = summary
    if not reasoning:
        return None
    return {"reasoning": reasoning}


def _assemble_agent_inputs(
    *,
    instructions: str | None,
    tools: Sequence[ToolInput] | None,
    mcp_tools: Sequence[MCPServerDescriptor] | None,
    sandbox_tools: Sequence[ToolInput] | None,
    web_request_tools: Sequence[ToolInput] | None,
    system_addendum: str | None,
    workflow_enabled: bool,
    workflow_durable_client: Any | None,
    workflow_agent_slug: str | None,
    agent_name: str | None,
    resolved_id: str | None,
    delegate_tools: Sequence[ToolInput] | None,
    workflow_policy: WorkflowPlanPolicy | None,
    app_root: Path | None = None,
) -> tuple[list[AgentTool], str | None]:
    root = app_root or get_app_root()
    descriptors = [
        *describe_tools(_runner.discover_user_tools(root).tools if tools is None else tools),
        *describe_tools(sandbox_tools or ()),
        *describe_tools(web_request_tools or ()),
    ]
    if workflow_enabled:
        from ...workflows.tools import build_workflow_tools

        descriptors.extend(
            build_workflow_tools(
                session_id=resolved_id or "",
                workflow_agent_slug=workflow_agent_slug or agent_name or "main",
                agent_name=agent_name or "main",
                durable_client=workflow_durable_client,
                policy=workflow_policy,
            )
        )
    resolved_tools: list[AgentTool] = list(build_maf_tools(descriptors))
    servers = (
        tuple(_runner.discover_mcp_servers(root).servers.values())
        if mcp_tools is None
        else tuple(mcp_tools)
    )
    if servers:
        from ._maf_mcp import build_maf_mcp_tools

        resolved_tools.extend(build_maf_mcp_tools(servers))
    if delegate_tools:
        resolved_tools.extend(build_maf_tools(describe_tools(delegate_tools)))
    effective = instructions.strip() if instructions and instructions.strip() else None
    if system_addendum:
        effective = (effective or "") + system_addendum
    return resolved_tools, effective


def _build_role_agent(
    chat_client: SupportsChatGetResponse[Any],
    *,
    agent_instructions: str | None,
    tools: Sequence[AgentTool | ToolDescriptor],
    skill_paths: Sequence[Path] | None,
    agent_name: str | None,
    history_provider: HistoryProvider | None,
    agent_configuration: AgentConfiguration,
) -> Agent[Any]:
    """Build one conservatively configured MAF harness agent for any role."""
    import warnings

    from agent_framework import SkillsProvider, create_harness_agent
    from agent_framework._feature_stage import ExperimentalWarning

    adapted_tools = [
        build_maf_tools((candidate,))[0] if isinstance(candidate, ToolDescriptor) else candidate
        for candidate in tools
    ]
    skills_provider = (
        SkillsProvider.from_paths(
            list(skill_paths),
            disable_load_skill_approval=True,
            disable_read_skill_resource_approval=True,
            disable_run_skill_script_approval=True,
        )
        if skill_paths
        else None
    )
    site_name = runtime_env_value(EnvVar.WEBSITE_SITE_NAME)
    maf_agent_name = f"{site_name}/{agent_name or 'main'}" if site_name else agent_name

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=ExperimentalWarning)
        return create_harness_agent(
            chat_client,
            id=agent_id(agent_name or "main"),
            name=maf_agent_name,
            harness_instructions="",
            agent_instructions=agent_instructions,
            tools=adapted_tools,
            history_provider=history_provider,
            skills_provider=skills_provider,
            disable_tool_auto_approval=True,
            disable_web_search=True,
            disable_todo=True,
            disable_mode=True,
            max_context_window_tokens=_max_context_window_tokens(agent_configuration),
            max_output_tokens=agent_configuration.max_output_tokens,
            disable_file_memory=True,
            default_options={"store": False},
        )


def _max_context_window_tokens(config: AgentConfiguration) -> int | None:
    if config.agent_framework is None or config.agent_framework.compaction is None:
        return None
    return config.agent_framework.compaction.max_context_window_tokens


def _build_delegated_agent(
    resolved: ResolvedAgent, capabilities: AgentCapabilities
) -> tuple[Agent[Any], InferenceTarget]:
    """Build a stateless specialist without expanding its own subagents."""
    client_manager = get_client_manager()
    chat_client, inference_target = client_manager.build_chat_client_with_target(resolved.model)
    resolved_tools, effective_instructions = _runner._assemble_agent_inputs(
        instructions=resolved.instructions,
        tools=list(capabilities.filtered_user_tools or []),
        mcp_tools=list(capabilities.filtered_mcp_tools or []),
        sandbox_tools=None,
        web_request_tools=capabilities.web_request_tools,
        system_addendum=None,
        workflow_enabled=False,
        workflow_durable_client=None,
        workflow_agent_slug=None,
        agent_name=resolved.slug,
        resolved_id=None,
        delegate_tools=None,
        workflow_policy=None,
        app_root=capabilities._harness.app_root if capabilities._harness is not None else None,
    )
    agent = _runner._build_role_agent(
        chat_client,
        agent_instructions=effective_instructions,
        tools=resolved_tools,
        skill_paths=capabilities.enabled_skill_paths,
        agent_name=resolved.slug,
        history_provider=None,
        agent_configuration=resolved.agent_configuration,
    )
    return agent, inference_target


async def run_leaf_agent_task(
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    task: str,
    *,
    timeout: float,
    execution_role: Literal["delegate", "workflow_subagent"],
) -> str:
    """Run one fresh stateless MAF specialist and return its response text."""
    specialist_agent, inference_target = _runner._build_delegated_agent(resolved, capabilities)
    usage_recorder = _harness_execution._AgentUsageRecorder(
        agent_name=resolved.slug,
        execution_role=execution_role,
        inference_target=inference_target,
    )
    try:
        response: AgentResponse[Any] = await asyncio.wait_for(specialist_agent.run(task), timeout=timeout)
    except asyncio.CancelledError:
        usage_recorder.emit()
        raise
    except TimeoutError:
        usage_recorder.emit()
        raise
    except Exception:
        usage_recorder.emit()
        raise
    usage_recorder.emit(_runner._response_usage_details(response))
    return response.text


async def _finalize_maf_stream(stream: Any, exc: BaseException) -> None:
    """Best-effort finalize the MAF stream chain on cancellation or timeout."""
    while stream is not None:
        next_stream = getattr(stream, "_inner_stream", None)
        run_cleanup_hooks = getattr(stream, "_run_cleanup_hooks", None)
        if callable(run_cleanup_hooks):
            with contextlib.suppress(Exception):
                has_stream_error_slot = hasattr(stream, "_stream_error")
                if has_stream_error_slot and stream._stream_error is None:
                    stream._stream_error = exc
                    try:
                        await run_cleanup_hooks()
                    finally:
                        stream._stream_error = None
                else:
                    await run_cleanup_hooks()
        stream = next_stream


async def _build_agent_session(
    *,
    instructions: str | None,
    session_id: str | None,
    tools: Sequence[AgentFunctionTool] | None,
    mcp_tools: Sequence[MCPServerDescriptor] | None,
    skill_paths: Sequence[Path] | None,
    model: str | None,
    sandbox_tools: Sequence[ToolInput] | None,
    system_addendum: str | None,
    workflow_enabled: bool,
    workflow_durable_client: Any | None,
    workflow_agent_slug: str | None = None,
    agent_name: str | None,
    web_request_tools: Sequence[ToolInput] | None = None,
    agent_configuration: AgentConfiguration | None = None,
    subagents: list[SubagentRef] | None = None,
    catalog: AgentCatalog | None = None,
    coordinator_deadline: float | None = None,
    workflow_policy: WorkflowPlanPolicy | None = None,
    app_root: Path | None = None,
    _harness: AppHarness | None = None,
) -> tuple[Any, Any, str, _runner._DelegateErrorTracker | None, InferenceTarget]:
    """Construct the existing fresh MAF agent/session and invocation metadata."""
    from agent_framework import AgentSession

    resolved_config = agent_configuration or AgentConfiguration()
    client_manager = get_client_manager()
    chat_client, inference_target = client_manager.build_chat_client_with_target(model)

    validated_id = _runner._validate_session_id(session_id)
    if validated_id is None:
        session = AgentSession()
        resolved_id = session.session_id
    else:
        resolved_id = validated_id
        session = AgentSession(session_id=resolved_id)

    history_agent_slug = _runner._resolve_history_agent_slug(agent_name, workflow_agent_slug)
    history_provider = _runner._build_history_provider(history_agent_slug)

    delegate_tools: list[ToolDescriptor] | None = None
    delegate_error_tracker: _runner._DelegateErrorTracker | None = None
    if subagents:
        effective_deadline = (
            coordinator_deadline
            if coordinator_deadline is not None
            else asyncio.get_running_loop().time() + _runner.DEFAULT_TIMEOUT
        )
        delegate_tools, delegate_error_tracker = await _runner.build_subagent_tools(
            subagents, catalog, coordinator_deadline=effective_deadline, _harness=_harness
        )

    resolved_tools, effective_instructions = _runner._assemble_agent_inputs(
        instructions=instructions,
        tools=tools,
        mcp_tools=mcp_tools,
        sandbox_tools=sandbox_tools,
        web_request_tools=web_request_tools,
        system_addendum=system_addendum,
        workflow_enabled=workflow_enabled,
        workflow_durable_client=workflow_durable_client,
        workflow_agent_slug=workflow_agent_slug,
        agent_name=agent_name,
        resolved_id=resolved_id,
        delegate_tools=delegate_tools,
        workflow_policy=workflow_policy,
        app_root=app_root,
    )
    agent = _runner._build_role_agent(
        chat_client,
        agent_instructions=effective_instructions,
        tools=resolved_tools,
        skill_paths=skill_paths,
        agent_name=agent_name,
        history_provider=history_provider,
        agent_configuration=resolved_config,
    )
    return agent, session, resolved_id, delegate_error_tracker, inference_target


def _content_type(item: Content) -> str:
    """Return the declared Agent Framework content type."""
    return item.type


def _content_text(item: Content) -> str:
    return item.text or ""


def _function_call_event(item: Content, *, turn_id: str | None = None) -> ToolCallEvidence:
    event: ToolCallEvidence = {
        "type": "tool_start",
        "tool_call_id": item.call_id or item.id,
        "tool_name": item.name,
        "arguments": item.arguments,
    }
    if turn_id is not None:
        event["turn_id"] = turn_id
    return event


def _message_role(message: Message) -> str:
    return message.role


def _merge_tool_arguments(previous: Any, current: Any) -> Any:
    if previous is None:
        return current
    if current is None:
        return previous
    if isinstance(previous, str) and isinstance(current, str):
        if current.startswith(previous):
            return current
        return previous + current
    return current


def _is_complete_json_argument(value: Any) -> bool:
    if not isinstance(value, str):
        return value is not None
    text = value.strip()
    if not text:
        return False
    try:
        json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return False
    return True


def _function_result_event(item: Content) -> dict[str, Any]:
    return {
        "type": "tool_end",
        "tool_call_id": item.call_id or item.id,
        "tool_name": item.name,
        "result": item.result,
    }


async def run(
    harness: AppHarness,
    request: HarnessRequest,
    *,
    timeout: float,
    instructions: str | None,
    system_addendum: str | None,
    session_id: str | None,
    model: str | None,
    agent_name: str | None,
    agent_configuration: AgentConfiguration | None,
    workflow_enabled: bool,
    workflow_durable_client: Any | None,
    workflow_agent_slug: str | None,
    subagents: list[SubagentRef] | None,
    catalog: AgentCatalog | None,
    workflow_policy: WorkflowPlanPolicy | None,
) -> _runner.AgentResult:
    """Execute one neutral request through MAF without changing its role policy."""
    loop = asyncio.get_running_loop()
    coordinator_deadline = request.deadline

    agent, session, resolved_id, delegate_error_tracker, inference_target = (
        await _runner._build_agent_session(
            instructions=instructions,
            session_id=session_id,
            tools=request.tools,
            mcp_tools=request.mcp_servers,
            skill_paths=(
                request.skill_source_paths
                if request.skill_source_paths is not None
                else tuple(skill.path for skill in request.skills) or None
            ),
            model=model,
            sandbox_tools=None,
            system_addendum=system_addendum,
            workflow_enabled=workflow_enabled,
            workflow_durable_client=workflow_durable_client,
            workflow_agent_slug=workflow_agent_slug,
            agent_name=agent_name,
            web_request_tools=None,
            agent_configuration=agent_configuration,
            subagents=subagents,
            catalog=catalog,
            coordinator_deadline=coordinator_deadline,
            workflow_policy=workflow_policy,
            app_root=harness.app_root,
            _harness=harness,
        )
    )

    try:
        async with _harness_execution._session_lock_bounded_by(
            resolved_id,
            coordinator_deadline,
            agent_slug=request.agent_slug,
        ):
            remaining_after_lock = max(0.0, coordinator_deadline - loop.time())
            if remaining_after_lock <= 0:
                raise TimeoutError
            usage_recorder = _harness_execution._AgentUsageRecorder(
                agent_name=agent_name or "main",
                execution_role="primary",
                inference_target=inference_target,
            )
            try:
                response: AgentResponse[Any] = await asyncio.wait_for(
                    agent.run(
                        request.prompt,
                        session=session,
                        options=_runner._build_chat_options_from_environment(),
                    ),
                    timeout=remaining_after_lock,
                )
            except asyncio.CancelledError:
                usage_recorder.emit()
                raise
            except TimeoutError:
                usage_recorder.emit()
                raise
            except Exception:
                usage_recorder.emit()
                raise
            usage_recorder.emit(_runner._response_usage_details(response))
    except TimeoutError:
        raise RuntimeError(f"Agent run timed out after {timeout}s") from None

    text = ""
    try:
        text = response.text
    except Exception:
        text = ""
    if not text:
        try:
            for msg in response.messages:
                for item in msg.contents:
                    if _content_type(item) == "text":
                        text += _content_text(item)
        except Exception as exc:
            logger.debug("Failed to extract response text: %s", exc)

    tool_calls: list[ToolCallEvidence] = []
    try:
        assistant_index = -1
        for msg in response.messages:
            is_assistant = _message_role(msg) == _ASSISTANT_ROLE
            if is_assistant:
                assistant_index += 1
            turn_id = f"response-{assistant_index}" if is_assistant else None
            for item in msg.contents:
                ctype = _content_type(item)
                if ctype == "function_call":
                    tool_calls.append(_function_call_event(item, turn_id=turn_id))
                elif ctype == "function_result":
                    call_id = item.call_id or item.id
                    if not call_id:
                        continue
                    matched = next(
                        (tc for tc in reversed(tool_calls) if tc.get("tool_call_id") == call_id),
                        None,
                    )
                    if matched is not None:
                        matched["result"] = item.result
                        matched["success"] = not _looks_like_tool_error(item.result)
    except Exception as exc:
        logger.debug("Failed to extract tool_calls: %s", exc)

    return _runner.AgentResult(
        session_id=resolved_id,
        content=text,
        model=inference_target.model or model or "unknown",
        tool_calls=tool_calls,
        delegate_error_count=delegate_error_tracker.count if delegate_error_tracker else 0,
    )


async def run_stream(
    harness: AppHarness,
    request: HarnessRequest,
    *,
    timeout: float,
    instructions: str | None,
    system_addendum: str | None,
    session_id: str | None,
    model: str | None,
    agent_name: str | None,
    display_name: str | None,
    agent_configuration: AgentConfiguration | None,
    workflow_enabled: bool,
    workflow_durable_client: Any | None,
    workflow_agent_slug: str | None,
    subagents: list[SubagentRef] | None,
    catalog: AgentCatalog | None,
    workflow_policy: WorkflowPlanPolicy | None,
) -> AsyncGenerator[str]:
    """Yield the existing SSE vocabulary for a selected MAF invocation."""
    loop = asyncio.get_running_loop()
    deadline = request.deadline

    try:
        agent, session, resolved_id, delegate_error_tracker, inference_target = (
            await _runner._build_agent_session(
                instructions=instructions,
                session_id=session_id,
                tools=request.tools,
                mcp_tools=request.mcp_servers,
                skill_paths=(
                    request.skill_source_paths
                    if request.skill_source_paths is not None
                    else tuple(skill.path for skill in request.skills) or None
                ),
                model=model,
                sandbox_tools=None,
                system_addendum=system_addendum,
                workflow_enabled=workflow_enabled,
                workflow_durable_client=workflow_durable_client,
                workflow_agent_slug=workflow_agent_slug,
                agent_name=agent_name,
                web_request_tools=None,
                agent_configuration=agent_configuration,
                subagents=subagents,
                catalog=catalog,
                coordinator_deadline=deadline,
                workflow_policy=workflow_policy,
                app_root=harness.app_root,
                _harness=harness,
            )
        )
    except Exception as exc:
        logger.error("Failed to build agent session: %s", exc, exc_info=True)
        yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"
        return

    yield f"data: {json.dumps({'type': 'session', 'session_id': resolved_id})}\n\n"

    with _runner.start_span(
        f"agent.run {agent_name or 'agent'}",
        lifecycle_stage=LifecycleStage.AGENT_RUN,
        attributes={
            "af.agent.name": agent_name,
            "af.agent.display_name": display_name,
            "af.agent.trigger_type": "stream",
            "af.agent.session_id": resolved_id,
            "af.agent.model": model,
        },
    ) as span:
        ordinary_tool_error_count = 0
        try:
            async with _harness_execution._session_lock_bounded_by(
                resolved_id,
                deadline,
                agent_slug=request.agent_slug,
            ):
                pending_tool_calls: dict[str, ToolCallEvidence] = {}
                emitted_tool_calls: set[str] = set()

                def buffer_function_call(item: Content) -> tuple[str | None, ToolCallEvidence]:
                    event = _function_call_event(item)
                    call_id = event.get("tool_call_id")
                    if not isinstance(call_id, str) or not call_id:
                        return None, event
                    pending = pending_tool_calls.setdefault(
                        call_id,
                        {
                            "type": "tool_start",
                            "tool_call_id": call_id,
                            "tool_name": event.get("tool_name"),
                            "arguments": None,
                        },
                    )
                    if event.get("tool_name"):
                        pending["tool_name"] = event["tool_name"]
                    pending["arguments"] = _merge_tool_arguments(
                        pending.get("arguments"),
                        event.get("arguments"),
                    )
                    return call_id, pending

                async def emit_tool_start_if_ready(
                    call_id: str, event: ToolCallEvidence
                ) -> AsyncIterator[str]:
                    if call_id in emitted_tool_calls:
                        return
                    if not _is_complete_json_argument(event.get("arguments")):
                        return
                    emitted_tool_calls.add(call_id)
                    yield f"data: {json.dumps(event)}\n\n"

                async def emit_tool_start_before_result(call_id: str | None) -> AsyncIterator[str]:
                    if call_id is None or call_id in emitted_tool_calls:
                        return
                    event = pending_tool_calls.get(call_id)
                    if event is None:
                        return
                    emitted_tool_calls.add(call_id)
                    yield f"data: {json.dumps(event)}\n\n"

                stream: Any = None
                stream_settled = False
                usage_recorder: _harness_execution._AgentUsageRecorder | None = None
                try:
                    usage_recorder = _harness_execution._AgentUsageRecorder(
                        agent_name=agent_name or "main",
                        execution_role="primary",
                        inference_target=inference_target,
                    )
                    stream = agent.run(
                        request.prompt,
                        stream=True,
                        session=session,
                        options=_runner._build_chat_options_from_environment(),
                    )
                    stream_iter = stream.__aiter__()
                    while True:
                        try:
                            remaining = max(0.0, deadline - loop.time())
                            if remaining <= 0:
                                raise TimeoutError
                            update = await asyncio.wait_for(stream_iter.__anext__(), timeout=remaining)
                        except StopAsyncIteration:
                            break
                        except (TimeoutError, asyncio.CancelledError) as exc:
                            await _runner._finalize_maf_stream(stream, exc)
                            stream_settled = True
                            raise
                        for item in getattr(update, "contents", None) or []:
                            ctype = _content_type(item)
                            if ctype == "text":
                                text = _content_text(item)
                                if text:
                                    yield f"data: {json.dumps({'type': 'delta', 'content': text})}\n\n"
                            elif ctype == "text_reasoning":
                                text = _content_text(item)
                                if text:
                                    yield (
                                        f"data: {json.dumps({'type': 'intermediate', 'content': text})}\n\n"
                                    )
                            elif ctype == "function_call":
                                call_id, event = buffer_function_call(item)
                                if call_id is None:
                                    yield f"data: {json.dumps(event)}\n\n"
                                else:
                                    async for output in emit_tool_start_if_ready(call_id, event):
                                        yield output
                            elif ctype == "function_result":
                                call_id = getattr(item, "call_id", None) or getattr(item, "id", None)
                                async for output in emit_tool_start_before_result(
                                    call_id if isinstance(call_id, str) else None
                                ):
                                    yield output
                                result_event = _function_result_event(item)
                                if _looks_like_tool_error(result_event.get("result")):
                                    ordinary_tool_error_count += 1
                                yield f"data: {json.dumps(result_event, default=str)}\n\n"
                    for call_id, event in pending_tool_calls.items():
                        if call_id not in emitted_tool_calls:
                            emitted_tool_calls.add(call_id)
                            yield f"data: {json.dumps(event)}\n\n"
                    span.set_attribute("af.agent.outcome", "success")
                    stream_settled = True
                    try:
                        yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    finally:
                        usage_details = None
                        try:
                            usage_details = await _runner._stream_usage_details(
                                stream,
                                remaining_timeout=max(0.0, deadline - loop.time()),
                            )
                        finally:
                            usage_recorder.emit(usage_details)
                except TimeoutError as exc:
                    if usage_recorder is not None:
                        usage_recorder.emit()
                    if not stream_settled:
                        await _runner._finalize_maf_stream(stream, exc)
                        stream_settled = True
                    span.set_attribute("af.agent.outcome", "error")
                    span.record_exception(
                        TimeoutError(f"Timeout after {timeout}s"), fault_domain=FaultDomain.RUNTIME
                    )
                    yield f"data: {json.dumps({'type': 'error', 'content': f'Timeout after {timeout}s'})}\n\n"
                except asyncio.CancelledError:
                    if usage_recorder is not None:
                        usage_recorder.emit()
                    raise
                except Exception as exc:
                    if usage_recorder is not None:
                        usage_recorder.emit()
                    if not stream_settled:
                        await _runner._finalize_maf_stream(stream, exc)
                        stream_settled = True
                    logger.error("Agent stream failed: %s", exc, exc_info=True)
                    span.set_attribute("af.agent.outcome", "error")
                    span.record_exception(exc, fault_domain=FaultDomain.UNKNOWN)
                    yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"
                finally:
                    if not stream_settled:
                        exc_at_teardown = sys.exc_info()[1] or asyncio.CancelledError(
                            "run_agent_stream torn down before completion"
                        )
                        await _runner._finalize_maf_stream(stream, exc_at_teardown)
                        if usage_recorder is not None:
                            usage_recorder.emit()
        except TimeoutError:
            span.set_attribute("af.agent.outcome", "error")
            span.record_exception(
                TimeoutError(f"Timeout after {timeout}s"), fault_domain=FaultDomain.RUNTIME
            )
            yield f"data: {json.dumps({'type': 'error', 'content': f'Timeout after {timeout}s'})}\n\n"
        finally:
            span.set_attribute(
                "af.agent.tool_error_count",
                (delegate_error_tracker.count if delegate_error_tracker else 0)
                + ordinary_tool_error_count,
            )
