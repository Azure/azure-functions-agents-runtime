"""SDK-independent agent requests, role policy, and lazy harness dispatch."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from ._function_tool import tool
from ._harness import (
    AppHarness,
    ExecutionRole,
    HarnessKind,
    HarnessRequest,
    UnsupportedCapabilityError,
    get_harness,
    prepare_tools,
    reject_unsupported,
    validate_configuration,
)
from ._history_identity import validate_agent_slug
from ._logger import logger
from ._observability import (
    FaultDomain,
    RuntimeSpan,
    current_span,
    record_delegate_call,
)
from ._observability import (
    start_span as start_span,
)
from ._session_id import SESSION_ID_PATTERN
from ._slug import delegate_tool_name
from ._tool_descriptor import ToolDescriptor, ToolInput, describe_tools
from .client_manager import InferenceTarget
from .client_manager import get_client_manager as get_client_manager
from .config import ResolvedAgent, SubagentRef
from .config.env import runtime_env_value
from .config.schema import AgentConfiguration
from .discovery.mcp import MCPServerDescriptor, discover_mcp_servers
from .discovery.skills import (
    SkillDescriptor,
    describe_skill_catalog,
    describe_skill_paths,
    discover_skills,
)
from .discovery.tools import discover_user_tools
from .registration.capabilities import AgentCapabilities, _merge_skill_descriptors
from .registration.catalog import AgentCatalog, CatalogEntry

if TYPE_CHECKING:
    from .workflows.schema import WorkflowPlanPolicy

type AgentFunctionTool = ToolInput
type AgentTool = ToolDescriptor | MCPServerDescriptor
type _AgentExecutionRole = ExecutionRole


# Retained private MAF compatibility seams; ordinary execution returns only AgentResult/SSE.
_MAF_COMPAT_NAMES = frozenset({
    "_assemble_agent_inputs",
    "_build_agent_session",
    "_build_chat_options_from_environment",
    "_build_delegated_agent",
    "_build_history_provider",
    "_build_role_agent",
    "_content_text",
    "_content_type",
    "_finalize_maf_stream",
    "_function_call_event",
    "_function_result_event",
    "_is_complete_json_argument",
    "_max_context_window_tokens",
    "_merge_tool_arguments",
    "_resolve_sessions_dir",
    "_response_usage_details",
    "_stream_usage_details",
    "resolve_config_dir",
})


def __getattr__(name: str) -> Any:
    if name in _MAF_COMPAT_NAMES:
        from . import _maf_tools

        return getattr(_maf_tools, name)
    raise AttributeError(name)


def _runtime_timeout_default() -> float:
    env_timeout = runtime_env_value("AZURE_FUNCTIONS_AGENTS_TIMEOUT_SECONDS")
    if env_timeout:
        try:
            return float(env_timeout)
        except ValueError:
            logger.warning(
                "Ignoring invalid AZURE_FUNCTIONS_AGENTS_TIMEOUT_SECONDS value: %s",
                env_timeout,
            )
    return 900.0


DEFAULT_TIMEOUT = _runtime_timeout_default()
DEFAULT_MODEL: str | None = runtime_env_value("AZURE_FUNCTIONS_AGENTS_MODEL") or None
_SESSION_ID_PATTERN = SESSION_ID_PATTERN
_USAGE_FIELD_NAMES = {
    "input_token_count": "input_tokens",
    "output_token_count": "output_tokens",
}
_FINAL_USAGE_TIMEOUT_SECONDS = 1.0


def _normalize_usage_details(usage_details: Any) -> dict[str, int]:
    if not isinstance(usage_details, Mapping):
        return {}
    normalized: dict[str, int] = {}
    for source_name, record_name in _USAGE_FIELD_NAMES.items():
        value = usage_details.get(source_name)
        if (
            record_name not in normalized
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
        ):
            normalized[record_name] = value
    return normalized


def _model_publisher(provider: str | None) -> str | None:
    return "openai" if provider in {"openai", "azure_openai"} else None


@dataclass
class _AgentUsageRecorder:
    """Attempt at most one internal token-usage record per invocation."""

    agent_name: str
    execution_role: _AgentExecutionRole
    inference_target: InferenceTarget = field(default_factory=InferenceTarget)
    _emission_attempted: bool = field(default=False, init=False)

    def emit(self, usage_details: Any = None) -> None:
        try:
            usage = _normalize_usage_details(usage_details)
        except Exception:
            usage = {}
        self.emit_counts(
            input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens")
        )

    def emit_counts(self, *, input_tokens: int | None, output_tokens: int | None) -> None:
        if self._emission_attempted:
            return
        self._emission_attempted = True
        try:
            payload = {
                "agent_name": self.agent_name,
                "event_name": "agent_token_usage",
                "execution_role": self.execution_role,
                "input_tokens": input_tokens,
                "model": self.inference_target.model,
                "model_publisher": _model_publisher(self.inference_target.provider),
                "output_tokens": output_tokens,
                "provider": self.inference_target.provider,
            }
            logger.info(
                "Agent token usage: %s",
                json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
            )
        except Exception:
            return


_SESSION_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}
_SESSION_LOCKS_GUARD = asyncio.Lock()


async def _get_session_lock(session_id: str, agent_slug: str = "main") -> asyncio.Lock:
    key = (agent_slug, session_id)
    async with _SESSION_LOCKS_GUARD:
        lock = _SESSION_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _SESSION_LOCKS[key] = lock
        return lock


@contextlib.asynccontextmanager
async def _session_lock_bounded_by(
    session_id: str, deadline: float, *, agent_slug: str = "main"
) -> AsyncIterator[None]:
    lock = await _get_session_lock(session_id, agent_slug)
    loop = asyncio.get_running_loop()
    await asyncio.wait_for(lock.acquire(), timeout=max(0.0, deadline - loop.time()))
    try:
        yield
    finally:
        lock.release()


@dataclass
class AgentResult:
    """Result of a non-streaming agent run."""

    session_id: str
    content: str
    content_intermediate: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    reasoning: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    delegate_error_count: int = 0


def _validate_session_id(session_id: str | None) -> str | None:
    if session_id is None:
        return None
    if not isinstance(session_id, str) or not _SESSION_ID_PATTERN.match(session_id):
        raise ValueError(f"Invalid session_id (must match {_SESSION_ID_PATTERN.pattern})")
    return session_id


def _resolve_history_agent_slug(
    agent_name: str | None, workflow_agent_slug: str | None
) -> str:
    if agent_name is not None:
        return agent_name
    if workflow_agent_slug is not None:
        return workflow_agent_slug
    return "main"


class _DelegateErrorTracker:
    """Per-request counter of recoverable specialist failures."""

    __slots__ = ("count",)

    def __init__(self) -> None:
        self.count = 0

    def record_error(self) -> None:
        self.count += 1


async def run_leaf_agent_task(
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    task: str,
    *,
    timeout: float,
    execution_role: Literal["delegate", "workflow_subagent"],
    _harness: AppHarness | None = None,
) -> str:
    """Run a fresh stateless specialist under its existing role contract."""
    harness = _harness or capabilities._harness or get_harness()
    if harness.name is HarnessKind.COPILOT:
        reject_unsupported(**{execution_role: True})
    from ._maf_tools import run_leaf_agent_task as run_maf_leaf

    return await run_maf_leaf(
        resolved, capabilities, task, timeout=timeout, execution_role=execution_role
    )


def _sanitize_delegate_failure(slug: str, exc: BaseException) -> str:
    return (
        f"The '{slug}' specialist could not complete this task. "
        "Consider trying again, rephrasing the request, or proceeding without it."
    )


def _record_generic_delegate_failure(
    span: RuntimeSpan, tracker: _DelegateErrorTracker, slug: str, exc: BaseException
) -> str:
    tracker.record_error()
    record_delegate_call(error=True)
    span.set_attribute("af.delegate.outcome", "error")
    span.record_exception(exc, fault_domain=FaultDomain.DELEGATE)
    return _sanitize_delegate_failure(slug, exc)


def _record_delegate_timeout(
    span: RuntimeSpan,
    tracker: _DelegateErrorTracker,
    slug: str,
    effective_timeout: float,
    exc: BaseException,
) -> str:
    tracker.record_error()
    record_delegate_call(error=True)
    span.set_attribute("af.delegate.outcome", "timeout")
    span.set_attribute("af.delegate.timeout_seconds", effective_timeout)
    span.record_exception(exc, fault_domain=FaultDomain.DELEGATE)
    return (
        f"The '{slug}' specialist did not respond in time and was "
        "stopped. Consider a narrower request, trying again, or proceeding without it."
    )


class _DelegateTaskParams(BaseModel):
    """Argument schema for a ``delegate_<slug>`` tool call: a single ``task`` string."""

    task: str = Field(
        description=(
            "A complete, self-contained instruction for the specialist. The "
            "specialist does not see the coordinator's conversation history "
            "or any other context — include every fact, detail, and "
            "requirement the specialist needs to complete the task."
        )
    )


def _build_delegate_tool(
    ref: SubagentRef,
    entry: CatalogEntry,
    *,
    coordinator_deadline: float,
    tracker: _DelegateErrorTracker,
    _harness: AppHarness | None = None,
) -> ToolDescriptor:
    resolved = entry.resolved
    capabilities = (
        entry.capabilities
        if _harness is None
        else replace(entry.capabilities, _harness=_harness)
    )
    slug = ref.agent

    @tool(
        name=delegate_tool_name(slug),
        description=ref.when or resolved.description,
        schema=_DelegateTaskParams,
        approval_mode="never_require",
    )
    async def delegate(params: _DelegateTaskParams) -> str:
        task = params.task
        span = current_span()
        span.set_attribute("af.delegate.specialist", slug)
        span.set_attribute("af.delegate.task_bytes", len(task))
        span.set_content("af.delegate.task", task)
        remaining = max(0.0, coordinator_deadline - asyncio.get_running_loop().time())
        timeout = min(resolved.timeout, remaining)
        if timeout <= 0:
            exc = TimeoutError(f"delegate_{slug}: coordinator budget exhausted before dispatch")
            return _record_delegate_timeout(span, tracker, slug, timeout, exc)
        try:
            result = await run_leaf_agent_task(
                resolved, capabilities, task, timeout=timeout, execution_role="delegate"
            )
        except asyncio.CancelledError:
            record_delegate_call(error=False)
            span.set_attribute("af.delegate.outcome", "cancelled")
            raise
        except TimeoutError as exc:
            return _record_delegate_timeout(span, tracker, slug, timeout, exc)
        except Exception as exc:
            return _record_generic_delegate_failure(span, tracker, slug, exc)
        record_delegate_call(error=False)
        span.set_attribute("af.delegate.outcome", "success")
        span.set_attribute("af.delegate.response_bytes", len(result))
        span.set_content("af.delegate.result", result)
        return result

    return delegate


async def build_subagent_tools(
    subagents: list[SubagentRef] | None,
    catalog: AgentCatalog | None,
    *,
    coordinator_deadline: float,
    _harness: AppHarness | None = None,
) -> tuple[list[ToolDescriptor], _DelegateErrorTracker]:
    """Build neutral delegate tools; each invocation creates an independent leaf."""
    tracker = _DelegateErrorTracker()
    if not subagents:
        return [], tracker
    assert catalog is not None, "subagents declared but no AgentCatalog was provided"
    delegates: list[ToolDescriptor] = []
    for ref in subagents:
        entry = catalog.get(ref.agent)
        assert entry is not None, f"subagents reference `{ref.agent}` was not found in the AgentCatalog"
        delegates.append(
            _build_delegate_tool(
                ref, entry, coordinator_deadline=coordinator_deadline, tracker=tracker,
                _harness=_harness,
            )
        )
    return delegates, tracker


def _request(
    harness: AppHarness,
    prompt: str,
    *,
    instructions: str | None,
    session_id: str | None,
    session_is_new: bool,
    tools: Sequence[ToolInput] | None,
    mcp_tools: Sequence[MCPServerDescriptor] | None,
    skill_paths: Sequence[Path] | None,
    skills: Sequence[SkillDescriptor] | None,
    skill_catalog: Sequence[SkillDescriptor] | None,
    sandbox_tools: Sequence[ToolInput] | None,
    web_request_tools: Sequence[ToolInput] | None,
    system_addendum: str | None,
    model: str | None,
    agent_name: str | None,
    workflow_agent_slug: str | None,
    agent_configuration: AgentConfiguration | None,
    deadline: float,
) -> HarnessRequest:
    descriptors = describe_tools(
        (
            *(discover_user_tools(harness.app_root).tools if tools is None else tools),
            *(sandbox_tools or ()),
            *(web_request_tools or ()),
        )
    )
    if harness.name is HarnessKind.COPILOT:
        descriptors = prepare_tools(descriptors)
    servers = (
        tuple(discover_mcp_servers(harness.app_root).servers.values())
        if mcp_tools is None
        else tuple(mcp_tools)
    )
    approved = (
        tuple(skills) if skills is not None else describe_skill_paths(skill_paths or ())
    )
    if skill_catalog is not None:
        discovered = tuple(skill_catalog)
    elif harness.name is HarnessKind.COPILOT:
        discovered = _merge_skill_descriptors(
            discover_skills(harness.app_root).descriptors,
            describe_skill_catalog(tuple(skill.path for skill in approved)),
        )
    else:
        discovered = approved
    validated_id = _validate_session_id(session_id)
    effective = instructions.strip() if instructions and instructions.strip() else None
    if system_addendum:
        effective = (effective or "") + system_addendum
    configuration = agent_configuration or AgentConfiguration()
    return HarnessRequest(
        prompt=prompt,
        instructions=effective,
        agent_slug=validate_agent_slug(
            _resolve_history_agent_slug(agent_name, workflow_agent_slug)
        ),
        session_id=validated_id or uuid.uuid4().hex,
        new_session=validated_id is None or session_is_new,
        model=model or harness.default_model or "",
        tools=descriptors,
        mcp_servers=servers,
        skills=approved,
        skill_catalog=_merge_skill_descriptors(discovered, approved),
        max_output_tokens=configuration.max_output_tokens,
        deadline=deadline,
    )


async def run_agent(
    prompt: str,
    *,
    instructions: str | None = None,
    timeout: float | None = None,
    tools: Sequence[AgentFunctionTool] | None = None,
    mcp_tools: Sequence[MCPServerDescriptor] | None = None,
    skill_paths: Sequence[Path] | None = None,
    model: str | None = None,
    session_id: str | None = None,
    sandbox_tools: Sequence[ToolInput] | None = None,
    system_addendum: str | None = None,
    workflow_enabled: bool = False,
    workflow_durable_client: Any | None = None,
    workflow_agent_slug: str | None = None,
    agent_name: str | None = None,
    web_request_tools: Sequence[ToolInput] | None = None,
    agent_configuration: AgentConfiguration | None = None,
    subagents: list[SubagentRef] | None = None,
    catalog: AgentCatalog | None = None,
    workflow_policy: WorkflowPlanPolicy | None = None,
    skills: Sequence[SkillDescriptor] | None = None,
    skill_catalog: Sequence[SkillDescriptor] | None = None,
    _harness: AppHarness | None = None,
    _session_is_new: bool = False,
) -> AgentResult:
    """Execute a prompt; None inventories discover, explicit empty inventories disable."""
    timeout = timeout if timeout is not None else DEFAULT_TIMEOUT
    deadline = asyncio.get_running_loop().time() + timeout
    harness = _harness or get_harness()
    if harness.name is HarnessKind.COPILOT:
        validate_configuration(agent_configuration or AgentConfiguration())
        reject_unsupported(
            subagents=bool(subagents), workflows=workflow_enabled or workflow_policy is not None
        )
        if not (model or harness.default_model):
            raise UnsupportedCapabilityError("Copilot preview requires an explicit model.")
    request = _request(
        harness,
        prompt,
        instructions=instructions,
        session_id=session_id,
        session_is_new=_session_is_new,
        tools=tools,
        mcp_tools=mcp_tools,
        skill_paths=skill_paths,
        skills=skills,
        skill_catalog=skill_catalog,
        sandbox_tools=sandbox_tools,
        web_request_tools=web_request_tools,
        system_addendum=system_addendum,
        model=model,
        agent_name=agent_name,
        workflow_agent_slug=workflow_agent_slug,
        agent_configuration=agent_configuration,
        deadline=deadline,
    )
    if harness.name is HarnessKind.COPILOT:
        from ._copilot import run

        return await run(harness, request)
    from ._maf_tools import run as run_maf

    return await run_maf(
        harness,
        request,
        timeout=timeout,
        instructions=instructions,
        system_addendum=system_addendum,
        session_id=session_id,
        model=model,
        agent_name=agent_name,
        agent_configuration=agent_configuration,
        workflow_enabled=workflow_enabled,
        workflow_durable_client=workflow_durable_client,
        workflow_agent_slug=workflow_agent_slug,
        subagents=subagents,
        catalog=catalog,
        workflow_policy=workflow_policy,
    )


async def run_agent_stream(
    prompt: str,
    *,
    instructions: str | None = None,
    timeout: float | None = None,
    tools: Sequence[AgentFunctionTool] | None = None,
    mcp_tools: Sequence[MCPServerDescriptor] | None = None,
    skill_paths: Sequence[Path] | None = None,
    model: str | None = None,
    session_id: str | None = None,
    sandbox_tools: Sequence[ToolInput] | None = None,
    system_addendum: str | None = None,
    workflow_enabled: bool = False,
    workflow_durable_client: Any | None = None,
    workflow_agent_slug: str | None = None,
    agent_name: str | None = None,
    display_name: str | None = None,
    web_request_tools: Sequence[ToolInput] | None = None,
    agent_configuration: AgentConfiguration | None = None,
    subagents: list[SubagentRef] | None = None,
    catalog: AgentCatalog | None = None,
    workflow_policy: WorkflowPlanPolicy | None = None,
    skills: Sequence[SkillDescriptor] | None = None,
    skill_catalog: Sequence[SkillDescriptor] | None = None,
    _harness: AppHarness | None = None,
) -> AsyncIterator[str]:
    """Yield the existing SSE vocabulary under the selected app context."""
    try:
        harness = _harness or get_harness()
        if harness.name is HarnessKind.COPILOT:
            reject_unsupported(streaming=True)
    except (ValueError, RuntimeError) as exc:
        logger.error("Agent harness selection failed: %s", exc)
        yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"
        return
    timeout = timeout if timeout is not None else DEFAULT_TIMEOUT
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        request = _request(
            harness,
            prompt,
            instructions=instructions,
            session_id=session_id,
            session_is_new=False,
            tools=tools,
            mcp_tools=mcp_tools,
            skill_paths=skill_paths,
            skills=skills,
            skill_catalog=skill_catalog,
            sandbox_tools=sandbox_tools,
            web_request_tools=web_request_tools,
            system_addendum=system_addendum,
            model=model,
            agent_name=agent_name,
            workflow_agent_slug=workflow_agent_slug,
            agent_configuration=agent_configuration,
            deadline=deadline,
        )
    except Exception as exc:
        logger.error("Failed to build agent session: %s", exc, exc_info=True)
        yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"
        return
    from ._maf_tools import run_stream

    stream = run_stream(
        harness,
        request,
        timeout=timeout,
        instructions=instructions,
        system_addendum=system_addendum,
        session_id=session_id,
        model=model,
        agent_name=agent_name,
        display_name=display_name,
        agent_configuration=agent_configuration,
        workflow_enabled=workflow_enabled,
        workflow_durable_client=workflow_durable_client,
        workflow_agent_slug=workflow_agent_slug,
        subagents=subagents,
        catalog=catalog,
        workflow_policy=workflow_policy,
    )
    try:
        async for event in stream:
            yield event
    finally:
        await stream.aclose()
