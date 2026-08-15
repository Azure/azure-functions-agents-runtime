from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from azure_functions_agents.config.schema import (
    BuiltinEndpointsConfig,
    ResolvedAgent,
    ToolsFilter,
)
from azure_functions_agents.registration.capabilities import AgentCapabilities
from azure_functions_agents.registration.catalog import CatalogEntry, build_catalog
from azure_functions_agents.workflows import engine
from azure_functions_agents.workflows.schema import (
    MAX_NODES,
    MAX_PARALLELISM,
    SUB_AGENT_TASK_TYPE,
    TOOL_TASK_TYPE,
    WAIT_TASK_TYPE,
)


class _FakeApp:
    def __init__(self) -> None:
        self.blueprints: list[Any] = []

    def register_blueprint(self, blueprint: Any) -> None:
        self.blueprints.append(blueprint)


def _make_resolved(slug: str, *, timeout: float = 12.0) -> ResolvedAgent:
    return ResolvedAgent(
        name=slug,
        slug=slug,
        description=f"{slug} description",
        trigger=None,
        instructions=f"{slug} instructions",
        is_main=False,
        builtin_endpoints=BuiltinEndpointsConfig(),
        model="test-model",
        timeout=timeout,
        enabled_mcp_names=[],
        enabled_skills_names=[],
        tool_filter=ToolsFilter(),
        sandbox_config=None,
        input_schema=None,
        response_schema=None,
        response_example=None,
        metadata={},
        source_file=f"{slug}.agent.md",
    )


def _catalog(*slugs: str):
    return build_catalog(
        {
            slug: CatalogEntry(_make_resolved(slug), AgentCapabilities())
            for slug in slugs
        }
    )


def _registered_function(name: str, *, catalog=None) -> Callable[..., Any]:
    app = _FakeApp()
    engine.register_workflows(app, catalog=catalog)
    [blueprint] = app.blueprints
    for builder in blueprint._function_builders:
        function = builder._function
        if function._name == name:
            registered = function._func
            if name == engine.ORCHESTRATOR_NAME:
                assert registered.__closure__ is not None
                return registered.__closure__[0].cell_contents
            return registered
    raise AssertionError(f"workflow function {name!r} was not registered")


@pytest.mark.asyncio
async def test_sub_agent_activity_uses_catalog_timeout_and_result_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, float, str]] = []

    async def run_leaf(
        resolved: ResolvedAgent,
        capabilities: AgentCapabilities,
        task: str,
        *,
        timeout: float,
        execution_role: str,
    ) -> str:
        calls.append((resolved.slug, task, timeout, execution_role))
        return "PR is ready to merge."

    monkeypatch.setattr(engine, "run_leaf_agent_task", run_leaf)
    activity = _registered_function(
        engine.SUB_AGENT_ACTIVITY_NAME,
        catalog=_catalog("pr_status_analyst"),
    )

    result = await activity(
        {
            "id": "analyze_pr",
            "agent": "pr_status_analyst",
            "task": "Analyze PR 117.",
            "workflow_id": "workflow-1",
        }
    )

    assert result == {
        "id": "analyze_pr",
        "result": {
            "agent": "pr_status_analyst",
            "text": "PR is ready to merge.",
        },
    }
    assert calls == [
        (
            "pr_status_analyst",
            "Analyze PR 117.",
            12.0,
            "workflow_subagent",
        )
    ]


@pytest.mark.asyncio
async def test_sub_agent_activity_fails_closed_on_catalog_miss() -> None:
    activity = _registered_function(
        engine.SUB_AGENT_ACTIVITY_NAME,
        catalog=_catalog("known"),
    )

    with pytest.raises(RuntimeError, match="not available"):
        await activity(
            {
                "id": "analyze_pr",
                "agent": "missing",
                "task": "Analyze PR 117.",
                "workflow_id": "workflow-1",
            }
        )


@pytest.mark.asyncio
async def test_sub_agent_activity_sanitizes_leaf_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "provider credential secret"

    async def fail(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError(secret)

    monkeypatch.setattr(engine, "run_leaf_agent_task", fail)
    activity = _registered_function(
        engine.SUB_AGENT_ACTIVITY_NAME,
        catalog=_catalog("pr_status_analyst"),
    )

    with pytest.raises(RuntimeError) as exc_info:
        await activity(
            {
                "id": "analyze_pr",
                "agent": "pr_status_analyst",
                "task": "Analyze PR 117.",
                "workflow_id": "workflow-1",
            }
        )

    assert str(exc_info.value) == (
        "task 'analyze_pr': Workflow Sub Agent 'pr_status_analyst' failed "
        "(error_code=workflow_subagent_execution_failed)"
    )
    assert secret not in str(exc_info.value)


class _Task:
    def __init__(self, result: Any = None) -> None:
        self.result = result
        self.is_completed = True
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class _FakeOrchestrationContext:
    def __init__(
        self,
        tasks: list[dict[str, Any]],
        result_for: Callable[[str, dict[str, Any]], dict[str, Any]],
    ) -> None:
        self.instance_id = "workflow-parent"
        self._input = {"tasks": tasks}
        self._result_for = result_for
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.last_wave = _Task([])
        self.cancel_task = _Task()
        self.statuses: list[str] = []

    def get_input(self) -> dict[str, Any]:
        return self._input

    def wait_for_external_event(self, name: str) -> _Task:
        assert name == engine.CANCEL_EVENT_NAME
        return self.cancel_task

    def call_activity(self, name: str, payload: dict[str, Any]) -> _Task:
        self.calls.append((name, payload))
        return _Task(self._result_for(name, payload))

    def task_all(self, tasks: list[_Task]) -> _Task:
        self.last_wave = _Task([task.result for task in tasks])
        return self.last_wave

    def task_any(self, tasks: list[_Task]) -> _Task:
        return _Task()

    def set_custom_status(self, status: str) -> None:
        self.statuses.append(status)


def _run_orchestrator(
    orchestrator: Callable[[Any], Any],
    context: _FakeOrchestrationContext,
) -> dict[str, Any]:
    generator = orchestrator(context)
    try:
        next(generator)
        while True:
            generator.send(context.last_wave)
    except StopIteration as stop:
        return stop.value


def test_orchestrator_fans_out_sub_agents_and_reduces_templated_results() -> None:
    tasks = [
        {
            "id": "analyze_117",
            "type": SUB_AGENT_TASK_TYPE,
            "agent": "pr_status_analyst",
            "task": "Analyze PR 117.",
            "depends_on": [],
        },
        {
            "id": "analyze_118",
            "type": SUB_AGENT_TASK_TYPE,
            "agent": "pr_status_analyst",
            "task": "Analyze PR 118.",
            "depends_on": [],
        },
        {
            "id": "report",
            "type": SUB_AGENT_TASK_TYPE,
            "agent": "report_writer",
            "task": (
                "Reduce 117=${analyze_117.result.text}; "
                "118=${analyze_118.result.text}."
            ),
            "depends_on": ["analyze_117", "analyze_118"],
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert name == engine.SUB_AGENT_ACTIVITY_NAME
        if payload["id"].startswith("analyze"):
            return {
                "id": payload["id"],
                "result": {
                    "agent": payload["agent"],
                    "text": f"summary-{payload['id']}",
                },
            }
        assert payload["task"] == (
            "Reduce 117=summary-analyze_117; 118=summary-analyze_118."
        )
        return {
            "id": "report",
            "result": {"agent": "report_writer", "text": "<html>report</html>"},
        }

    context = _FakeOrchestrationContext(tasks, result_for)
    orchestrator = _registered_function(
        engine.ORCHESTRATOR_NAME,
        catalog=_catalog("pr_status_analyst", "report_writer"),
    )

    result = _run_orchestrator(orchestrator, context)

    assert result["results"]["report"] == {
        "agent": "report_writer",
        "text": "<html>report</html>",
    }
    assert [payload["id"] for _, payload in context.calls[:2]] == [
        "analyze_117",
        "analyze_118",
    ]
    assert all(
        payload["workflow_id"] == "workflow-parent"
        for _, payload in context.calls
    )
    assert context.statuses == [
        "0/3 tasks done, running=analyze_117,analyze_118",
        "2/3 tasks done, next=report",
        "2/3 tasks done, running=report",
        "3/3 tasks done",
    ]


# ---------------------------------------------------------------------------
# Dynamic (data-driven) orchestration — Issue #1276.
# ---------------------------------------------------------------------------


class _DynamicContext(_FakeOrchestrationContext):
    """Fake context that also supports timers, a clock, and a persisted policy."""

    def __init__(
        self,
        tasks: list[dict[str, Any]],
        result_for: Callable[[str, dict[str, Any]], dict[str, Any]],
        *,
        policy: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        super().__init__(tasks, result_for)
        self._input["policy"] = policy or {}
        self._now = now or datetime(2024, 1, 1, tzinfo=UTC)
        self.timers: list[_Task] = []

    @property
    def current_utc_datetime(self) -> datetime:
        return self._now

    def create_timer(self, deadline: datetime) -> _Task:
        timer = _Task()
        timer.is_completed = False
        self.timers.append(timer)
        return timer


def _run_dynamic(
    tasks: list[dict[str, Any]],
    *,
    policy: dict[str, Any],
    result_for: Callable[[str, dict[str, Any]], dict[str, Any]],
    now: datetime | None = None,
) -> tuple[dict[str, Any], _DynamicContext]:
    context = _DynamicContext(tasks, result_for, policy=policy, now=now)
    orchestrator = _registered_function(engine.ORCHESTRATOR_NAME)
    result = _run_orchestrator(orchestrator, context)
    return result, context


def _activity_ids(context: _FakeOrchestrationContext, name: str) -> list[str]:
    return [payload["id"] for called, payload in context.calls if called == name]


# --- Static-path preservation ---------------------------------------------


def test_plan_is_dynamic_detection() -> None:
    static = [{"id": "a", "type": TOOL_TASK_TYPE, "tool": "t", "depends_on": []}]
    with_when = [
        {
            "id": "a",
            "type": TOOL_TASK_TYPE,
            "tool": "t",
            "depends_on": [],
            "when": {"ref": "${b.result.x}", "operator": "equals", "value": 1},
        }
    ]
    with_for_each = [
        {
            "id": "a",
            "type": TOOL_TASK_TYPE,
            "tool": "t",
            "depends_on": [],
            "for_each": "${b.result.items}",
        }
    ]
    assert engine._plan_is_dynamic(static) is False
    assert engine._plan_is_dynamic(with_when) is True
    assert engine._plan_is_dynamic(with_for_each) is True


def test_static_plan_keeps_exact_string_custom_status() -> None:
    tasks = [
        {"id": "a", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "b",
            "type": TOOL_TASK_TYPE,
            "tool": "collect",
            "args": {},
            "depends_on": ["a"],
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"id": payload["id"], "result": {"ok": payload["id"]}}

    context = _FakeOrchestrationContext(tasks, result_for)
    orchestrator = _registered_function(engine.ORCHESTRATOR_NAME)
    result = _run_orchestrator(orchestrator, context)

    assert result == {"results": {"a": {"ok": "a"}, "b": {"ok": "b"}}}
    # Static path publishes plain strings, never structured dict snapshots.
    assert all(isinstance(status, str) for status in context.statuses)
    assert context.statuses == [
        "0/2 tasks done, running=a",
        "1/2 tasks done, next=b",
        "1/2 tasks done, running=b",
        "2/2 tasks done",
    ]


# --- Conditions ------------------------------------------------------------


def test_condition_true_resolves_args_and_runs() -> None:
    tasks = [
        {"id": "src", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "act",
            "type": TOOL_TASK_TYPE,
            "tool": "noop",
            "args": {"echoed": "${src.result.val}"},
            "depends_on": ["src"],
            "when": {"ref": "${src.result.flag}", "operator": "equals", "value": True},
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["id"] == "src":
            return {"id": "src", "result": {"flag": True, "val": "hi"}}
        return {"id": payload["id"], "result": {"ok": True}}

    result, context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "noop"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert result["results"]["act"] == {"ok": True}
    act_call = next(p for _, p in context.calls if p["id"] == "act")
    assert act_call["args"] == {"echoed": "hi"}


def test_condition_false_skips_before_resolving_args() -> None:
    # ``act`` references a missing path in its args; if the predicate were
    # evaluated after args (or not at all) this plan would fail. Predicate
    # runs first, the task is skipped, and the bad args are never resolved.
    tasks = [
        {"id": "src", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "act",
            "type": TOOL_TASK_TYPE,
            "tool": "noop",
            "args": {"x": "${src.result.MISSING}"},
            "depends_on": ["src"],
            "when": {"ref": "${src.result.flag}", "operator": "equals", "value": True},
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"id": "src", "result": {"flag": False}}

    result, context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "noop"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert result["results"]["act"] is None
    assert "noop" not in [p.get("tool") for _, p in context.calls]
    assert result.get("failed") is None
    final = context.statuses[-1]
    assert final["nodes"]["act"]["state"] == "skipped"


def test_normal_skip_unlocks_descendants() -> None:
    tasks = [
        {"id": "src", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "b",
            "type": TOOL_TASK_TYPE,
            "tool": "noop",
            "args": {},
            "depends_on": ["src"],
            "when": {"ref": "${src.result.flag}", "operator": "equals", "value": True},
        },
        {
            "id": "c",
            "type": TOOL_TASK_TYPE,
            "tool": "finish",
            "args": {},
            "depends_on": ["b"],
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["id"] == "src":
            return {"id": "src", "result": {"flag": False}}
        return {"id": payload["id"], "result": {"done": payload["id"]}}

    result, context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "noop", "finish"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert result["results"]["b"] is None
    assert result["results"]["c"] == {"done": "c"}
    # A skipped node satisfies the dependency without dispatching an activity.
    assert "noop" not in [p.get("tool") for _, p in context.calls]


def test_full_reference_to_skipped_result_resolves_to_null() -> None:
    tasks = [
        {"id": "src", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "skipped",
            "type": TOOL_TASK_TYPE,
            "tool": "noop",
            "args": {},
            "depends_on": ["src"],
            "when": {"ref": "${src.result.run}", "operator": "equals", "value": True},
        },
        {
            "id": "sink",
            "type": TOOL_TASK_TYPE,
            "tool": "finish",
            "args": {"value": "${skipped.result}"},
            "depends_on": ["skipped"],
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["id"] == "src":
            return {"id": "src", "result": {"run": False}}
        return {
            "id": payload["id"],
            "result": {"seen": payload["args"]["value"]},
        }

    result, context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "noop", "finish"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert result["results"]["skipped"] is None
    assert result["results"]["sink"] == {"seen": None}
    sink_call = next(payload for _, payload in context.calls if payload["id"] == "sink")
    assert sink_call["args"] == {"value": None}


def test_dotted_reference_below_skipped_result_is_controlled_failure() -> None:
    tasks = [
        {"id": "src", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "skipped",
            "type": TOOL_TASK_TYPE,
            "tool": "noop",
            "args": {},
            "depends_on": ["src"],
            "when": {"ref": "${src.result.run}", "operator": "equals", "value": True},
        },
        {
            "id": "sink",
            "type": TOOL_TASK_TYPE,
            "tool": "finish",
            "args": {"value": "${skipped.result.field}"},
            "depends_on": ["skipped"],
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"id": "src", "result": {"run": False}}

    result, context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "noop", "finish"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert result["failed"] is True
    assert result["error_code"] == "workflow_reference_unresolved"
    assert result["node_id"] == "sink"
    assert result["path"] is None
    assert result["results"] == {"src": {"run": False}, "skipped": None}
    assert context.statuses[-1]["nodes"]["sink"]["state"] == "failed"
    assert "finish" not in [payload.get("tool") for _, payload in context.calls]


def test_non_scalar_condition_is_condition_invalid() -> None:
    tasks = [
        {"id": "disc", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "act",
            "type": TOOL_TASK_TYPE,
            "tool": "noop",
            "args": {},
            "depends_on": ["disc"],
            "when": {"ref": "${disc.result.obj}", "operator": "equals", "value": "x"},
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"id": "disc", "result": {"obj": {"a": 1}}}

    result, _ = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "noop"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert result["failed"] is True
    assert result["error_code"] == "workflow_condition_invalid"
    assert result["node_id"] == "act"


# --- for_each expansion ----------------------------------------------------


def test_expanded_mixed_run_skip_aggregate_source_order() -> None:
    tasks = [
        {"id": "disc", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "analyze",
            "type": TOOL_TASK_TYPE,
            "tool": "at",
            "args": {"i": "${index}"},
            "depends_on": ["disc"],
            "for_each": "${disc.result.items}",
            "when": {"ref": "${item.open}", "operator": "equals", "value": True},
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["id"] == "disc":
            return {
                "id": "disc",
                "result": {"items": [{"open": True}, {"open": False}, {"open": True}]},
            }
        return {"id": payload["id"], "result": {"idx": payload["args"]["i"]}}

    result, context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "at"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert result["results"]["analyze"] == [
        {"index": 0, "status": "completed", "result": {"idx": 0}},
        {"index": 1, "status": "skipped", "result": None},
        {"index": 2, "status": "completed", "result": {"idx": 2}},
    ]
    # The skipped element (index 1) never dispatches an activity.
    assert _activity_ids(context, engine._ACTIVITY_NAME) == [
        "disc",
        "analyze[0]",
        "analyze[2]",
    ]


def test_empty_expansion_aggregates_immediately() -> None:
    tasks = [
        {"id": "disc", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "analyze",
            "type": TOOL_TASK_TYPE,
            "tool": "at",
            "args": {},
            "depends_on": ["disc"],
            "for_each": "${disc.result.items}",
        },
        {
            "id": "sink",
            "type": TOOL_TASK_TYPE,
            "tool": "noop",
            "args": {"all": "${analyze.result}"},
            "depends_on": ["analyze"],
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["id"] == "disc":
            return {"id": "disc", "result": {"items": []}}
        return {"id": payload["id"], "result": {"ok": True}}

    result, context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "at", "noop"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert result["results"]["analyze"] == []
    assert "at" not in [p.get("tool") for _, p in context.calls]
    sink_call = next(p for _, p in context.calls if p["id"] == "sink")
    assert sink_call["args"] == {"all": []}


def test_all_skipped_expansion_aggregates_without_dispatch() -> None:
    tasks = [
        {"id": "disc", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "analyze",
            "type": TOOL_TASK_TYPE,
            "tool": "at",
            "args": {"i": "${index}"},
            "depends_on": ["disc"],
            "for_each": "${disc.result.items}",
            "when": {"ref": "${item.open}", "operator": "equals", "value": True},
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": "disc",
            "result": {"items": [{"open": False}, {"open": False}]},
        }

    result, context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "at"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert result["results"]["analyze"] == [
        {"index": 0, "status": "skipped", "result": None},
        {"index": 1, "status": "skipped", "result": None},
    ]
    assert _activity_ids(context, engine._ACTIVITY_NAME) == ["disc"]
    assert context.statuses[-1]["nodes"]["analyze"]["state"] == "aggregated"


def test_numeric_scheduling_under_parallel_cap() -> None:
    count = 12
    tasks = [
        {"id": "disc", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "analyze",
            "type": TOOL_TASK_TYPE,
            "tool": "at",
            "args": {"i": "${index}"},
            "depends_on": ["disc"],
            "for_each": "${disc.result.items}",
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["id"] == "disc":
            return {"id": "disc", "result": {"items": [{} for _ in range(count)]}}
        return {"id": payload["id"], "result": {"idx": payload["args"]["i"]}}

    result, context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "at"], "allowed_subagents": []},
        result_for=result_for,
    )

    # Numeric (not lexical) order: analyze[10] follows analyze[2], not analyze[1].
    assert _activity_ids(context, engine._ACTIVITY_NAME) == [
        "disc",
        *[f"analyze[{i}]" for i in range(count)],
    ]
    assert len(result["results"]["analyze"]) == count
    # Parallelism cap: a wave never runs more than MAX_PARALLELISM instances,
    # and the first analyze wave saturates the cap (proving a second wave ran).
    running_peaks = [
        s["counts"]["running"] for s in context.statuses if isinstance(s, dict)
    ]
    assert max(running_peaks) <= MAX_PARALLELISM
    assert MAX_PARALLELISM in running_peaks


def test_multiple_expansions_run_in_sorted_logical_id_order() -> None:
    tasks = [
        {"id": "disc", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "grp_a",
            "type": TOOL_TASK_TYPE,
            "tool": "at",
            "args": {},
            "depends_on": ["disc"],
            "for_each": "${disc.result.a}",
        },
        {
            "id": "grp_b",
            "type": TOOL_TASK_TYPE,
            "tool": "at",
            "args": {},
            "depends_on": ["disc"],
            "for_each": "${disc.result.b}",
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["id"] == "disc":
            return {"id": "disc", "result": {"a": [{}], "b": [{}]}}
        return {"id": payload["id"], "result": {"ok": payload["id"]}}

    _, context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "at"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert _activity_ids(context, engine._ACTIVITY_NAME) == [
        "disc",
        "grp_a[0]",
        "grp_b[0]",
    ]


def test_node_limit_atomic_rejection_counts_skipped_items() -> None:
    # 50 elements + 1 reserved non-for_each node (disc) exceeds MAX_NODES.
    # Every element would be skipped by the predicate, yet the plan is
    # rejected before any instance is created — skipped items still consume
    # the budget, and the rejection is atomic (no partial materialization).
    over = MAX_NODES  # 50 elements → 1 + 50 > 50
    tasks = [
        {"id": "disc", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "analyze",
            "type": TOOL_TASK_TYPE,
            "tool": "at",
            "args": {},
            "depends_on": ["disc"],
            "for_each": "${disc.result.items}",
            "when": {"ref": "${item.open}", "operator": "equals", "value": True},
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"id": "disc", "result": {"items": [{"open": False} for _ in range(over)]}}

    result, context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "at"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert result["failed"] is True
    assert result["error_code"] == "workflow_node_limit_exceeded"
    assert result["node_id"] == "analyze"
    # Atomic: only ``disc`` ran; no analyze instances were dispatched.
    assert _activity_ids(context, engine._ACTIVITY_NAME) == ["disc"]


def test_node_limit_is_cumulative_across_expansions() -> None:
    tasks = [
        {"id": "disc", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "grp_a",
            "type": TOOL_TASK_TYPE,
            "tool": "at",
            "args": {},
            "depends_on": ["disc"],
            "for_each": "${disc.result.a}",
        },
        {
            "id": "grp_b",
            "type": TOOL_TASK_TYPE,
            "tool": "at",
            "args": {},
            "depends_on": ["disc"],
            "for_each": "${disc.result.b}",
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": "disc",
            "result": {"a": [{} for _ in range(24)], "b": [{} for _ in range(26)]},
        }

    result, context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "at"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert result["failed"] is True
    assert result["error_code"] == "workflow_node_limit_exceeded"
    assert result["node_id"] == "grp_b"
    assert result["results"] == {
        "disc": {"a": [{} for _ in range(24)], "b": [{} for _ in range(26)]}
    }
    assert _activity_ids(context, engine._ACTIVITY_NAME) == ["disc"]


def test_dynamic_replay_produces_identical_calls_statuses_and_results() -> None:
    tasks = [
        {"id": "disc", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "analyze",
            "type": TOOL_TASK_TYPE,
            "tool": "at",
            "args": {"i": "${index}"},
            "depends_on": ["disc"],
            "for_each": "${disc.result.items}",
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["id"] == "disc":
            return {"id": "disc", "result": {"items": [{}, {}, {}]}}
        return {"id": payload["id"], "result": {"idx": payload["args"]["i"]}}

    first_result, first_context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "at"], "allowed_subagents": []},
        result_for=result_for,
    )
    replay_result, replay_context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "at"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert replay_result == first_result
    assert replay_context.calls == first_context.calls
    assert replay_context.statuses == first_context.statuses


def test_for_each_non_array_is_iteration_not_array() -> None:
    tasks = [
        {"id": "disc", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "analyze",
            "type": TOOL_TASK_TYPE,
            "tool": "at",
            "args": {},
            "depends_on": ["disc"],
            "for_each": "${disc.result.items}",
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"id": "disc", "result": {"items": {"not": "a list"}}}

    result, _ = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "at"], "allowed_subagents": []},
        result_for=result_for,
    )

    # Stable, flat controlled-failure envelope shape.
    assert set(result) == {"failed", "error", "error_code", "node_id", "path", "results"}
    assert result["failed"] is True
    assert result["error_code"] == "workflow_iteration_not_array"
    assert result["node_id"] == "analyze"
    assert result["path"] == "${disc.result.items}"
    # Committed logical results are preserved through the failure.
    assert result["results"]["disc"] == {"items": {"not": "a list"}}


def test_for_each_missing_upstream_path_is_reference_unresolved() -> None:
    tasks = [
        {"id": "disc", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "analyze",
            "type": TOOL_TASK_TYPE,
            "tool": "at",
            "args": {},
            "depends_on": ["disc"],
            "for_each": "${disc.result.MISSING}",
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"id": "disc", "result": {"items": [{}]}}

    result, _ = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "at"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert result["failed"] is True
    assert result["error_code"] == "workflow_reference_unresolved"
    assert result["node_id"] == "analyze"


def test_expanded_missing_item_path_uses_instance_node_id() -> None:
    tasks = [
        {"id": "disc", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "analyze",
            "type": TOOL_TASK_TYPE,
            "tool": "at",
            "args": {},
            "depends_on": ["disc"],
            "for_each": "${disc.result.items}",
            "when": {"ref": "${item.missing}", "operator": "equals", "value": True},
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"id": "disc", "result": {"items": [{}]}}

    result, _ = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "at"], "allowed_subagents": []},
        result_for=result_for,
    )

    assert result["failed"] is True
    assert result["error_code"] == "workflow_reference_unresolved"
    assert result["node_id"] == "analyze[0]"
    assert result["path"] == "${item.missing}"


# --- Immutable owner policy (fail closed) ----------------------------------


def test_expanded_tool_outside_policy_raises() -> None:
    tasks = [
        {"id": "disc", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "analyze",
            "type": TOOL_TASK_TYPE,
            "tool": "restricted",
            "args": {},
            "depends_on": ["disc"],
            "for_each": "${disc.result.items}",
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"id": "disc", "result": {"items": [{}]}}

    with pytest.raises(RuntimeError, match="outside the persisted workflow owner policy"):
        _run_dynamic(
            tasks,
            policy={"allowed_tools": ["collect"], "allowed_subagents": []},
            result_for=result_for,
        )


def test_expanded_sub_agent_outside_policy_raises() -> None:
    tasks = [
        {"id": "disc", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "analyze",
            "type": SUB_AGENT_TASK_TYPE,
            "agent": "unlisted",
            "task": "Analyze ${item}.",
            "depends_on": ["disc"],
            "for_each": "${disc.result.items}",
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"id": "disc", "result": {"items": ["x"]}}

    with pytest.raises(RuntimeError, match="outside the persisted workflow owner policy"):
        _run_dynamic(
            tasks,
            policy={"allowed_tools": ["collect"], "allowed_subagents": ["known"]},
            result_for=result_for,
        )


# --- Structured status snapshots -------------------------------------------


def test_dynamic_status_snapshots_track_states_and_counts() -> None:
    tasks = [
        {"id": "disc", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "analyze",
            "type": TOOL_TASK_TYPE,
            "tool": "at",
            "args": {"i": "${index}"},
            "depends_on": ["disc"],
            "for_each": "${disc.result.items}",
            "when": {"ref": "${item.open}", "operator": "equals", "value": True},
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["id"] == "disc":
            return {
                "id": "disc",
                "result": {"items": [{"open": True}, {"open": False}, {"open": True}]},
            }
        return {"id": payload["id"], "result": {"idx": payload["args"]["i"]}}

    _, context = _run_dynamic(
        tasks,
        policy={"allowed_tools": ["collect", "at"], "allowed_subagents": []},
        result_for=result_for,
    )

    snapshots = [s for s in context.statuses if isinstance(s, dict)]
    assert snapshots, "dynamic path must publish structured snapshots"
    assert all(s["schema_version"] == 2 for s in snapshots)

    analyze_states = [s["nodes"]["analyze"]["state"] for s in snapshots]
    assert "expanded" in analyze_states
    assert "running" in analyze_states

    final = snapshots[-1]
    assert final["nodes"]["analyze"]["state"] == "aggregated"
    assert final["counts"] == {
        "logical_total": 2,
        "materialized_total": 4,  # disc + 3 analyze instances (incl. skipped)
        "completed": 3,  # disc + analyze[0] + analyze[2]
        "skipped": 1,  # analyze[1]
        "running": 0,
    }


# --- Cancellation with a dynamic timer -------------------------------------


def test_dynamic_cancellation_cancels_timer_and_returns_partial() -> None:
    tasks = [
        {"id": "t1", "type": TOOL_TASK_TYPE, "tool": "collect", "args": {}, "depends_on": []},
        {
            "id": "w1",
            "type": WAIT_TASK_TYPE,
            "duration": "PT1H",
            "depends_on": ["t1"],
            "when": {"ref": "${t1.result.go}", "operator": "equals", "value": True},
        },
    ]

    def result_for(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"id": "t1", "result": {"go": True}}

    context = _DynamicContext(
        tasks,
        result_for,
        policy={"allowed_tools": ["collect"], "allowed_subagents": []},
    )
    context.cancel_task.result = "user-request"
    orchestrator = _registered_function(engine.ORCHESTRATOR_NAME)

    gen = orchestrator(context)
    next(gen)  # yields the t1 wave
    gen.send(context.last_wave)  # completes t1, expands w1, dispatches its timer
    result: dict[str, Any] = {}
    try:
        gen.send(context.cancel_task)  # cancel while the timer is pending
    except StopIteration as stop:
        result = stop.value

    assert result["canceled"] is True
    assert result["reason"] == "user-request"
    assert result["results"]["t1"] == {"go": True}
    assert result["completed_count"] == 1
    assert result["total_count"] == 2
    # The pending durable timer was cancelled.
    assert context.timers and all(timer.cancelled for timer in context.timers)
