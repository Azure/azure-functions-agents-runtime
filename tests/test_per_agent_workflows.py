from __future__ import annotations

from types import MappingProxyType
from typing import Any

import azure.durable_functions as df
import pytest

from azure_functions_agents._function_tool import WorkflowTool
from azure_functions_agents.app import create_function_app
from azure_functions_agents.config.schema import (
    BuiltinEndpointsConfig,
    ResolvedAgent,
    ToolsFilter,
    TriggerSpec,
    WorkflowConfig,
    WorkflowSubagentRef,
)
from azure_functions_agents.registration.capabilities import AgentCapabilities
from azure_functions_agents.registration.catalog import CatalogEntry, build_catalog
from azure_functions_agents.workflows import context, engine, integration, schema, tools


def _write_agent(tmp_path, filename: str, frontmatter: str) -> None:
    (tmp_path / filename).write_text(
        f"---\n{frontmatter.strip()}\n---\nAssist the user.\n",
        encoding="utf-8",
    )


def _function_names(app: Any) -> list[str]:
    return [function.get_function_name() for function in app.get_functions()]


def test_non_main_workflow_owner_without_main_creates_dfapp(tmp_path) -> None:
    _write_agent(
        tmp_path,
        "incident.agent.md",
        """
name: Incident
description: Triage incidents.
builtin_endpoints:
  chat_api: true
workflows:
  enabled: true
""",
    )

    app = create_function_app(tmp_path)

    assert isinstance(app, df.DFApp)
    names = _function_names(app)
    assert names.count(engine.ORCHESTRATOR_NAME) == 1
    assert names.count("agents_workflow_run_tool") == 1
    assert names.count(engine.SUB_AGENT_ACTIVITY_NAME) == 1
    assert "agent_incident_builtin_chat" in names


def test_multiple_workflow_owners_register_one_durable_blueprint(tmp_path) -> None:
    for slug in ("incident", "release"):
        _write_agent(
            tmp_path,
            f"{slug}.agent.md",
            f"""
name: {slug.title()}
description: Handle {slug}.
builtin_endpoints:
  chat_api: true
workflows:
  enabled: true
""",
        )

    app = create_function_app(tmp_path)
    names = _function_names(app)

    assert names.count(engine.ORCHESTRATOR_NAME) == 1
    assert names.count("agents_workflow_run_tool") == 1
    assert names.count(engine.SUB_AGENT_ACTIVITY_NAME) == 1
    assert "agent_incident_builtin_workflows" in names
    assert "agent_release_builtin_workflows" in names


def test_shared_workflow_subagent_registers_one_durable_activity(tmp_path) -> None:
    for slug in ("incident", "release"):
        _write_agent(
            tmp_path,
            f"{slug}.agent.md",
            f"""
name: {slug.title()}
description: Handle {slug}.
builtin_endpoints:
  chat_api: true
workflows:
  enabled: true
  subagents:
    - agent: analyst
""",
        )
    _write_agent(
        tmp_path,
        "analyst.agent.md",
        """
name: Analyst
description: Analyze one bounded task.
""",
    )

    app = create_function_app(tmp_path)

    assert _function_names(app).count(engine.SUB_AGENT_ACTIVITY_NAME) == 1


def test_mcp_only_workflow_owner_is_eligible(tmp_path) -> None:
    _write_agent(
        tmp_path,
        "mcp_owner.agent.md",
        """
name: MCP Owner
description: Starts workflows over MCP.
builtin_endpoints:
  mcp: true
workflows:
  enabled: true
""",
    )

    app = create_function_app(tmp_path)

    assert isinstance(app, df.DFApp)
    names = _function_names(app)
    assert "agent_mcp_owner_builtin_mcp" in names
    assert names.count(engine.ORCHESTRATOR_NAME) == 1


def test_unknown_trigger_workflow_owner_fails_composition(tmp_path) -> None:
    _write_agent(
        tmp_path,
        "unknown.agent.md",
        """
name: Unknown Trigger
description: Must not create an inert workflow owner.
trigger:
  type: imaginary_trigger
workflows:
  enabled: true
""",
    )

    with pytest.raises(ValueError, match=r"trigger\.type.*imaginary_trigger"):
        create_function_app(tmp_path)


def test_callable_non_trigger_decorator_fails_workflow_owner_composition(tmp_path) -> None:
    _write_agent(
        tmp_path,
        "binding.agent.md",
        """
name: Binding
description: An input binding cannot start an agent.
trigger:
  type: blob_input
workflows:
  enabled: true
""",
    )

    with pytest.raises(ValueError, match=r"trigger\.type.*blob_input"):
        create_function_app(tmp_path)


@pytest.mark.parametrize(
    "starter",
    [
        "",
        "builtin_endpoints:\n  debug_chat_ui: true",
    ],
)
def test_enabled_workflow_owner_requires_eligible_starter(tmp_path, starter: str) -> None:
    _write_agent(
        tmp_path,
        "inert.agent.md",
        f"""
name: Inert
description: Has no workflow starter.
{starter}
workflows:
  enabled: true
""",
    )

    with pytest.raises(
        ValueError,
        match=r"workflows\.enabled.*eligible workflow starter",
    ):
        create_function_app(tmp_path)


def _resolved(
    slug: str,
    *,
    tools_enabled: tuple[str, ...] = (),
    subagents: tuple[str, ...] = (),
) -> tuple[ResolvedAgent, AgentCapabilities]:
    workflow_tools = [
        WorkflowTool(name, f"{name} description", lambda args, name=name: {name: args})
        for name in tools_enabled
    ]
    resolved = ResolvedAgent(
        name=slug,
        slug=slug,
        description=f"{slug} description",
        trigger=TriggerSpec(type="timer_trigger", args={"schedule": "0 * * * * *"}),
        instructions=f"{slug} instructions",
        is_main=slug == "main",
        builtin_endpoints=BuiltinEndpointsConfig(),
        model=None,
        timeout=30,
        enabled_mcp_names=[],
        enabled_skills_names=[],
        tool_filter=ToolsFilter(),
        workflows=WorkflowConfig(
            enabled=True,
            subagents=tuple(WorkflowSubagentRef(agent=agent) for agent in subagents),
        ),
        sandbox_config=None,
        input_schema=None,
        response_schema=None,
        response_example=None,
        source_file=f"{slug}.agent.md",
    )
    return resolved, AgentCapabilities(filtered_workflow_tools=workflow_tools)


def test_owner_policy_catalog_is_immutable_and_keeps_owner_grants_independent() -> None:
    owner_a, capabilities_a = _resolved(
        "owner_a",
        tools_enabled=("shared",),
        subagents=("specialist_a",),
    )
    owner_b, capabilities_b = _resolved(
        "owner_b",
        tools_enabled=("shared", "only_b"),
        subagents=("specialist_b",),
    )
    specialist_a, specialist_capabilities_a = _resolved("specialist_a")
    specialist_a.workflows = None
    specialist_b, specialist_capabilities_b = _resolved("specialist_b")
    specialist_b.workflows = None
    catalog = build_catalog(
        {
            "owner_a": CatalogEntry(owner_a, capabilities_a),
            "owner_b": CatalogEntry(owner_b, capabilities_b),
            "specialist_a": CatalogEntry(specialist_a, specialist_capabilities_a),
            "specialist_b": CatalogEntry(specialist_b, specialist_capabilities_b),
        }
    )
    handlers = integration.build_workflow_handler_catalog(
        [
            WorkflowTool("shared", "shared description", lambda args: args),
            WorkflowTool("only_b", "only B description", lambda args: args),
        ]
    )

    policies = integration.build_workflow_owner_policy_catalog(catalog, handlers)

    assert isinstance(policies, MappingProxyType)
    assert policies["owner_a"].allowed_tools == frozenset({"shared"})
    assert policies["owner_b"].allowed_tools == frozenset({"shared", "only_b"})
    assert policies["owner_a"].allowed_subagents == frozenset({"specialist_a"})
    assert policies["owner_b"].allowed_subagents == frozenset({"specialist_b"})
    with pytest.raises(TypeError):
        policies["new"] = schema.WorkflowPlanPolicy(frozenset())  # type: ignore[index]


def test_owner_addenda_render_only_owner_specific_tools_and_subagents() -> None:
    owner_a, capabilities_a = _resolved(
        "owner_a",
        tools_enabled=("tool_a",),
        subagents=("specialist_a",),
    )
    owner_b, capabilities_b = _resolved(
        "owner_b",
        tools_enabled=("tool_b",),
        subagents=("specialist_b",),
    )
    specialist_a, specialist_capabilities_a = _resolved("specialist_a")
    specialist_a.workflows = None
    specialist_b, specialist_capabilities_b = _resolved("specialist_b")
    specialist_b.workflows = None
    catalog = build_catalog(
        {
            "owner_a": CatalogEntry(owner_a, capabilities_a),
            "owner_b": CatalogEntry(owner_b, capabilities_b),
            "specialist_a": CatalogEntry(
                specialist_a,
                specialist_capabilities_a,
            ),
            "specialist_b": CatalogEntry(
                specialist_b,
                specialist_capabilities_b,
            ),
        }
    )
    handlers = integration.build_workflow_handler_catalog(
        [
            WorkflowTool("tool_a", "Tool A", lambda args: args),
            WorkflowTool("tool_b", "Tool B", lambda args: args),
        ]
    )
    policies = integration.build_workflow_owner_policy_catalog(catalog, handlers)

    owner_a_integration = integration.build_owner_workflow_integration(
        policies["owner_a"],
        handlers,
    )
    owner_b_integration = integration.build_owner_workflow_integration(
        policies["owner_b"],
        handlers,
    )

    for addendum in (
        owner_a_integration.chat_system_addendum,
        owner_a_integration.trigger_system_addendum,
    ):
        assert addendum is not None
        assert "`tool_a`" in addendum
        assert "`specialist_a`" in addendum
        assert "`tool_b`" not in addendum
        assert "`specialist_b`" not in addendum
    for addendum in (
        owner_b_integration.chat_system_addendum,
        owner_b_integration.trigger_system_addendum,
    ):
        assert addendum is not None
        assert "`tool_b`" in addendum
        assert "`specialist_b`" in addendum
        assert "`tool_a`" not in addendum
        assert "`specialist_a`" not in addendum


def test_owner_and_session_identity_uses_distinct_128_bit_prefixes() -> None:
    first = context.new_workflow_instance_id("owner_a", "same-session")
    second = context.new_workflow_instance_id("owner_b", "same-session")

    first_prefix = first.split("-", 1)[0]
    second_prefix = second.split("-", 1)[0]
    assert len(first_prefix) == 32
    assert len(second_prefix) == 32
    assert first_prefix != second_prefix
    assert context.session_instance_prefix("a", "bc") != context.session_instance_prefix(
        "ab", "c"
    )
    assert context.session_owns_workflow("owner_a", "same-session", first)
    assert not context.session_owns_workflow("owner_b", "same-session", first)
    assert not context.session_owns_workflow(
        "owner_a",
        "same-session",
        "0123456789ab-00000000000000000000000000000000",
    )


class _StatusClient:
    def __init__(self, statuses: list[Any]) -> None:
        self.statuses = statuses
        self.status_by_id = {status.instance_id: status for status in statuses}
        self.terminated: list[str] = []
        self.canceled: list[str] = []

    async def get_status_all(self) -> list[Any]:
        return self.statuses

    async def get_status(self, workflow_id: str) -> Any:
        return self.status_by_id.get(workflow_id)

    async def terminate(self, workflow_id: str, reason: str) -> None:
        self.terminated.append(workflow_id)

    async def raise_event(self, workflow_id: str, event: str, reason: str) -> None:
        self.canceled.append(workflow_id)


class _Status:
    def __init__(self, instance_id: str) -> None:
        self.instance_id = instance_id
        self.runtime_status = "Running"
        self.custom_status = None
        self.output = None
        self.created_time = None
        self.last_updated_time = None


@pytest.mark.asyncio
async def test_same_session_cross_owner_management_is_not_found() -> None:
    workflow_id = context.new_workflow_instance_id("owner_a", "same-session")
    client = _StatusClient([_Status(workflow_id)])
    owner_b = context.WorkflowSessionContext(
        owner_slug="owner_b",
        session_id="same-session",
        agent_name="Owner B",
        durable_client=client,
        token="",
    )

    assert await tools.fetch_session_workflows(client, "owner_b", "same-session") == []
    assert (
        await tools.fetch_session_workflow_status(
            client, "owner_b", "same-session", workflow_id
        )
        is None
    )
    status = await tools.get_workflow_status(
        tools.GetWorkflowStatusParams(workflow_id=workflow_id), owner_b
    )
    cancel = await tools.cancel_workflow(
        tools.CancelWorkflowParams(workflow_id=workflow_id), owner_b
    )
    terminate = await tools.terminate_workflow(
        tools.TerminateWorkflowParams(workflow_id=workflow_id), owner_b
    )

    assert '"status": 404' in status
    assert '"status": 404' in cancel
    assert '"status": 404' in terminate
    assert client.canceled == []
    assert client.terminated == []


@pytest.mark.asyncio
async def test_active_count_is_isolated_by_owner_under_shared_session() -> None:
    client = _StatusClient(
        [_Status(context.new_workflow_instance_id("owner_a", "same-session"))]
    )

    assert (
        await tools.count_active_session_workflows(
            client,
            "owner_a",
            "same-session",
        )
        == 1
    )
    assert (
        await tools.count_active_session_workflows(
            client,
            "owner_b",
            "same-session",
        )
        == 0
    )
