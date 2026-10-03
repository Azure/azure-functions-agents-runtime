"""Lazy MAF adaptation and compatibility for legacy SDK tool extensions."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import sys
import uuid
import warnings
import weakref
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_framework import (
    Agent,
    AgentResponse,
    AgentSession,
    HistoryProvider,
    MCPStreamableHTTPTool,
    SupportsChatGetResponse,
)
from agent_framework import (
    FunctionTool as FunctionTool,
)
from agent_framework._feature_stage import ExperimentalWarning

from . import runner as _runner
from ._agent_identity import agent_id
from ._harness import AppHarness, HarnessRequest
from ._history_identity import validate_agent_slug
from ._logger import logger
from ._observability import FaultDomain, LifecycleStage
from ._tool_descriptor import (
    ToolCallable,
    ToolDescriptor,
    ToolInput,
    ToolPolicy,
    _input_model,
    _is_maf_tool,
    describe_tools,
)
from .client_manager import InferenceTarget, get_client_manager
from .config import ResolvedAgent, SubagentRef
from .config.env import EnvVar, runtime_env_value
from .config.paths import get_app_root
from .config.paths import resolve_config_dir as resolve_config_dir
from .config.schema import AgentConfiguration
from .discovery.mcp import MCPServerDescriptor, discover_mcp_servers
from .discovery.tools import discover_user_tools
from .registration._handlers import _looks_like_tool_error
from .registration.capabilities import AgentCapabilities
from .registration.catalog import AgentCatalog

if TYPE_CHECKING:
    from .workflows.schema import WorkflowPlanPolicy

type _MAFAgentTool = FunctionTool | MCPStreamableHTTPTool


@dataclass
class _LegacyTool:
    options: dict[str, Any] | None = None
    tool: FunctionTool | None = None


# SDK extension state stays here; neutral inventories carry only opaque keys.
_LEGACY_TOOLS: dict[str, _LegacyTool] = {}
_MAF_TOOL_ATTRIBUTES = frozenset({
    "_cached_parameters",
    "_context_parameter_name",
    "_declaration_only",
    "_input_model_explicitly_provided",
    "_input_schema_cached",
    "_instance",
    "_invocation_duration_histogram",
    "_invoke_sync_on_event_loop",
    "_schema_supplied",
    "__azure_functions_agents_workflow_tool__",
    "additional_properties",
    "approval_mode",
    "description",
    "func",
    "input_model",
    "invocation_count",
    "invocation_exception_count",
    "kind",
    "max_invocations",
    "max_invocation_exceptions",
    "name",
    "result_parser",
    "type",
})


def _unavailable(**arguments: Any) -> Any:
    raise TypeError("This tool requires the MAF compatibility adapter.")


def describe_maf_tool(candidate: object) -> ToolDescriptor:
    """Snapshot a legacy tool's metadata while retaining its SDK state only here."""
    if not isinstance(candidate, FunctionTool):
        raise TypeError("Expected a MAF FunctionTool.")
    unsupported: set[str] = set()
    if type(candidate) is not FunctionTool:
        unsupported.add("custom tool class")
    if candidate.max_invocations is not None:
        unsupported.add("max_invocations")
    if candidate.max_invocation_exceptions is not None:
        unsupported.add("max_invocation_exceptions")
    if candidate.declaration_only:
        unsupported.add("declaration_only")
    if candidate.result_parser is not None:
        unsupported.add("result_parser")
    if candidate.kind is not None:
        unsupported.add("kind")
    if candidate.additional_properties is not None:
        unsupported.add("additional_properties")
    if candidate._context_parameter_name is not None:
        unsupported.add("invocation context")
    if candidate._instance is not None:
        unsupported.add("bound SDK tool")
    if candidate._invoke_sync_on_event_loop:
        unsupported.add("sync invocation scheduling")
    if candidate.type != "function_tool":
        unsupported.add("tool type")
    if candidate.func is not None and _is_maf_tool(candidate.func):
        unsupported.add("nested SDK tool")
    unsupported.update(set(vars(candidate)) - _MAF_TOOL_ATTRIBUTES)
    func = candidate.func if candidate.func is not None else _unavailable
    if _is_maf_tool(func) or (
        inspect.ismethod(func)
        and type(func.__self__).__module__.startswith("agent_framework")
    ):
        unsupported.add("SDK callable")
        func = _unavailable
    model = None
    if not unsupported and candidate.input_model is not None:
        if not candidate._input_model_explicitly_provided:
            model = _input_model(candidate.name, func)
        elif candidate.input_model.__module__.startswith("agent_framework"):
            unsupported.add("SDK input model")
        else:
            model = candidate.input_model
    key = f"raw:{uuid.uuid4().hex}"
    policy = ToolPolicy(
        approval_mode=candidate.approval_mode,
        maf_only_options=tuple(sorted(unsupported)),
        maf_compatibility_key=key,
    )
    descriptor = ToolDescriptor.create(
        name=candidate.name,
        description=candidate.description,
        func=func,
        input_model=model,
        parameters=candidate.parameters(),
        policy=policy,
    )
    _LEGACY_TOOLS[key] = _LegacyTool(tool=candidate)
    weakref.finalize(policy, _LEGACY_TOOLS.pop, key, None)
    return descriptor


def with_maf_options(
    descriptor: ToolDescriptor, options: dict[str, Any]
) -> ToolDescriptor:
    """Defer MAF-specific keyword handling until the MAF execution boundary."""
    if "input_model" in options:
        raise TypeError("tool() supplies input_model through its schema argument.")
    for name in ("max_invocations", "max_invocation_exceptions"):
        value = options.get(name)
        if value is not None and value < 1:
            raise ValueError(f"{name} must be at least 1 or None.")
    key = f"runtime:{uuid.uuid4().hex}"
    policy = replace(
        descriptor.policy,
        maf_only_options=tuple(sorted(options)),
        maf_compatibility_key=key,
    )
    declaration = replace(descriptor, policy=policy)
    _LEGACY_TOOLS[key] = _LegacyTool(options=dict(options))
    weakref.finalize(policy, _LEGACY_TOOLS.pop, key, None)
    return declaration


def _maf_callable(descriptor: ToolDescriptor) -> ToolCallable:
    if descriptor.input_model is None:
        async def validated(**arguments: Any) -> Any:
            return await descriptor.invoke(arguments=arguments)

        return validated
    if inspect.iscoroutinefunction(descriptor.func):
        async def asynchronous(**arguments: Any) -> Any:
            result = descriptor.func(**arguments)
            return await result if inspect.isawaitable(result) else result

        return asynchronous

    def synchronous(**arguments: Any) -> Any:
        return descriptor.func(**arguments)

    return synchronous


def _function_tool(descriptor: ToolDescriptor, **options: Any) -> FunctionTool:
    legacy = bool(descriptor.policy.maf_only_options)
    return FunctionTool(
        name=descriptor.name,
        description=descriptor.description,
        func=descriptor.func if legacy else _maf_callable(descriptor),
        input_model=(
            descriptor.input_model or descriptor.parameters()
            if not legacy or descriptor.input_model_is_explicit
            else None
        ),
        approval_mode=descriptor.policy.approval_mode,
        **options,
    )


def build_maf_tools(descriptors: Sequence[ToolDescriptor]) -> list[FunctionTool]:
    """Materialize only the selected tools, preserving legacy SDK object identity."""
    tools: list[FunctionTool] = []
    for descriptor in descriptors:
        key = descriptor.policy.maf_compatibility_key
        if key is None:
            tools.append(_function_tool(descriptor))
            continue
        legacy = _LEGACY_TOOLS[key]
        if legacy.options is not None and inspect.ismethod(descriptor.func):
            tools.append(_function_tool(descriptor, **legacy.options))
            continue
        if legacy.tool is None:
            legacy.tool = _function_tool(descriptor, **(legacy.options or {}))
        tools.append(legacy.tool)
    return tools


def _resolve_sessions_dir(agent_slug: str) -> Path:
    slug = validate_agent_slug(agent_slug)
    base = Path(_runner.resolve_config_dir()).resolve() / "agent-sessions" / slug
    base.mkdir(parents=True, exist_ok=True)
    return base


def _build_history_provider(agent_slug: str) -> HistoryProvider:
    from ._blob_history import build_blob_provider_from_environment
    from ._file_history import ScopedFileHistoryProvider

    blob_provider = build_blob_provider_from_environment(agent_slug=agent_slug)
    if blob_provider is not None:
        return blob_provider
    scoped_dir = _resolve_sessions_dir(agent_slug)
    return ScopedFileHistoryProvider(storage_root=scoped_dir.parent, agent_slug=agent_slug)


def _build_chat_options_from_environment() -> dict[str, Any] | None:
    reasoning: dict[str, str] = {}
    effort = runtime_env_value("AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT")
    if effort:
        reasoning["effort"] = effort
    summary = runtime_env_value("AZURE_FUNCTIONS_AGENTS_REASONING_SUMMARY")
    if summary:
        reasoning["summary"] = summary
    return {"reasoning": reasoning} if reasoning else None


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
            timeout=min(remaining_timeout, _runner._FINAL_USAGE_TIMEOUT_SECONDS),
        )
        return _response_usage_details(response)
    except Exception:
        return None


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
) -> tuple[list[_MAFAgentTool], str | None]:
    root = app_root or get_app_root()
    descriptors = [
        *describe_tools(discover_user_tools(root).tools if tools is None else tools),
        *describe_tools(sandbox_tools or ()),
        *describe_tools(web_request_tools or ()),
    ]
    if workflow_enabled:
        from .workflows.tools import build_workflow_tools

        descriptors.extend(
            build_workflow_tools(
                session_id=resolved_id or "",
                workflow_agent_slug=workflow_agent_slug or agent_name or "main",
                agent_name=agent_name or "main",
                durable_client=workflow_durable_client,
                policy=workflow_policy,
            )
        )
    resolved_tools: list[_MAFAgentTool] = list(build_maf_tools(descriptors))
    servers = (
        tuple(discover_mcp_servers(root).servers.values())
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


def _max_context_window_tokens(config: AgentConfiguration) -> int | None:
    if config.agent_framework is None or config.agent_framework.compaction is None:
        return None
    return config.agent_framework.compaction.max_context_window_tokens


def _build_role_agent(
    chat_client: SupportsChatGetResponse[Any],
    *,
    agent_instructions: str | None,
    tools: Sequence[_MAFAgentTool | ToolDescriptor],
    skill_paths: Sequence[Path] | None,
    agent_name: str | None,
    history_provider: HistoryProvider | None,
    agent_configuration: AgentConfiguration,
) -> Agent[Any]:
    from agent_framework import SkillsProvider, create_harness_agent

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


def _build_delegated_agent(
    resolved: ResolvedAgent, capabilities: AgentCapabilities
) -> tuple[Agent[Any], InferenceTarget]:
    chat_client, target = get_client_manager().build_chat_client_with_target(resolved.model)
    tools, instructions = _runner._assemble_agent_inputs(
        instructions=resolved.instructions,
        tools=capabilities.filtered_user_tools or (),
        mcp_tools=capabilities.filtered_mcp_tools or (),
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
    )
    agent = _runner._build_role_agent(
        chat_client,
        agent_instructions=instructions,
        tools=tools,
        skill_paths=capabilities.enabled_skill_paths,
        agent_name=resolved.slug,
        history_provider=None,
        agent_configuration=resolved.agent_configuration,
    )
    return agent, target


async def _build_agent_session(
    *,
    instructions: str | None,
    session_id: str | None,
    tools: Sequence[ToolInput] | None,
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
) -> tuple[Agent[Any], AgentSession, str, _runner._DelegateErrorTracker | None, InferenceTarget]:
    configuration = agent_configuration or AgentConfiguration()
    chat_client, target = get_client_manager().build_chat_client_with_target(model)
    validated_id = _runner._validate_session_id(session_id)
    session = AgentSession() if validated_id is None else AgentSession(session_id=validated_id)
    history_slug = _runner._resolve_history_agent_slug(agent_name, workflow_agent_slug)
    history_provider = _runner._build_history_provider(history_slug)
    delegates: list[ToolDescriptor] | None = None
    tracker: _runner._DelegateErrorTracker | None = None
    if subagents:
        deadline = (
            coordinator_deadline
            if coordinator_deadline is not None
            else asyncio.get_running_loop().time() + _runner.DEFAULT_TIMEOUT
        )
        delegates, tracker = await _runner.build_subagent_tools(
            subagents, catalog, coordinator_deadline=deadline, _harness=_harness
        )
    resolved_tools, effective = _runner._assemble_agent_inputs(
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
        resolved_id=session.session_id,
        delegate_tools=delegates,
        workflow_policy=workflow_policy,
        app_root=app_root,
    )
    agent = _runner._build_role_agent(
        chat_client,
        agent_instructions=effective,
        tools=resolved_tools,
        skill_paths=skill_paths,
        agent_name=agent_name,
        history_provider=history_provider,
        agent_configuration=configuration,
    )
    return agent, session, session.session_id, tracker, target


async def run_leaf_agent_task(
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    task: str,
    *,
    timeout: float,
    execution_role: _runner._AgentExecutionRole,
) -> str:
    agent, target = _runner._build_delegated_agent(resolved, capabilities)
    recorder = _runner._AgentUsageRecorder(
        agent_name=resolved.slug, execution_role=execution_role, inference_target=target
    )
    try:
        response: AgentResponse[Any] = await asyncio.wait_for(agent.run(task), timeout=timeout)
    except (asyncio.CancelledError, Exception):
        recorder.emit()
        raise
    recorder.emit(_runner._response_usage_details(response))
    return response.text


async def _finalize_maf_stream(stream: Any, exc: BaseException) -> None:
    while stream is not None:
        next_stream = getattr(stream, "_inner_stream", None)
        cleanup = getattr(stream, "_run_cleanup_hooks", None)
        if callable(cleanup):
            with contextlib.suppress(Exception):
                if hasattr(stream, "_stream_error") and stream._stream_error is None:
                    stream._stream_error = exc
                    try:
                        await cleanup()
                    finally:
                        stream._stream_error = None
                else:
                    await cleanup()
        stream = next_stream


def _content_type(item: Any) -> str:
    return str(getattr(item, "type", "") or "")


def _content_text(item: Any) -> str:
    return str(getattr(item, "text", "") or "")


def _function_call_event(item: Any) -> dict[str, Any]:
    return {
        "type": "tool_start",
        "tool_call_id": getattr(item, "call_id", None) or getattr(item, "id", None),
        "tool_name": getattr(item, "name", None),
        "arguments": getattr(item, "arguments", None),
    }


def _merge_tool_arguments(previous: Any, current: Any) -> Any:
    if previous is None:
        return current
    if current is None:
        return previous
    if isinstance(previous, str) and isinstance(current, str):
        return current if current.startswith(previous) else previous + current
    return current


def _is_complete_json_argument(value: Any) -> bool:
    if not isinstance(value, str):
        return value is not None
    try:
        json.loads(value.strip())
    except (TypeError, json.JSONDecodeError):
        return False
    return True


def _function_result_event(item: Any) -> dict[str, Any]:
    return {
        "type": "tool_end",
        "tool_call_id": getattr(item, "call_id", None) or getattr(item, "id", None),
        "tool_name": getattr(item, "name", None),
        "result": getattr(item, "result", None),
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
    agent, session, resolved_id, tracker, target = await _runner._build_agent_session(
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
        web_request_tools=None,
        system_addendum=system_addendum,
        workflow_enabled=workflow_enabled,
        workflow_durable_client=workflow_durable_client,
        workflow_agent_slug=workflow_agent_slug,
        agent_name=agent_name,
        agent_configuration=agent_configuration,
        subagents=subagents,
        catalog=catalog,
        coordinator_deadline=request.deadline,
        workflow_policy=workflow_policy,
        app_root=harness.app_root,
        _harness=harness,
    )
    try:
        async with _runner._session_lock_bounded_by(
            resolved_id, request.deadline, agent_slug=request.agent_slug
        ):
            remaining = max(0.0, request.deadline - asyncio.get_running_loop().time())
            if remaining <= 0:
                raise TimeoutError
            recorder = _runner._AgentUsageRecorder(
                agent_name=agent_name or "main", execution_role="primary", inference_target=target
            )
            try:
                response: AgentResponse[Any] = await asyncio.wait_for(
                    agent.run(
                        request.prompt,
                        session=session,
                        options=_runner._build_chat_options_from_environment(),
                    ),
                    timeout=remaining,
                )
            except (asyncio.CancelledError, Exception):
                recorder.emit()
                raise
            recorder.emit(_runner._response_usage_details(response))
    except TimeoutError:
        raise RuntimeError(f"Agent run timed out after {timeout}s") from None
    try:
        text = response.text
    except Exception:
        text = ""
    if not text:
        try:
            for message in response.messages:
                for item in getattr(message, "contents", None) or []:
                    if _content_type(item) == "text":
                        text += _content_text(item)
        except Exception as exc:
            logger.debug("Failed to extract response text: %s", exc)
    calls: list[dict[str, Any]] = []
    try:
        for message in response.messages:
            for item in getattr(message, "contents", None) or []:
                kind = _content_type(item)
                if kind == "function_call":
                    calls.append(_function_call_event(item))
                elif kind == "function_result":
                    call_id = getattr(item, "call_id", None) or getattr(item, "id", None)
                    match = next(
                        (call for call in reversed(calls) if call.get("tool_call_id") == call_id),
                        None,
                    )
                    if match is not None:
                        match["result"] = getattr(item, "result", None)
    except Exception as exc:
        logger.debug("Failed to extract tool_calls: %s", exc)
    return _runner.AgentResult(
        session_id=resolved_id,
        content=text,
        tool_calls=calls,
        delegate_error_count=tracker.count if tracker else 0,
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
    try:
        agent, session, resolved_id, tracker, target = await _runner._build_agent_session(
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
            web_request_tools=None,
            system_addendum=system_addendum,
            workflow_enabled=workflow_enabled,
            workflow_durable_client=workflow_durable_client,
            workflow_agent_slug=workflow_agent_slug,
            agent_name=agent_name,
            agent_configuration=agent_configuration,
            subagents=subagents,
            catalog=catalog,
            coordinator_deadline=request.deadline,
            workflow_policy=workflow_policy,
            app_root=harness.app_root,
            _harness=harness,
        )
    except Exception as exc:
        logger.error("Failed to build agent session: %s", exc, exc_info=True)
        yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"
        return
    yield f"data: {json.dumps({'type': 'session', 'session_id': resolved_id})}\n\n"
    loop = asyncio.get_running_loop()
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
        ordinary_errors = 0
        try:
            async with _runner._session_lock_bounded_by(
                resolved_id, request.deadline, agent_slug=request.agent_slug
            ):
                pending: dict[str, dict[str, Any]] = {}
                emitted: set[str] = set()
                stream: Any = None
                settled = False
                recorder: _runner._AgentUsageRecorder | None = None
                try:
                    recorder = _runner._AgentUsageRecorder(
                        agent_name=agent_name or "main",
                        execution_role="primary",
                        inference_target=target,
                    )
                    stream = agent.run(
                        request.prompt,
                        stream=True,
                        session=session,
                        options=_runner._build_chat_options_from_environment(),
                    )
                    iterator = stream.__aiter__()
                    while True:
                        try:
                            remaining = max(0.0, request.deadline - loop.time())
                            if remaining <= 0:
                                raise TimeoutError
                            update = await asyncio.wait_for(iterator.__anext__(), timeout=remaining)
                        except StopAsyncIteration:
                            break
                        except (TimeoutError, asyncio.CancelledError) as exc:
                            await _runner._finalize_maf_stream(stream, exc)
                            settled = True
                            raise
                        for item in getattr(update, "contents", None) or []:
                            kind = _content_type(item)
                            if kind in {"text", "text_reasoning"}:
                                text = _content_text(item)
                                if text:
                                    event_kind = "delta" if kind == "text" else "intermediate"
                                    yield f"data: {json.dumps({'type': event_kind, 'content': text})}\n\n"
                            elif kind == "function_call":
                                event = _function_call_event(item)
                                call_id = event.get("tool_call_id")
                                if not isinstance(call_id, str) or not call_id:
                                    yield f"data: {json.dumps(event)}\n\n"
                                    continue
                                buffered = pending.setdefault(
                                    call_id,
                                    {
                                        "type": "tool_start",
                                        "tool_call_id": call_id,
                                        "tool_name": event.get("tool_name"),
                                        "arguments": None,
                                    },
                                )
                                if event.get("tool_name"):
                                    buffered["tool_name"] = event["tool_name"]
                                buffered["arguments"] = _merge_tool_arguments(
                                    buffered["arguments"], event.get("arguments")
                                )
                                if call_id not in emitted and _is_complete_json_argument(
                                    buffered["arguments"]
                                ):
                                    emitted.add(call_id)
                                    yield f"data: {json.dumps(buffered)}\n\n"
                            elif kind == "function_result":
                                call_id = getattr(item, "call_id", None) or getattr(item, "id", None)
                                if isinstance(call_id, str) and call_id not in emitted:
                                    start = pending.get(call_id)
                                    if start is not None:
                                        emitted.add(call_id)
                                        yield f"data: {json.dumps(start)}\n\n"
                                event = _function_result_event(item)
                                if _looks_like_tool_error(event.get("result")):
                                    ordinary_errors += 1
                                yield f"data: {json.dumps(event, default=str)}\n\n"
                    for call_id, event in pending.items():
                        if call_id not in emitted:
                            emitted.add(call_id)
                            yield f"data: {json.dumps(event)}\n\n"
                    span.set_attribute("af.agent.outcome", "success")
                    settled = True
                    try:
                        yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    finally:
                        usage = None
                        try:
                            usage = await _runner._stream_usage_details(
                                stream, remaining_timeout=max(0.0, request.deadline - loop.time())
                            )
                        finally:
                            recorder.emit(usage)
                except TimeoutError as exc:
                    if recorder is not None:
                        recorder.emit()
                    if not settled:
                        await _runner._finalize_maf_stream(stream, exc)
                        settled = True
                    span.set_attribute("af.agent.outcome", "error")
                    span.record_exception(
                        TimeoutError(f"Timeout after {timeout}s"), fault_domain=FaultDomain.RUNTIME
                    )
                    yield f"data: {json.dumps({'type': 'error', 'content': f'Timeout after {timeout}s'})}\n\n"
                except asyncio.CancelledError:
                    if recorder is not None:
                        recorder.emit()
                    raise
                except Exception as exc:
                    if recorder is not None:
                        recorder.emit()
                    if not settled:
                        await _runner._finalize_maf_stream(stream, exc)
                        settled = True
                    logger.error("Agent stream failed: %s", exc, exc_info=True)
                    span.set_attribute("af.agent.outcome", "error")
                    span.record_exception(exc, fault_domain=FaultDomain.UNKNOWN)
                    yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"
                finally:
                    if not settled:
                        teardown_error = sys.exc_info()[1] or asyncio.CancelledError(
                            "run_agent_stream torn down before completion"
                        )
                        await _runner._finalize_maf_stream(stream, teardown_error)
                        if recorder is not None:
                            recorder.emit()
        except TimeoutError:
            span.set_attribute("af.agent.outcome", "error")
            span.record_exception(
                TimeoutError(f"Timeout after {timeout}s"), fault_domain=FaultDomain.RUNTIME
            )
            yield f"data: {json.dumps({'type': 'error', 'content': f'Timeout after {timeout}s'})}\n\n"
        finally:
            span.set_attribute(
                "af.agent.tool_error_count", (tracker.count if tracker else 0) + ordinary_errors
            )
