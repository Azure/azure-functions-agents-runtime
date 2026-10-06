"""Private Copilot-backed implementation of the bound execution facade."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ..._function_tool import FunctionTool
from ...config import ResolvedAgent, SubagentRef
from ...config.schema import AgentConfiguration
from ...discovery.mcp import MCPTool, discover_mcp_servers
from ...discovery.tools import discover_user_tools
from ...harness._history_identity import validate_agent_slug
from ...registration.capabilities import AgentCapabilities
from ...registration.catalog import AgentCatalog
from ...streaming_events import HostedSkillEvent
from .._agent_runner import AgentFunctionTool, AgentRunner
from .._harness_binding import AppHarness, HarnessRequest, UnsupportedCapabilityError
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
        del timeout, workflow_durable_client, catalog

        configuration = agent_configuration or AgentConfiguration()
        _copilot_preview.validate_configuration(configuration)
        resolved_mcp = (
            list(discover_mcp_servers(self._harness.app_root).servers.values())
            if mcp_tools is None
            else mcp_tools
        )
        _copilot_preview.reject_unsupported(
            mcp=bool(resolved_mcp),
            skills=bool(skill_paths),
            subagents=bool(subagents),
            workflows=workflow_enabled or workflow_policy is not None,
        )
        resolved_model = model or self._harness.default_model
        if not resolved_model:
            raise UnsupportedCapabilityError("Copilot preview requires an explicit model.")
        user_tools = (
            list(discover_user_tools(self._harness.app_root).tools) if tools is None else list(tools)
        )
        resolved_tools = _copilot_preview.prepare_tools(
            [*user_tools, *list(sandbox_tools or []), *list(web_request_tools or [])]
        )
        validated_id = _validate_session_id(session_id)
        effective_instructions = instructions.strip() if instructions and instructions.strip() else None
        if system_addendum:
            effective_instructions = (effective_instructions or "") + system_addendum
        history_agent_slug = validate_agent_slug(
            _resolve_history_agent_slug(agent_name, workflow_agent_slug)
        )
        return await _copilot_execution.run(
            self._harness,
            HarnessRequest(
                prompt=prompt,
                instructions=effective_instructions,
                agent_slug=history_agent_slug,
                session_id=validated_id or uuid.uuid4().hex,
                new_session=validated_id is None or session_is_new,
                model=resolved_model,
                tools=resolved_tools,
                max_output_tokens=configuration.max_output_tokens,
                deadline=deadline,
            ),
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


def _validate_session_id(session_id: str | None) -> str | None:
    from ...runner import _validate_session_id as validate_session_id

    return validate_session_id(session_id)


def _resolve_history_agent_slug(
    agent_name: str | None, workflow_agent_slug: str | None
) -> str:
    from ...runner import _resolve_history_agent_slug as resolve_history_agent_slug

    return resolve_history_agent_slug(agent_name, workflow_agent_slug)


def create_runner(harness: AppHarness) -> AgentRunner:
    """Create the Copilot-backed bound execution facade for one app binding."""
    return AgentRunner(_CopilotHarnessRunner(harness))
