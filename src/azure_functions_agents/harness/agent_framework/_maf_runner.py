"""Private MAF-backed implementation of the bound execution facade."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ..._function_tool import FunctionTool
from ...config import ResolvedAgent, SubagentRef
from ...config.schema import AgentConfiguration
from ...discovery.mcp import MCPTool
from ...registration.capabilities import AgentCapabilities
from ...registration.catalog import AgentCatalog
from ...streaming_events import HostedSkillEvent
from .._agent_runner import AgentFunctionTool, AgentRunner
from .._harness_binding import AppHarness
from . import _maf_execution

if TYPE_CHECKING:
    from azure.durable_functions import DurableFunctionsClient

    from ...runner import AgentResult
    from ...workflows.schema import WorkflowPlanPolicy


class _MAFHarnessRunner:
    def __init__(self, harness: AppHarness) -> None:
        self._harness = harness

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
    ) -> AgentResult:
        return await _maf_execution.run_agent(
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
            web_request_tools=web_request_tools,
            agent_configuration=agent_configuration,
            subagents=subagents,
            catalog=catalog,
            workflow_policy=workflow_policy,
            _harness=self._harness,
            _session_is_new=session_is_new,
            _deadline=deadline,
        )

    def run_agent_events(
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
        execution_surface: str | None = None,
    ) -> AsyncGenerator[HostedSkillEvent]:
        return _maf_execution.run_agent_events(
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
            execution_surface=execution_surface,
            _harness=self._harness,
            _deadline=deadline,
        )

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
    ) -> AsyncGenerator[str]:
        return _maf_execution.run_agent_stream(
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
            _harness=self._harness,
            _deadline=deadline,
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
        return await _maf_execution.run_leaf_agent_task(
            resolved,
            capabilities,
            task,
            timeout=timeout,
            execution_role=execution_role,
        )


def create_runner(harness: AppHarness) -> AgentRunner:
    """Create the MAF-backed bound execution facade for one app binding."""
    return AgentRunner(_MAFHarnessRunner(harness))
