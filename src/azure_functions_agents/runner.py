"""SDK-independent agent requests, role policy, and lazy harness dispatch."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict

from pydantic import BaseModel, Field

from ._function_tool import tool
from ._logger import logger
from ._observability import (
    FaultDomain,
    RuntimeSpan,
    current_span,
    record_delegate_call,
)
from ._session_id import SESSION_ID_PATTERN
from ._slug import delegate_tool_name
from ._tool_descriptor import ToolDescriptor, ToolInput, describe_tools
from .config import ResolvedAgent, SubagentRef
from .config.env import runtime_env_value
from .config.schema import AgentConfiguration
from .discovery.mcp import MCPServerDescriptor
from .discovery.mcp import discover_mcp_servers as discover_mcp_servers
from .discovery.skills import (
    SkillDescriptor,
    describe_skill_catalog,
)
from .discovery.tools import discover_user_tools as discover_user_tools
from .harness._agent_runner import get_agent_runner
from .harness._harness_binding import (
    AppHarness,
    HarnessRequest,
    get_harness,
)
from .harness._history_identity import validate_agent_slug
from .registration.capabilities import (
    AgentCapabilities,
    _merge_skill_descriptors,
)
from .registration.catalog import AgentCatalog, CatalogEntry

if TYPE_CHECKING:
    from azure.durable_functions import DurableFunctionsClient

    from .workflows.schema import WorkflowPlanPolicy

type AgentFunctionTool = ToolInput
type AgentTool = ToolDescriptor | MCPServerDescriptor


class ToolCallEvidence(TypedDict):
    """Framework-neutral evidence for one observed tool call."""

    type: Literal["tool_start"]
    tool_call_id: str | None
    tool_name: str | None
    arguments: Any
    turn_id: NotRequired[str]
    result: NotRequired[Any]
    success: NotRequired[bool]


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


@dataclass
class AgentResult:
    """Result of a non-streaming agent run."""

    session_id: str
    content: str
    content_intermediate: list[str] = field(default_factory=list)
    tool_calls: list[ToolCallEvidence] = field(default_factory=list)
    reasoning: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    delegate_error_count: int = 0
    model: str = "unknown"


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
    return await get_agent_runner(harness).run_leaf_agent_task(
        resolved,
        capabilities,
        task,
        timeout=timeout,
        execution_role=execution_role,
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
            "or any other context \u2014 include every fact, detail, and "
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
    servers = (
        tuple(discover_mcp_servers(harness.app_root).servers.values())
        if mcp_tools is None
        else tuple(mcp_tools)
    )
    if skills is not None:
        approved = tuple(skills)
    elif skill_paths is not None:
        approved = describe_skill_catalog(skill_paths)
    else:
        approved = ()
    discovered = tuple(skill_catalog) if skill_catalog is not None else approved
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
    workflow_durable_client: DurableFunctionsClient | None = None,
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
    validate_agent_slug(_resolve_history_agent_slug(agent_name, workflow_agent_slug))
    timeout = timeout if timeout is not None else DEFAULT_TIMEOUT
    coordinator_deadline = asyncio.get_running_loop().time() + timeout
    harness = _harness or get_harness()
    return await get_agent_runner(harness).run_agent(
        prompt,
        instructions=instructions,
        timeout=timeout,
        deadline=coordinator_deadline,
        tools=tools,
        mcp_tools=mcp_tools,
        skill_paths=skill_paths,
        skills=skills,
        skill_catalog=skill_catalog,
        sandbox_tools=sandbox_tools,
        web_request_tools=web_request_tools,
        system_addendum=system_addendum,
        model=model,
        session_id=session_id,
        agent_name=agent_name,
        workflow_agent_slug=workflow_agent_slug,
        agent_configuration=agent_configuration,
        workflow_enabled=workflow_enabled,
        workflow_durable_client=workflow_durable_client,
        subagents=subagents,
        catalog=catalog,
        workflow_policy=workflow_policy,
        session_is_new=_session_is_new,
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
    workflow_durable_client: DurableFunctionsClient | None = None,
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
    timeout = timeout if timeout is not None else DEFAULT_TIMEOUT
    validate_agent_slug(_resolve_history_agent_slug(agent_name, workflow_agent_slug))
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        harness = _harness or get_harness()
        selected = get_agent_runner(harness)
        stream = selected.run_agent_stream(
            prompt,
            instructions=instructions,
            timeout=timeout,
            deadline=deadline,
            tools=tools,
            mcp_tools=mcp_tools,
            skill_paths=skill_paths,
            skills=skills,
            skill_catalog=skill_catalog,
            model=model,
            session_id=session_id,
            sandbox_tools=sandbox_tools,
            system_addendum=system_addendum,
            workflow_enabled=workflow_enabled,
            workflow_durable_client=workflow_durable_client,
            workflow_agent_slug=workflow_agent_slug,
            agent_name=agent_name,
            display_name=display_name,
            web_request_tools=web_request_tools,
            agent_configuration=agent_configuration,
            subagents=subagents,
            catalog=catalog,
            workflow_policy=workflow_policy,
        )
    except Exception as exc:
        logger.error("Agent harness selection failed: %s", exc)
        yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"
        return
    async with aclosing(stream) as stream:
        async for event in stream:
            yield event
