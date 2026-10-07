"""Private MAF-backed implementation of the bound execution facade."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from azure_functions_agents import runner as _runner

from ..._tool_descriptor import ToolInput
from ...config import ResolvedAgent, SubagentRef
from ...config.schema import AgentConfiguration
from ...discovery.mcp import MCPServerDescriptor
from ...discovery.skills import SkillDescriptor
from ...registration.capabilities import AgentCapabilities
from ...registration.catalog import AgentCatalog
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
        session_is_new: bool = False,
    ) -> AgentResult:
        request = _runner._request(
            self._harness,
            prompt,
            instructions=instructions,
            session_id=session_id,
            session_is_new=session_is_new,
            deadline=deadline,
            tools=tools,
            mcp_tools=mcp_tools,
            skill_paths=skill_paths,
            skills=skills,
            skill_catalog=skill_catalog,
            model=model,
            sandbox_tools=sandbox_tools,
            system_addendum=system_addendum,
            workflow_agent_slug=workflow_agent_slug,
            agent_name=agent_name,
            web_request_tools=web_request_tools,
            agent_configuration=agent_configuration,
        )
        return await _maf_execution.run(
            self._harness,
            request,
            timeout=timeout if timeout is not None else _runner.DEFAULT_TIMEOUT,
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

    def run_agent_stream(
        self,
        prompt: str,
        *,
        instructions: str | None = None,
        timeout: float | None = None,
        deadline: float,
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
    ) -> AsyncGenerator[str]:
        request = _runner._request(
            self._harness,
            prompt,
            instructions=instructions,
            session_id=session_id,
            session_is_new=False,
            deadline=deadline,
            tools=tools,
            mcp_tools=mcp_tools,
            skill_paths=skill_paths,
            skills=skills,
            skill_catalog=skill_catalog,
            model=model,
            sandbox_tools=sandbox_tools,
            system_addendum=system_addendum,
            workflow_agent_slug=workflow_agent_slug,
            agent_name=agent_name,
            web_request_tools=web_request_tools,
            agent_configuration=agent_configuration,
        )
        return _maf_execution.run_stream(
            self._harness,
            request,
            timeout=timeout if timeout is not None else _runner.DEFAULT_TIMEOUT,
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
            replace(capabilities, _harness=self._harness),
            task,
            timeout=timeout,
            execution_role=execution_role,
        )


def create_runner(harness: AppHarness) -> AgentRunner:
    """Create the MAF-backed bound execution facade for one app binding."""
    return AgentRunner(_MAFHarnessRunner(harness))
