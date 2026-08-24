"""Unit tests for the workflow tool registry and integration glue (M1 step 3c).

Exercises:

- ``register_workflow_tool`` invariants: collision, reserved names,
  async acceptance, public/private flag.
- ``validate_plan(allowed_tools=...)`` honoring the explicit allowlist
  and isolating it from the module-level fallback used by older tests.
- ``build_workflow_integration`` registering discovered workflow tools,
  honoring ``workflows.exclude``, and emitting an addendum that lists the
  effective workflow tool set.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import get_type_hints

import pytest

from azure_functions_agents._function_tool import (
    WorkflowTool,
    WorkflowToolMetadata,
    workflow_tool,
)
from azure_functions_agents.config.schema import WorkflowSubagentRef
from azure_functions_agents.registration.capabilities import AgentCapabilities
from azure_functions_agents.registration.catalog import CatalogEntry, build_catalog
from azure_functions_agents.workflows import context, engine, integration, registry, schema, tools


def test_workflow_tool_public_annotations_resolve_at_runtime() -> None:
    expected = schema.WorkflowRetryPolicy | None

    assert get_type_hints(WorkflowTool)["retry"] == expected
    assert get_type_hints(WorkflowToolMetadata)["retry"] == expected
    assert get_type_hints(workflow_tool)["retry"] == expected


@pytest.fixture(autouse=True)
def _reset_registry():
    """Restore the registry around every test.

    The engine's ``__echo`` registration runs at module import; we
    cache + restore the entries explicitly so other tests in the suite
    see the same starting state regardless of order.
    """
    saved_entries = dict(registry._REGISTRY)
    saved_allow = registry.get_app_config()
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(saved_entries)
    registry.set_app_config(saved_allow if saved_allow is not None else frozenset())
    # set_app_config requires a frozenset; restore None when there was none
    if saved_allow is None:
        registry._APP_ALLOWLIST = None


def _noop(args):
    return {"args": dict(args)}


class _FakeStatus:
    def __init__(
        self,
        instance_id,
        runtime_status,
        *,
        updated_seconds=0,
        output=None,
        custom_status=None,
    ):
        timestamp = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(
            seconds=updated_seconds
        )
        self.instance_id = instance_id
        self.runtime_status = runtime_status
        self.custom_status = custom_status
        self.output = output
        self.created_time = timestamp
        self.last_updated_time = timestamp


class _FailingDurableClient:
    secret = "durable storage account internal details"

    async def start_new(self, *args, **kwargs):
        raise RuntimeError(self.secret)

    async def get_status(self, *args, **kwargs):
        raise RuntimeError(self.secret)

    async def get_status_all(self, *args, **kwargs):
        raise RuntimeError(self.secret)

    async def terminate(self, *args, **kwargs):
        raise RuntimeError(self.secret)

    async def raise_event(self, *args, **kwargs):
        raise RuntimeError(self.secret)


class _CappedDurableClient:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.started = False
        self.start_kwargs = None

    async def get_status_all(self, *args, **kwargs):
        return self.statuses

    async def start_new(self, *args, **kwargs):
        self.started = True
        self.start_kwargs = kwargs
        return kwargs["instance_id"]


@pytest.fixture
def failing_workflow_session():
    return "session-1"


def _registered_blueprint_function(
    name,
    *,
    workflow_agent_policies=None,
):
    app = _FakeApp()
    engine.register_workflows(
        app, workflow_agent_policies=workflow_agent_policies
    )
    [blueprint] = app.blueprints
    for builder in blueprint._function_builders:
        function = builder._function
        if function._name == name:
            return function._func
    raise AssertionError(f"workflow function {name!r} was not registered")


# ---- registry ---------------------------------------------------------------


def test_register_workflow_tool_rejects_collision():
    registry.register_workflow_tool("alpha", "alpha tool", _noop)
    with pytest.raises(ValueError, match="already registered"):
        registry.register_workflow_tool("alpha", "alpha tool again", _noop)


def test_compatibility_session_registry_does_not_expose_or_confuse_registration_token():
    first_client = object()
    second_client = object()
    first_token = context.register_workflow_session(
        "workflow-agent",
        "session",
        "Workflow Agent",
        first_client,
    )
    second_token = context.register_workflow_session(
        "workflow-agent",
        "session",
        "Workflow Agent",
        second_client,
    )

    registered = context.get_workflow_session("workflow-agent", "session")
    assert registered is not None
    assert registered.durable_client is second_client
    assert not hasattr(registered, "token")

    context.unregister_workflow_session("workflow-agent", "session", first_token)
    assert context.get_workflow_session("workflow-agent", "session") is registered

    context.unregister_workflow_session("workflow-agent", "session", second_token)
    assert context.get_workflow_session("workflow-agent", "session") is None


def test_register_workflow_tool_rejects_reserved_name():
    for reserved in registry.RESERVED_TOOL_NAMES:
        with pytest.raises(ValueError, match="reserved"):
            registry.register_workflow_tool(reserved, "no", _noop)


def test_reserved_names_match_management_tools():
    """Parity guard: RESERVED_TOOL_NAMES must enumerate every tool that
    ``build_workflow_tools`` actually injects, otherwise a future
    addition could shadow a node-target name without anyone noticing.
    """
    actual = {tool.name for tool in tools.build_workflow_tools()}
    assert actual == set(registry.RESERVED_TOOL_NAMES)


def test_register_workflow_tool_accepts_async_handler():
    async def async_handler(args):
        return {}

    registry.register_workflow_tool("asynctool", "yes", async_handler)
    assert registry.get_entry("asynctool").handler is async_handler


def test_registry_freezes_workflow_execution_metadata():
    retry = schema.WorkflowRetryPolicy(
        max_attempts=2,
        backoff=schema.WorkflowRetryBackoff(
            initial="PT1S", multiplier=2.0, max="PT2S"
        ),
    )
    registry.register_workflow_tool(
        "bounded",
        "bounded tool",
        _noop,
        timeout="PT5S",
        retry=retry,
    )

    entry = registry.get_entry("bounded")
    assert entry is not None
    assert entry.timeout == "PT5S"
    assert entry.retry is retry
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.timeout = "PT6S"


def test_register_workflow_tool_rejects_non_callable():
    with pytest.raises(ValueError, match="must be a callable"):
        registry.register_workflow_tool("badtool", "no", "not a callable")  # type: ignore[arg-type]


def test_register_workflow_tool_rejects_blank_name():
    with pytest.raises(ValueError, match="non-empty string"):
        registry.register_workflow_tool("", "no", _noop)


def test_public_flag_excludes_tool_from_default_set():
    registry.register_workflow_tool("private_one", "no", _noop, public=False)
    registry.register_workflow_tool("public_one", "yes", _noop, public=True)
    public = registry.public_tool_names()
    assert "public_one" in public
    assert "private_one" not in public
    # __echo (registered at engine import) is also private.
    assert "__echo" not in public


# ---- validate_plan(allowed_tools=...) --------------------------------------


def _plan_one_tool(tool_name):
    return {
        "tasks": [
            {"id": "t1", "type": "tool", "tool": tool_name, "args": {}, "depends_on": []}
        ]
    }


def test_validate_plan_explicit_allowlist_accepts():
    registry.register_workflow_tool("evidence", "x", _noop)
    plan = schema.validate_plan(
        _plan_one_tool("evidence"), allowed_tools={"evidence"}
    )
    assert plan.tasks[0].tool == "evidence"


def test_validate_plan_explicit_allowlist_rejects_disallowed():
    registry.register_workflow_tool("evidence", "x", _noop)
    with pytest.raises(schema.PlanValidationError, match="not workflow-safe"):
        schema.validate_plan(
            _plan_one_tool("evidence"), allowed_tools={"something_else"}
        )


def test_validate_plan_requires_explicit_allowlist():
    # The fallback is gone — callers must pass allowed_tools.
    with pytest.raises(TypeError):
        schema.validate_plan(_plan_one_tool("__echo"))  # type: ignore[call-arg]


def test_validate_plan_with_empty_allowlist_rejects_any_tool():
    with pytest.raises(schema.PlanValidationError, match="not workflow-safe"):
        schema.validate_plan(_plan_one_tool("__echo"), allowed_tools=set())


# ---- build_workflow_integration --------------------------------------------


class _FakeApp:
    """Minimal stand-in so we can call build_workflow_integration without
    spinning up a real azure.functions FunctionApp.

    register_workflows only calls .register_blueprint on us.
    """

    def __init__(self):
        self.blueprints = []

    def register_blueprint(self, bp):
        self.blueprints.append(bp)


def _enable_metadata(exclude=None):
    block = {"enabled": True}
    if exclude is not None:
        block["exclude"] = exclude
    return {"workflows": block}


def _workflow_tool(
    name: str,
    description: str,
    handler=_noop,
    *,
    public: bool = True,
) -> WorkflowTool:
    return WorkflowTool(name, description, handler, public=public)


def _agent_catalog(**descriptions: str):
    return build_catalog(
        {
            slug: CatalogEntry(
                SimpleNamespace(slug=slug, description=description),  # type: ignore[arg-type]
                AgentCapabilities(),
            )
            for slug, description in descriptions.items()
        }
    )


def test_integration_default_workflow_tools_are_public_tools_only():
    result = integration.build_workflow_integration(
        _FakeApp(),
        _enable_metadata(),
        workflow_tools=[
            _workflow_tool("alpha", "alpha desc"),
            _workflow_tool("beta", "beta desc", public=False),
        ],
    )
    assert result.workflow_tools  # 5 management tools registered
    assert "alpha" in result.chat_system_addendum
    assert "beta" not in result.chat_system_addendum
    # __echo is private and must not leak into the default allowlist.
    assert "__echo" not in result.chat_system_addendum
    effective = registry.get_app_config()
    assert effective is not None
    assert "alpha" in effective and "beta" not in effective


def test_integration_exclude_filters_public_workflow_tools():
    result = integration.build_workflow_integration(
        _FakeApp(),
        _enable_metadata(exclude=["beta"]),
        workflow_tools=[
            _workflow_tool("alpha", "alpha desc"),
            _workflow_tool("beta", "beta desc"),
        ],
    )
    assert "alpha" in result.chat_system_addendum
    assert "beta" not in result.chat_system_addendum
    assert registry.get_app_config() == frozenset({"alpha"})


def test_integration_malformed_exclude_fails_at_app_start():
    with pytest.raises(RuntimeError, match="must be a list of non-empty strings"):
        integration.build_workflow_integration(
            _FakeApp(), {"workflows": {"enabled": True, "exclude": "not-a-list"}}
        )


def test_integration_no_workflow_tools_yields_empty_effective_set():
    result = integration.build_workflow_integration(
        _FakeApp(), _enable_metadata()
    )
    assert result.workflow_tools  # management tools still come back
    assert "No tool tasks are currently allowed" in result.chat_system_addendum
    assert "No Sub Agent tasks are allowed" in result.chat_system_addendum
    assert result.plan_policy is not None
    assert result.plan_policy.allowed_subagents == frozenset()
    assert registry.get_app_config() == frozenset()


def test_integration_disabled_returns_empty_and_does_not_set_config():
    # Stash a sentinel and ensure the disabled path doesn't clobber it.
    registry.set_app_config(frozenset({"sentinel"}))
    result = integration.build_workflow_integration(
        _FakeApp(), {"workflows": {"enabled": False}}
    )
    assert result.workflow_tools == []
    assert result.chat_system_addendum is None
    assert result.trigger_system_addendum is None
    assert registry.get_app_config() == frozenset({"sentinel"})


def test_addendum_includes_per_tool_descriptions():
    result = integration.build_workflow_integration(
        _FakeApp(),
        _enable_metadata(),
        workflow_tools=[
            _workflow_tool(
                "demo_evidence_tool",
                "Sample tool for the addendum-rendering test.",
            )
        ],
    )
    addendum = result.chat_system_addendum
    assert "## Long-running work: workflows" in addendum
    assert "### Available workflow tools" in addendum
    assert "`demo_evidence_tool`" in addendum
    assert "Sample tool for the addendum-rendering test." in addendum


def test_data_driven_control_flow_grammar_uses_progressive_skill_disclosure():
    result = integration.build_workflow_integration(
        _FakeApp(),
        _enable_metadata(),
        workflow_tools=[_workflow_tool("alpha", "alpha desc")],
    )

    for addendum in (result.chat_system_addendum, result.trigger_system_addendum):
        assert "data-driven-workflows" not in addendum
        assert "`for_each`" not in addendum
        assert "`when`" not in addendum
        assert "${item.path.to.field}" not in addendum
        assert "{index, status, result}" not in addendum

    skill_path = integration.data_driven_workflows_skill_path()
    skill = (skill_path / "SKILL.md").read_text(encoding="utf-8")
    assert "name: data-driven-workflows" in skill
    assert "Irrelevant to fixed task lists" in skill
    assert "`for_each`" in skill
    assert "tool` or `sub_agent`" in skill
    assert "never `wait`" in skill
    assert "${discover.result.items}" in skill
    assert "${item}" in skill
    assert "${item.path.to.field}" in skill
    assert "${index}" in skill
    assert "`item` and `index` are reserved task ids" in skill
    assert "Keep the target tool/agent name static" in skill
    assert "`when`" in skill
    assert '"operator": "equals" | "not_equals"' in skill
    assert "JSON scalar" in skill
    assert "exact typed equality" in skill
    assert "skip does not propagate" in skill
    assert "evaluated before" in skill
    assert "{index, status, result}" in skill
    assert "source order" in skill
    assert "${node_id.result}" in skill
    assert "already bounded" in skill


def test_integration_builds_owner_specific_policy_and_sub_agent_guidance() -> None:
    result = integration.build_workflow_integration(
        _FakeApp(),
        _enable_metadata(),
        workflow_tools=[_workflow_tool("publish", "Publish the report.")],
        workflow_subagents=[
            WorkflowSubagentRef(
                agent="pr_status_analyst",
                when="Analyze one pull request.",
            ),
            WorkflowSubagentRef(agent="report_writer"),
        ],
        catalog=_agent_catalog(
            pr_status_analyst="Default analyst description.",
            report_writer="Create an actionable report.",
        ),
    )

    assert result.plan_policy == schema.WorkflowPlanPolicy(
        allowed_tools=frozenset({"publish"}),
        allowed_subagents=frozenset({"pr_status_analyst", "report_writer"}),
        subagent_guidance=(
            ("pr_status_analyst", "Analyze one pull request."),
            ("report_writer", "Create an actionable report."),
        ),
        subagent_timeout_ms={
            "pr_status_analyst": 900_000,
            "report_writer": 900_000,
        },
    )
    assert "### Available workflow Sub Agents" in result.chat_system_addendum
    assert "`pr_status_analyst`" in result.chat_system_addendum
    assert "Analyze one pull request." in result.chat_system_addendum
    assert "`${node_id.result.text}`" in result.chat_system_addendum


def test_integration_derives_immutable_execution_resolution_metadata() -> None:
    catalog = _agent_catalog(analyst="Analyze one report.")
    catalog["analyst"].resolved.timeout = 45.5
    retry = _bounded_retry()
    result = integration.build_workflow_integration(
        _FakeApp(),
        _enable_metadata(),
        workflow_tools=[
            WorkflowTool(
                "publish",
                "Publish.",
                _noop,
                timeout="PT5S",
                retry=retry,
            )
        ],
        workflow_subagents=[WorkflowSubagentRef(agent="analyst")],
        catalog=catalog,
    )

    assert result.plan_policy is not None
    assert result.plan_policy.tool_execution["publish"] == (
        schema.WorkflowToolExecutionPolicy(timeout="PT5S", retry=retry)
    )
    assert result.plan_policy.subagent_timeout_ms == {"analyst": 45_500}
    with pytest.raises(TypeError):
        result.plan_policy.subagent_timeout_ms["analyst"] = 1


def test_integration_fails_closed_when_authorized_sub_agent_is_missing() -> None:
    with pytest.raises(RuntimeError, match="not available in the AgentCatalog"):
        integration.build_workflow_integration(
            _FakeApp(),
            _enable_metadata(),
            workflow_subagents=[WorkflowSubagentRef(agent="missing")],
            catalog=_agent_catalog(known="Known specialist."),
        )


def test_integration_policies_for_different_owners_do_not_mix() -> None:
    catalog = _agent_catalog(
        analyst_a="Analyze A.",
        analyst_b="Analyze B.",
    )
    first = integration.build_workflow_integration(
        _FakeApp(),
        _enable_metadata(),
        workflow_subagents=[WorkflowSubagentRef(agent="analyst_a")],
        catalog=catalog,
    )
    second = integration.build_workflow_integration(
        _FakeApp(),
        _enable_metadata(),
        workflow_subagents=[WorkflowSubagentRef(agent="analyst_b")],
        catalog=catalog,
    )

    assert first.plan_policy is not None
    assert second.plan_policy is not None
    assert first.plan_policy.allowed_subagents == frozenset({"analyst_a"})
    assert second.plan_policy.allowed_subagents == frozenset({"analyst_b"})


def test_addendum_enforces_fire_and_forget_no_poll_guidance():
    """Regression guard: the addendum, the start_workflow tool description,
    and the get_workflow_status tool description must all instruct the LLM
    to NOT poll after start_workflow. The chat UI is the result channel.
    A previous version of these prompts told the agent to poll, which kept
    the agent's turn alive and (a) burned tokens and (b) blocked the chat
    input box from re-enabling — surfacing as the demo bug that motivated
    this guard.
    """
    result = integration.build_workflow_integration(
        _FakeApp(),
        _enable_metadata(),
        workflow_tools=[
            _workflow_tool(
                "demo_evidence_tool",
                "Sample tool for the no-poll regression test.",
            )
        ],
    )
    # Addendum contract: explicit fire-and-forget framing + explicit
    # negative on get_workflow_status auto-polling.
    addendum = result.chat_system_addendum
    assert "fire-and-forget" in addendum
    assert "end your turn" in addendum
    assert "do not call `get_workflow_status` to wait" in addendum
    assert "End workflows with a small summary task" in addendum
    assert "Do not return large raw evidence" in addendum
    trigger_addendum = result.trigger_system_addendum
    assert "fire-and-forget" in trigger_addendum
    assert "end this agent turn promptly" in trigger_addendum
    assert "do not poll `get_workflow_status`" in trigger_addendum.lower()
    # Tool descriptions must not encourage polling either, otherwise the
    # tool-call contract overrides the addendum.
    descriptions = {tool.name: tool.description for tool in result.workflow_tools}
    assert "fire-and-forget" in descriptions["start_workflow"]
    assert "do not poll get_workflow_status" in descriptions["start_workflow"]
    assert "only when the user explicitly asks" in descriptions["get_workflow_status"]
    # Negative checks: the prior wording must not creep back in.
    assert "Poll this" not in descriptions["get_workflow_status"]
    assert "call get_workflow_status to check progress" not in descriptions["start_workflow"]


def test_addendum_documents_workflow_notification_contract():
    """Regression guard: the addendum and both relevant tool descriptions
    must teach the agent the chat-client-injected `<workflow-notification>`
    envelope contract — call get_workflow_status once per listed
    `<workflow-id>`, summarize, do not start follow-on work. Without this
    guidance the agent either (a) ignores the synthetic prompt as noise
    or (b) tries to keep polling instead of treating the notification as
    terminal. The XML envelope shape (modeled on the `<task-notification>`
    pattern from Claude Code-style harnesses) is preferred over a
    free-form prefix because it is robust against prefix collisions in
    user input and lets a future UI parse the wrapper for richer
    rendering without changing the agent contract.
    """
    result = integration.build_workflow_integration(
        _FakeApp(),
        _enable_metadata(),
        workflow_tools=[
            _workflow_tool(
                "demo_evidence_tool",
                "Sample tool for the notification-contract regression test.",
            )
        ],
    )
    # Addendum must name the envelope shape verbatim (so the LLM sees
    # the exact tags it will receive) and explain the one-shot
    # summarize-only contract.
    addendum = result.chat_system_addendum
    assert "<workflow-notification>" in addendum
    assert "<workflow-id>" in addendum
    assert "<status>" in addendum
    assert "summary-only" in addendum
    # The auto-injected per-turn prompt is intentionally data-only
    # (envelope + a one-line tool reminder). Anything previously inlined
    # in that prompt and trimmed away must be pinned here so a future
    # refactor of the addendum doesn't silently drop the contract:
    #   * "no follow-on workflows" — the notification turn is summary-only
    #     and the agent must not start new workflows or extra tool calls
    #     unless the user later asks for a deeper look.
    #   * race handling — if `get_workflow_status` returns a non-terminal
    #     status after a notification, the agent must say so and end the
    #     turn rather than polling again.
    #   * empty-output handling — terminated/canceled workflows with no
    #     usable final output must be reported plainly.
    #   * cancel-vs-terminate guidance — `cancel_workflow` is preferred
    #     when the user changes their mind; `terminate_workflow` is the
    #     abrupt escape hatch.
    assert "do not start new workflows" in addendum
    assert "non-terminal" in addendum
    assert "do not poll again" in addendum
    assert "without a usable final output" in addendum
    assert "say so plainly" in addendum
    assert "cancel_workflow" in addendum
    assert "terminate_workflow" in addendum
    # The legacy free-form prefix must not creep back in — it would
    # produce conflicting guidance and confuse the agent.
    assert "[Workflow notification]" not in addendum
    trigger_addendum = result.trigger_system_addendum
    assert "There is no built-in chat poller" in trigger_addendum
    assert "synthetic `<workflow-notification>` turn" in trigger_addendum
    assert "chat client injects a synthetic user message" not in trigger_addendum
    assert "do not poll" in trigger_addendum.lower()
    # Tool descriptions must reinforce the same envelope so the LLM
    # sees it both at system-prompt time and at tool-call selection time.
    descriptions = {tool.name: tool.description for tool in result.workflow_tools}
    assert "<workflow-notification>" in descriptions["start_workflow"]
    assert "<workflow-notification>" in descriptions["get_workflow_status"]
    assert "[Workflow notification]" not in descriptions["start_workflow"]
    assert "[Workflow notification]" not in descriptions["get_workflow_status"]


# ---- workflow activity failure handling -------------------------------------


@pytest.mark.asyncio
async def test_workflow_activity_logs_tool_exceptions_without_raising_raw_details(caplog):
    secret_message = "downstream API token and account details"

    def exploding_tool(args):
        raise RuntimeError(secret_message)

    registry.register_workflow_tool("exploding", "Always fails.", exploding_tool)
    activity = _registered_blueprint_function(
        "agents_workflow_run_tool",
        workflow_agent_policies={
            "test-agent": schema.WorkflowPlanPolicy(
                allowed_tools=frozenset({"exploding"}),
                allowed_subagents=frozenset(),
            )
        },
    )

    with pytest.raises(RuntimeError) as excinfo:
        await activity(
            {
                "id": "explode",
                "tool": "exploding",
                "args": {},
                "workflow_agent_slug": "test-agent",
                "workflow_id": "workflow-1",
            }
        )

    assert str(excinfo.value) == "task 'explode': workflow-safe tool failed"
    assert secret_message not in str(excinfo.value)
    assert any(
        record.message
        == (
            "workflow activity failed: workflow_id=workflow-1 "
            "workflow_agent=test-agent id=explode tool=exploding"
        )
        and record.exc_info
        and secret_message in str(record.exc_info[1])
        for record in caplog.records
    )


# ---- workflow tool error handling -------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call_tool", "expected_error", "expected_log"),
    [
        (
            lambda workflow_id, session: tools.start_workflow(
                tools.StartWorkflowParams(
                    tasks=[{"id": "pause", "type": "wait", "duration": "PT1S"}]
                ),
                session,
            ),
            "failed to start workflow",
            "start_workflow: client.get_status_all failed",
        ),
        (
            lambda workflow_id, session: tools.get_workflow_status(
                tools.GetWorkflowStatusParams(workflow_id=workflow_id),
                session,
            ),
            "failed to fetch workflow status",
            "get_workflow_status: client.get_status failed",
        ),
        (
            lambda workflow_id, session: tools.list_workflows(
                tools.ListWorkflowsParams(),
                session,
            ),
            "failed to list workflows",
            "list_workflows: fetch_session_workflows failed",
        ),
        (
            lambda workflow_id, session: tools.terminate_workflow(
                tools.TerminateWorkflowParams(workflow_id=workflow_id),
                session,
            ),
            "failed to terminate workflow",
            "terminate_workflow: client.terminate failed",
        ),
        (
            lambda workflow_id, session: tools.cancel_workflow(
                tools.CancelWorkflowParams(workflow_id=workflow_id),
                session,
            ),
            "failed to cancel workflow",
            "cancel_workflow: client.raise_event failed",
        ),
    ],
)
async def test_workflow_tools_log_durable_exceptions_without_returning_details(
    failing_workflow_session, caplog, call_tool, expected_error, expected_log
):
    registry.set_app_config(frozenset())
    workflow_id = context.new_workflow_instance_id(
        "test-agent",
        failing_workflow_session,
    )
    session = context.WorkflowSessionContext(
        workflow_agent_slug="test-agent",
        session_id=failing_workflow_session,
        agent_name="test-agent",
        durable_client=_FailingDurableClient(),
    )

    text_result = await call_tool(workflow_id, session)

    assert text_result == json.dumps({"error": expected_error})
    assert _FailingDurableClient.secret not in text_result
    assert any(
        record.message.startswith(expected_log)
        and record.exc_info
        and _FailingDurableClient.secret in str(record.exc_info[1])
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_start_workflow_rejects_new_workflow_when_session_active_cap_reached():
    session_id = "session-1"
    statuses = [
        _FakeStatus(
            context.new_workflow_instance_id("test-agent", session_id),
            "Running",
            updated_seconds=i,
        )
        for i in range(10)
    ]
    client = _CappedDurableClient(statuses)
    session = context.WorkflowSessionContext(
        workflow_agent_slug="test-agent",
        session_id=session_id,
        agent_name="test-agent",
        durable_client=client,
    )
    registry.set_app_config(frozenset())
    result = await tools.start_workflow(
        tools.StartWorkflowParams(
            tasks=[{"id": "pause", "type": "wait", "duration": "PT1S"}]
        ),
        session,
    )

    assert json.loads(result) == {
        "error": "too many active workflows for this session",
        "active": 10,
        "limit": 10,
    }
    assert client.started is False


@pytest.mark.asyncio
async def test_start_workflow_uses_passed_policy_for_sub_agent_authorization() -> None:
    class _UnexpectedClient:
        async def get_status_all(self):
            raise AssertionError("authorization must fail before Durable scheduling")

    session = context.WorkflowSessionContext(
        workflow_agent_slug="coordinator",
        session_id="session-1",
        agent_name="coordinator",
        durable_client=_UnexpectedClient(),
    )
    params = tools.StartWorkflowParams(
        tasks=[
            {
                "id": "analyze",
                "type": "sub_agent",
                "agent": "not_allowed",
                "task": "Analyze one pull request.",
            }
        ]
    )
    policy = schema.WorkflowPlanPolicy(
        allowed_tools=frozenset(),
        allowed_subagents=frozenset({"pr_status_analyst"}),
    )

    result = await tools.start_workflow(params, session, policy=policy)

    assert "not authorized" in json.loads(result)["error"]


@pytest.mark.asyncio
async def test_start_workflow_threads_workflow_agent_slug_into_durable_input() -> None:
    client = _CappedDurableClient([])
    session = context.WorkflowSessionContext(
        workflow_agent_slug="incident",
        session_id="session-1",
        agent_name="Incident",
        durable_client=client,
    )
    policy = schema.WorkflowPlanPolicy(
        allowed_tools=frozenset(),
        allowed_subagents=frozenset(),
    )

    result = await tools.start_workflow(
        tools.StartWorkflowParams(
            tasks=[{"id": "pause", "type": "wait", "duration": "PT1S"}]
        ),
        session,
        policy=policy,
    )

    assert "workflow_id" in json.loads(result)
    assert client.start_kwargs["client_input"]["workflow_agent_slug"] == "incident"
    assert client.start_kwargs["client_input"]["workflow_agent"] == {
        "workflow_agent_slug": "incident",
        "session_id": "session-1",
        "agent_name": "Incident",
    }


def test_start_workflow_params_survive_framework_default_materialization() -> None:
    original = tools.StartWorkflowParams(
        tasks=[
            {
                "id": "analyze",
                "type": "sub_agent",
                "agent": "pr_status_analyst",
                "task": "Analyze one pull request.",
            },
            {
                "id": "publish",
                "tool": "publish_report",
                "args": {"report": "${analyze.result.text}"},
                "depends_on": ["analyze"],
            },
        ]
    )

    materialized = tools.StartWorkflowParams.model_validate(original.model_dump())

    assert set(materialized.model_dump()["tasks"][0]) == {
        "id",
        "type",
        "agent",
        "task",
        "depends_on",
    }


def test_start_workflow_params_serialize_dynamic_task_fields_when_supplied() -> None:
    params = tools.StartWorkflowParams(
        tasks=[
            {
                "id": "discover",
                "tool": "discover_pull_requests",
            },
            {
                "id": "analyze",
                "tool": "analyze_pull_request",
                "depends_on": ["discover"],
                "for_each": "${discover.result.items}",
                "when": {
                    "ref": "${item.open}",
                    "operator": "equals",
                    "value": True,
                },
                "args": {"url": "${item.url}", "index": "${index}"},
            },
        ]
    )

    assert params.model_dump()["tasks"][1] == {
        "id": "analyze",
        "depends_on": ["discover"],
        "when": {"ref": "${item.open}", "operator": "equals", "value": True},
        "type": "tool",
        "tool": "analyze_pull_request",
        "args": {"url": "${item.url}", "index": "${index}"},
        "for_each": "${discover.result.items}",
    }


def test_start_workflow_params_reject_execution_on_wait_task() -> None:
    with pytest.raises(ValueError, match="execution"):
        tools.StartWorkflowParams.model_validate(
            {
                "tasks": [
                    {
                        "id": "pause",
                        "type": "wait",
                        "duration": "PT1S",
                        "execution": {},
                    }
                ]
            }
        )


@pytest.mark.asyncio
async def test_start_workflow_serializes_stable_reference_validation_metadata() -> None:
    class _UnexpectedClient:
        async def get_status_all(self):
            raise AssertionError("validation must fail before Durable scheduling")

    session = context.WorkflowSessionContext(
        workflow_agent_slug="coordinator",
        session_id="session-1",
        agent_name="coordinator",
        durable_client=_UnexpectedClient(),
    )
    params = tools.StartWorkflowParams(
        tasks=[
            {
                "id": "target",
                "tool": "__echo",
                "args": {"value": "${missing.result.value}"},
            }
        ]
    )
    policy = schema.WorkflowPlanPolicy(
        allowed_tools=frozenset({"__echo"}),
        allowed_subagents=frozenset(),
    )

    result = json.loads(await tools.start_workflow(params, session, policy=policy))

    assert result["error_code"] == "workflow_reference_unresolved"
    assert result["node_id"] == "target"
    assert result["path"] == "args.value"
    assert "unknown task" in result["error"]


@pytest.mark.asyncio
async def test_fetch_session_workflows_returns_newest_session_workflows_up_to_v1_cap():
    session_id = "session-1"
    other_session_id = "session-2"
    statuses = [
        _FakeStatus(
            context.new_workflow_instance_id("test-agent", session_id),
            "Completed",
            updated_seconds=i,
        )
        for i in range(30)
    ]
    statuses.extend(
        _FakeStatus(
            context.new_workflow_instance_id("test-agent", other_session_id),
            "Completed",
            updated_seconds=100 + i,
        )
        for i in range(3)
    )
    client = _CappedDurableClient(statuses)

    envelopes = await tools.fetch_session_workflows(
        client,
        "test-agent",
        session_id,
    )

    assert len(envelopes) == 25
    assert [env["last_updated_time"] for env in envelopes] == sorted(
        [env["last_updated_time"] for env in envelopes],
        reverse=True,
    )
    assert envelopes[0]["last_updated_time"].endswith("00:00:29+00:00")
    assert envelopes[-1]["last_updated_time"].endswith("00:00:05+00:00")


# --- Controlled-failure status mapping (Issue #1276) -----------------------


def _failed_output() -> dict:
    return {
        "failed": True,
        "error": "task 'analyze': for_each did not resolve to an array",
        "error_code": "workflow_iteration_not_array",
        "node_id": "analyze",
        "path": "${disc.result.items}",
        "results": {"disc": {"items": {"not": "a list"}}},
    }


def test_status_envelope_maps_failed_output_to_failed_runtime_status():
    status = _FakeStatus(
        "wf-1",
        "Completed",
        output=_failed_output(),
        custom_status={"schema_version": 2},
    )

    envelope = tools.status_envelope(status)

    assert envelope["runtime_status"] == "Failed"
    # The controlled-failure payload is passed through untouched.
    assert envelope["output"] == _failed_output()


def test_is_active_status_false_for_failed_output():
    status = _FakeStatus("wf-1", "Completed", output=_failed_output())

    assert tools._is_active_status(status) is False


def test_status_envelope_completed_success_stays_completed():
    status = _FakeStatus(
        "wf-1", "Completed", output={"results": {"a": {"ok": True}}}
    )

    assert tools.status_envelope(status)["runtime_status"] == "Completed"


def test_status_envelope_canceled_output_still_maps_to_canceled():
    status = _FakeStatus(
        "wf-1",
        "Completed",
        output={"results": {}, "canceled": True, "reason": "stop"},
    )

    assert tools.status_envelope(status)["runtime_status"] == "Canceled"


def test_status_envelope_native_failed_output_is_untouched():
    native = {"opaque": "provider stack trace"}
    status = _FakeStatus("wf-1", "Failed", output=native)

    envelope = tools.status_envelope(status)

    # A native Durable Failed keeps its runtime_status and opaque output;
    # the controlled-failure adapter only fires on Completed outputs.
    assert envelope["runtime_status"] == "Failed"
    assert envelope["output"] == native


# --- Owner policy persisted in the orchestration client input --------------


class _CapturingDurableClient:
    def __init__(self):
        self.client_input = None

    async def get_status_all(self, *args, **kwargs):
        return []

    async def start_new(self, *args, **kwargs):
        self.client_input = kwargs["client_input"]
        return kwargs["instance_id"]


@pytest.mark.asyncio
async def test_start_workflow_persists_sorted_owner_policy_in_client_input():
    client = _CapturingDurableClient()
    session = context.WorkflowSessionContext(
        workflow_agent_slug="coordinator",
        session_id="session-1",
        agent_name="coordinator",
        durable_client=client,
    )
    params = tools.StartWorkflowParams(
        tasks=[{"id": "target", "tool": "__echo", "args": {"value": "hi"}}]
    )
    policy = schema.WorkflowPlanPolicy(
        allowed_tools=frozenset({"__echo"}),
        allowed_subagents=frozenset({"zeta", "alpha"}),
    )

    result = json.loads(await tools.start_workflow(params, session, policy=policy))

    assert "workflow_id" in result
    persisted = client.client_input["policy"]
    assert persisted["allowed_tools"] == sorted(policy.allowed_tools)
    assert persisted["allowed_subagents"] == ["alpha", "zeta"]


def _bounded_retry(attempts: int = 2) -> schema.WorkflowRetryPolicy:
    return schema.WorkflowRetryPolicy(
        max_attempts=attempts,
        backoff=schema.WorkflowRetryBackoff(
            initial="PT1S",
            multiplier=2.0,
            max="PT5S",
        ),
    )


@pytest.mark.asyncio
async def test_start_workflow_persists_decorator_precedence_as_effective_policy():
    client = _CappedDurableClient([])
    session = context.WorkflowSessionContext(
        workflow_agent_slug="coordinator",
        session_id="session-1",
        agent_name="coordinator",
        durable_client=client,
    )
    policy = schema.WorkflowPlanPolicy(
        allowed_tools=frozenset({"publish"}),
        tool_execution={
            "publish": schema.WorkflowToolExecutionPolicy(
                timeout="PT5S",
                retry=schema.WorkflowRetryPolicy(max_attempts=1),
            )
        },
    )
    params = tools.StartWorkflowParams(
        tasks=[
            {
                "id": "publish",
                "tool": "publish",
                "execution": {
                    "timeout": "PT20S",
                    "retry": _bounded_retry().model_dump(),
                    "continue_on_error": True,
                },
            }
        ]
    )

    result = json.loads(await tools.start_workflow(params, session, policy=policy))

    assert "workflow_id" in result
    assert client.start_kwargs["client_input"]["tasks"][0]["execution"] == {
        "timeout_ms": 5_000,
        "max_attempts": 1,
        "retry_delays_ms": [],
        "continue_on_error": True,
        "timeout_source": "decorator",
        "retry_source": "decorator",
    }


@pytest.mark.asyncio
async def test_start_workflow_persists_authored_empty_execution_defaults():
    client = _CappedDurableClient([])
    session = context.WorkflowSessionContext(
        workflow_agent_slug="coordinator",
        session_id="session-1",
        agent_name="coordinator",
        durable_client=client,
    )
    policy = schema.WorkflowPlanPolicy(allowed_tools=frozenset({"publish"}))
    params = tools.StartWorkflowParams(
        tasks=[{"id": "publish", "tool": "publish", "execution": {}}]
    )

    result = json.loads(await tools.start_workflow(params, session, policy=policy))

    assert "workflow_id" in result
    assert client.start_kwargs["client_input"]["tasks"][0]["execution"] == {
        "timeout_ms": 600_000,
        "max_attempts": 1,
        "retry_delays_ms": [],
        "continue_on_error": False,
        "timeout_source": "runtime_default",
        "retry_source": "runtime_default",
    }


@pytest.mark.asyncio
async def test_start_workflow_rejects_subagent_timeout_above_resolved_bound():
    client = _CappedDurableClient([])
    session = context.WorkflowSessionContext(
        workflow_agent_slug="coordinator",
        session_id="session-1",
        agent_name="coordinator",
        durable_client=client,
    )
    policy = schema.WorkflowPlanPolicy(
        allowed_tools=frozenset(),
        allowed_subagents=frozenset({"analyst"}),
        subagent_timeout_ms={"analyst": 60_000},
    )
    params = tools.StartWorkflowParams(
        tasks=[
            {
                "id": "analyze",
                "type": "sub_agent",
                "agent": "analyst",
                "task": "Analyze.",
                "execution": {"timeout": "PT2M"},
            }
        ]
    )

    result = json.loads(await tools.start_workflow(params, session, policy=policy))

    assert "resolved agent timeout" in result["error"]
    assert client.started is False


@pytest.mark.asyncio
async def test_start_workflow_keeps_policy_free_task_input_byte_compatible():
    client = _CappedDurableClient([])
    session = context.WorkflowSessionContext(
        workflow_agent_slug="coordinator",
        session_id="session-1",
        agent_name="coordinator",
        durable_client=client,
    )
    policy = schema.WorkflowPlanPolicy(allowed_tools=frozenset({"publish"}))
    params = tools.StartWorkflowParams(
        tasks=[{"id": "publish", "tool": "publish", "args": {"value": "ok"}}]
    )

    result = json.loads(await tools.start_workflow(params, session, policy=policy))

    assert "workflow_id" in result
    assert client.start_kwargs["client_input"]["tasks"] == [
        {
            "id": "publish",
            "type": "tool",
            "tool": "publish",
            "args": {"value": "ok"},
            "depends_on": [],
        }
    ]
