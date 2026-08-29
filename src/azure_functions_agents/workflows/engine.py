"""Workflow engine: Durable Functions orchestrator + activities (M1 step 3b).

Wave-based DAG scheduler with two task primitives:

- ``tool`` tasks dispatch to a workflow-safe handler via the activity.
- ``wait`` tasks resolve to a durable timer (``context.create_timer``).
  Their result is ``{"waited_until": "<iso>"}`` so downstream templating
  refs see something useful.

Cooperative cancel is implemented as a single ``wait_for_external_event``
("cancel") task that races the wave via ``context.task_any``. When the
event fires we return a ``canceled=True`` envelope and stop scheduling.
The Durable runtime_status remains ``Completed`` (Durable doesn't have
a first-class cooperative-cancel terminal state); the tool-facing
envelope (see :mod:`.tools`) translates that to ``runtime_status="Canceled"``
when the orchestrator's output indicates cancellation.

What is intentionally still *not* here: retries and per-task timeouts.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, NotRequired, TypedDict, cast

import azure.durable_functions as df
import azure.functions as func

from azure_functions_agents._logger import logger
from azure_functions_agents.registration.catalog import AgentCatalog
from azure_functions_agents.runner import run_leaf_agent_task

from . import registry
from .activity import (
    ActivityFailure,
    WorkflowTaskTimeoutError,
    authorization_outcome,
    early_policy_outcome_with_telemetry,
    failure_is_continuable,
    handler_contract_outcome,
    invoke_handler,
    invoke_policy_handler,
    validate_activity_result,
    validate_policy_activity_input,
)
from .native_retry import create_durable_retry_policy, decode_durable_retry_failure
from .schema import (
    ECHO_TOOL_NAME,
    MAX_NODES,
    MAX_PARALLELISM,
    MAX_WAIT_DURATION,
    SUB_AGENT_TASK_TYPE,
    TOOL_TASK_TYPE,
    WAIT_TASK_TYPE,
    EffectiveWorkflowTaskExecution,
    TemplateResolutionError,
    WorkflowCondition,
    WorkflowPayload,
    WorkflowPlanPolicy,
    WorkflowTaskInput,
    evaluate_condition,
    parse_iso8601_datetime,
    parse_iso8601_duration,
    resolve_template_value,
)

ORCHESTRATOR_NAME = "agents_workflow_orchestrator"
CANCEL_EVENT_NAME = "cancel"
_ACTIVITY_NAME = "agents_workflow_run_tool"
SUB_AGENT_ACTIVITY_NAME = "agents_workflow_run_sub_agent"

WORKFLOW_SAFE_ECHO_TOOL = ECHO_TOOL_NAME


class _ActivityInputBase(TypedDict):
    id: str
    workflow_agent_slug: str
    workflow_id: str
    # Present only when a retry policy was frozen at submission time. Its
    # absence is what keeps histories written by earlier runtime versions on the
    # legacy dispatch and legacy result envelope during replay.
    task_id: NotRequired[str]
    execution: NotRequired[EffectiveWorkflowTaskExecution]


class _ToolActivityInput(_ActivityInputBase):
    tool: str
    args: dict[str, Any]


class _SubAgentActivityInput(_ActivityInputBase):
    agent: str
    task: str


type _ActivityInput = _ToolActivityInput | _SubAgentActivityInput


def _run_echo(args: dict[str, Any]) -> dict[str, Any]:
    """Trivial workflow-safe tool used by unit tests.

    Registered as ``public=False`` — it stays available for tests but
    is not included in the default allowlist handed to agents, so a
    workflow-enabled agent can't reach for ``__echo`` by accident.
    """
    return {"echoed": args}


# Registered exactly once at module import. Reserved-name and async
# guards live in registry.register_workflow_tool.
if registry.get_entry(ECHO_TOOL_NAME) is None:
    registry.register_workflow_tool(
        ECHO_TOOL_NAME,
        "Internal echo tool used by the workflow unit tests. "
        "Returns its args under an 'echoed' key.",
        _run_echo,
        public=False,
    )


def _wait_deadline(context: df.DurableOrchestrationContext, task: Mapping[str, Any]) -> Any:
    """Compute the absolute UTC deadline for a wait task.

    Validation already enforced exactly one of ``duration`` / ``until``
    and bounded both to ``MAX_WAIT_DURATION``. We re-parse here because
    the orchestrator only sees the JSON wire payload, not the Pydantic
    model. We also re-check the horizon against ``current_utc_datetime``
    as deterministic defense-in-depth — the validator's check used wall
    clock at submit time, which can drift between submit and execution.
    """
    now = context.current_utc_datetime
    if task.get("duration") is not None:
        delta = parse_iso8601_duration(task["duration"])
        deadline = now + delta
    else:
        deadline = parse_iso8601_datetime(task["until"])
    if deadline - now > MAX_WAIT_DURATION:
        raise RuntimeError(
            f"task {task.get('id')!r}: wait deadline exceeds the "
            f"maximum of {MAX_WAIT_DURATION}"
        )
    return deadline


def _persisted_execution(
    task: Mapping[str, Any],
) -> EffectiveWorkflowTaskExecution | None:
    """Return the retry policy frozen into the orchestration input, if any.

    Replay safety depends on reading this only from the persisted payload:
    a currently deployed ``@workflow_tool`` retry declaration must never change
    how an already-started orchestration dispatches its Activities.
    """
    execution = task.get("execution")
    if not isinstance(execution, dict) or "durable_retry_policy" not in execution:
        return None
    return cast(EffectiveWorkflowTaskExecution, execution)


def _policy_activity_fields(
    task: Mapping[str, Any],
    *,
    logical_id: str,
) -> dict[str, Any]:
    """Return the extra Activity input keys a policy-aware task carries."""
    execution = _persisted_execution(task)
    if execution is None:
        return {}
    return {"task_id": logical_id, "execution": execution}


def _continue_on_error(task: Mapping[str, Any]) -> bool:
    """Return whether the persisted policy lets the DAG proceed past a failure.

    Read from the persisted orchestration input only, and matched by identity so
    a payload whose value is merely truthy cannot relax the DAG.
    """
    execution = _persisted_execution(task)
    return execution is not None and execution.get("continue_on_error") is True


def _continued_failure_result(failure: ActivityFailure) -> dict[str, Any]:
    """Build the sanitized result a continued node commits instead of failing.

    It reuses the ``failed`` shape the orchestrator already returns for
    controlled failures, so a downstream ``${node.result...}`` reference or a
    ``when`` predicate sees one stable schema.
    """
    return {
        "failed": True,
        "error_code": failure["error_code"],
        "error": failure["error"],
        "kind": failure["kind"],
    }


def _call_task_activity(
    context: df.DurableOrchestrationContext,
    name: str,
    activity_input: dict[str, Any],
) -> Any:
    """Dispatch through the retry driver frozen into the orchestration input."""
    execution = _persisted_execution(activity_input)
    if execution is None:
        return context.call_activity(name, activity_input)
    return context.call_activity_with_retry(
        name,
        create_durable_retry_policy(execution["durable_retry_policy"]),
        activity_input,
    )


@dataclass(frozen=True)
class _TaskOutcome:
    """A committed node result plus, when it was continued, the failure behind it.

    ``continued_failure`` is scheduler metadata only. It never reaches the
    workflow output — the node's committed ``result`` is the sanitized ``failed``
    envelope — but it lets the structured status report *which* failure was
    continued without re-deriving it from the result shape (a successful handler
    may legitimately return a dict with a ``failed`` key).
    """

    result: Any
    continued_failure: ActivityFailure | None


def _resolve_task_outcome(
    node_id: str,
    *,
    policy_aware: bool,
    continue_on_error: bool,
    raw: Any,
) -> _TaskOutcome:
    """Return a node result, raising unless a continuable failure may be committed.

    ``raw`` is either the value the Activity produced or the exception Durable
    reported for it. A raised exception only reaches here once Durable has
    exhausted the frozen attempt budget, so continuation is never applied while
    an attempt is still owed.
    """
    if isinstance(raw, BaseException):
        failure = decode_durable_retry_failure(node_id, raw) if policy_aware else None
        if failure is None:
            # An opaque Durable failure carries no classification, so it cannot
            # be shown to be a continuable application failure.
            raise raw
    elif not policy_aware:
        return _TaskOutcome(raw["result"], None)
    else:
        succeeded, outcome = validate_activity_result(node_id, raw)
        if succeeded:
            return _TaskOutcome(outcome, None)
        failure = cast(ActivityFailure, outcome)
    if continue_on_error and failure_is_continuable(failure):
        logger.info(
            "workflow task continued past failure: node=%s code=%s",
            node_id,
            failure["error_code"],
        )
        return _TaskOutcome(_continued_failure_result(failure), failure)
    raise RuntimeError(f"task {node_id!r}: {failure['error']} ({failure['error_code']})")


def _decode_wave_failure(node_ids: list[str], error: BaseException) -> BaseException:
    """Replace an exhausted native-retry failure with its sanitized cause.

    The sanitized payload is matched back to its node by id. Node order is
    deterministic, and the decode is a pure function of persisted data, so
    replay is unaffected.
    """
    for node_id in node_ids:
        failure = decode_durable_retry_failure(node_id, error)
        if failure is not None:
            return RuntimeError(
                f"task {node_id!r}: {failure['error']} ({failure['error_code']})"
            )
    return error


def _completed_task_outcome(task: Any) -> Any:
    """Return a completed Durable task's result, or the failure it raised.

    Durable Functions Python 2.x raises from ``Task.result``; 1.x returned the
    exception object instead. Normalizing both shapes here keeps wave failure
    handling (timer cancellation, then rethrow) in exactly one place.
    """
    try:
        return task.result
    except Exception as exc:
        return exc


def _await_wave(
    context: df.DurableOrchestrationContext,
    cancel_task: Any,
    wave_tasks: list[Any],
) -> Any:
    """Await a whole wave alongside the cancel event, one completion at a time.

    Durable's composite tasks only learn about a child completing when that
    child is a *leaf*: ``CompositeTask`` subclasses never notify their own
    parent. Racing the cancel event against ``task_all(wave)`` would therefore
    nest one composite inside another and never resume. Selecting over the
    individual wave tasks keeps every child a leaf of the task being awaited.

    Yields to Durable until either the cancel event wins or every wave task has
    completed. Returns ``None`` when canceled, otherwise the per-task outcomes
    in wave order, where a failed task contributes its exception.
    """
    outcomes: dict[int, Any] = {}
    pending = list(range(len(wave_tasks)))
    while pending:
        winner = yield context.task_any(
            [cancel_task, *(wave_tasks[index] for index in pending)]
        )
        if winner is cancel_task:
            return None
        completed = next(
            (index for index in pending if wave_tasks[index] is winner),
            None,
        )
        if completed is None:
            raise RuntimeError("workflow task selection returned an unknown task")
        pending.remove(completed)
        outcomes[completed] = _completed_task_outcome(wave_tasks[completed])
    return [outcomes[index] for index in range(len(wave_tasks))]


def _first_wave_failure(wave_results: list[Any]) -> BaseException | None:
    """Return the first failure in wave order, matching ``task_all`` semantics."""
    for outcome in wave_results:
        if isinstance(outcome, BaseException):
            return outcome
    return None


def _plan_is_dynamic(tasks: list[WorkflowTaskInput]) -> bool:
    """Return whether any task opts into data-driven control flow.

    A plan is *dynamic* if any task carries a ``when`` predicate or a
    ``for_each`` expansion. Fully static plans (neither field on any task)
    keep the original wave scheduler with its exact string ``custom_status``
    behavior, so existing regression coverage is unchanged.
    """
    return any(
        task.get("when") is not None or task.get("for_each") is not None
        for task in tasks
    )


def _cancel_static_wave_timers(
    wave_specs: list[dict[str, Any]],
    wave_tasks: list[Any],
) -> None:
    """Cancel any still-pending wait timers scheduled in this wave."""
    for spec, task in zip(wave_specs, wave_tasks, strict=True):
        if spec["type"] == WAIT_TASK_TYPE and not task.is_completed:
            task.cancel()


def _run_static_workflow(
    context: df.DurableOrchestrationContext,
    payload: WorkflowPayload,
    tasks: list[WorkflowTaskInput],
) -> Any:
    """Execute a static-DAG plan in deterministic waves (pre-#1276 behavior).

    Input: ``{"tasks": [{"id", "type", "tool"?, "args"?, "duration"?,
    "until"?, "depends_on"}, ...]}``.

    Return on success: ``{"results": {task_id: result, ...}}``.
    Return on cooperative cancel: ``{"results": ..., "canceled": True,
    "reason": <event payload>, "completed_count": N, "total_count": M}``.
    """
    by_id: dict[str, WorkflowTaskInput] = {t["id"]: t for t in tasks}
    deps: dict[str, set[str]] = {
        t["id"]: set(t.get("depends_on") or []) for t in tasks
    }
    workflow_agent_slug = str(payload.get("workflow_agent_slug") or "")
    results: dict[str, Any] = {}
    remaining: set[str] = set(by_id)
    total = len(tasks)

    cancel_task = context.wait_for_external_event(CANCEL_EVENT_NAME)

    while remaining:
        ready = sorted(
            tid for tid in remaining if not (deps[tid] - results.keys())
        )
        if not ready:
            raise RuntimeError(
                "workflow stalled: no tasks ready to run but "
                f"{len(remaining)} task(s) remain. This indicates a "
                "validation bug or an unsatisfiable dependency on the "
                "submitted plan."
            )

        wave = ready[:MAX_PARALLELISM]
        wave_specs: list[dict[str, Any]] = []
        wave_tasks: list[Any] = []
        for tid in wave:
            task = by_id[tid]
            if task["type"] == TOOL_TASK_TYPE:
                try:
                    resolved_args = resolve_template_value(
                        task.get("args") or {}, results
                    )
                except TemplateResolutionError as exc:
                    raise RuntimeError(
                        f"task {tid!r}: template resolution failed: {exc}"
                    ) from exc
                wave_tasks.append(
                    _call_task_activity(
                        context,
                        _ACTIVITY_NAME,
                        {
                            "id": tid,
                            "tool": task["tool"],
                            "args": resolved_args,
                            "workflow_agent_slug": workflow_agent_slug,
                            "workflow_id": context.instance_id,
                            **_policy_activity_fields(task, logical_id=tid),
                        },
                    )
                )
                wave_specs.append({
                    "id": tid,
                    "type": TOOL_TASK_TYPE,
                    "policy_aware": _persisted_execution(task) is not None,
                    "continue_on_error": _continue_on_error(task),
                })
            elif task["type"] == SUB_AGENT_TASK_TYPE:
                try:
                    resolved_task = resolve_template_value(task["task"], results)
                except TemplateResolutionError as exc:
                    raise RuntimeError(
                        f"task {tid!r}: template resolution failed: {exc}"
                    ) from exc
                if not isinstance(resolved_task, str):
                    raise RuntimeError(
                        f"task {tid!r}: resolved Sub Agent task must be a string"
                    )
                wave_tasks.append(
                    _call_task_activity(
                        context,
                        SUB_AGENT_ACTIVITY_NAME,
                        {
                            "id": tid,
                            "agent": task["agent"],
                            "task": resolved_task,
                            "workflow_id": context.instance_id,
                            "workflow_agent_slug": workflow_agent_slug,
                            **_policy_activity_fields(task, logical_id=tid),
                        },
                    )
                )
                wave_specs.append({
                    "id": tid,
                    "type": SUB_AGENT_TASK_TYPE,
                    "policy_aware": _persisted_execution(task) is not None,
                    "continue_on_error": _continue_on_error(task),
                })
            elif task["type"] == WAIT_TASK_TYPE:
                deadline = _wait_deadline(context, task)
                wave_tasks.append(context.create_timer(deadline))
                wave_specs.append(
                    {
                        "id": tid,
                        "type": WAIT_TASK_TYPE,
                        "deadline": deadline.isoformat(),
                    }
                )
            else:
                raise RuntimeError(
                    f"task {tid!r}: unsupported task type {task['type']!r}"
                )

        context.set_custom_status(
            f"{len(results)}/{total} tasks done, running={','.join(wave)}"
        )
        wave_results = yield from _await_wave(context, cancel_task, wave_tasks)
        if wave_results is None:
            reason = cancel_task.result
            _cancel_static_wave_timers(wave_specs, wave_tasks)
            context.set_custom_status(
                f"canceled at {len(results)}/{total} tasks done"
            )
            logger.info(
                "workflow canceled: instance=%s workflow_agent=%s reason=%r",
                context.instance_id,
                workflow_agent_slug,
                reason,
            )
            return {
                "results": results,
                "canceled": True,
                "reason": reason,
                "completed_count": len(results),
                "total_count": total,
            }

        failure = _first_wave_failure(wave_results)
        if failure is not None:
            # ``_await_wave`` already reports every node's own outcome, so a
            # continuable wave is applied node by node instead of failing whole.
            continuable_wave = any(
                spec.get("continue_on_error") for spec in wave_specs
            )
            _cancel_static_wave_timers(wave_specs, wave_tasks)
            if not continuable_wave:
                raise _decode_wave_failure(
                    [spec["id"] for spec in wave_specs if spec.get("policy_aware")],
                    failure,
                )
        for spec, raw in zip(wave_specs, wave_results, strict=True):
            tid = spec["id"]
            if spec["type"] in {TOOL_TASK_TYPE, SUB_AGENT_TASK_TYPE}:
                results[tid] = _resolve_task_outcome(
                    tid,
                    policy_aware=bool(spec.get("policy_aware")),
                    continue_on_error=bool(spec.get("continue_on_error")),
                    raw=raw,
                ).result
            else:
                results[tid] = {"waited_until": spec["deadline"]}
            remaining.discard(tid)

        running_id = ""
        next_ready = sorted(
            tid for tid in remaining if not (deps[tid] - results.keys())
        )
        if next_ready:
            running_id = next_ready[0]
        done = len(results)
        if running_id:
            context.set_custom_status(
                f"{done}/{total} tasks done, next={running_id}"
            )
        else:
            context.set_custom_status(f"{done}/{total} tasks done")

    return {"results": results}


# ---------------------------------------------------------------------------
# Dynamic (data-driven) orchestration — Issue #1276.
# ---------------------------------------------------------------------------


def _failure_envelope(
    *,
    error: str,
    error_code: str,
    node_id: str,
    path: str | None,
    results: dict[str, Any],
) -> dict[str, Any]:
    """Build the flat controlled-failure output the status adapter maps to Failed."""
    return {
        "failed": True,
        "error": error,
        "error_code": error_code,
        "node_id": node_id,
        "path": path,
        "results": results,
    }


type _LogicalState = Literal[
    "pending",
    "running",
    "skipped",
    "expanded",
    "aggregated",
    "completed",
    "failed",
]
type _InstanceState = Literal["pending", "running", "skipped", "completed", "failed"]
type _InstanceKind = Literal["activity", "timer"]


class _MaterializedInstance(TypedDict):
    logical_id: str
    index: int | None
    instance_id: str
    state: _InstanceState
    result: Any
    resolved: NotRequired[Any]
    kind: NotRequired[_InstanceKind]
    deadline: NotRequired[str]
    # Scheduler-only metadata for a node that ``continue_on_error`` committed a
    # sanitized failure for. Its ``state`` deliberately stays ``completed``: the
    # aggregate ``{index, status, result}`` contract and every terminal-state
    # predicate below are unchanged, and the structured status projects the
    # continuation separately.
    continued_failure: NotRequired[ActivityFailure]


@dataclass
class _DynamicWorkflowState:
    by_id: dict[str, WorkflowTaskInput]
    deps: dict[str, set[str]]
    allowed_tools: frozenset[str]
    allowed_subagents: frozenset[str]
    workflow_agent_slug: str
    results: dict[str, Any]
    logical_state: dict[str, _LogicalState]
    node_instances: dict[str, list[_MaterializedInstance]]
    expanded_count: dict[str, int]
    budget_used: int
    policy_aware: bool


def _new_dynamic_workflow_state(
    payload: WorkflowPayload,
    tasks: list[WorkflowTaskInput],
) -> _DynamicWorkflowState:
    by_id = {task["id"]: task for task in tasks}
    policy_input = payload.get("policy")
    allowed_tools = (
        frozenset(policy_input.get("allowed_tools", []))
        if policy_input is not None
        else frozenset()
    )
    allowed_subagents = (
        frozenset(policy_input.get("allowed_subagents", []))
        if policy_input is not None
        else frozenset()
    )
    return _DynamicWorkflowState(
        by_id=by_id,
        deps={
            task["id"]: set(task.get("depends_on", []))
            for task in tasks
        },
        allowed_tools=allowed_tools,
        allowed_subagents=allowed_subagents,
        workflow_agent_slug=payload.get("workflow_agent_slug", ""),
        results={},
        logical_state={task_id: "pending" for task_id in by_id},
        node_instances={},
        expanded_count={},
        budget_used=sum(1 for task in tasks if task.get("for_each") is None),
        # Read from the persisted orchestration input, so it is identical on
        # every replay and identical for every runtime version that replays
        # this history. A plan whose tasks froze no policy keeps emitting the
        # pre-existing schema_version 2 status.
        policy_aware=any(_persisted_execution(task) is not None for task in tasks),
    )


def _materialized_total(
    node_instances: dict[str, list[_MaterializedInstance]],
) -> int:
    return sum(len(instances) for instances in node_instances.values())


#: How many continued failures may carry their `last_failure_kind` /
#: `last_error_code` detail in one status object. Durable caps a custom status
#: at 16 KB, and a fully-expanded plan can materialize ``MAX_NODES`` instances;
#: the cap is applied in deterministic (logical id, index) order so replay
#: reproduces exactly the same status.
_STATUS_FAILURE_DETAIL_LIMIT = 20


def _reported_instance_state(instance: _MaterializedInstance) -> str:
    """Project the scheduler state a status reader should see.

    A continued failure runs to a committed result, so the scheduler keeps it
    ``completed``; the status reports it as ``failed_continued`` so an operator
    is not told a node succeeded when its result is a sanitized failure.
    """
    if instance["state"] == "completed" and "continued_failure" in instance:
        return "failed_continued"
    return instance["state"]


def _dynamic_status(state: _DynamicWorkflowState) -> dict[str, Any]:
    """Build the versioned structured ``custom_status`` object.

    Version 2 is the data-driven workflow snapshot and is emitted unchanged for
    any plan that froze no execution policy. Version 3 adds task-execution
    reporting for policy-aware plans: the declared attempt budget, which node
    failed or was continued past, and that Durable — not the orchestrator — owns
    retry. Attempts in flight are deliberately not reported: Durable owns the
    budget and a replayed orchestration cannot observe it (FRD 0004 Decision 73).

    ``counts`` are instance-level for completed/skipped/running (and, in v3,
    pending/failed/failed_continued) and node-level for ``logical_total``;
    ``materialized_total`` counts every materialized instance (including skipped
    ones) in both versions. ``nodes`` renders logical node state, plus
    per-instance state for expanded ``for_each`` nodes.
    """
    completed = skipped = running = pending = failed = failed_continued = 0
    for insts in state.node_instances.values():
        for inst in insts:
            instance_state = _reported_instance_state(inst)
            if instance_state == "completed":
                completed += 1
            elif instance_state == "skipped":
                skipped += 1
            elif instance_state == "running":
                running += 1
            elif instance_state == "pending":
                pending += 1
            elif instance_state == "failed":
                failed += 1
            elif instance_state == "failed_continued":
                failed_continued += 1

    detail_budget = _STATUS_FAILURE_DETAIL_LIMIT

    def execution_fields(instance: _MaterializedInstance) -> dict[str, Any]:
        nonlocal detail_budget
        if not state.policy_aware:
            return {}
        execution = _persisted_execution(state.by_id[instance["logical_id"]])
        fields: dict[str, Any] = {}
        if execution is not None:
            fields["max_attempts"] = execution["max_attempts"]
        failure = instance.get("continued_failure")
        if failure is not None and detail_budget > 0:
            detail_budget -= 1
            fields["last_failure_kind"] = failure["kind"]
            fields["last_error_code"] = failure["error_code"]
        return fields

    nodes: dict[str, Any] = {}
    for lid, task in state.by_id.items():
        instances = state.node_instances.get(lid, [])
        node: dict[str, Any] = {"state": state.logical_state[lid]}
        if task.get("for_each") is not None and lid in state.expanded_count:
            node["expanded_count"] = state.expanded_count[lid]
            node["instances"] = {
                inst["instance_id"]: {
                    "state": _reported_instance_state(inst),
                    **execution_fields(inst),
                }
                for inst in instances
            }
        elif state.policy_aware:
            if instances:
                node["state"] = _reported_logical_state(state, lid, instances[0])
                node.update(execution_fields(instances[0]))
            else:
                execution = _persisted_execution(task)
                if execution is not None:
                    node["max_attempts"] = execution["max_attempts"]
        nodes[lid] = node

    counts: dict[str, Any] = {
        "logical_total": len(state.by_id),
        "materialized_total": _materialized_total(state.node_instances),
        "completed": completed,
        "skipped": skipped,
        "running": running,
    }
    status: dict[str, Any] = {
        "schema_version": 3 if state.policy_aware else 2,
        "counts": counts,
        "nodes": nodes,
    }
    if state.policy_aware:
        counts["pending"] = pending
        counts["failed"] = failed
        counts["failed_continued"] = failed_continued
        # Durable owns the attempt budget, so a reader must not expect this
        # runtime to publish attempt numbers or backoff deadlines.
        status["retry_driver"] = "durable"
    return status


def _reported_logical_state(
    state: _DynamicWorkflowState,
    logical_id: str,
    instance: _MaterializedInstance,
) -> str:
    """Project a non-iterated node's state, mirroring its single instance."""
    logical_state = state.logical_state[logical_id]
    if logical_state == "completed":
        return _reported_instance_state(instance)
    return logical_state


_UNBOUND = object()


def _resolve_dynamic_args(
    task: WorkflowTaskInput,
    results: dict[str, Any],
    *,
    item: Any = _UNBOUND,
    index: int | None = None,
) -> Any:
    """Resolve the executable value field for a tool/sub_agent task or instance.

    When ``item`` is left unbound (normal, non-iterated task) the iteration
    locals are not passed through, so ``resolve_template_value`` uses its own
    unbound sentinel. Iterated instances pass the bound ``item`` / ``index``.
    """
    kwargs: dict[str, Any] = {}
    if item is not _UNBOUND:
        kwargs["item"] = item
        kwargs["index"] = index
    if task["type"] == TOOL_TASK_TYPE:
        return resolve_template_value(task["args"], results, **kwargs)
    if task["type"] != SUB_AGENT_TASK_TYPE:
        raise RuntimeError(
            f"task {task['id']!r}: wait tasks have no executable value field"
        )
    resolved_task = resolve_template_value(task["task"], results, **kwargs)
    if not isinstance(resolved_task, str):
        raise TemplateResolutionError(
            f"resolved Sub Agent task must be a string, got "
            f"{type(resolved_task).__name__}"
        )
    return resolved_task


def _publish_dynamic_status(
    context: df.DurableOrchestrationContext,
    state: _DynamicWorkflowState,
) -> None:
    context.set_custom_status(_dynamic_status(state))


def _dynamic_failure(
    context: df.DurableOrchestrationContext,
    state: _DynamicWorkflowState,
    *,
    error: str,
    error_code: str,
    node_id: str,
    path: str | None,
    logical_id: str,
) -> dict[str, Any]:
    state.logical_state[logical_id] = "failed"
    _publish_dynamic_status(context, state)
    logger.info(
        "workflow failed: instance=%s node=%s code=%s",
        context.instance_id,
        node_id,
        error_code,
    )
    return _failure_envelope(
        error=error,
        error_code=error_code,
        node_id=node_id,
        path=path,
        results=state.results,
    )


def _aggregate_dynamic_node(state: _DynamicWorkflowState, logical_id: str) -> None:
    instances = sorted(
        state.node_instances[logical_id],
        key=lambda instance: instance["index"] if instance["index"] is not None else -1,
    )
    state.results[logical_id] = [
        {
            "index": instance["index"],
            "status": instance["state"],
            "result": instance["result"],
        }
        for instance in instances
    ]
    state.logical_state[logical_id] = "aggregated"


def _materialize_for_each_node(
    context: df.DurableOrchestrationContext,
    state: _DynamicWorkflowState,
    logical_id: str,
    task: WorkflowTaskInput,
) -> dict[str, Any] | None:
    ref = task.get("for_each")
    if ref is None:
        raise RuntimeError(f"task {logical_id!r}: missing for_each reference")
    try:
        collection = resolve_template_value(ref, state.results)
    except TemplateResolutionError as exc:
        return _dynamic_failure(
            context,
            state,
            error=str(exc),
            error_code=exc.error_code,
            node_id=logical_id,
            path=ref,
            logical_id=logical_id,
        )
    if not isinstance(collection, list):
        return _dynamic_failure(
            context,
            state,
            error=(
                f"task {logical_id!r}: for_each did not resolve to an "
                f"array (got {type(collection).__name__})"
            ),
            error_code="workflow_iteration_not_array",
            node_id=logical_id,
            path=ref,
            logical_id=logical_id,
        )
    count = len(collection)
    if state.budget_used + count > MAX_NODES:
        return _dynamic_failure(
            context,
            state,
            error=(
                f"task {logical_id!r}: expanding for_each over {count} element(s) "
                f"would exceed the materialized-node limit of {MAX_NODES}"
            ),
            error_code="workflow_node_limit_exceeded",
            node_id=logical_id,
            path=ref,
            logical_id=logical_id,
        )

    state.budget_used += count
    state.expanded_count[logical_id] = count
    state.logical_state[logical_id] = "expanded"
    instances: list[_MaterializedInstance] = []
    condition_input = task.get("when")
    for index, element in enumerate(collection):
        instance_id = f"{logical_id}[{index}]"
        if condition_input is not None:
            try:
                should_run = evaluate_condition(
                    WorkflowCondition.model_validate(condition_input),
                    state.results,
                    item=element,
                    index=index,
                )
            except TemplateResolutionError as exc:
                state.node_instances[logical_id] = instances
                return _dynamic_failure(
                    context,
                    state,
                    error=str(exc),
                    error_code=exc.error_code,
                    node_id=instance_id,
                    path=condition_input["ref"],
                    logical_id=logical_id,
                )
            if not should_run:
                instances.append({
                    "logical_id": logical_id,
                    "index": index,
                    "instance_id": instance_id,
                    "state": "skipped",
                    "result": None,
                })
                continue
        try:
            resolved = _resolve_dynamic_args(
                task,
                state.results,
                item=element,
                index=index,
            )
        except TemplateResolutionError as exc:
            state.node_instances[logical_id] = instances
            return _dynamic_failure(
                context,
                state,
                error=str(exc),
                error_code=exc.error_code,
                node_id=instance_id,
                path=None,
                logical_id=logical_id,
            )
        instances.append({
            "logical_id": logical_id,
            "index": index,
            "instance_id": instance_id,
            "state": "pending",
            "result": None,
            "resolved": resolved,
        })

    state.node_instances[logical_id] = instances
    if not any(instance["state"] == "pending" for instance in instances):
        _aggregate_dynamic_node(state, logical_id)
    _publish_dynamic_status(context, state)
    return None


def _materialize_normal_node(
    context: df.DurableOrchestrationContext,
    state: _DynamicWorkflowState,
    logical_id: str,
    task: WorkflowTaskInput,
) -> dict[str, Any] | None:
    condition_input = task.get("when")
    if condition_input is not None:
        try:
            should_run = evaluate_condition(
                WorkflowCondition.model_validate(condition_input),
                state.results,
            )
        except TemplateResolutionError as exc:
            return _dynamic_failure(
                context,
                state,
                error=str(exc),
                error_code=exc.error_code,
                node_id=logical_id,
                path=condition_input["ref"],
                logical_id=logical_id,
            )
        if not should_run:
            state.results[logical_id] = None
            state.logical_state[logical_id] = "skipped"
            state.node_instances[logical_id] = [{
                "logical_id": logical_id,
                "index": None,
                "instance_id": logical_id,
                "state": "skipped",
                "result": None,
            }]
            return None

    resolved: Any = None
    if task["type"] in {TOOL_TASK_TYPE, SUB_AGENT_TASK_TYPE}:
        try:
            resolved = _resolve_dynamic_args(task, state.results)
        except TemplateResolutionError as exc:
            return _dynamic_failure(
                context,
                state,
                error=str(exc),
                error_code=exc.error_code,
                node_id=logical_id,
                path=None,
                logical_id=logical_id,
            )
    state.node_instances[logical_id] = [{
        "logical_id": logical_id,
        "index": None,
        "instance_id": logical_id,
        "state": "pending",
        "result": None,
        "resolved": resolved,
    }]
    return None


def _materialize_ready_nodes(
    context: df.DurableOrchestrationContext,
    state: _DynamicWorkflowState,
) -> dict[str, Any] | None:
    progressed = True
    while progressed:
        progressed = False
        pending = sorted(
            task_id
            for task_id in state.by_id
            if state.logical_state[task_id] == "pending"
            and task_id not in state.node_instances
        )
        for logical_id in pending:
            if state.deps[logical_id] - state.results.keys():
                continue
            task = state.by_id[logical_id]
            if task.get("for_each") is not None:
                failure = _materialize_for_each_node(
                    context,
                    state,
                    logical_id,
                    task,
                )
            else:
                failure = _materialize_normal_node(
                    context,
                    state,
                    logical_id,
                    task,
                )
            if failure is not None:
                return failure
            progressed = True

        aggregatable = sorted(
            task_id
            for task_id, task in state.by_id.items()
            if task.get("for_each") is not None
            and state.logical_state[task_id] in {"expanded", "running"}
        )
        for logical_id in aggregatable:
            instances = state.node_instances.get(logical_id, [])
            if instances and all(
                instance["state"] in {"completed", "skipped"}
                for instance in instances
            ):
                _aggregate_dynamic_node(state, logical_id)
                progressed = True
    return None


def _dynamic_workflow_complete(state: _DynamicWorkflowState) -> bool:
    return all(
        node_state in {"completed", "skipped", "aggregated"}
        for node_state in state.logical_state.values()
    )


def _collect_runnable_instances(
    state: _DynamicWorkflowState,
) -> list[_MaterializedInstance]:
    runnable = [
        instance
        for instances in state.node_instances.values()
        for instance in instances
        if instance["state"] == "pending"
    ]
    runnable.sort(
        key=lambda instance: (
            instance["logical_id"],
            instance["index"] if instance["index"] is not None else -1,
        )
    )
    return runnable[:MAX_PARALLELISM]


def _dispatch_dynamic_wave(
    context: df.DurableOrchestrationContext,
    state: _DynamicWorkflowState,
    wave: list[_MaterializedInstance],
) -> list[Any]:
    wave_tasks: list[Any] = []
    for instance in wave:
        logical_id = instance["logical_id"]
        task = state.by_id[logical_id]
        if task["type"] == TOOL_TASK_TYPE:
            if task["tool"] not in state.allowed_tools:
                raise RuntimeError(
                    f"task {instance['instance_id']!r}: tool {task['tool']!r} is "
                    "outside the persisted workflow owner policy"
                )
            wave_tasks.append(
                _call_task_activity(
                    context,
                    _ACTIVITY_NAME,
                    {
                        "id": instance["instance_id"],
                        "tool": task["tool"],
                        "args": instance["resolved"],
                        "workflow_agent_slug": state.workflow_agent_slug,
                        "workflow_id": context.instance_id,
                        **_policy_activity_fields(task, logical_id=logical_id),
                    },
                )
            )
            instance["kind"] = "activity"
        elif task["type"] == SUB_AGENT_TASK_TYPE:
            if task["agent"] not in state.allowed_subagents:
                raise RuntimeError(
                    f"task {instance['instance_id']!r}: Sub Agent "
                    f"{task['agent']!r} is outside the persisted workflow owner policy"
                )
            wave_tasks.append(
                _call_task_activity(
                    context,
                    SUB_AGENT_ACTIVITY_NAME,
                    {
                        "id": instance["instance_id"],
                        "agent": task["agent"],
                        "task": instance["resolved"],
                        "workflow_id": context.instance_id,
                        "workflow_agent_slug": state.workflow_agent_slug,
                        **_policy_activity_fields(task, logical_id=logical_id),
                    },
                )
            )
            instance["kind"] = "activity"
        elif task["type"] == WAIT_TASK_TYPE:
            deadline = _wait_deadline(context, task)
            instance["deadline"] = deadline.isoformat()
            wave_tasks.append(context.create_timer(deadline))
            instance["kind"] = "timer"
        else:
            raise RuntimeError(
                f"task {instance['instance_id']!r}: unsupported task type "
                f"{task['type']!r}"
            )
        instance["state"] = "running"
        state.logical_state[logical_id] = "running"
    return wave_tasks


def _cancel_dynamic_wave_timers(
    wave: list[_MaterializedInstance],
    wave_tasks: list[Any],
) -> None:
    for instance, task in zip(wave, wave_tasks, strict=True):
        if instance.get("kind") == "timer" and not task.is_completed:
            task.cancel()


def _restore_canceled_dynamic_wave(
    state: _DynamicWorkflowState,
    wave: list[_MaterializedInstance],
    wave_tasks: list[Any],
) -> None:
    _cancel_dynamic_wave_timers(wave, wave_tasks)
    for instance in wave:
        instance["state"] = "pending"
    for logical_id in {instance["logical_id"] for instance in wave}:
        state.logical_state[logical_id] = (
            "expanded"
            if state.by_id[logical_id].get("for_each") is not None
            else "pending"
        )


def _mark_dynamic_wave_failure(
    state: _DynamicWorkflowState,
    wave: list[_MaterializedInstance],
    wave_results: list[Any],
) -> None:
    """Record the instance whose failure ends the workflow.

    ``_await_wave`` reports each node's own outcome in wave order, so the
    failing instance is identified positionally — it does not depend on the
    failure carrying a sanitized, decodable cause, and an opaque Durable failure
    is attributed just as precisely as a classified one.
    """
    for instance, outcome in zip(wave, wave_results, strict=True):
        if isinstance(outcome, BaseException):
            instance["state"] = "failed"
            state.logical_state[instance["logical_id"]] = "failed"
            return


def _apply_dynamic_wave_results(
    state: _DynamicWorkflowState,
    wave: list[_MaterializedInstance],
    wave_results: list[Any],
) -> None:
    for instance, raw in zip(wave, wave_results, strict=True):
        if instance.get("kind") == "timer":
            instance["result"] = {"waited_until": instance["deadline"]}
        else:
            logical_id = instance["logical_id"]
            task = state.by_id[logical_id]
            try:
                outcome = _resolve_task_outcome(
                    instance["instance_id"],
                    policy_aware=_persisted_execution(task) is not None,
                    continue_on_error=_continue_on_error(task),
                    raw=raw,
                )
            except BaseException:
                # The node that ends the workflow is recorded before the failure
                # propagates, so the last published status names it instead of
                # freezing on the pre-failure snapshot.
                instance["state"] = "failed"
                state.logical_state[logical_id] = "failed"
                raise
            instance["result"] = outcome.result
            if outcome.continued_failure is not None:
                instance["continued_failure"] = outcome.continued_failure
        instance["state"] = "completed"
        if instance["index"] is None:
            logical_id = instance["logical_id"]
            state.results[logical_id] = instance["result"]
            state.logical_state[logical_id] = "completed"


def _run_dynamic_workflow(
    context: df.DurableOrchestrationContext,
    payload: WorkflowPayload,
    tasks: list[WorkflowTaskInput],
) -> Any:
    """Execute a data-driven plan with deterministic phase helpers."""
    state = _new_dynamic_workflow_state(payload, tasks)
    cancel_task = context.wait_for_external_event(CANCEL_EVENT_NAME)

    while True:
        failure = _materialize_ready_nodes(context, state)
        if failure is not None:
            return failure
        if _dynamic_workflow_complete(state):
            break

        wave = _collect_runnable_instances(state)
        if not wave:
            active_count = sum(
                1
                for node_state in state.logical_state.values()
                if node_state not in {"completed", "skipped", "aggregated"}
            )
            raise RuntimeError(
                "workflow stalled: no runnable instances but "
                f"{active_count} logical node(s) are not terminal. This indicates "
                "a scheduler invariant violation."
            )

        wave_tasks = _dispatch_dynamic_wave(context, state, wave)
        _publish_dynamic_status(context, state)
        wave_results = yield from _await_wave(context, cancel_task, wave_tasks)
        if wave_results is None:
            reason = cancel_task.result
            _restore_canceled_dynamic_wave(state, wave, wave_tasks)
            _publish_dynamic_status(context, state)
            logger.info(
                "workflow canceled: instance=%s workflow_agent=%s reason=%r",
                context.instance_id,
                state.workflow_agent_slug,
                reason,
            )
            return {
                "results": state.results,
                "canceled": True,
                "reason": reason,
                "completed_count": len(state.results),
                "total_count": len(state.by_id),
            }

        wave_failure = _first_wave_failure(wave_results)
        if wave_failure is not None:
            # See _run_static_workflow: per-node outcomes are already available.
            continuable_wave = any(
                _continue_on_error(state.by_id[instance["logical_id"]])
                for instance in wave
            )
            _cancel_dynamic_wave_timers(wave, wave_tasks)
            if not continuable_wave:
                error = _decode_wave_failure(
                    [
                        instance["instance_id"]
                        for instance in wave
                        if _persisted_execution(state.by_id[instance["logical_id"]])
                        is not None
                    ],
                    wave_failure,
                )
                _mark_dynamic_wave_failure(state, wave, wave_results)
                _publish_dynamic_status(context, state)
                raise error
        try:
            _apply_dynamic_wave_results(state, wave, wave_results)
        except BaseException:
            _cancel_dynamic_wave_timers(wave, wave_tasks)
            _publish_dynamic_status(context, state)
            raise
        _publish_dynamic_status(context, state)

    _publish_dynamic_status(context, state)
    return {"results": state.results}


def register_workflows(
    app: func.FunctionApp,
    *,
    catalog: AgentCatalog | None = None,
    handler_catalog: registry.WorkflowHandlerCatalog | None = None,
    workflow_agent_policies: Mapping[str, WorkflowPlanPolicy] | None = None,
) -> None:
    """Register the workflow orchestrator + activities on ``app``.

    Expected to be invoked exactly once during app construction.
    Registering twice would double-register Durable bindings and fail
    at worker index time.
    """
    bp = df.Blueprint()

    def require_workflow_agent_policy(
        task: _ActivityInput,
    ) -> tuple[str, WorkflowPlanPolicy]:
        workflow_agent_slug = task["workflow_agent_slug"]
        policy = (
            workflow_agent_policies.get(workflow_agent_slug)
            if workflow_agent_policies is not None
            else None
        )
        if not workflow_agent_slug or policy is None:
            logger.error(
                "workflow activity agent policy miss: "
                "workflow_id=%s node_id=%s workflow_agent=%s",
                task["workflow_id"],
                task["id"],
                workflow_agent_slug or "<missing>",
            )
            raise RuntimeError(
                f"task {task['id']!r}: workflow agent policy is not available"
            )
        return workflow_agent_slug, policy

    @bp.activity_trigger(input_name="task")
    async def agents_workflow_run_tool(task: _ToolActivityInput) -> dict[str, Any]:
        policy_aware = "execution" in task

        def early(outcome: Any) -> dict[str, Any]:
            """Record the span for a failure that never reaches the handler."""
            return dict(
                early_policy_outcome_with_telemetry(
                    task,
                    target_type="tool",
                    target_name=task.get("tool"),
                    outcome=outcome,
                )
            )

        if policy_aware:
            invalid = validate_policy_activity_input(task, target_type="tool")
            if invalid is not None:
                return early(invalid)
        task_id = task["id"]
        tool_name = task["tool"]
        args = task["args"]
        try:
            workflow_agent_slug, policy = require_workflow_agent_policy(task)
        except RuntimeError:
            if policy_aware:
                return early(authorization_outcome(task_id))
            raise
        workflow_id = task["workflow_id"]
        if tool_name not in policy.allowed_tools:
            logger.error(
                "workflow tool authorization denied: "
                "workflow_id=%s node_id=%s workflow_agent=%s tool=%s",
                workflow_id,
                task_id,
                workflow_agent_slug,
                tool_name,
            )
            if policy_aware:
                return early(authorization_outcome(task_id))
            raise RuntimeError(
                f"task {task_id!r}: workflow tool {tool_name!r} is not authorized"
            )
        entry = (
            handler_catalog.get(tool_name)
            if handler_catalog is not None
            else registry.get_entry(tool_name)
        )
        if entry is None:
            if policy_aware:
                return early(handler_contract_outcome(task_id))
            raise ValueError(
                f"task {task_id!r}: tool {tool_name!r} is not registered "
                "in the workflow-safe tool registry"
            )
        logger.info(
            "workflow activity running: "
            "workflow_id=%s workflow_agent=%s id=%s tool=%s",
            workflow_id,
            workflow_agent_slug,
            task_id,
            tool_name,
        )
        if policy_aware:
            return dict(
                await invoke_policy_handler(
                    entry.handler,
                    args,
                    task=task,
                    target=tool_name,
                )
            )
        try:
            result = await invoke_handler(entry.handler, args)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "workflow activity failed: "
                "workflow_id=%s workflow_agent=%s id=%s tool=%s",
                workflow_id,
                workflow_agent_slug,
                task_id,
                tool_name,
            )
            raise RuntimeError(
                f"task {task_id!r}: workflow-safe tool failed"
            ) from None
        # Determinism contract: activity results must be JSON-serializable
        # (Durable persists them via its own JSON pipeline; this is a fast
        # local guard so a non-serializable result fails inside the activity
        # with a clearer message instead of deeper in the runtime).
        json.dumps(result)
        return {"id": task_id, "result": result}

    @bp.activity_trigger(input_name="task")
    async def agents_workflow_run_sub_agent(
        task: _SubAgentActivityInput,
    ) -> dict[str, Any]:
        policy_aware = "execution" in task

        def early(outcome: Any) -> dict[str, Any]:
            """Record the span for a failure that never reaches the Sub Agent."""
            return dict(
                early_policy_outcome_with_telemetry(
                    task,
                    target_type="sub_agent",
                    target_name=task.get("agent"),
                    outcome=outcome,
                )
            )

        if policy_aware:
            invalid = validate_policy_activity_input(task, target_type="sub_agent")
            if invalid is not None:
                return early(invalid)
        task_id = task["id"]
        agent_slug = task["agent"]
        workflow_id = task["workflow_id"]
        try:
            workflow_agent_slug, policy = require_workflow_agent_policy(task)
        except RuntimeError:
            if policy_aware:
                return early(authorization_outcome(task_id))
            raise
        if agent_slug not in policy.allowed_subagents:
            logger.error(
                "workflow sub-agent authorization denied: "
                "workflow_id=%s node_id=%s workflow_agent=%s agent=%s",
                workflow_id,
                task_id,
                workflow_agent_slug,
                agent_slug,
            )
            if policy_aware:
                return early(authorization_outcome(task_id))
            raise RuntimeError(
                f"task {task_id!r}: Workflow Sub Agent {agent_slug!r} is not authorized"
            )
        if catalog is None or agent_slug not in catalog:
            logger.error(
                "workflow sub-agent catalog miss: "
                "workflow_id=%s node_id=%s workflow_agent=%s agent=%s",
                workflow_id,
                task_id,
                workflow_agent_slug,
                agent_slug,
            )
            if policy_aware:
                return early(authorization_outcome(task_id))
            raise RuntimeError(
                f"task {task_id!r}: Workflow Sub Agent {agent_slug!r} is not available"
            )

        entry = catalog[agent_slug]
        logger.info(
            "workflow sub-agent activity running: "
            "workflow_id=%s node_id=%s workflow_agent=%s agent=%s",
            workflow_id,
            task_id,
            workflow_agent_slug,
            agent_slug,
        )
        if policy_aware:

            async def run_policy_sub_agent(_: dict[str, Any]) -> dict[str, Any]:
                try:
                    text = await run_leaf_agent_task(
                        entry.resolved,
                        entry.capabilities,
                        task["task"],
                        timeout=entry.resolved.timeout,
                        execution_role="workflow_subagent",
                    )
                except TimeoutError:
                    # The agent's own resolved timeout can be tighter than the
                    # attempt deadline; keep both on the same classification so
                    # either one is retried under the frozen policy.
                    raise WorkflowTaskTimeoutError from None
                return {"agent": agent_slug, "text": text}

            return dict(
                await invoke_policy_handler(
                    run_policy_sub_agent,
                    {},
                    task=task,
                    target=agent_slug,
                    target_type="sub_agent",
                )
            )
        try:
            text = await run_leaf_agent_task(
                entry.resolved,
                entry.capabilities,
                task["task"],
                timeout=entry.resolved.timeout,
                execution_role="workflow_subagent",
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            logger.exception(
                "workflow sub-agent activity timed out: workflow_id=%s node_id=%s agent=%s",
                workflow_id,
                task_id,
                agent_slug,
            )
            raise RuntimeError(
                f"task {task_id!r}: Workflow Sub Agent {agent_slug!r} timed out"
            ) from None
        except Exception:
            logger.exception(
                "workflow sub-agent activity failed: workflow_id=%s node_id=%s agent=%s",
                workflow_id,
                task_id,
                agent_slug,
            )
            raise RuntimeError(
                f"task {task_id!r}: Workflow Sub Agent {agent_slug!r} failed "
                "(error_code=workflow_subagent_execution_failed)"
            ) from None

        result = {
            "id": task_id,
            "result": {
                "agent": agent_slug,
                "text": text,
            },
        }
        json.dumps(result)
        return result

    @bp.orchestration_trigger(context_name="context")  # type: ignore[arg-type]
    def agents_workflow_orchestrator(context: df.DurableOrchestrationContext) -> Any:
        """Execute a workflow plan, selecting the static or dynamic scheduler.

        A plan is *static* when no task carries a ``when`` predicate or a
        ``for_each`` expansion; it runs through :func:`_run_static_workflow`
        with its exact pre-#1276 wave scheduling and string ``custom_status``
        behavior. Any ``when`` / ``for_each`` selects
        :func:`_run_dynamic_workflow`, which materializes instances, aggregates
        results, and publishes structured status (``schema_version`` 2, or 3
        when the plan froze a task execution policy).

        Determinism contract (both paths):
        - Ready/runnable sets ordered deterministically before each wave.
        - Templates resolved against the JSON-normalized ``results`` dict.
        - Time read only via ``context.current_utc_datetime``.
        - No I/O outside ``call_activity`` / ``create_timer`` /
          ``wait_for_external_event``.
        """
        raw_payload: Any = context.get_input()
        if raw_payload is None:
            payload: WorkflowPayload = {
                "tasks": [],
                "workflow_agent_slug": "",
            }
        else:
            payload = raw_payload
        tasks = list(payload.get("tasks", []))
        if _plan_is_dynamic(tasks):
            return (yield from _run_dynamic_workflow(context, payload, tasks))
        return (yield from _run_static_workflow(context, payload, tasks))

    app.register_blueprint(bp)


__all__ = [
    "CANCEL_EVENT_NAME",
    "ORCHESTRATOR_NAME",
    "SUB_AGENT_ACTIVITY_NAME",
    "WORKFLOW_SAFE_ECHO_TOOL",
    "register_workflows",
]
