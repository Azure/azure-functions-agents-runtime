"""Workflow plan schema (M1 step 3b — DAG + templating + wait tasks).

Scope at 3b: arbitrary-DAG plans whose nodes are ``tool`` or ``wait``
tasks, with cycle detection, ``depends_on`` validation, result-templating
refs validated against the upstream closure, and ISO-8601 ``duration`` /
``until`` parsing for ``wait`` tasks. Cooperative cancel is implemented in
the engine; the validator only ensures wait specifications are well-
formed.

Templating syntax:
    ``${node_id.result}``                  — entire prior result
    ``${node_id.result.path.to.field}``    — dotted path traversal

References may appear anywhere in a string ``args`` value. A full-string
match preserves the referenced value's native type (dict/list/number);
embedded matches inside a larger string are stringified (JSON for non-
strings, raw for strings). Path traversal happens at orchestrator-run
time against JSON-normalized prior outputs; an unresolved path is a
deterministic runtime failure (see :func:`resolve_template_value`).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field, ValidationError


class PlanValidationError(ValueError):
    """Raised when a plan fails structural or semantic validation.

    Message is intended to be surfaced to the LLM caller so it can
    self-correct and resubmit.
    """


class TemplateResolutionError(ValueError):
    """Raised at orchestration time when a template path cannot be resolved."""


TOOL_TASK_TYPE: str = "tool"
WAIT_TASK_TYPE: str = "wait"
SUPPORTED_TASK_TYPES: frozenset[str] = frozenset({TOOL_TASK_TYPE, WAIT_TASK_TYPE})


class WorkflowTask(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)
    type: str = Field(default=TOOL_TASK_TYPE)
    # ``tool`` is required for type=tool, must be omitted for type=wait.
    tool: Optional[str] = Field(default=None)
    args: Dict[str, Any] = Field(default_factory=dict)
    depends_on: List[str] = Field(default_factory=list)
    # Wait-task fields. Exactly one of ``duration`` (ISO-8601 like
    # ``"PT30S"``) or ``until`` (ISO-8601 datetime) must be set when
    # ``type == "wait"``; both must be omitted otherwise.
    duration: Optional[str] = Field(default=None)
    until: Optional[str] = Field(default=None)


class WorkflowPlan(BaseModel):
    version: int = Field(default=1)
    tasks: List[WorkflowTask] = Field(..., min_length=1)


ECHO_TOOL_NAME: str = "__echo"

# Hard caps. M1 defaults; configurable from frontmatter lands in M5.
MAX_NODES: int = 50
MAX_PARALLELISM: int = 10
MAX_WAIT_DURATION: timedelta = timedelta(hours=24)

# Template ref grammar:
#   ${id.result}           — entire result
#   ${id.result.a.b.c}     — dotted path into a JSON-shaped result
# id matches task-id syntax (alnum / underscore / hyphen).
_TEMPLATE_RE = re.compile(
    r"\$\{([A-Za-z0-9_\-]+)\.result"
    r"(?:\.([A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)*))?\}"
)
# Catches malformed ``${...}`` that doesn't conform to _TEMPLATE_RE so we
# can fail loudly rather than silently leaving the literal in the args.
_TEMPLATE_LIKE_RE = re.compile(r"\$\{[^}]*\}")
# Detects an unclosed ``${`` — a `${` that is not followed by a balanced
# ``}`` before end-of-string. We check this separately because
# _TEMPLATE_LIKE_RE requires the closing brace and would silently miss
# unterminated refs like ``"${a.result"``.
_TEMPLATE_UNCLOSED_RE = re.compile(r"\$\{[^}]*\Z")


def validate_plan(
    raw: Dict[str, Any],
    *,
    allowed_tools: Set[str],
) -> WorkflowPlan:
    """Validate and normalize a plan dict.

    ``allowed_tools`` is the set of tool names admitted as ``type=tool``
    node targets. In production this is computed by
    :func:`build_workflow_integration` from the agent's frontmatter and
    the registry. There is no fallback — the validator never invents
    its own allowlist.

    Raises :class:`PlanValidationError` with a caller-friendly message on
    any structural or semantic problem.
    """
    effective_allowed = frozenset(allowed_tools)
    try:
        plan = WorkflowPlan.model_validate(raw)
    except ValidationError as exc:
        raise PlanValidationError(f"plan does not match schema: {exc}") from exc

    if len(plan.tasks) > MAX_NODES:
        raise PlanValidationError(
            f"plan has {len(plan.tasks)} tasks but the per-plan limit is "
            f"{MAX_NODES}. Break the work into smaller workflows or reduce "
            "the number of nodes."
        )

    seen: Set[str] = set()
    for task in plan.tasks:
        if task.id in seen:
            raise PlanValidationError(f"duplicate task id: {task.id!r}")
        seen.add(task.id)

        if task.type not in SUPPORTED_TASK_TYPES:
            raise PlanValidationError(
                f"task {task.id!r}: type {task.type!r} is not supported. "
                f"Supported types: {sorted(SUPPORTED_TASK_TYPES)}"
            )

        if task.type == TOOL_TASK_TYPE:
            if not task.tool:
                raise PlanValidationError(
                    f"task {task.id!r}: 'tool' field is required for "
                    "type=tool tasks"
                )
            if task.tool not in effective_allowed:
                raise PlanValidationError(
                    f"task {task.id!r}: tool {task.tool!r} is not workflow-safe. "
                    f"Allowed tools: {sorted(effective_allowed)}"
                )
            if task.duration is not None or task.until is not None:
                raise PlanValidationError(
                    f"task {task.id!r}: 'duration' and 'until' are only "
                    "valid on type=wait tasks"
                )
        elif task.type == WAIT_TASK_TYPE:
            if task.tool is not None:
                raise PlanValidationError(
                    f"task {task.id!r}: 'tool' is not valid on type=wait tasks"
                )
            if task.args:
                raise PlanValidationError(
                    f"task {task.id!r}: 'args' is not valid on type=wait tasks "
                    "(use 'duration' or 'until' instead)"
                )
            has_duration = task.duration is not None
            has_until = task.until is not None
            if has_duration == has_until:
                raise PlanValidationError(
                    f"task {task.id!r}: type=wait tasks must specify exactly "
                    "one of 'duration' (ISO-8601 like 'PT30S') or 'until' "
                    "(ISO-8601 datetime); not both, not neither"
                )
            if has_duration:
                try:
                    delta = parse_iso8601_duration(task.duration)
                except ValueError as exc:
                    raise PlanValidationError(
                        f"task {task.id!r}: invalid duration {task.duration!r}: "
                        f"{exc}"
                    ) from exc
                if delta <= timedelta(0):
                    raise PlanValidationError(
                        f"task {task.id!r}: duration must be positive"
                    )
                if delta > MAX_WAIT_DURATION:
                    raise PlanValidationError(
                        f"task {task.id!r}: duration exceeds the maximum of "
                        f"{MAX_WAIT_DURATION}"
                    )
            else:
                try:
                    until_dt = parse_iso8601_datetime(task.until)
                except ValueError as exc:
                    raise PlanValidationError(
                        f"task {task.id!r}: invalid until {task.until!r}: {exc}"
                    ) from exc
                # Cap `until` at the same horizon as `duration`. Using
                # wall-clock at submit time is fine — this is a submit-time
                # admission gate, not a replay-deterministic computation.
                # Defense-in-depth check happens again in the orchestrator
                # against context.current_utc_datetime (see engine.py).
                horizon = datetime.now(timezone.utc) + MAX_WAIT_DURATION
                if until_dt > horizon:
                    raise PlanValidationError(
                        f"task {task.id!r}: until {task.until!r} is more than "
                        f"{MAX_WAIT_DURATION} in the future"
                    )

    # Validate ``depends_on`` edges reference known task ids (no self-loops,
    # no duplicates) and detect cycles.
    by_id: Dict[str, WorkflowTask] = {t.id: t for t in plan.tasks}
    for task in plan.tasks:
        dep_set: Set[str] = set()
        for dep in task.depends_on:
            if dep == task.id:
                raise PlanValidationError(
                    f"task {task.id!r}: depends_on cannot reference itself"
                )
            if dep in dep_set:
                raise PlanValidationError(
                    f"task {task.id!r}: duplicate dependency {dep!r}"
                )
            if dep not in by_id:
                raise PlanValidationError(
                    f"task {task.id!r}: depends_on references unknown task {dep!r}"
                )
            dep_set.add(dep)

    cycle = _detect_cycle(plan)
    if cycle is not None:
        pretty = " -> ".join(cycle)
        raise PlanValidationError(
            f"plan contains a dependency cycle: {pretty}"
        )

    # Validate templating refs against the upstream closure of each task.
    upstream = _upstream_closure(plan)
    for task in plan.tasks:
        _validate_task_templates(task, upstream[task.id], by_id)

    return plan


def _detect_cycle(plan: WorkflowPlan) -> List[str] | None:
    """Return a cycle as an ordered task-id list if present, else None."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: Dict[str, int] = {t.id: WHITE for t in plan.tasks}
    deps: Dict[str, List[str]] = {t.id: list(t.depends_on) for t in plan.tasks}
    parent: Dict[str, str] = {}

    def dfs(start: str) -> List[str] | None:
        # Iterative DFS so deeply-nested plans don't blow the recursion limit.
        stack: List[Tuple[str, int]] = [(start, 0)]
        path: List[str] = []
        while stack:
            node, idx = stack[-1]
            if idx == 0:
                if color[node] == GRAY:
                    # Cycle: walk back via ``parent`` until we close the loop.
                    cycle = [node]
                    cur = path[-1]
                    while cur != node:
                        cycle.append(cur)
                        cur = parent[cur]
                    cycle.append(node)
                    cycle.reverse()
                    return cycle
                if color[node] == BLACK:
                    stack.pop()
                    continue
                color[node] = GRAY
                path.append(node)
            children = deps[node]
            if idx < len(children):
                stack[-1] = (node, idx + 1)
                child = children[idx]
                if color.get(child, BLACK) != BLACK:
                    parent[child] = node
                    stack.append((child, 0))
                continue
            color[node] = BLACK
            path.pop()
            stack.pop()
        return None

    for tid in deps:
        if color[tid] == WHITE:
            cycle = dfs(tid)
            if cycle is not None:
                return cycle
    return None


def _upstream_closure(plan: WorkflowPlan) -> Dict[str, Set[str]]:
    """Return ``{task_id: set of all transitive predecessors}``.

    Cycle detection is expected to have run before this; the
    ``in_progress`` guard is defense-in-depth so a future refactor that
    moved this earlier wouldn't blow the recursion limit on a cycle.
    """
    deps: Dict[str, List[str]] = {t.id: list(t.depends_on) for t in plan.tasks}
    closure: Dict[str, Set[str]] = {}
    in_progress: Set[str] = set()

    def compute(tid: str) -> Set[str]:
        if tid in closure:
            return closure[tid]
        if tid in in_progress:
            # Should be unreachable — _detect_cycle ran first. Raising here
            # surfaces the invariant violation instead of recursing forever.
            raise PlanValidationError(
                f"internal error: cycle reached _upstream_closure at {tid!r}"
            )
        in_progress.add(tid)
        acc: Set[str] = set()
        for d in deps[tid]:
            acc.add(d)
            acc.update(compute(d))
        closure[tid] = acc
        in_progress.discard(tid)
        return acc

    for tid in deps:
        compute(tid)
    return closure


def _validate_task_templates(
    task: WorkflowTask,
    upstream_ids: Set[str],
    by_id: Dict[str, WorkflowTask],
) -> None:
    """Ensure every ``${...}`` reference in the task's args is well-formed
    and points to an upstream task.
    """
    for path, value in _walk_strings(task.args, ()):
        # Catch unterminated ``${`` (no closing brace before end of string)
        # before the inner finditer loop, which only sees balanced ``${...}``.
        if _TEMPLATE_UNCLOSED_RE.search(value):
            raise PlanValidationError(
                f"task {task.id!r}: unterminated template reference at "
                f"args path {_format_arg_path(path)} — missing closing '}}'"
            )
        # Catch ``${...}`` literals that don't match the strict template
        # regex — silently leaving these in args would defeat the point of
        # templating.
        for like_match in _TEMPLATE_LIKE_RE.finditer(value):
            literal = like_match.group(0)
            if not _TEMPLATE_RE.fullmatch(literal):
                raise PlanValidationError(
                    f"task {task.id!r}: malformed template "
                    f"reference {literal!r} at args path "
                    f"{_format_arg_path(path)} — expected "
                    "${{node_id.result}} or ${{node_id.result.path}}"
                )
        for ref_match in _TEMPLATE_RE.finditer(value):
            ref_id = ref_match.group(1)
            if ref_id not in by_id:
                raise PlanValidationError(
                    f"task {task.id!r}: template references unknown task "
                    f"{ref_id!r} at args path {_format_arg_path(path)}"
                )
            if ref_id not in upstream_ids:
                raise PlanValidationError(
                    f"task {task.id!r}: template references {ref_id!r} which "
                    "is not an upstream dependency. Add it to depends_on or "
                    "remove the reference."
                )


def _walk_strings(
    obj: Any, path: Tuple[Any, ...]
) -> List[Tuple[Tuple[Any, ...], str]]:
    """Yield (path-tuple, string-value) pairs for every string leaf in obj."""
    out: List[Tuple[Tuple[Any, ...], str]] = []
    if isinstance(obj, str):
        out.append((path, obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(_walk_strings(v, path + (k,)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(_walk_strings(v, path + (i,)))
    return out


def _format_arg_path(path: Tuple[Any, ...]) -> str:
    if not path:
        return "<root>"
    parts: List[str] = []
    for p in path:
        parts.append(f"[{p}]" if isinstance(p, int) else f".{p}")
    return "args" + "".join(parts)


def resolve_template_value(value: Any, results: Dict[str, Any]) -> Any:
    """Substitute template refs in ``value`` against ``results``.

    Used by the orchestrator immediately before scheduling each task. The
    results dict is keyed by task id and holds JSON-normalized outputs of
    completed upstream tasks. Raises :class:`TemplateResolutionError` if a
    referenced node hasn't completed (which should be impossible if the
    plan was validated and the wave scheduler is correct) or a dotted path
    cannot be traversed.
    """
    if isinstance(value, str):
        full = _TEMPLATE_RE.fullmatch(value)
        if full is not None:
            return _resolve_ref(full.group(1), full.group(2), results)
        any_like = _TEMPLATE_LIKE_RE.search(value)
        if any_like is None and not _TEMPLATE_UNCLOSED_RE.search(value):
            return value

        def repl(match: re.Match) -> str:
            resolved = _resolve_ref(match.group(1), match.group(2), results)
            if isinstance(resolved, str):
                return resolved
            return json.dumps(resolved, sort_keys=True)

        substituted = _TEMPLATE_RE.sub(repl, value)
        # Defense-in-depth: if validation was bypassed, an unmatched
        # ``${...}`` token could survive substitution. Surface that as a
        # deterministic failure rather than passing a half-resolved string
        # to the activity.
        leftover = _TEMPLATE_LIKE_RE.search(substituted) or _TEMPLATE_UNCLOSED_RE.search(
            substituted
        )
        if leftover is not None:
            raise TemplateResolutionError(
                f"unresolved template token {leftover.group(0)!r} survived "
                "substitution (plan validation may have been bypassed)"
            )
        return substituted
    if isinstance(value, dict):
        return {k: resolve_template_value(v, results) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_template_value(v, results) for v in value]
    return value


def _resolve_ref(node_id: str, dotted_path: str | None, results: Dict[str, Any]) -> Any:
    if node_id not in results:
        raise TemplateResolutionError(
            f"template references {node_id!r} but no result is available "
            "(upstream task hasn't completed)"
        )
    cur: Any = results[node_id]
    if not dotted_path:
        return cur
    parts = dotted_path.split(".")
    for i, part in enumerate(parts):
        if isinstance(cur, dict):
            if part not in cur:
                raise TemplateResolutionError(
                    f"template path ${{{node_id}.result.{dotted_path}}} "
                    f"failed at segment {part!r}: key not present"
                )
            cur = cur[part]
        elif isinstance(cur, list):
            try:
                idx = int(part)
            except ValueError as exc:
                raise TemplateResolutionError(
                    f"template path ${{{node_id}.result.{dotted_path}}} "
                    f"failed at segment {part!r}: list index must be an integer"
                ) from exc
            if idx < 0 or idx >= len(cur):
                raise TemplateResolutionError(
                    f"template path ${{{node_id}.result.{dotted_path}}} "
                    f"failed at segment {part!r}: index out of range"
                )
            cur = cur[idx]
        else:
            traversed = ".".join(parts[:i])
            raise TemplateResolutionError(
                f"template path ${{{node_id}.result.{dotted_path}}} "
                f"failed at segment {part!r}: parent value at "
                f"{traversed or '<root>'} is not a dict or list"
            )
    return cur


def plan_to_activity_inputs(plan: WorkflowPlan) -> List[Dict[str, Any]]:
    """Flatten a validated plan into the JSON list the orchestrator iterates.

    The orchestrator needs ``depends_on`` to drive wave scheduling, plus
    ``type`` so it knows whether to call an activity or schedule a timer,
    so we keep them on the wire alongside id/tool/args/duration/until.
    """
    out: List[Dict[str, Any]] = []
    for t in plan.tasks:
        entry: Dict[str, Any] = {
            "id": t.id,
            "type": t.type,
            "depends_on": list(t.depends_on),
        }
        if t.type == TOOL_TASK_TYPE:
            entry["tool"] = t.tool
            entry["args"] = dict(t.args)
        else:  # WAIT_TASK_TYPE
            if t.duration is not None:
                entry["duration"] = t.duration
            if t.until is not None:
                entry["until"] = t.until
        out.append(entry)
    return out


# ISO-8601 helpers ---------------------------------------------------------
#
# We accept the ``PnDTnHnMnS`` subset of ISO-8601 durations because that is
# what humans (and LLMs) actually emit, and the full grammar (years/months,
# week form, fractional components in non-final positions) is overkill.
# Acceptance grammar:
#   P[<n>D][T[<n>H][<n>M][<n>(.<n>)?S]]
# At least one component is required. ``until`` accepts ISO-8601 datetimes
# via ``datetime.fromisoformat`` plus the trailing-Z form some emitters use.

_DURATION_RE = re.compile(
    r"^P"
    r"(?:(?P<days>\d+)D)?"
    r"(?:T"
    r"(?:(?P<hours>\d+)H)?"
    r"(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?"
    r")?$"
)


def parse_iso8601_duration(text: str) -> timedelta:
    """Parse an ISO-8601 duration in the ``PnDTnHnMnS`` subset.

    Raises ``ValueError`` with a caller-friendly message on any parse
    problem. Returns a ``timedelta``; callers enforce upper/lower bounds.
    """
    if not isinstance(text, str) or not text:
        raise ValueError("duration must be a non-empty ISO-8601 string")
    m = _DURATION_RE.match(text)
    if not m:
        raise ValueError(
            "expected ISO-8601 duration like 'PT30S', 'PT5M', 'PT1H30M', "
            "or 'P1DT2H'"
        )
    if all(g is None for g in m.groupdict().values()):
        raise ValueError("duration has no components")
    if "T" in text and all(
        m.group(name) is None for name in ("hours", "minutes", "seconds")
    ):
        raise ValueError("duration time section has no components")
    days = int(m.group("days") or 0)
    hours = int(m.group("hours") or 0)
    minutes = int(m.group("minutes") or 0)
    seconds = float(m.group("seconds") or 0.0)
    try:
        return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
    except OverflowError as exc:
        # `timedelta(...)` raises OverflowError for oversized inputs (e.g.
        # P1000000000D). Re-raise as ValueError so callers — which only
        # catch ValueError to produce caller-friendly PlanValidationError —
        # see a clean rejection path.
        raise ValueError(f"duration is too large: {exc}") from exc


def parse_iso8601_datetime(text: str) -> datetime:
    """Parse an ISO-8601 datetime; require explicit timezone awareness.

    The trailing-``Z`` form is accepted as UTC for convenience. Naive
    datetimes are rejected — mixing tz-naive and tz-aware datetimes inside
    the orchestrator would surface as confusing TypeErrors at run time.
    """
    if not isinstance(text, str) or not text:
        raise ValueError("until must be a non-empty ISO-8601 string")
    candidate = text
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(
            "expected ISO-8601 datetime like '2026-04-25T17:30:00Z' or "
            "'2026-04-25T10:30:00-07:00'"
        ) from exc
    if dt.tzinfo is None:
        raise ValueError(
            "datetime must include a timezone offset (use trailing 'Z' for "
            "UTC, or an explicit '+HH:MM' offset)"
        )
    return dt.astimezone(timezone.utc)


__all__ = [
    "ECHO_TOOL_NAME",
    "MAX_NODES",
    "MAX_PARALLELISM",
    "MAX_WAIT_DURATION",
    "PlanValidationError",
    "SUPPORTED_TASK_TYPES",
    "TOOL_TASK_TYPE",
    "TemplateResolutionError",
    "WAIT_TASK_TYPE",
    "WorkflowPlan",
    "WorkflowTask",
    "parse_iso8601_datetime",
    "parse_iso8601_duration",
    "plan_to_activity_inputs",
    "resolve_template_value",
    "validate_plan",
]
