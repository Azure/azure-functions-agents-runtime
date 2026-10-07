"""Private Copilot-backed implementation of the bound execution facade."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncGenerator, Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from azure_functions_agents import runner as _runner

from ..._observability import FaultDomain, LifecycleStage, start_span
from ..._tool_descriptor import ToolDescriptor, ToolInput
from ...config import ResolvedAgent, SubagentRef
from ...config.schema import AgentConfiguration
from ...discovery.mcp import MCPServerDescriptor
from ...discovery.skills import SkillDescriptor
from ...registration.capabilities import AgentCapabilities
from ...registration.catalog import AgentCatalog
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
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> AgentResult:
        del timeout

        configuration = agent_configuration or AgentConfiguration()
        _copilot_preview.validate_configuration(configuration)
        if workflow_enabled and workflow_policy is None:
            raise UnsupportedCapabilityError(
                "Copilot workflow management requires a bound per-agent workflow policy."
            )
        if workflow_policy is not None and not workflow_enabled:
            raise UnsupportedCapabilityError(
                "Copilot workflow policy cannot be supplied without enabling workflows."
            )
        resolved_model = model or self._harness.default_model
        if not resolved_model:
            raise UnsupportedCapabilityError("Copilot preview requires an explicit model.")

        effective_skill_paths = None if skill_paths is None else list(skill_paths)
        effective_addendum = system_addendum
        if workflow_enabled:
            from ...workflows.integration import data_driven_workflows_skill_path

            workflow_skill = data_driven_workflows_skill_path()
            remaining_skill_paths = (
                None
                if effective_skill_paths is None
                else [path for path in effective_skill_paths if path != workflow_skill]
            )
            if skills or remaining_skill_paths:
                _copilot_preview.reject_unsupported(skills=True)
            effective_skill_paths = remaining_skill_paths
            workflow_grammar = (workflow_skill / "SKILL.md").read_text(encoding="utf-8")
            effective_addendum = (
                f"{system_addendum}\n\n{workflow_grammar}" if system_addendum else workflow_grammar
            )

        request = _runner._request(
            self._harness,
            prompt,
            instructions=instructions,
            session_id=session_id,
            session_is_new=session_is_new,
            tools=tools,
            mcp_tools=mcp_tools,
            skill_paths=effective_skill_paths,
            skills=skills,
            skill_catalog=skill_catalog,
            sandbox_tools=sandbox_tools,
            web_request_tools=web_request_tools,
            system_addendum=effective_addendum,
            model=resolved_model,
            agent_name=agent_name,
            workflow_agent_slug=workflow_agent_slug,
            agent_configuration=configuration,
            deadline=deadline,
        )

        delegate_tools: list[ToolDescriptor] = []
        delegate_tracker = None
        if subagents:
            delegate_tools, delegate_tracker = await _runner.build_subagent_tools(
                subagents,
                catalog,
                coordinator_deadline=deadline,
                _harness=self._harness,
            )

        workflow_tools: list[ToolDescriptor] = []
        if workflow_enabled:
            from ...workflows.tools import build_workflow_tools

            workflow_tools = build_workflow_tools(
                session_id=request.session_id,
                workflow_agent_slug=workflow_agent_slug or agent_name or "main",
                agent_name=agent_name or "main",
                durable_client=workflow_durable_client,
                policy=workflow_policy,
            )

        request = replace(
            request,
            tools=_copilot_preview.prepare_tools(
                [*request.tools, *workflow_tools, *delegate_tools]
            ),
            event_sink=event_sink,
        )
        result = await _copilot_execution.run(self._harness, request)
        result.delegate_error_count = delegate_tracker.count if delegate_tracker else 0
        return result

    async def run_agent_stream(
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
        session_is_new: bool = False,
    ) -> AsyncGenerator[str]:
        validated_id = _validate_session_id(session_id)
        resolved_id = validated_id or uuid.uuid4().hex
        events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        emitted_text = False

        async def execute() -> AgentResult:
            try:
                return await self.run_agent(
                    prompt,
                    instructions=instructions,
                    timeout=timeout,
                    deadline=deadline,
                    tools=tools,
                    mcp_tools=mcp_tools,
                    skill_paths=skill_paths,
                    model=model,
                    session_id=resolved_id,
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
                    skills=skills,
                    skill_catalog=skill_catalog,
                    session_is_new=session_is_new or validated_id is None,
                    event_sink=events.put_nowait,
                )
            finally:
                events.put_nowait(None)

        with start_span(
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
            execution = asyncio.create_task(execute())
            try:
                while (event := await events.get()) is not None:
                    if event["type"] == "delta":
                        emitted_text = True
                    yield f"data: {json.dumps(event, default=str)}\n\n"
                result = await execution
                from ...registration._handlers import _set_run_result_attributes

                _set_run_result_attributes(span, result)
                if not emitted_text and result.content:
                    yield f"data: {json.dumps({'type': 'delta', 'content': result.content})}\n\n"
                span.set_attribute("af.agent.outcome", "success")
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
            except asyncio.CancelledError:
                span.set_attribute("af.agent.outcome", "cancelled")
                raise
            except Exception as exc:
                span.set_attribute("af.agent.outcome", "error")
                span.record_exception(exc, fault_domain=FaultDomain.RUNTIME)
                yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"
            finally:
                if not execution.done():
                    execution.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await execution

    async def run_leaf_agent_task(
        self,
        resolved: ResolvedAgent,
        capabilities: AgentCapabilities,
        task: str,
        *,
        timeout: float,
        execution_role: Literal["delegate", "workflow_subagent"],
    ) -> str:
        _copilot_preview.validate_configuration(resolved.agent_configuration)
        resolved_model = resolved.model or self._harness.default_model
        if not resolved_model:
            raise UnsupportedCapabilityError("Copilot preview requires an explicit model.")
        result = await _copilot_execution.run(
            self._harness,
            HarnessRequest(
                prompt=task,
                instructions=resolved.instructions,
                agent_slug=resolved.slug,
                session_id=uuid.uuid4().hex,
                new_session=True,
                model=resolved_model,
                tools=_copilot_preview.prepare_tools(
                    [
                        *list(capabilities.filtered_user_tools or []),
                        *list(capabilities.web_request_tools or []),
                    ]
                ),
                max_output_tokens=resolved.agent_configuration.max_output_tokens,
                deadline=asyncio.get_running_loop().time() + timeout,
                mcp_servers=tuple(capabilities.filtered_mcp_tools or ()),
                skills=tuple(capabilities.skills or ()),
                skill_catalog=tuple(capabilities.skill_catalog or ()),
                execution_role=execution_role,
            ),
        )
        return result.content


def _validate_session_id(session_id: str | None) -> str | None:
    from ...runner import _validate_session_id as validate_session_id

    return validate_session_id(session_id)


def create_runner(harness: AppHarness) -> AgentRunner:
    """Create the Copilot-backed bound execution facade for one app binding."""
    return AgentRunner(_CopilotHarnessRunner(harness))
