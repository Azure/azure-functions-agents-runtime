"""Private Copilot-backed implementation of the bound execution facade."""

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
from ...streaming_events import HostedSkillEvent
from .._agent_runner import AgentFunctionTool, AgentRunner
from .._harness_binding import AppHarness, UnsupportedCapabilityError
from . import _copilot_execution, _copilot_preview

if TYPE_CHECKING:
    from azure.durable_functions import DurableFunctionsClient

    from ...runner import AgentResult
    from ...workflows.schema import WorkflowPlanPolicy


class _CopilotHarnessRunner:
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
        del timeout, workflow_durable_client, catalog

        configuration = agent_configuration or AgentConfiguration()
        _copilot_preview.validate_configuration(configuration)
        _copilot_preview.reject_unsupported(
            subagents=bool(subagents),
            workflows=workflow_enabled or workflow_policy is not None,
        )
        resolved_model = model or self._harness.default_model
        if not resolved_model:
            raise UnsupportedCapabilityError("Copilot preview requires an explicit model.")
        request = _runner._request(
            self._harness,
            prompt,
            instructions=instructions,
            session_id=session_id,
            session_is_new=session_is_new,
            tools=tools,
            mcp_tools=mcp_tools,
            skill_paths=skill_paths,
            skills=skills,
            skill_catalog=skill_catalog,
            sandbox_tools=sandbox_tools,
            web_request_tools=web_request_tools,
            system_addendum=system_addendum,
            model=resolved_model,
            agent_name=agent_name,
            workflow_agent_slug=workflow_agent_slug,
            agent_configuration=configuration,
            deadline=deadline,
        )
        request = replace(request, tools=_copilot_preview.prepare_tools(request.tools))
        return await _copilot_execution.run(self._harness, request)

    def run_agent_events(
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
        execution_surface: str | None = None,
    ) -> AsyncGenerator[HostedSkillEvent]:
        del (
            prompt,
            instructions,
            timeout,
            deadline,
            tools,
            mcp_tools,
            skill_paths,
            model,
            session_id,
            sandbox_tools,
            system_addendum,
            workflow_enabled,
            workflow_durable_client,
            workflow_agent_slug,
            agent_name,
            display_name,
            web_request_tools,
            agent_configuration,
            subagents,
            catalog,
            workflow_policy,
            execution_surface,
        )
        _copilot_preview.reject_unsupported(streaming=True)
        raise AssertionError("Copilot streaming should have been rejected.")

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
        del (
            prompt,
            instructions,
            timeout,
            deadline,
            tools,
            mcp_tools,
            skill_paths,
            model,
            session_id,
            sandbox_tools,
            system_addendum,
            workflow_enabled,
            workflow_durable_client,
            workflow_agent_slug,
            agent_name,
            display_name,
            web_request_tools,
            agent_configuration,
            subagents,
            catalog,
            workflow_policy,
            skills,
            skill_catalog,
        )
        _copilot_preview.reject_unsupported(streaming=True)
        raise AssertionError("Copilot streaming should have been rejected.")

    async def run_leaf_agent_task(
        self,
        resolved: ResolvedAgent,
        capabilities: AgentCapabilities,
        task: str,
        *,
        timeout: float,
        execution_role: Literal["delegate", "workflow_subagent"],
    ) -> str:
        del resolved, capabilities, task, timeout
        _copilot_preview.reject_unsupported(**{execution_role: True})
        raise AssertionError("Copilot delegated execution should have been rejected.")


def create_runner(harness: AppHarness) -> AgentRunner:
    """Create the Copilot-backed bound execution facade for one app binding."""
    return AgentRunner(_CopilotHarnessRunner(harness))
