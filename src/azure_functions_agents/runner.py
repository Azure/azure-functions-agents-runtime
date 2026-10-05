"""Public execution facade and host tool/delegation policy."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict

from pydantic import BaseModel, Field

from ._function_tool import FunctionTool, tool
from ._logger import logger
from ._observability import FaultDomain, RuntimeSpan, current_span, record_delegate_call
from ._session_id import SESSION_ID_PATTERN
from ._slug import delegate_tool_name
from .config import ResolvedAgent, SubagentRef
from .config.env import runtime_env_value
from .config.schema import AgentConfiguration
from .discovery.mcp import MCPTool
from .harness._agent_runner import get_agent_runner
from .harness._harness_binding import AppHarness, get_harness
from .harness._history_identity import validate_agent_slug
from .registration.capabilities import AgentCapabilities
from .registration.catalog import AgentCatalog, CatalogEntry

if TYPE_CHECKING:
    from azure.durable_functions import DurableFunctionsClient

    from .workflows.schema import WorkflowPlanPolicy

type AgentFunctionTool = FunctionTool | Callable[..., Any]
type AgentTool = AgentFunctionTool | MCPTool


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
    """Return ``session_id`` if it matches the safe pattern; raise on invalid input."""
    if session_id is None:
        return None
    if not isinstance(session_id, str) or not _SESSION_ID_PATTERN.match(session_id):
        raise ValueError(f"Invalid session_id (must match {_SESSION_ID_PATTERN.pattern})")
    return session_id


def _resolve_history_agent_slug(
    agent_name: str | None,
    workflow_agent_slug: str | None,
) -> str:
    if agent_name is not None:
        return agent_name
    if workflow_agent_slug is not None:
        return workflow_agent_slug
    return "main"


class _DelegateErrorTracker:
    """Count recoverable specialist failures for one coordinator invocation."""

    __slots__ = ("count",)

    def __init__(self) -> None:
        self.count = 0

    def record_error(self) -> None:
        self.count += 1


def _assemble_agent_inputs(
    *,
    instructions: str | None,
    tools: list[AgentFunctionTool] | None,
    mcp_tools: list[MCPTool] | None,
    sandbox_tools: list[FunctionTool] | None,
    web_request_tools: list[FunctionTool] | None,
    system_addendum: str | None,
    workflow_enabled: bool,
    workflow_durable_client: DurableFunctionsClient | None,
    workflow_agent_slug: str | None,
    agent_name: str | None,
    resolved_id: str | None,
    delegate_tools: list[FunctionTool] | None,
    workflow_policy: WorkflowPlanPolicy | None,
) -> tuple[list[AgentTool], str | None]:
    """Compatibility shim for the MAF-local tool assembly helper."""
    from .harness.agent_framework._maf_execution import assemble_agent_inputs

    return assemble_agent_inputs(
        instructions=instructions,
        tools=tools,
        mcp_tools=mcp_tools,
        app_root=None,
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
    )


async def run_leaf_agent_task(
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    task: str,
    *,
    timeout: float,
    execution_role: Literal["delegate", "workflow_subagent"],
) -> str:
    """Run one fresh stateless specialist and return its response text."""
    harness = capabilities._harness or get_harness()
    return await get_agent_runner(harness).run_leaf_agent_task(
        resolved,
        capabilities,
        task,
        timeout=timeout,
        execution_role=execution_role,
    )


def _sanitize_delegate_failure(slug: str, exc: BaseException) -> str:
    """Return a generic specialist failure without exposing internal details."""
    return (
        f"The '{slug}' specialist could not complete this task. "
        "Consider trying again, rephrasing the request, or proceeding without it."
    )


def _record_generic_delegate_failure(
    span: RuntimeSpan, tracker: _DelegateErrorTracker, slug: str, exc: BaseException
) -> str:
    """Record a recoverable delegate failure and return the sanitized model-facing string."""
    tracker.record_error()
    record_delegate_call(error=True)
    span.set_attribute("af.delegate.outcome", "error")
    span.record_exception(exc, fault_domain=FaultDomain.DELEGATE)
    return _sanitize_delegate_failure(slug, exc)


def _record_delegate_timeout(
    span: RuntimeSpan, tracker: _DelegateErrorTracker, slug: str, effective_timeout: float, exc: BaseException
) -> str:
    """Record a recoverable delegate timeout and return the model-facing string."""
    tracker.record_error()
    record_delegate_call(error=True)
    span.set_attribute("af.delegate.outcome", "timeout")
    span.set_attribute("af.delegate.timeout_seconds", effective_timeout)
    span.record_exception(exc, fault_domain=FaultDomain.DELEGATE)
    return (
        f"The '{slug}' specialist did not respond in time and was "
        "stopped. Consider a narrower request, trying again, or "
        "proceeding without it."
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
) -> FunctionTool:
    """Build one host-authorized specialist tool with its coordinator's deadline."""
    resolved = entry.resolved
    capabilities = entry.capabilities
    slug = ref.agent
    tool_name = delegate_tool_name(slug)
    description = ref.when or resolved.description
    specialist_timeout = resolved.timeout

    @tool(
        name=tool_name,
        description=description,
        schema=_DelegateTaskParams,
        approval_mode="never_require",
    )
    async def delegate(params: _DelegateTaskParams) -> str:
        loop = asyncio.get_running_loop()
        task_text = params.task

        span = current_span()
        span.set_attribute("af.delegate.specialist", slug)
        span.set_attribute("af.delegate.task_bytes", len(task_text))
        span.set_content("af.delegate.task", task_text)

        remaining = max(0.0, coordinator_deadline - loop.time())
        effective_timeout = min(specialist_timeout, remaining)
        if effective_timeout <= 0:
            exc = TimeoutError(f"delegate_{slug}: coordinator budget exhausted before dispatch")
            return _record_delegate_timeout(span, tracker, slug, effective_timeout, exc)

        try:
            result = await run_leaf_agent_task(
                resolved,
                capabilities,
                task_text,
                timeout=effective_timeout,
                execution_role="delegate",
            )
        except asyncio.CancelledError:
            record_delegate_call(error=False)
            span.set_attribute("af.delegate.outcome", "cancelled")
            raise
        except TimeoutError as exc:
            return _record_delegate_timeout(span, tracker, slug, effective_timeout, exc)
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
) -> tuple[list[FunctionTool], _DelegateErrorTracker]:
    """Build specialist tools whose calls each execute a fresh isolated leaf."""
    tracker = _DelegateErrorTracker()
    tools: list[FunctionTool] = []
    if not subagents:
        return tools, tracker
    assert catalog is not None, "subagents declared but no AgentCatalog was provided"

    for ref in subagents:
        entry = catalog.get(ref.agent)
        assert entry is not None, f"subagents reference `{ref.agent}` was not found in the AgentCatalog"
        tools.append(
            _build_delegate_tool(
                ref,
                entry,
                coordinator_deadline=coordinator_deadline,
                tracker=tracker,
            )
        )
    return tools, tracker


async def run_agent(
    prompt: str,
    *,
    instructions: str | None = None,
    timeout: float | None = None,
    tools: list[AgentFunctionTool] | None = None,
    mcp_tools: list[MCPTool] | None = None,
    skill_paths: list[Path] | None = None,
    model: str | None = None,
    session_id: str | None = None,
    sandbox_tools: list[FunctionTool] | None = None,
    system_addendum: str | None = None,
    workflow_enabled: bool = False,
    workflow_durable_client: DurableFunctionsClient | None = None,
    workflow_agent_slug: str | None = None,
    agent_name: str | None = None,
    web_request_tools: list[FunctionTool] | None = None,
    agent_configuration: AgentConfiguration | None = None,
    subagents: list[SubagentRef] | None = None,
    catalog: AgentCatalog | None = None,
    workflow_policy: WorkflowPlanPolicy | None = None,
    _harness: AppHarness | None = None,
    _session_is_new: bool = False,
) -> AgentResult:
    """Execute through the bound harness; ``None`` tools discover and ``[]`` disables."""
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
        model=model,
        session_id=session_id,
        sandbox_tools=sandbox_tools,
        system_addendum=system_addendum,
        workflow_enabled=workflow_enabled,
        workflow_durable_client=workflow_durable_client,
        workflow_agent_slug=workflow_agent_slug,
        agent_name=agent_name,
        web_request_tools=web_request_tools,
        agent_configuration=agent_configuration,
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
    tools: list[AgentFunctionTool] | None = None,
    mcp_tools: list[MCPTool] | None = None,
    skill_paths: list[Path] | None = None,
    model: str | None = None,
    session_id: str | None = None,
    sandbox_tools: list[FunctionTool] | None = None,
    system_addendum: str | None = None,
    workflow_enabled: bool = False,
    workflow_durable_client: DurableFunctionsClient | None = None,
    workflow_agent_slug: str | None = None,
    agent_name: str | None = None,
    display_name: str | None = None,
    web_request_tools: list[FunctionTool] | None = None,
    agent_configuration: AgentConfiguration | None = None,
    subagents: list[SubagentRef] | None = None,
    catalog: AgentCatalog | None = None,
    workflow_policy: WorkflowPlanPolicy | None = None,
    _harness: AppHarness | None = None,
) -> AsyncIterator[str]:
    """Yield existing SSE events; selection failures emit one terminal error."""
    timeout = timeout if timeout is not None else DEFAULT_TIMEOUT
    validate_agent_slug(_resolve_history_agent_slug(agent_name, workflow_agent_slug))
    try:
        harness = _harness or get_harness()
        selected = get_agent_runner(harness)
        stream = selected.run_agent_stream(
            prompt,
            instructions=instructions,
            timeout=timeout,
            tools=tools,
            mcp_tools=mcp_tools,
            skill_paths=skill_paths,
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
    except (ValueError, RuntimeError) as exc:
        logger.error("Agent harness selection failed: %s", exc)
        yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"
        return

    async with aclosing(stream) as stream:
        async for event in stream:
            yield event
