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

What is intentionally still *not* here: retries / per-task timeouts and
a per-agent workflow-safe tool registry (M3).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import azure.durable_functions as df
import azure.functions as func

from azure_functions_agents._logger import logger
from azure_functions_agents.registration.catalog import AgentCatalog
from azure_functions_agents.runner import run_leaf_agent_task

from . import registry
from .schema import (
    ECHO_TOOL_NAME,
    MAX_NODES,
    MAX_PARALLELISM,
    MAX_WAIT_DURATION,
    SUB_AGENT_TASK_TYPE,
    TOOL_TASK_TYPE,
    WAIT_TASK_TYPE,
    TemplateResolutionError,
    WorkflowCondition,
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


def _wait_deadline(context: df.DurableOrchestrationContext, task: dict[str, Any]) -> Any:
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


def _plan_is_dynamic(tasks: list[dict[str, Any]]) -> bool:
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


def _run_static_workflow(
    context: df.DurableOrchestrationContext,
    payload: dict[str, Any],
    tasks: list[dict[str, Any]],
) -> Any:
    """Execute a static-DAG plan in deterministic waves (pre-#1276 behavior).

    Input: ``{"tasks": [{"id", "type", "tool"?, "args"?, "duration"?,
    "until"?, "depends_on"}, ...]}``.

    Return on success: ``{"results": {task_id: result, ...}}``.
    Return on cooperative cancel: ``{"results": ..., "canceled": True,
    "reason": <event payload>, "completed_count": N, "total_count": M}``.
    """
    by_id: dict[str, dict[str, Any]] = {t["id"]: t for t in tasks}
    deps: dict[str, set[str]] = {
        t["id"]: set(t.get("depends_on") or []) for t in tasks
    }
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
            ttype = task.get("type") or TOOL_TASK_TYPE
            if ttype == TOOL_TASK_TYPE:
                try:
                    resolved_args = resolve_template_value(
                        task.get("args") or {}, results
                    )
                except TemplateResolutionError as exc:
                    raise RuntimeError(
                        f"task {tid!r}: template resolution failed: {exc}"
                    ) from exc
                wave_tasks.append(
                    context.call_activity(
                        _ACTIVITY_NAME,
                        {
                            "id": tid,
                            "tool": task["tool"],
                            "args": resolved_args,
                        },
                    )
                )
                wave_specs.append({"id": tid, "type": TOOL_TASK_TYPE})
            elif ttype == SUB_AGENT_TASK_TYPE:
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
                    context.call_activity(
                        SUB_AGENT_ACTIVITY_NAME,
                        {
                            "id": tid,
                            "agent": task["agent"],
                            "task": resolved_task,
                            "workflow_id": context.instance_id,
                        },
                    )
                )
                wave_specs.append({"id": tid, "type": SUB_AGENT_TASK_TYPE})
            elif ttype == WAIT_TASK_TYPE:
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
                    f"task {tid!r}: unsupported task type {ttype!r}"
                )

        context.set_custom_status(
            f"{len(results)}/{total} tasks done, running={','.join(wave)}"
        )
        wave_task = context.task_all(wave_tasks)
        winner = yield context.task_any([cancel_task, wave_task])
        if winner is cancel_task:
            reason = cancel_task.result
            for spec, t in zip(wave_specs, wave_tasks, strict=True):
                if spec["type"] == WAIT_TASK_TYPE and not t.is_completed:
                    t.cancel()
            context.set_custom_status(
                f"canceled at {len(results)}/{total} tasks done"
            )
            logger.info(
                "workflow canceled: instance=%s reason=%r",
                context.instance_id,
                reason,
            )
            return {
                "results": results,
                "canceled": True,
                "reason": reason,
                "completed_count": len(results),
                "total_count": total,
            }

        wave_results = wave_task.result
        for spec, raw in zip(wave_specs, wave_results, strict=True):
            tid = spec["id"]
            if spec["type"] in {TOOL_TASK_TYPE, SUB_AGENT_TASK_TYPE}:
                results[tid] = raw["result"]
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


def _materialized_total(node_instances: dict[str, list[dict[str, Any]]]) -> int:
    return sum(len(insts) for insts in node_instances.values())


def _dynamic_status(
    by_id: dict[str, dict[str, Any]],
    logical_state: dict[str, str],
    node_instances: dict[str, list[dict[str, Any]]],
    expanded_count: dict[str, int],
) -> dict[str, Any]:
    """Build the versioned (schema_version=2) structured ``custom_status`` object.

    ``counts`` are instance-level for completed/skipped/running and node-level
    for ``logical_total``; ``materialized_total`` counts every materialized
    instance (including skipped ones). ``nodes`` renders logical node state,
    plus per-instance state for expanded ``for_each`` nodes.
    """
    completed = skipped = running = 0
    for insts in node_instances.values():
        for inst in insts:
            state = inst["state"]
            if state == "completed":
                completed += 1
            elif state == "skipped":
                skipped += 1
            elif state == "running":
                running += 1

    nodes: dict[str, Any] = {}
    for lid, task in by_id.items():
        node: dict[str, Any] = {"state": logical_state[lid]}
        if task.get("for_each") is not None and lid in expanded_count:
            node["expanded_count"] = expanded_count[lid]
            node["instances"] = {
                inst["instance_id"]: {"state": inst["state"]}
                for inst in node_instances.get(lid, [])
            }
        nodes[lid] = node

    return {
        "schema_version": 2,
        "counts": {
            "logical_total": len(by_id),
            "materialized_total": _materialized_total(node_instances),
            "completed": completed,
            "skipped": skipped,
            "running": running,
        },
        "nodes": nodes,
    }


_UNBOUND = object()


def _resolve_dynamic_args(
    task: dict[str, Any],
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
    ttype = task.get("type") or TOOL_TASK_TYPE
    if ttype == TOOL_TASK_TYPE:
        return resolve_template_value(task.get("args") or {}, results, **kwargs)
    resolved_task = resolve_template_value(task["task"], results, **kwargs)
    if not isinstance(resolved_task, str):
        raise TemplateResolutionError(
            f"resolved Sub Agent task must be a string, got "
            f"{type(resolved_task).__name__}"
        )
    return resolved_task


def _run_dynamic_workflow(
    context: df.DurableOrchestrationContext,
    payload: dict[str, Any],
    tasks: list[dict[str, Any]],
) -> Any:
    """Execute a data-driven plan with ``when`` / ``for_each`` control flow.

    Operates on the logical DAG, materializing ``for_each`` instances
    deterministically by ``(logical_id, numeric index)``. Controlled runtime
    failures are *returned* as a flat ``{failed: true, ...}`` envelope (mapped
    to ``Failed`` by the status adapter); unexpected invariants and policy
    violations still raise natively.
    """
    by_id: dict[str, dict[str, Any]] = {t["id"]: t for t in tasks}
    deps: dict[str, set[str]] = {
        t["id"]: set(t.get("depends_on") or []) for t in tasks
    }

    policy_input = payload.get("policy") or {}
    allowed_tools = frozenset(policy_input.get("allowed_tools") or [])
    allowed_subagents = frozenset(policy_input.get("allowed_subagents") or [])

    results: dict[str, Any] = {}
    logical_state: dict[str, str] = {tid: "pending" for tid in by_id}
    # ``node_instances[lid]`` holds every materialized instance for a node in
    # source order. Normal nodes have exactly one instance whose id is the
    # logical id and whose index is None; for_each nodes have one per element.
    node_instances: dict[str, list[dict[str, Any]]] = {}
    expanded_count: dict[str, int] = {}

    # Node budget: reserve one node per non-for_each logical task up front.
    budget_used = sum(1 for t in tasks if t.get("for_each") is None)

    def publish() -> None:
        context.set_custom_status(
            _dynamic_status(by_id, logical_state, node_instances, expanded_count)
        )

    def fail(
        *,
        error: str,
        error_code: str,
        node_id: str,
        path: str | None,
        lid: str,
    ) -> dict[str, Any]:
        logical_state[lid] = "failed"
        publish()
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
            results=results,
        )

    def deps_ready(lid: str) -> bool:
        return not (deps[lid] - results.keys())

    def aggregate(lid: str) -> None:
        insts = sorted(node_instances[lid], key=lambda i: i["index"])
        results[lid] = [
            {"index": i["index"], "status": i["state"], "result": i["result"]}
            for i in insts
        ]
        logical_state[lid] = "aggregated"

    cancel_task = context.wait_for_external_event(CANCEL_EVENT_NAME)

    while True:
        # --- Phase A: resolve all progress that needs no Activity (skips,
        # expansions, aggregations) to a fixpoint. Deterministic: pending
        # nodes are visited in sorted logical-id order so budget accounting
        # and expansion order do not depend on dict iteration order.
        progressed = True
        while progressed:
            progressed = False
            for lid in sorted(t for t in by_id if logical_state[t] == "pending"):
                if not deps_ready(lid):
                    continue
                task = by_id[lid]

                if task.get("for_each") is not None:
                    # Resolve the full upstream array reference.
                    ref = task["for_each"]
                    try:
                        collection = resolve_template_value(ref, results)
                    except TemplateResolutionError as exc:
                        return fail(
                            error=str(exc),
                            error_code=exc.error_code,
                            node_id=lid,
                            path=ref,
                            lid=lid,
                        )
                    if not isinstance(collection, list):
                        return fail(
                            error=(
                                f"task {lid!r}: for_each did not resolve to an "
                                f"array (got {type(collection).__name__})"
                            ),
                            error_code="workflow_iteration_not_array",
                            node_id=lid,
                            path=ref,
                            lid=lid,
                        )
                    count = len(collection)
                    if budget_used + count > MAX_NODES:
                        return fail(
                            error=(
                                f"task {lid!r}: expanding for_each over "
                                f"{count} element(s) would exceed the "
                                f"materialized-node limit of {MAX_NODES}"
                            ),
                            error_code="workflow_node_limit_exceeded",
                            node_id=lid,
                            path=ref,
                            lid=lid,
                        )
                    budget_used += count
                    expanded_count[lid] = count
                    logical_state[lid] = "expanded"
                    instances: list[dict[str, Any]] = []
                    when = task.get("when")
                    for index, element in enumerate(collection):
                        instance_id = f"{lid}[{index}]"
                        if when is not None:
                            try:
                                run = evaluate_condition(
                                    WorkflowCondition.model_validate(when),
                                    results,
                                    item=element,
                                    index=index,
                                )
                            except TemplateResolutionError as exc:
                                node_instances[lid] = instances
                                return fail(
                                    error=str(exc),
                                    error_code=exc.error_code,
                                    node_id=instance_id,
                                    path=when["ref"],
                                    lid=lid,
                                )
                            if not run:
                                instances.append({
                                    "logical_id": lid,
                                    "index": index,
                                    "instance_id": instance_id,
                                    "state": "skipped",
                                    "result": None,
                                })
                                continue
                        try:
                            resolved = _resolve_dynamic_args(
                                task, results, item=element, index=index
                            )
                        except TemplateResolutionError as exc:
                            node_instances[lid] = instances
                            return fail(
                                error=str(exc),
                                error_code=exc.error_code,
                                node_id=instance_id,
                                path=None,
                                lid=lid,
                            )
                        instances.append({
                            "logical_id": lid,
                            "index": index,
                            "instance_id": instance_id,
                            "state": "pending",
                            "result": None,
                            "resolved": resolved,
                        })
                    node_instances[lid] = instances
                    # Empty expansion, or every element skipped, aggregates now.
                    if not any(i["state"] == "pending" for i in instances):
                        aggregate(lid)
                    # Publish a meaningful snapshot at expansion time so the
                    # transient ``expanded`` state (and per-instance skips) are
                    # observable before Phase B flips runnable instances to
                    # ``running``.
                    publish()
                    progressed = True
                    continue

                # Normal (non-for_each) node.
                when = task.get("when")
                if when is not None:
                    try:
                        run = evaluate_condition(
                            WorkflowCondition.model_validate(when), results
                        )
                    except TemplateResolutionError as exc:
                        return fail(
                            error=str(exc),
                            error_code=exc.error_code,
                            node_id=lid,
                            path=when["ref"],
                            lid=lid,
                        )
                    if not run:
                        results[lid] = None
                        logical_state[lid] = "skipped"
                        node_instances[lid] = [{
                            "logical_id": lid,
                            "index": None,
                            "instance_id": lid,
                            "state": "skipped",
                            "result": None,
                        }]
                        progressed = True
                        continue

                ttype = task.get("type") or TOOL_TASK_TYPE
                resolved_value: Any = None
                if ttype in {TOOL_TASK_TYPE, SUB_AGENT_TASK_TYPE}:
                    try:
                        resolved_value = _resolve_dynamic_args(task, results)
                    except TemplateResolutionError as exc:
                        return fail(
                            error=str(exc),
                            error_code=exc.error_code,
                            node_id=lid,
                            path=None,
                            lid=lid,
                        )
                node_instances[lid] = [{
                    "logical_id": lid,
                    "index": None,
                    "instance_id": lid,
                    "state": "pending",
                    "result": None,
                    "resolved": resolved_value,
                }]
                logical_state[lid] = "running"
                progressed = True

            # Aggregate for_each nodes whose instances are all terminal.
            for lid in sorted(
                t
                for t in by_id
                if by_id[t].get("for_each") is not None
                and logical_state[t] in {"expanded", "running"}
            ):
                insts = node_instances.get(lid, [])
                if insts and all(
                    i["state"] in {"completed", "skipped"} for i in insts
                ):
                    aggregate(lid)
                    progressed = True

        if all(
            logical_state[t] in {"completed", "skipped", "aggregated"}
            for t in by_id
        ):
            break

        # --- Phase B: gather runnable instances across every logical node and
        # schedule up to MAX_PARALLELISM. Ordering key is the numeric
        # (logical_id, index) tuple, never the rendered instance-id string
        # (so analyze[10] runs after analyze[2]).
        runnable: list[dict[str, Any]] = []
        for insts in node_instances.values():
            for inst in insts:
                if inst["state"] == "pending":
                    runnable.append(inst)
        if not runnable:
            raise RuntimeError(
                "workflow stalled: no runnable instances but "
                f"{sum(1 for s in logical_state.values() if s not in {'completed', 'skipped', 'aggregated'})}"
                " logical node(s) are not terminal. This indicates a "
                "scheduler invariant violation."
            )
        runnable.sort(
            key=lambda inst: (
                inst["logical_id"],
                inst["index"] if inst["index"] is not None else -1,
            )
        )
        wave = runnable[:MAX_PARALLELISM]

        wave_tasks: list[Any] = []
        wave_specs: list[dict[str, Any]] = []
        for inst in wave:
            lid = inst["logical_id"]
            task = by_id[lid]
            ttype = task.get("type") or TOOL_TASK_TYPE
            if ttype == TOOL_TASK_TYPE:
                if task["tool"] not in allowed_tools:
                    raise RuntimeError(
                        f"task {inst['instance_id']!r}: tool {task['tool']!r} is "
                        "outside the persisted workflow owner policy"
                    )
                wave_tasks.append(
                    context.call_activity(
                        _ACTIVITY_NAME,
                        {
                            "id": inst["instance_id"],
                            "tool": task["tool"],
                            "args": inst["resolved"],
                        },
                    )
                )
                inst["kind"] = "activity"
            elif ttype == SUB_AGENT_TASK_TYPE:
                if task["agent"] not in allowed_subagents:
                    raise RuntimeError(
                        f"task {inst['instance_id']!r}: Sub Agent "
                        f"{task['agent']!r} is outside the persisted workflow "
                        "owner policy"
                    )
                wave_tasks.append(
                    context.call_activity(
                        SUB_AGENT_ACTIVITY_NAME,
                        {
                            "id": inst["instance_id"],
                            "agent": task["agent"],
                            "task": inst["resolved"],
                            "workflow_id": context.instance_id,
                        },
                    )
                )
                inst["kind"] = "activity"
            elif ttype == WAIT_TASK_TYPE:
                deadline = _wait_deadline(context, task)
                inst["deadline"] = deadline.isoformat()
                wave_tasks.append(context.create_timer(deadline))
                inst["kind"] = "timer"
            else:
                raise RuntimeError(
                    f"task {inst['instance_id']!r}: unsupported task type {ttype!r}"
                )
            inst["state"] = "running"
            if logical_state[lid] == "expanded":
                logical_state[lid] = "running"
            wave_specs.append(inst)

        publish()
        wave_task = context.task_all(wave_tasks)
        winner = yield context.task_any([cancel_task, wave_task])
        if winner is cancel_task:
            reason = cancel_task.result
            for inst, t in zip(wave_specs, wave_tasks, strict=True):
                if inst.get("kind") == "timer" and not t.is_completed:
                    t.cancel()
                inst["state"] = "pending"
            for lid in list(logical_state):
                if logical_state[lid] == "running" and by_id[lid].get(
                    "for_each"
                ) is not None:
                    logical_state[lid] = "expanded"
            publish()
            logger.info(
                "workflow canceled: instance=%s reason=%r",
                context.instance_id,
                reason,
            )
            return {
                "results": results,
                "canceled": True,
                "reason": reason,
                "completed_count": len(results),
                "total_count": len(by_id),
            }

        wave_results = wave_task.result
        for inst, raw in zip(wave_specs, wave_results, strict=True):
            if inst.get("kind") == "timer":
                inst["result"] = {"waited_until": inst["deadline"]}
            else:
                inst["result"] = raw["result"]
            inst["state"] = "completed"
            if inst["index"] is None:
                lid = inst["logical_id"]
                results[lid] = inst["result"]
                logical_state[lid] = "completed"

        publish()

    publish()
    return {"results": results}


def register_workflows(
    app: func.FunctionApp,
    *,
    catalog: AgentCatalog | None = None,
) -> None:
    """Register the workflow orchestrator + activities on ``app``.

    Expected to be invoked exactly once during app construction.
    Registering twice would double-register Durable bindings and fail
    at worker index time.
    """
    bp = df.Blueprint()

    @bp.activity_trigger(input_name="task")  # type: ignore[untyped-decorator]
    def agents_workflow_run_tool(task) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        task_id = task["id"]
        tool_name = task["tool"]
        args = task.get("args") or {}
        handler = registry.get_handler(tool_name)
        if handler is None:
            raise ValueError(
                f"task {task_id!r}: tool {tool_name!r} is not registered "
                "in the workflow-safe tool registry"
            )
        logger.info("workflow activity running: id=%s tool=%s", task_id, tool_name)
        try:
            result = handler(args)
        except Exception:
            logger.exception(
                "workflow activity failed: id=%s tool=%s", task_id, tool_name
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

    @bp.activity_trigger(input_name="task")  # type: ignore[untyped-decorator]
    async def agents_workflow_run_sub_agent(task) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        task_id = str(task["id"])
        agent_slug = str(task["agent"])
        workflow_id = str(task.get("workflow_id") or "")
        if catalog is None or agent_slug not in catalog:
            logger.error(
                "workflow sub-agent catalog miss: workflow_id=%s node_id=%s agent=%s",
                workflow_id,
                task_id,
                agent_slug,
            )
            raise RuntimeError(
                f"task {task_id!r}: Workflow Sub Agent {agent_slug!r} is not available"
            )

        entry = catalog[agent_slug]
        logger.info(
            "workflow sub-agent activity running: workflow_id=%s node_id=%s agent=%s",
            workflow_id,
            task_id,
            agent_slug,
        )
        try:
            text = await run_leaf_agent_task(
                entry.resolved,
                entry.capabilities,
                str(task["task"]),
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

    @bp.orchestration_trigger(context_name="context")  # type: ignore[untyped-decorator]
    def agents_workflow_orchestrator(context: df.DurableOrchestrationContext) -> Any:
        """Execute a workflow plan, selecting the static or dynamic scheduler.

        A plan is *static* when no task carries a ``when`` predicate or a
        ``for_each`` expansion; it runs through :func:`_run_static_workflow`
        with its exact pre-#1276 wave scheduling and string ``custom_status``
        behavior. Any ``when`` / ``for_each`` selects
        :func:`_run_dynamic_workflow`, which materializes instances, aggregates
        results, and publishes structured (schema_version=2) status.

        Determinism contract (both paths):
        - Ready/runnable sets ordered deterministically before each wave.
        - Templates resolved against the JSON-normalized ``results`` dict.
        - Time read only via ``context.current_utc_datetime``.
        - No I/O outside ``call_activity`` / ``create_timer`` /
          ``wait_for_external_event``.
        """
        payload: dict[str, Any] = context.get_input() or {}
        tasks: list[dict[str, Any]] = list(payload.get("tasks") or [])
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
