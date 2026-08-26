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
import inspect
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal, NotRequired, TypedDict, cast

import azure.durable_functions as df
import azure.functions as func

from azure_functions_agents._logger import logger
from azure_functions_agents.registration.catalog import AgentCatalog
from azure_functions_agents.runner import run_leaf_agent_task

from . import registry
from .context import _workflow_task_idempotency_key
from .policy import (
    ActivityFailure,
    authorization_outcome,
    decide_retry,
    early_policy_outcome_with_telemetry,
    invoke_policy_handler,
    validate_activity_result,
    validate_policy_activity_input,
)
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
    execution: NotRequired[EffectiveWorkflowTaskExecution]
    task_id: NotRequired[str]
    node_instance_id: NotRequired[str]
    attempt: NotRequired[int]
    max_attempts: NotRequired[int]
    idempotency_key: NotRequired[str]


class _ToolActivityInput(_ActivityInputBase):
    tool: str
    args: dict[str, Any]


class _SubAgentActivityInput(_ActivityInputBase):
    agent: str
    task: str


type _ActivityInput = _ToolActivityInput | _SubAgentActivityInput


async def _invoke_handler_once(handler: Any, args: dict[str, Any]) -> Any:
    if inspect.iscoroutinefunction(handler):
        return await handler(args)
    result = await asyncio.to_thread(handler, args)
    if inspect.isawaitable(result):
        return await result
    return result


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


def _plan_is_dynamic(tasks: list[WorkflowTaskInput]) -> bool:
    """Return whether any task opts into data-driven control flow.

    A plan is *dynamic* if any task carries a ``when`` predicate or a
    ``for_each`` expansion. Fully static plans (neither field on any task)
    keep the original wave scheduler with its exact string ``custom_status``
    behavior, so existing regression coverage is unchanged.
    """
    return any(
        task.get("when") is not None
        or task.get("for_each") is not None
        or "execution" in task
        for task in tasks
    )


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
                    context.call_activity(
                        _ACTIVITY_NAME,
                        {
                            "id": tid,
                            "tool": task["tool"],
                            "args": resolved_args,
                            "workflow_agent_slug": workflow_agent_slug,
                            "workflow_id": context.instance_id,
                        },
                    )
                )
                wave_specs.append({"id": tid, "type": TOOL_TASK_TYPE})
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
                    context.call_activity(
                        SUB_AGENT_ACTIVITY_NAME,
                        {
                            "id": tid,
                            "agent": task["agent"],
                            "task": resolved_task,
                            "workflow_id": context.instance_id,
                            "workflow_agent_slug": workflow_agent_slug,
                        },
                    )
                )
                wave_specs.append({"id": tid, "type": SUB_AGENT_TASK_TYPE})
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

        wave_results = wave_task.result
        if isinstance(wave_results, BaseException):
            for spec, t in zip(wave_specs, wave_tasks, strict=True):
                if spec["type"] == WAIT_TASK_TYPE and not t.is_completed:
                    t.cancel()
            raise wave_results
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


type _LogicalState = Literal[
    "pending",
    "running",
    "retry_wait",
    "skipped",
    "expanded",
    "aggregated",
    "aggregated_with_errors",
    "completed",
    "failed_continued",
    "failed",
]
type _InstanceState = Literal[
    "pending",
    "running",
    "retry_wait",
    "skipped",
    "completed",
    "failed_continued",
    "failed",
]
type _InstanceKind = Literal["activity", "timer", "retry_timer"]


class _MaterializedInstance(TypedDict):
    logical_id: str
    index: int | None
    instance_id: str
    state: _InstanceState
    result: Any
    resolved: NotRequired[Any]
    kind: NotRequired[_InstanceKind]
    deadline: NotRequired[str]
    execution: NotRequired[EffectiveWorkflowTaskExecution]
    attempt: NotRequired[int]
    idempotency_key: NotRequired[str]
    last_failure: NotRequired[ActivityFailure]
    retry_deadline: NotRequired[str]
    retry_ready: NotRequired[bool]


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
        policy_aware=any("execution" in task for task in tasks),
    )


def _materialized_total(
    node_instances: dict[str, list[_MaterializedInstance]],
) -> int:
    return sum(len(instances) for instances in node_instances.values())


def _dynamic_status(state: _DynamicWorkflowState) -> dict[str, Any]:
    """Build the versioned structured ``custom_status`` object.

    Version 2 preserves the data-driven workflow shape. Version 3 classifies
    executable units and adds bounded execution fields for policy-aware plans.
    """
    completed = skipped = running = pending = retry_wait = failed_continued = failed = 0
    for insts in state.node_instances.values():
        for inst in insts:
            instance_state = inst["state"]
            if instance_state == "completed":
                completed += 1
            elif instance_state == "skipped":
                skipped += 1
            elif instance_state == "running":
                running += 1
            elif instance_state == "pending":
                pending += 1
            elif instance_state == "retry_wait":
                retry_wait += 1
            elif instance_state == "failed_continued":
                failed_continued += 1
            elif instance_state == "failed":
                failed += 1

    def execution_fields(instance: _MaterializedInstance) -> dict[str, Any]:
        execution = instance.get("execution")
        if execution is None:
            return {}
        fields: dict[str, Any] = {"max_attempts": execution["max_attempts"]}
        attempt = instance.get("attempt", 0)
        if attempt > 0:
            fields["attempt"] = attempt
        if instance["state"] == "retry_wait" and "retry_deadline" in instance:
            fields["next_retry_time"] = instance["retry_deadline"]
        failure = instance.get("last_failure")
        if failure is not None:
            fields["last_failure_kind"] = failure["kind"]
            fields["last_error_code"] = failure["error_code"]
        return fields

    nodes: dict[str, Any] = {}
    for lid, task in state.by_id.items():
        node: dict[str, Any] = {"state": state.logical_state[lid]}
        if task.get("for_each") is not None and lid in state.expanded_count:
            node["expanded_count"] = state.expanded_count[lid]
            node["instances"] = {
                inst["instance_id"]: {
                    "state": inst["state"],
                    **execution_fields(inst),
                }
                for inst in state.node_instances.get(lid, [])
            }
        elif state.policy_aware and lid in state.node_instances:
            node.update(execution_fields(state.node_instances[lid][0]))
        elif state.policy_aware and task.get("for_each") is None:
            if "execution" in task:
                execution = cast(
                    EffectiveWorkflowTaskExecution,
                    cast(Mapping[str, Any], task)["execution"],
                )
                node["max_attempts"] = execution["max_attempts"]
        nodes[lid] = node

    counts = {
            "logical_total": len(state.by_id),
            "materialized_total": _materialized_total(state.node_instances),
            "completed": completed,
            "skipped": skipped,
            "running": running,
    }
    if state.policy_aware:
        unmaterialized_normal = sum(
            1
            for task_id, task in state.by_id.items()
            if task.get("for_each") is None
            and task_id not in state.node_instances
        )
        pending += unmaterialized_normal
        materialized_total = (
            sum(1 for task in state.by_id.values() if task.get("for_each") is None)
            + sum(
                len(state.node_instances.get(task_id, []))
                for task_id, task in state.by_id.items()
                if task.get("for_each") is not None
            )
        )
        counts = {
            "logical_total": len(state.by_id),
            "materialized_total": materialized_total,
            "pending": pending,
            "running": running,
            "retry_wait": retry_wait,
            "completed": completed,
            "skipped": skipped,
            "failed_continued": failed_continued,
            "failed": failed,
        }
    return {
        "schema_version": 3 if state.policy_aware else 2,
        "counts": counts,
        "nodes": nodes,
    }


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


def _terminalize_abandoned_retries(state: _DynamicWorkflowState) -> None:
    for instances in state.node_instances.values():
        for instance in instances:
            if instance["state"] == "retry_wait" or instance.get("retry_ready") is True:
                instance["state"] = "failed"
                instance.pop("retry_deadline", None)
                instance.pop("retry_ready", None)
                state.logical_state[instance["logical_id"]] = "failed"


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
    _terminalize_abandoned_retries(state)
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
    state.logical_state[logical_id] = (
        "aggregated_with_errors"
        if any(instance["state"] == "failed_continued" for instance in instances)
        else "aggregated"
    )


def _new_materialized_instance(
    *,
    context: df.DurableOrchestrationContext,
    task: WorkflowTaskInput,
    logical_id: str,
    instance_id: str,
    index: int | None,
    state: _InstanceState,
    result: Any,
    resolved: Any = _UNBOUND,
) -> _MaterializedInstance:
    instance: _MaterializedInstance = {
        "logical_id": logical_id,
        "index": index,
        "instance_id": instance_id,
        "state": state,
        "result": result,
    }
    if resolved is not _UNBOUND:
        instance["resolved"] = resolved
    if "execution" in task:
        execution = cast(
            EffectiveWorkflowTaskExecution,
            cast(Mapping[str, Any], task)["execution"],
        )
        instance["execution"] = execution
        instance["attempt"] = 0
        instance["idempotency_key"] = _workflow_task_idempotency_key(
            context.instance_id,
            instance_id,
        )
    return instance


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
                instances.append(
                    _new_materialized_instance(
                        context=context,
                        task=task,
                        logical_id=logical_id,
                        instance_id=instance_id,
                        index=index,
                        state="skipped",
                        result=None,
                    )
                )
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
        instances.append(
            _new_materialized_instance(
                context=context,
                task=task,
                logical_id=logical_id,
                instance_id=instance_id,
                index=index,
                state="pending",
                result=None,
                resolved=resolved,
            )
        )

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
            state.node_instances[logical_id] = [
                _new_materialized_instance(
                    context=context,
                    task=task,
                    logical_id=logical_id,
                    instance_id=logical_id,
                    index=None,
                    state="skipped",
                    result=None,
                )
            ]
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
    state.node_instances[logical_id] = [
        _new_materialized_instance(
            context=context,
            task=task,
            logical_id=logical_id,
            instance_id=logical_id,
            index=None,
            state="pending",
            result=None,
            resolved=resolved,
        )
    ]
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
            and state.logical_state[task_id] in {
                "expanded",
                "running",
            }
        )
        for logical_id in aggregatable:
            instances = state.node_instances.get(logical_id, [])
            if instances and all(
                instance["state"] in {"completed", "skipped", "failed_continued"}
                for instance in instances
            ):
                _aggregate_dynamic_node(state, logical_id)
                progressed = True
    return None


def _dynamic_workflow_complete(state: _DynamicWorkflowState) -> bool:
    return all(
        node_state
        in {
            "completed",
            "failed_continued",
            "skipped",
            "aggregated",
            "aggregated_with_errors",
        }
        for node_state in state.logical_state.values()
    )


def _collect_runnable_instances(
    state: _DynamicWorkflowState,
) -> list[_MaterializedInstance]:
    pending = [
        instance
        for instances in state.node_instances.values()
        for instance in instances
        if instance["state"] == "pending"
    ]
    def order(instance: _MaterializedInstance) -> tuple[str, int]:
        return (
            instance["logical_id"],
            instance["index"] if instance["index"] is not None else -1,
        )
    pending.sort(key=order)
    return pending[:MAX_PARALLELISM]


def _prepare_dynamic_attempt(instance: _MaterializedInstance) -> None:
    """Advance the explicit runtime attempt before Activity dispatch."""
    if instance.get("retry_ready"):
        instance["attempt"] += 1
        instance.pop("retry_ready", None)
    elif "execution" in instance:
        instance["attempt"] = 1


def _dispatch_dynamic_wave(
    context: df.DurableOrchestrationContext,
    state: _DynamicWorkflowState,
    wave: list[_MaterializedInstance],
) -> list[Any]:
    wave_tasks: list[Any] = []
    for instance in wave:
        logical_id = instance["logical_id"]
        task = state.by_id[logical_id]
        _prepare_dynamic_attempt(instance)
        if task["type"] == TOOL_TASK_TYPE:
            if task["tool"] not in state.allowed_tools:
                raise RuntimeError(
                    f"task {instance['instance_id']!r}: tool {task['tool']!r} is "
                    "outside the persisted workflow owner policy"
                )
            activity_input: dict[str, Any] = {
                "id": instance["instance_id"],
                "tool": task["tool"],
                "args": instance["resolved"],
                "workflow_agent_slug": state.workflow_agent_slug,
                "workflow_id": context.instance_id,
            }
            if "execution" in instance:
                execution = instance["execution"]
                activity_input.update({
                    "execution": execution,
                    "task_id": logical_id,
                    "node_instance_id": instance["instance_id"],
                    "attempt": instance["attempt"],
                    "max_attempts": execution["max_attempts"],
                    "idempotency_key": instance["idempotency_key"],
                })
            wave_tasks.append(
                context.call_activity(
                    _ACTIVITY_NAME,
                    activity_input,
                )
            )
            instance["kind"] = "activity"
        elif task["type"] == SUB_AGENT_TASK_TYPE:
            if task["agent"] not in state.allowed_subagents:
                raise RuntimeError(
                    f"task {instance['instance_id']!r}: Sub Agent "
                    f"{task['agent']!r} is outside the persisted workflow owner policy"
                )
            subagent_input: dict[str, Any] = {
                "id": instance["instance_id"],
                "agent": task["agent"],
                "task": instance["resolved"],
                "workflow_id": context.instance_id,
                "workflow_agent_slug": state.workflow_agent_slug,
            }
            if "execution" in instance:
                execution = instance["execution"]
                subagent_input.update({
                    "execution": execution,
                    "task_id": logical_id,
                    "node_instance_id": instance["instance_id"],
                    "attempt": instance["attempt"],
                    "max_attempts": execution["max_attempts"],
                    "idempotency_key": instance["idempotency_key"],
                })
            wave_tasks.append(
                context.call_activity(
                    SUB_AGENT_ACTIVITY_NAME,
                    subagent_input,
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


def _mark_dynamic_retry_wait(
    context: df.DurableOrchestrationContext,
    state: _DynamicWorkflowState,
    instance: _MaterializedInstance,
    *,
    delay_ms: int,
) -> None:
    """Persist the next explicit retry deadline without creating its Durable timer."""
    retry_deadline = context.current_utc_datetime + timedelta(milliseconds=delay_ms)
    instance["retry_deadline"] = retry_deadline.isoformat()
    instance["state"] = "retry_wait"
    state.logical_state[instance["logical_id"]] = (
        "retry_wait" if instance["index"] is None else "running"
    )


def _mark_dynamic_continued_failure(
    instance: _MaterializedInstance,
    failure: ActivityFailure,
) -> None:
    """Materialize the existing sanitized continued-failure result."""
    instance["result"] = {
        "failed": True,
        "error_code": failure["error_code"],
        "error": failure["error"],
        "kind": failure["kind"],
        "attempts": instance["attempt"],
    }
    instance["state"] = "failed_continued"


def _cancel_dynamic_wave_timers(
    wave: list[_MaterializedInstance],
    wave_tasks: list[Any],
) -> None:
    for instance, task in zip(wave, wave_tasks, strict=True):
        if instance.get("kind") in {"timer", "retry_timer"} and not task.is_completed:
            task.cancel()


def _restore_canceled_dynamic_wave(
    state: _DynamicWorkflowState,
    wave: list[_MaterializedInstance],
    wave_tasks: list[Any],
) -> None:
    _cancel_dynamic_wave_timers(wave, wave_tasks)
    for instance in wave:
        if instance.get("kind") != "retry_timer":
            instance["state"] = "pending"
    for logical_id in {instance["logical_id"] for instance in wave}:
        if not all(
            instance.get("kind") == "retry_timer"
            for instance in wave
            if instance["logical_id"] == logical_id
        ):
            state.logical_state[logical_id] = (
                "expanded"
                if state.by_id[logical_id].get("for_each") is not None
                else "pending"
            )


def _apply_dynamic_wave_results(
    context: df.DurableOrchestrationContext,
    state: _DynamicWorkflowState,
    wave: list[_MaterializedInstance],
    wave_results: list[Any],
) -> dict[str, Any] | None:
    failures: list[tuple[_MaterializedInstance, ActivityFailure]] = []
    ordered = sorted(
        zip(wave, wave_results, strict=True),
        key=lambda pair: (
            pair[0]["logical_id"],
            pair[0]["index"] if pair[0]["index"] is not None else -1,
        ),
    )
    for instance, raw in ordered:
        kind = instance.get("kind")
        if kind == "retry_timer":
            instance["state"] = "pending"
            instance["retry_ready"] = True
            instance.pop("retry_deadline", None)
            continue
        if kind == "timer":
            instance["result"] = {"waited_until": instance["deadline"]}
            instance["state"] = "completed"
        elif "execution" not in instance:
            if isinstance(raw, BaseException):
                raise raw
            instance["result"] = raw["result"]
            instance["state"] = "completed"
        else:
            if isinstance(raw, BaseException):
                failure: ActivityFailure | None = {
                    "error_code": "workflow_task_activity_infrastructure",
                    "error": "Task Activity failed before returning an outcome.",
                    "kind": "activity_infrastructure",
                    "retryable": True,
                    "continuable": True,
                }
            else:
                succeeded, outcome = validate_activity_result(
                    instance["instance_id"],
                    raw,
                )
                if succeeded:
                    instance["result"] = outcome
                    instance["state"] = "completed"
                    failure = None
                else:
                    failure = cast(ActivityFailure, outcome)
            if failure is not None:
                instance["last_failure"] = failure
                execution = instance["execution"]
                attempt = instance["attempt"]
                disposition = decide_retry(
                    execution,
                    attempt=attempt,
                    failure=failure,
                )
                if disposition.action == "retry":
                    delay_ms = disposition.delay_ms
                    if delay_ms is None:
                        raise RuntimeError("retry disposition is missing its delay")
                    _mark_dynamic_retry_wait(
                        context,
                        state,
                        instance,
                        delay_ms=delay_ms,
                    )
                    continue
                if disposition.action == "continue":
                    _mark_dynamic_continued_failure(instance, failure)
                else:
                    instance["state"] = "failed"
                    state.logical_state[instance["logical_id"]] = "failed"
                    failures.append((instance, failure))
                    continue
        if instance["index"] is None and instance["state"] in {
            "completed",
            "failed_continued",
        }:
            logical_id = instance["logical_id"]
            state.results[logical_id] = instance["result"]
            state.logical_state[logical_id] = (
                "failed_continued"
                if instance["state"] == "failed_continued"
                else "completed"
            )

    if failures:
        instance, failure = failures[0]
        result = _dynamic_failure(
            context,
            state,
            error=failure["error"],
            error_code=failure["error_code"],
            node_id=instance["instance_id"],
            path=None,
            logical_id=instance["logical_id"],
        )
        result["attempts"] = instance["attempt"]
        result["kind"] = failure["kind"]
        return result
    return None


def _run_dynamic_workflow(
    context: df.DurableOrchestrationContext,
    payload: WorkflowPayload,
    tasks: list[WorkflowTaskInput],
) -> Any:
    """Execute a data-driven plan with deterministic phase helpers."""
    state = _new_dynamic_workflow_state(payload, tasks)
    cancel_task = context.wait_for_external_event(CANCEL_EVENT_NAME)
    retry_timers: dict[str, tuple[_MaterializedInstance, Any]] = {}

    def cancel_retry_timers() -> None:
        for _, timer in retry_timers.values():
            if not timer.is_completed:
                timer.cancel()

    def complete_retry_timer(instance: _MaterializedInstance) -> None:
        retry_timers.pop(instance["instance_id"], None)
        instance["state"] = "pending"
        instance["retry_ready"] = True
        instance.pop("retry_deadline", None)

    while True:
        failure = _materialize_ready_nodes(context, state)
        if failure is not None:
            cancel_retry_timers()
            return failure
        if _dynamic_workflow_complete(state):
            break

        for instances in state.node_instances.values():
            for instance in instances:
                if (
                    instance["state"] == "retry_wait"
                    and instance["instance_id"] not in retry_timers
                ):
                    deadline = parse_iso8601_datetime(instance["retry_deadline"])
                    retry_timers[instance["instance_id"]] = (
                        instance,
                        context.create_timer(deadline),
                    )

        wave = _collect_runnable_instances(state)
        wave_tasks = _dispatch_dynamic_wave(context, state, wave) if wave else []
        if not wave_tasks and not retry_timers:
            active_count = sum(
                1
                for node_state in state.logical_state.values()
                if node_state
                not in {
                    "completed",
                    "failed_continued",
                    "skipped",
                    "aggregated",
                    "aggregated_with_errors",
                }
            )
            raise RuntimeError(
                "workflow stalled: no runnable instances but "
                f"{active_count} logical node(s) are not terminal. This indicates "
                "a scheduler invariant violation."
            )

        _publish_dynamic_status(context, state)
        pending_wave = list(zip(wave, wave_tasks, strict=True))
        wave_results_by_id: dict[str, Any] = {}
        while pending_wave:
            retry_waitables = [timer for _, timer in retry_timers.values()]
            winner = yield context.task_any(
                [cancel_task, *[task for _, task in pending_wave], *retry_waitables]
            )
            if winner is cancel_task:
                reason = cancel_task.result
                _restore_canceled_dynamic_wave(state, wave, wave_tasks)
                cancel_retry_timers()
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
            retry_instance = next(
                (
                    instance
                    for instance, timer in retry_timers.values()
                    if winner is timer
                ),
                None,
            )
            if retry_instance is not None:
                complete_retry_timer(retry_instance)
                continue
            completed_index = next(
                (
                    index
                    for index, (_, task) in enumerate(pending_wave)
                    if winner is task
                ),
                None,
            )
            if completed_index is None:
                raise RuntimeError("workflow task_any returned an unknown task")
            instance, completed_task = pending_wave.pop(completed_index)
            try:
                completed_result = completed_task.result
            except Exception as exc:
                completed_result = exc
            wave_results_by_id[instance["instance_id"]] = completed_result

        if not wave:
            retry_waitables = [timer for _, timer in retry_timers.values()]
            winner = yield context.task_any([cancel_task, *retry_waitables])
            if winner is cancel_task:
                reason = cancel_task.result
                cancel_retry_timers()
                _publish_dynamic_status(context, state)
                return {
                    "results": state.results,
                    "canceled": True,
                    "reason": reason,
                    "completed_count": len(state.results),
                    "total_count": len(state.by_id),
                }
            retry_instance = next(
                (
                    instance
                    for instance, timer in retry_timers.values()
                    if winner is timer
                ),
                None,
            )
            if retry_instance is None:
                raise RuntimeError("workflow task_any returned an unknown retry timer")
            complete_retry_timer(retry_instance)
            continue

        wave_results = [
            wave_results_by_id[instance["instance_id"]]
            for instance in wave
        ]
        try:
            failure = _apply_dynamic_wave_results(context, state, wave, wave_results)
        except BaseException:
            _cancel_dynamic_wave_timers(wave, wave_tasks)
            cancel_retry_timers()
            raise
        if failure is not None:
            _cancel_dynamic_wave_timers(wave, wave_tasks)
            cancel_retry_timers()
            return failure
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

    @bp.activity_trigger(input_name="task")  # type: ignore[untyped-decorator]
    async def agents_workflow_run_tool(task: _ToolActivityInput) -> dict[str, Any]:
        policy_aware = "execution" in task
        if policy_aware:
            invalid = validate_policy_activity_input(task, target_type="tool")
            if invalid is not None:
                return early_policy_outcome_with_telemetry(
                    task, target_type="tool", target_name=task.get("tool"), outcome=invalid
                )
        task_id = task["id"]
        tool_name = task["tool"]
        args = task["args"]
        try:
            workflow_agent_slug, policy = require_workflow_agent_policy(task)
        except RuntimeError:
            if policy_aware:
                return early_policy_outcome_with_telemetry(
                    task, target_type="tool", target_name=tool_name,
                    outcome=authorization_outcome(task_id),
                )
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
                return early_policy_outcome_with_telemetry(
                    task, target_type="tool", target_name=tool_name,
                    outcome=authorization_outcome(task_id),
                )
            raise RuntimeError(f"task {task_id!r}: workflow tool {tool_name!r} is not authorized")
        entry = (
            handler_catalog.get(tool_name)
            if handler_catalog is not None
            else registry.get_entry(tool_name)
        )
        if entry is None:
            if policy_aware:
                return early_policy_outcome_with_telemetry(
                    task, target_type="tool", target_name=tool_name,
                    outcome=authorization_outcome(task_id),
                )
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
            result = await _invoke_handler_once(entry.handler, args)
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

    @bp.activity_trigger(input_name="task")  # type: ignore[untyped-decorator]
    async def agents_workflow_run_sub_agent(
        task: _SubAgentActivityInput,
    ) -> dict[str, Any]:
        policy_aware = "execution" in task
        if policy_aware:
            invalid = validate_policy_activity_input(task, target_type="sub_agent")
            if invalid is not None:
                return early_policy_outcome_with_telemetry(
                    task, target_type="sub_agent", target_name=task.get("agent"),
                    outcome=invalid,
                )
        task_id = task["id"]
        agent_slug = task["agent"]
        workflow_id = task["workflow_id"]
        try:
            workflow_agent_slug, policy = require_workflow_agent_policy(task)
        except RuntimeError:
            if policy_aware:
                return early_policy_outcome_with_telemetry(
                    task, target_type="sub_agent", target_name=agent_slug,
                    outcome=authorization_outcome(task_id),
                )
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
                return early_policy_outcome_with_telemetry(
                    task, target_type="sub_agent", target_name=agent_slug,
                    outcome=authorization_outcome(task_id),
                )
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
                return early_policy_outcome_with_telemetry(
                    task, target_type="sub_agent", target_name=agent_slug,
                    outcome=authorization_outcome(task_id),
                )
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

            async def run_policy_sub_agent(_: dict[str, Any]) -> str:
                execution = task["execution"]
                return await run_leaf_agent_task(
                    entry.resolved,
                    entry.capabilities,
                    task["task"],
                    timeout=execution["timeout_ms"] / 1000,
                    execution_role="workflow_subagent",
                )

            outcome = await invoke_policy_handler(
                run_policy_sub_agent,
                {},
                task=task,
                target=agent_slug,
                target_type="sub_agent",
            )
            if outcome["ok"]:
                outcome["result"] = {
                    "agent": agent_slug,
                    "text": outcome["result"],
                }
            return dict(outcome)
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

    @bp.orchestration_trigger(context_name="context")  # type: ignore[untyped-decorator]
    def agents_workflow_orchestrator(context: df.DurableOrchestrationContext) -> Any:
        """Execute a workflow plan, selecting the static or dynamic scheduler.

        A plan is *static* when no task carries a ``when`` predicate or a
        ``for_each`` expansion; it runs through :func:`_run_static_workflow`
        with its exact pre-#1276 wave scheduling and string ``custom_status``
        behavior. Any ``when`` / ``for_each`` / execution policy selects
        :func:`_run_dynamic_workflow`, which materializes instances, aggregates
        results, and publishes structured version 2 or 3 status.

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
