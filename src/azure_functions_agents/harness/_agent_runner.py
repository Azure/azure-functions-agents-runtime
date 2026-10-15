"""Private app-bound runner contract and thread-safe construction cache."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from .._agent_run_diagnostics import AgentExecutionSurface
from .._tool_descriptor import ToolDescriptor
from ..config import ResolvedAgent, SubagentRef
from ..config.schema import AgentConfiguration
from ..discovery.mcp import MCPServerDescriptor
from ..discovery.skills import SkillDescriptor
from ..registration.capabilities import AgentCapabilities
from ..registration.catalog import AgentCatalog
from ._harness_binding import (
    AppHarness,
    HarnessSessionState,
    SessionHistory,
)

if TYPE_CHECKING:
    from azure.durable_functions import DurableFunctionsClient

    from ..runner import AgentResult
    from ..workflows.schema import WorkflowPlanPolicy

type AgentFunctionTool = ToolDescriptor


class AgentRunner(Protocol):
    @property
    def default_model(self) -> str | None: ...

    @property
    def auto_record_exceptions(self) -> bool: ...

    @property
    def include_raw_tool_calls_in_sensitive_logs(self) -> bool: ...

    @property
    def session_history_unavailable_message(self) -> str | None: ...

    @property
    def validate_agent(self) -> Callable[[ResolvedAgent, AgentCapabilities], None] | None: ...

    @property
    def validate_hosted_skill(self) -> Callable[[ResolvedAgent], None] | None: ...

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
        sandbox_tools: Sequence[ToolDescriptor] | None = None,
        system_addendum: str | None = None,
        workflow_enabled: bool = False,
        workflow_durable_client: DurableFunctionsClient | None = None,
        workflow_agent_slug: str | None = None,
        agent_name: str | None = None,
        web_request_tools: Sequence[ToolDescriptor] | None = None,
        agent_configuration: AgentConfiguration | None = None,
        subagents: list[SubagentRef] | None = None,
        catalog: AgentCatalog | None = None,
        workflow_policy: WorkflowPlanPolicy | None = None,
        skills: Sequence[SkillDescriptor] | None = None,
        skill_catalog: Sequence[SkillDescriptor] | None = None,
        session_is_new: bool = False,
        session_state: HarnessSessionState | None = None,
    ) -> AgentResult: ...

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
        sandbox_tools: Sequence[ToolDescriptor] | None = None,
        system_addendum: str | None = None,
        workflow_enabled: bool = False,
        workflow_durable_client: DurableFunctionsClient | None = None,
        workflow_agent_slug: str | None = None,
        agent_name: str | None = None,
        display_name: str | None = None,
        web_request_tools: Sequence[ToolDescriptor] | None = None,
        agent_configuration: AgentConfiguration | None = None,
        subagents: list[SubagentRef] | None = None,
        catalog: AgentCatalog | None = None,
        workflow_policy: WorkflowPlanPolicy | None = None,
        skills: Sequence[SkillDescriptor] | None = None,
        skill_catalog: Sequence[SkillDescriptor] | None = None,
        session_is_new: bool = False,
        session_state: HarnessSessionState | None = None,
        execution_surface: AgentExecutionSurface = AgentExecutionSurface.RUNNER_API,
    ) -> AsyncGenerator[str]: ...

    async def get_session_history(self, agent_slug: str, session_id: str) -> SessionHistory: ...

    async def run_leaf_agent_task(
        self,
        resolved: ResolvedAgent,
        capabilities: AgentCapabilities,
        task: str,
        *,
        timeout: float,
        execution_role: Literal["delegate", "workflow_subagent"],
    ) -> str: ...

def get_agent_runner(harness: AppHarness) -> AgentRunner:
    """Construct one resource-free runner per immutable app binding."""
    with harness._resources.guard:
        if harness._resources.runner is None:
            harness._resources.runner = harness._create_runner(harness)
        return harness._resources.runner
