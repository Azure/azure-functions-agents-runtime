"""Unit tests for the workflow tool registry and integration glue (M1 step 3c).

Exercises:

- ``register_workflow_tool`` invariants: collision, reserved names,
  async rejection, public/private flag.
- ``validate_plan(allowed_tools=...)`` honoring the explicit allowlist
  and isolating it from the module-level fallback used by older tests.
- ``build_workflow_integration`` reading ``workflows.allowed_tools``
  from frontmatter, rejecting unknown / reserved entries, defaulting
  to the public-tools set, and emitting an addendum that lists the
  effective allowlist.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from azure_functions_agents.workflows import context, engine, integration, registry, schema, tools


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

    async def get_status_all(self, *args, **kwargs):
        return self.statuses

    async def start_new(self, *args, **kwargs):
        self.started = True
        return kwargs["instance_id"]


@pytest.fixture
def failing_workflow_session():
    session_id = "session-1"
    token = context.register_workflow_session(
        session_id,
        "test-agent",
        _FailingDurableClient(),
    )
    try:
        yield session_id
    finally:
        context.unregister_workflow_session(session_id, token)


def _registered_blueprint_function(name):
    app = _FakeApp()
    engine.register_workflows(app)
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


def test_register_workflow_tool_rejects_async_handler():
    async def async_handler(args):
        return {}

    with pytest.raises(ValueError, match="async handlers are not supported"):
        registry.register_workflow_tool("asynctool", "no", async_handler)


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


def _enable_metadata(allowed=None):
    block = {"enabled": True}
    if allowed is not None:
        block["allowed_tools"] = allowed
    return {"workflows": block}


def test_integration_default_allowlist_is_public_tools_only():
    registry.register_workflow_tool("alpha", "alpha desc", _noop)
    registry.register_workflow_tool("beta", "beta desc", _noop, public=False)
    tools, addendum = integration.build_workflow_integration(
        _FakeApp(), _enable_metadata()
    )
    assert tools  # 5 management tools registered
    assert "alpha" in addendum
    assert "beta" not in addendum
    # __echo is private and must not leak into the default allowlist.
    assert "__echo" not in addendum
    effective = registry.get_app_config()
    assert effective is not None
    assert "alpha" in effective and "beta" not in effective


def test_integration_explicit_allowlist_admits_private_tools():
    registry.register_workflow_tool("alpha", "alpha desc", _noop, public=False)
    _, addendum = integration.build_workflow_integration(
        _FakeApp(), _enable_metadata(allowed=["alpha"])
    )
    assert "alpha" in addendum
    assert registry.get_app_config() == frozenset({"alpha"})


def test_integration_unknown_tool_in_allowlist_fails_at_app_start():
    with pytest.raises(RuntimeError, match="unknown tool name"):
        integration.build_workflow_integration(
            _FakeApp(), _enable_metadata(allowed=["does_not_exist"])
        )


def test_integration_reserved_tool_in_allowlist_fails_at_app_start():
    with pytest.raises(RuntimeError, match="cannot include workflow-management"):
        integration.build_workflow_integration(
            _FakeApp(), _enable_metadata(allowed=["start_workflow"])
        )


def test_integration_malformed_allowlist_fails_at_app_start():
    with pytest.raises(RuntimeError, match="must be a list of non-empty strings"):
        integration.build_workflow_integration(
            _FakeApp(), {"workflows": {"enabled": True, "allowed_tools": "not-a-list"}}
        )


def test_integration_empty_allowlist_yields_empty_effective_set():
    tools, addendum = integration.build_workflow_integration(
        _FakeApp(), _enable_metadata(allowed=[])
    )
    assert tools  # management tools still come back
    assert "No tool tasks are currently allowed" in addendum
    assert registry.get_app_config() == frozenset()


def test_integration_disabled_returns_empty_and_does_not_set_config():
    # Stash a sentinel and ensure the disabled path doesn't clobber it.
    registry.set_app_config(frozenset({"sentinel"}))
    tools, addendum = integration.build_workflow_integration(
        _FakeApp(), {"workflows": {"enabled": False}}
    )
    assert tools == [] and addendum is None
    assert registry.get_app_config() == frozenset({"sentinel"})


def test_addendum_includes_per_tool_descriptions():
    registry.register_workflow_tool(
        "demo_evidence_tool",
        "Sample tool for the addendum-rendering test.",
        _noop,
    )
    _, addendum = integration.build_workflow_integration(
        _FakeApp(), _enable_metadata(allowed=["demo_evidence_tool"])
    )
    assert "## Long-running work: workflows" in addendum
    assert "### Available workflow tools" in addendum
    assert "`demo_evidence_tool`" in addendum
    assert "Sample tool for the addendum-rendering test." in addendum


def test_addendum_enforces_fire_and_forget_no_poll_guidance():
    """Regression guard: the addendum, the start_workflow tool description,
    and the get_workflow_status tool description must all instruct the LLM
    to NOT poll after start_workflow. The chat UI is the result channel.
    A previous version of these prompts told the agent to poll, which kept
    the agent's turn alive and (a) burned tokens and (b) blocked the chat
    input box from re-enabling — surfacing as the demo bug that motivated
    this guard.
    """
    registry.register_workflow_tool(
        "demo_evidence_tool",
        "Sample tool for the no-poll regression test.",
        _noop,
    )
    tools, addendum = integration.build_workflow_integration(
        _FakeApp(), _enable_metadata(allowed=["demo_evidence_tool"])
    )
    # Addendum contract: explicit fire-and-forget framing + explicit
    # negative on get_workflow_status auto-polling.
    assert "fire-and-forget" in addendum
    assert "end your turn" in addendum
    assert "do not call `get_workflow_status` to wait" in addendum
    assert "End workflows with a small summary task" in addendum
    assert "Do not return large raw evidence" in addendum
    # Tool descriptions must not encourage polling either, otherwise the
    # tool-call contract overrides the addendum.
    descriptions = {tool.name: tool.description for tool in tools}
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
    registry.register_workflow_tool(
        "demo_evidence_tool",
        "Sample tool for the notification-contract regression test.",
        _noop,
    )
    tools, addendum = integration.build_workflow_integration(
        _FakeApp(), _enable_metadata(allowed=["demo_evidence_tool"])
    )
    # Addendum must name the envelope shape verbatim (so the LLM sees
    # the exact tags it will receive) and explain the one-shot
    # summarize-only contract.
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
    # Tool descriptions must reinforce the same envelope so the LLM
    # sees it both at system-prompt time and at tool-call selection time.
    descriptions = {tool.name: tool.description for tool in tools}
    assert "<workflow-notification>" in descriptions["start_workflow"]
    assert "<workflow-notification>" in descriptions["get_workflow_status"]
    assert "[Workflow notification]" not in descriptions["start_workflow"]
    assert "[Workflow notification]" not in descriptions["get_workflow_status"]


# ---- workflow activity failure handling -------------------------------------


def test_workflow_activity_logs_tool_exceptions_without_raising_raw_details(caplog):
    secret_message = "downstream API token and account details"

    def exploding_tool(args):
        raise RuntimeError(secret_message)

    registry.register_workflow_tool("exploding", "Always fails.", exploding_tool)
    activity = _registered_blueprint_function("agents_workflow_run_tool")

    with pytest.raises(RuntimeError) as excinfo:
        activity({"id": "explode", "tool": "exploding", "args": {}})

    assert str(excinfo.value) == "task 'explode': workflow-safe tool failed"
    assert secret_message not in str(excinfo.value)
    assert any(
        record.message == "workflow activity failed: id=explode tool=exploding"
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
    workflow_id = context.new_workflow_instance_id(failing_workflow_session)
    session = context.WorkflowSessionContext(
        session_id=failing_workflow_session,
        agent_name="test-agent",
        durable_client=_FailingDurableClient(),
        token="",
    )

    text_result = await call_tool(workflow_id, session)

    assert text_result == json.dumps({"error": expected_error})
    assert _FailingDurableClient.secret not in text_result
    assert any(
        record.message == expected_log
        and record.exc_info
        and _FailingDurableClient.secret in str(record.exc_info[1])
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_start_workflow_rejects_new_workflow_when_session_active_cap_reached():
    session_id = "session-1"
    statuses = [
        _FakeStatus(
            context.new_workflow_instance_id(session_id),
            "Running",
            updated_seconds=i,
        )
        for i in range(10)
    ]
    client = _CappedDurableClient(statuses)
    session = context.WorkflowSessionContext(
        session_id=session_id,
        agent_name="test-agent",
        durable_client=client,
        token="",
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
async def test_fetch_session_workflows_returns_newest_session_workflows_up_to_v1_cap():
    session_id = "session-1"
    other_session_id = "session-2"
    statuses = [
        _FakeStatus(
            context.new_workflow_instance_id(session_id),
            "Completed",
            updated_seconds=i,
        )
        for i in range(30)
    ]
    statuses.extend(
        _FakeStatus(
            context.new_workflow_instance_id(other_session_id),
            "Completed",
            updated_seconds=100 + i,
        )
        for i in range(3)
    )
    client = _CappedDurableClient(statuses)

    envelopes = await tools.fetch_session_workflows(client, session_id)

    assert len(envelopes) == 25
    assert [env["last_updated_time"] for env in envelopes] == sorted(
        [env["last_updated_time"] for env in envelopes],
        reverse=True,
    )
    assert envelopes[0]["last_updated_time"].endswith("00:00:29+00:00")
    assert envelopes[-1]["last_updated_time"].endswith("00:00:05+00:00")
