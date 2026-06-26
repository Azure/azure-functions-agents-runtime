"""Unit tests for the workflow plan validator and template resolver.

These are pure-Python tests — no Azurite, no Durable, no func host.
They exercise:

- structural validation (schema, duplicate ids, max nodes, unknown tool)
- DAG validation (unknown / self / duplicate dependency, cycle detection)
- template-reference validation (malformed, unknown ref, non-upstream ref)
- runtime template resolution (full-string, embedded, dotted path,
  missing path, non-traversable parent, list indexing).
- wait-task validation (duration, until, mutual exclusion, caps)
- ISO-8601 helpers (duration / datetime parsing).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from azure_functions_agents.workflows.schema import (
    ECHO_TOOL_NAME,
    MAX_NODES,
    MAX_WAIT_DURATION,
    PlanValidationError,
    TemplateResolutionError,
    parse_iso8601_datetime,
    parse_iso8601_duration,
    plan_to_activity_inputs,
    resolve_template_value,
)
from azure_functions_agents.workflows.schema import validate_plan as _validate_plan


# Schema tests use the internal echo tool; the production allowlist is
# computed at app start by ``build_workflow_integration``. Wrap once so
# every call site stays terse.
_TEST_ALLOWLIST = frozenset({ECHO_TOOL_NAME})


def validate_plan(raw, *, allowed_tools=_TEST_ALLOWLIST):
    return _validate_plan(raw, allowed_tools=allowed_tools)


def _task(tid, depends_on=None, args=None, tool=ECHO_TOOL_NAME, type_="tool"):
    return {
        "id": tid,
        "type": type_,
        "tool": tool,
        "args": args or {},
        "depends_on": depends_on or [],
    }


def _wait(tid, duration=None, until=None, depends_on=None):
    out = {
        "id": tid,
        "type": "wait",
        "depends_on": depends_on or [],
    }
    if duration is not None:
        out["duration"] = duration
    if until is not None:
        out["until"] = until
    return out


def _plan(*tasks):
    return {"version": 1, "tasks": list(tasks)}


# ---------------------------------------------------------------------------
# structural validation
# ---------------------------------------------------------------------------


def test_validates_minimal_single_task_plan():
    plan = validate_plan(_plan(_task("a")))
    assert [t.id for t in plan.tasks] == ["a"]


def test_rejects_duplicate_task_ids():
    with pytest.raises(PlanValidationError, match="duplicate task id"):
        validate_plan(_plan(_task("a"), _task("a", depends_on=["a"])))


def test_rejects_unknown_tool():
    with pytest.raises(PlanValidationError, match="not workflow-safe"):
        validate_plan(_plan(_task("a", tool="not_a_real_tool")))


def test_rejects_unsupported_task_type():
    with pytest.raises(PlanValidationError, match="not supported"):
        validate_plan(_plan(_task("a", type_="bogus")))


def test_rejects_plans_over_max_nodes():
    tasks = [_task(f"t{i}") for i in range(MAX_NODES + 1)]
    with pytest.raises(PlanValidationError, match="per-plan limit"):
        validate_plan(_plan(*tasks))


# ---------------------------------------------------------------------------
# DAG validation
# ---------------------------------------------------------------------------


def test_accepts_arbitrary_dag_with_fan_out_and_fan_in():
    plan = validate_plan(
        _plan(
            _task("root"),
            _task("left", depends_on=["root"]),
            _task("right", depends_on=["root"]),
            _task("join", depends_on=["left", "right"]),
        )
    )
    assert [t.id for t in plan.tasks] == ["root", "left", "right", "join"]


def test_accepts_multiple_independent_roots():
    validate_plan(_plan(_task("a"), _task("b")))


def test_rejects_self_dependency():
    with pytest.raises(PlanValidationError, match="cannot reference itself"):
        validate_plan(_plan(_task("a", depends_on=["a"])))


def test_rejects_unknown_dependency():
    with pytest.raises(PlanValidationError, match="unknown task"):
        validate_plan(_plan(_task("a", depends_on=["ghost"])))


def test_rejects_duplicate_dependency_entries():
    with pytest.raises(PlanValidationError, match="duplicate dependency"):
        validate_plan(
            _plan(_task("a"), _task("b", depends_on=["a", "a"]))
        )


def test_detects_two_node_cycle():
    with pytest.raises(PlanValidationError, match="dependency cycle"):
        validate_plan(
            _plan(
                _task("a", depends_on=["b"]),
                _task("b", depends_on=["a"]),
            )
        )


def test_detects_three_node_cycle():
    with pytest.raises(PlanValidationError, match="dependency cycle"):
        validate_plan(
            _plan(
                _task("a", depends_on=["c"]),
                _task("b", depends_on=["a"]),
                _task("c", depends_on=["b"]),
            )
        )


def test_detects_cycle_in_disconnected_component():
    # Clean root component (root -> leaf) plus a separate cyclic component
    # (x <-> y). The DFS must explore the second component too.
    with pytest.raises(PlanValidationError, match="dependency cycle"):
        validate_plan(
            _plan(
                _task("root"),
                _task("leaf", depends_on=["root"]),
                _task("x", depends_on=["y"]),
                _task("y", depends_on=["x"]),
            )
        )


# ---------------------------------------------------------------------------
# template-reference validation
# ---------------------------------------------------------------------------


def test_accepts_full_string_template_to_upstream_task():
    validate_plan(
        _plan(
            _task("a"),
            _task(
                "b",
                depends_on=["a"],
                args={"echo_of": "${a.result}"},
            ),
        )
    )


def test_accepts_dotted_path_template_to_upstream_task():
    validate_plan(
        _plan(
            _task("a"),
            _task(
                "b",
                depends_on=["a"],
                args={"name": "${a.result.echoed.msg}"},
            ),
        )
    )


def test_accepts_template_in_nested_args():
    validate_plan(
        _plan(
            _task("a"),
            _task(
                "b",
                depends_on=["a"],
                args={"nested": {"items": ["${a.result}"]}},
            ),
        )
    )


def test_rejects_template_referencing_unknown_task():
    with pytest.raises(PlanValidationError, match="unknown task"):
        validate_plan(
            _plan(
                _task("a"),
                _task(
                    "b",
                    depends_on=["a"],
                    args={"x": "${ghost.result}"},
                ),
            )
        )


def test_rejects_template_referencing_non_upstream_task():
    with pytest.raises(PlanValidationError, match="not an upstream dependency"):
        validate_plan(
            _plan(
                _task("a"),
                _task("b"),  # no dependency on a
                _task("c", depends_on=["b"], args={"x": "${a.result}"}),
            )
        )


def test_rejects_malformed_template_literal():
    with pytest.raises(PlanValidationError, match="malformed template"):
        validate_plan(
            _plan(
                _task("a"),
                _task("b", depends_on=["a"], args={"x": "${a}"}),
            )
        )


@pytest.mark.parametrize(
    "template",
    [
        "${a.result..msg}",
        "${a.result.echoed.}",
        "${a.result...}",
    ],
)
def test_rejects_template_paths_with_empty_segments(template):
    with pytest.raises(PlanValidationError, match="malformed template"):
        validate_plan(
            _plan(
                _task("a"),
                _task("b", depends_on=["a"], args={"x": template}),
            )
        )


def test_rejects_unterminated_template_literal():
    with pytest.raises(PlanValidationError, match="unterminated template"):
        validate_plan(
            _plan(
                _task("a"),
                _task("b", depends_on=["a"], args={"x": "${a.result"}),
            )
        )


def test_rejects_mixed_valid_and_malformed_refs_in_one_string():
    # A valid ref next to a malformed one must still be rejected — we
    # don't want the validator to short-circuit on the first valid match.
    with pytest.raises(PlanValidationError, match="malformed template"):
        validate_plan(
            _plan(
                _task("a"),
                _task(
                    "b",
                    depends_on=["a"],
                    args={"x": "${a.result} and ${a}"},
                ),
            )
        )


def test_accepts_multiple_valid_refs_in_one_string():
    validate_plan(
        _plan(
            _task("a"),
            _task("b"),
            _task(
                "c",
                depends_on=["a", "b"],
                args={"x": "a=${a.result} b=${b.result}"},
            ),
        )
    )


# ---------------------------------------------------------------------------
# plan_to_activity_inputs preserves depends_on
# ---------------------------------------------------------------------------


def test_plan_to_activity_inputs_includes_depends_on():
    plan = validate_plan(
        _plan(
            _task("a"),
            _task("b", depends_on=["a"], args={"k": "v"}),
        )
    )
    inputs = plan_to_activity_inputs(plan)
    assert inputs == [
        {"id": "a", "type": "tool", "tool": ECHO_TOOL_NAME, "args": {}, "depends_on": []},
        {"id": "b", "type": "tool", "tool": ECHO_TOOL_NAME, "args": {"k": "v"}, "depends_on": ["a"]},
    ]


# ---------------------------------------------------------------------------
# runtime template resolution
# ---------------------------------------------------------------------------


def test_resolve_full_string_returns_native_value():
    assert resolve_template_value(
        "${a.result}", {"a": {"echoed": {"msg": "hi"}}}
    ) == {"echoed": {"msg": "hi"}}


def test_resolve_dotted_path_into_dict():
    assert resolve_template_value(
        "${a.result.echoed.msg}", {"a": {"echoed": {"msg": "hi"}}}
    ) == "hi"


def test_resolve_dotted_path_into_list():
    assert resolve_template_value(
        "${a.result.items.1}", {"a": {"items": ["zero", "one", "two"]}}
    ) == "one"


def test_resolve_embedded_template_stringifies_non_strings():
    out = resolve_template_value(
        "result was ${a.result.echoed}",
        {"a": {"echoed": {"msg": "hi"}}},
    )
    assert out == 'result was {"msg": "hi"}'


def test_resolve_embedded_template_keeps_strings_raw():
    out = resolve_template_value(
        "hello ${a.result.name}!", {"a": {"name": "world"}}
    )
    assert out == "hello world!"


def test_resolve_walks_dicts_and_lists():
    args = {"outer": {"inner": ["${a.result}", "literal"]}}
    out = resolve_template_value(args, {"a": 42})
    assert out == {"outer": {"inner": [42, "literal"]}}


def test_resolve_missing_node_raises():
    with pytest.raises(TemplateResolutionError, match="hasn't completed"):
        resolve_template_value("${a.result}", {})


def test_resolve_missing_dict_key_raises():
    with pytest.raises(TemplateResolutionError, match="key not present"):
        resolve_template_value("${a.result.missing}", {"a": {"present": 1}})


def test_resolve_non_traversable_parent_raises():
    with pytest.raises(TemplateResolutionError, match="not a dict or list"):
        resolve_template_value("${a.result.x}", {"a": "scalar"})


def test_resolve_list_index_out_of_range_raises():
    with pytest.raises(TemplateResolutionError, match="out of range"):
        resolve_template_value("${a.result.5}", {"a": [1, 2]})


def test_resolve_list_index_non_integer_raises():
    with pytest.raises(TemplateResolutionError, match="must be an integer"):
        resolve_template_value("${a.result.notanint}", {"a": [1, 2]})


def test_resolve_passthrough_for_non_template_strings():
    assert resolve_template_value("plain", {"a": 1}) == "plain"


def test_resolve_passthrough_for_non_strings():
    assert resolve_template_value(42, {}) == 42
    assert resolve_template_value(None, {}) is None
    assert resolve_template_value(True, {}) is True


# ---------------------------------------------------------------------------
# wait-task validation
# ---------------------------------------------------------------------------


def test_accepts_wait_task_with_duration():
    plan = validate_plan(_plan(_wait("pause", duration="PT30S")))
    assert plan.tasks[0].type == "wait"
    assert plan.tasks[0].duration == "PT30S"


def test_accepts_wait_task_with_until():
    target = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    plan = validate_plan(_plan(_wait("pause", until=target)))
    assert plan.tasks[0].until == target


def test_accepts_dag_mixing_tool_and_wait_tasks():
    plan = validate_plan(
        _plan(
            _task("fetch"),
            _wait("settle", duration="PT5S", depends_on=["fetch"]),
            _task("summarize", depends_on=["settle"]),
        )
    )
    assert [t.type for t in plan.tasks] == ["tool", "wait", "tool"]


def test_rejects_wait_task_with_neither_duration_nor_until():
    with pytest.raises(PlanValidationError, match="exactly one of"):
        validate_plan(_plan(_wait("pause")))


def test_rejects_wait_task_with_both_duration_and_until():
    with pytest.raises(PlanValidationError, match="exactly one of"):
        validate_plan(
            _plan(_wait("pause", duration="PT30S", until="2099-01-01T00:00:00Z"))
        )


def test_rejects_wait_task_with_tool_field():
    with pytest.raises(PlanValidationError, match="'tool' is not valid"):
        validate_plan(
            _plan(
                {
                    "id": "pause",
                    "type": "wait",
                    "tool": ECHO_TOOL_NAME,
                    "duration": "PT30S",
                }
            )
        )


def test_rejects_wait_task_with_args():
    with pytest.raises(PlanValidationError, match="'args' is not valid"):
        validate_plan(
            _plan(
                {
                    "id": "pause",
                    "type": "wait",
                    "duration": "PT30S",
                    "args": {"x": 1},
                }
            )
        )


def test_rejects_tool_task_with_duration():
    with pytest.raises(PlanValidationError, match="only valid on type=wait"):
        validate_plan(
            _plan(
                {
                    "id": "a",
                    "type": "tool",
                    "tool": ECHO_TOOL_NAME,
                    "duration": "PT30S",
                }
            )
        )


def test_rejects_zero_duration():
    with pytest.raises(PlanValidationError, match="must be positive"):
        validate_plan(_plan(_wait("pause", duration="PT0S")))


def test_rejects_duration_over_24h():
    with pytest.raises(PlanValidationError, match="exceeds the maximum"):
        validate_plan(_plan(_wait("pause", duration="P2D")))


def test_rejects_malformed_duration():
    with pytest.raises(PlanValidationError, match="invalid duration"):
        validate_plan(_plan(_wait("pause", duration="30s")))


def test_rejects_duration_with_empty_time_section():
    with pytest.raises(PlanValidationError, match="invalid duration"):
        validate_plan(_plan(_wait("pause", duration="P1DT")))


def test_rejects_naive_datetime_in_until():
    with pytest.raises(PlanValidationError, match="invalid until"):
        validate_plan(_plan(_wait("pause", until="2099-01-01T00:00:00")))


def test_plan_to_activity_inputs_includes_wait_fields():
    plan = validate_plan(
        _plan(
            _task("fetch"),
            _wait("settle", duration="PT5S", depends_on=["fetch"]),
        )
    )
    inputs = plan_to_activity_inputs(plan)
    assert inputs == [
        {
            "id": "fetch",
            "type": "tool",
            "tool": ECHO_TOOL_NAME,
            "args": {},
            "depends_on": [],
        },
        {
            "id": "settle",
            "type": "wait",
            "duration": "PT5S",
            "depends_on": ["fetch"],
        },
    ]


# ---------------------------------------------------------------------------
# ISO-8601 helpers
# ---------------------------------------------------------------------------


def test_parse_duration_seconds():
    assert parse_iso8601_duration("PT30S") == timedelta(seconds=30)


def test_parse_duration_minutes_and_seconds():
    assert parse_iso8601_duration("PT5M30S") == timedelta(minutes=5, seconds=30)


def test_parse_duration_hours_and_minutes():
    assert parse_iso8601_duration("PT1H30M") == timedelta(hours=1, minutes=30)


def test_parse_duration_days():
    assert parse_iso8601_duration("P1D") == timedelta(days=1)


def test_parse_duration_compound():
    assert parse_iso8601_duration("P1DT2H3M4S") == timedelta(
        days=1, hours=2, minutes=3, seconds=4
    )


def test_parse_duration_fractional_seconds():
    assert parse_iso8601_duration("PT1.5S") == timedelta(seconds=1.5)


def test_parse_duration_rejects_bare_p():
    with pytest.raises(ValueError):
        parse_iso8601_duration("P")


def test_parse_duration_rejects_lowercase():
    with pytest.raises(ValueError):
        parse_iso8601_duration("pt30s")


def test_parse_duration_rejects_empty():
    with pytest.raises(ValueError):
        parse_iso8601_duration("")


def test_parse_datetime_z_suffix():
    dt = parse_iso8601_datetime("2026-04-25T17:30:00Z")
    assert dt == datetime(2026, 4, 25, 17, 30, tzinfo=timezone.utc)


def test_parse_datetime_with_offset():
    dt = parse_iso8601_datetime("2026-04-25T10:30:00-07:00")
    # Normalized to UTC.
    assert dt == datetime(2026, 4, 25, 17, 30, tzinfo=timezone.utc)


def test_parse_datetime_rejects_naive():
    with pytest.raises(ValueError, match="timezone"):
        parse_iso8601_datetime("2026-04-25T17:30:00")


def test_parse_datetime_rejects_garbage():
    with pytest.raises(ValueError):
        parse_iso8601_datetime("not-a-date")


def test_max_wait_duration_is_24_hours():
    # Sanity check on the constant — the validator depends on it.
    assert MAX_WAIT_DURATION == timedelta(hours=24)


def test_rejects_overflow_duration():
    # P1000000000D produces a timedelta() OverflowError; the parser must
    # surface this as ValueError so validate_plan can produce a clean
    # PlanValidationError instead of a 500.
    with pytest.raises(ValueError):
        parse_iso8601_duration("P1000000000D")


def test_validate_rejects_overflow_duration():
    with pytest.raises(PlanValidationError, match="invalid duration"):
        validate_plan(_plan(_wait("pause", duration="P1000000000D")))


def test_rejects_far_future_until():
    # Far-future `until` would create a workflow that lives for years —
    # capped to the same horizon as `duration`.
    with pytest.raises(PlanValidationError, match="more than"):
        validate_plan(_plan(_wait("pause", until="2099-01-01T00:00:00Z")))


def test_accepts_until_within_horizon():
    # 1 hour from now is well within the 24h cap.
    target = datetime.now(timezone.utc) + timedelta(hours=1)
    plan = validate_plan(_plan(_wait("pause", until=target.isoformat())))
    assert plan.tasks[0].until == target.isoformat()


def test_accepts_past_until_value():
    # Past `until` is allowed — the orchestrator's timer will fire
    # immediately. Capping in the past would force callers to add slop
    # for clock skew between authoring and execution; benign as-is.
    plan = validate_plan(_plan(_wait("pause", until="2000-01-01T00:00:00Z")))
    assert plan.tasks[0].until == "2000-01-01T00:00:00Z"


# ---------------------------------------------------------------------------
# additional DAG / cycle edge cases (M1 step 4)
# ---------------------------------------------------------------------------


def test_detects_four_node_cycle():
    """Cycle detection must walk longer back-edges, not just two/three hops."""
    raw = _plan(
        _task("a", depends_on=["d"]),
        _task("b", depends_on=["a"]),
        _task("c", depends_on=["b"]),
        _task("d", depends_on=["c"]),
    )
    with pytest.raises(PlanValidationError, match="cycle"):
        validate_plan(raw)


def test_detects_cycle_reachable_from_acyclic_prefix():
    """A forward chain feeding into a cycle still fails — the cycle must
    be reported even though some nodes are reachable from a clean root.
    """
    raw = _plan(
        _task("root"),
        _task("a", depends_on=["root", "c"]),
        _task("b", depends_on=["a"]),
        _task("c", depends_on=["b"]),
    )
    with pytest.raises(PlanValidationError, match="cycle"):
        validate_plan(raw)


def test_detects_cycle_with_no_roots():
    """If every node has an incoming edge (no roots), validation must
    still surface the underlying cycle — not crash, not loop forever.
    """
    raw = _plan(
        _task("a", depends_on=["b"]),
        _task("b", depends_on=["a"]),
    )
    with pytest.raises(PlanValidationError, match="cycle"):
        validate_plan(raw)


def test_accepts_diamond_dag_with_cross_edges():
    """Classic diamond: A -> {B, C} -> D. Both branches must validate
    and D's templating may reference both B and C.
    """
    raw = _plan(
        _task("a"),
        _task("b", depends_on=["a"]),
        _task("c", depends_on=["a"]),
        _task(
            "d",
            depends_on=["b", "c"],
            args={"left": "${b.result}", "right": "${c.result}"},
        ),
    )
    plan = validate_plan(raw)
    assert {t.id for t in plan.tasks} == {"a", "b", "c", "d"}


def test_accepts_dag_with_transitive_cross_edge():
    """A task may depend on both a direct parent and a transitive
    ancestor (A -> B -> C plus A -> C). This is legal in a DAG and
    must not be flagged as a cycle or duplicate edge.
    """
    raw = _plan(
        _task("a"),
        _task("b", depends_on=["a"]),
        _task("c", depends_on=["a", "b"]),
    )
    plan = validate_plan(raw)
    assert plan.tasks[2].depends_on == ["a", "b"]


def test_accepts_plan_at_exact_max_nodes():
    """MAX_NODES is the cap (inclusive). A plan with exactly MAX_NODES
    tasks must validate; the existing test only covers the over-cap path.
    """
    tasks = [_task(f"n{i}") for i in range(MAX_NODES)]
    plan = validate_plan(_plan(*tasks))
    assert len(plan.tasks) == MAX_NODES


def test_template_to_transitive_ancestor_is_allowed():
    """Templating a non-immediate ancestor is fine as long as it's in
    the upstream closure — covers the "diamond + grandparent ref" case.
    """
    raw = _plan(
        _task("root"),
        _task("mid", depends_on=["root"]),
        _task(
            "leaf",
            depends_on=["mid"],
            args={"top": "${root.result}"},
        ),
    )
    plan = validate_plan(raw)
    assert plan.tasks[2].args["top"] == "${root.result}"


def test_template_to_sibling_is_rejected():
    """Two children of the same parent are NOT in each other's upstream
    closure — referencing a sibling must fail validation.
    """
    raw = _plan(
        _task("root"),
        _task("left", depends_on=["root"]),
        _task(
            "right",
            depends_on=["root"],
            args={"borrow": "${left.result}"},
        ),
    )
    with pytest.raises(PlanValidationError, match="not an upstream dependency"):
        validate_plan(raw)


def test_wait_task_can_depend_on_tool_task():
    """``wait`` nodes may sit downstream of evidence-gathering tools
    (e.g. fetch-then-cooldown). Round-trip through plan_to_activity_inputs
    so we also catch any hidden assumption that wait tasks must be roots.
    """
    raw = _plan(
        _task("fetch"),
        _wait("cooldown", duration="PT5M", depends_on=["fetch"]),
        _task("retry", depends_on=["cooldown"]),
    )
    plan = validate_plan(raw)
    inputs = plan_to_activity_inputs(plan)
    cooldown = next(i for i in inputs if i["id"] == "cooldown")
    assert cooldown["depends_on"] == ["fetch"]
    assert cooldown["duration"] == "PT5M"


def test_rejects_blank_task_id():
    """``id`` is constrained to ``min_length=1``. An empty string must
    surface a clean validation error rather than silently behaving as a
    nameless node.
    """
    raw = _plan(_task(""))
    with pytest.raises(PlanValidationError):
        validate_plan(raw)


def test_rejects_non_string_task_id():
    raw = _plan({"id": 42, "type": "tool", "tool": ECHO_TOOL_NAME, "args": {}})
    with pytest.raises(PlanValidationError):
        validate_plan(raw)

