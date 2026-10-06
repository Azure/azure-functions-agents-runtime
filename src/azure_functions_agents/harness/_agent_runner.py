"""Private app-bound execution runner selection and forwarding."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from .._function_tool import FunctionTool
from ..config import ResolvedAgent, SubagentRef
from ..config.schema import AgentConfiguration
from ..discovery.mcp import MCPTool
from ..registration.capabilities import AgentCapabilities
from ..registration.catalog import AgentCatalog
from ._harness_binding import AppHarness, HarnessKind

if TYPE_CHECKING:
    from azure.durable_functions import DurableFunctionsClient

    from ..runner import AgentResult
    from ..workflows.schema import WorkflowPlanPolicy

type AgentFunctionTool = FunctionTool | Callable[..., Any]


class _HarnessRunner(Protocol):
    async def run_agent(
        self,
        prompt: str,
        *,
        instructions: str | None = None,
        timeout: float | None = None,
        deadline: float,
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
        session_is_new: bool = False,
    ) -> AgentResult: ...

    def run_agent_stream(
        self,
        prompt: str,
        *,
        instructions: str | None = None,
        timeout: float | None = None,
        deadline: float,
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
        session_is_new: bool = False,
    ) -> AsyncGenerator[str]: ...

    async def run_leaf_agent_task(
        self,
        resolved: ResolvedAgent,
        capabilities: AgentCapabilities,
        task: str,
        *,
        timeout: float,
        execution_role: Literal["delegate", "workflow_subagent"],
    ) -> str: ...


class AgentRunner:
    """Concrete app-bound facade over one selected harness implementation."""

    def __init__(self, backend: _HarnessRunner) -> None:
        self._backend = backend

    async def run_agent(
        self,
        prompt: str,
        *,
        instructions: str | None = None,
        timeout: float | None = None,
        deadline: float | None = None,
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
        session_is_new: bool = False,
    ) -> AgentResult:
        effective_deadline = deadline
        if effective_deadline is None:
            effective_deadline = asyncio.get_running_loop().time() + (timeout or 0.0)
        return await self._backend.run_agent(
            prompt,
            instructions=instructions,
            timeout=timeout,
            deadline=effective_deadline,
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
            session_is_new=session_is_new,
        )

    def run_agent_stream(
        self,
        prompt: str,
        *,
        instructions: str | None = None,
        timeout: float | None = None,
        deadline: float | None = None,
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
        session_is_new: bool = False,
    ) -> AsyncGenerator[str]:
        effective_deadline = deadline
        if effective_deadline is None:
            effective_deadline = asyncio.get_running_loop().time() + (timeout or 0.0)
        return self._backend.run_agent_stream(
            prompt,
            instructions=instructions,
            timeout=timeout,
            deadline=effective_deadline,
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
            session_is_new=session_is_new,
        )

    async def run_leaf_agent_task(
        self,
        resolved: ResolvedAgent,
        capabilities: AgentCapabilities,
        task: str,
        *,
        timeout: float,
        execution_role: Literal["delegate", "workflow_subagent"],
    ) -> str:
        return await self._backend.run_leaf_agent_task(
            resolved,
            capabilities,
            task,
            timeout=timeout,
            execution_role=execution_role,
        )


def _build_runner(harness: AppHarness) -> AgentRunner:
    if harness.name is HarnessKind.COPILOT:
        from .copilot_sdk._copilot_runner import create_runner
    else:
        from .agent_framework._maf_runner import create_runner

    return create_runner(harness)


def get_agent_runner(harness: AppHarness) -> AgentRunner:
    """Return the per-app execution facade, caching it in the bound resource cell."""
    with harness._resources.guard:
        if harness._resources.runner is None:
            harness._resources.runner = _build_runner(harness)
        return harness._resources.runner
