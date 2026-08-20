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
from typing import TYPE_CHECKING, Any, Literal

import azure.durable_functions as df
import azure.functions as func

from azure_functions_agents._logger import logger
from azure_functions_agents.registration.catalog import AgentCatalog

from . import registry
from .workflow_schema import (
    ECHO_TOOL_NAME,
    MAX_PARALLELISM,
    MAX_WAIT_DURATION,
    SUB_AGENT_TASK_TYPE,
    TOOL_TASK_TYPE,
    WAIT_TASK_TYPE,
    TemplateResolutionError,
    parse_iso8601_datetime,
    parse_iso8601_duration,
    resolve_template_value,
)

if TYPE_CHECKING:
    from azure_functions_agents.config.schema import ResolvedAgent
    from azure_functions_agents.registration.capabilities import AgentCapabilities


async def run_leaf_agent_task(
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    task: str,
    *,
    timeout: float,
    execution_role: Literal["workflow_subagent"],
) -> str:
    """Load the runner only when a Workflow Sub Agent activity executes."""
    from azure_functions_agents.runner import run_leaf_agent_task as execute_leaf_task

    return await execute_leaf_task(
        resolved,
        capabilities,
        task,
        timeout=timeout,
        execution_role=execution_role,
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


def _run_tool_activity(task: dict[str, Any]) -> dict[str, Any]:
    """Execute one workflow-safe tool task; the tool activity's body."""
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


async def _run_sub_agent_activity(
    task: dict[str, Any], catalog: AgentCatalog | None
) -> dict[str, Any]:
    """Execute one Workflow Sub Agent task; the sub-agent activity's body."""
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


def _next_wave(
    remaining: set[str],
    deps: dict[str, set[str]],
    results: dict[str, Any],
) -> list[str]:
    """Return the id-sorted, parallelism-capped set of ready tasks.

    Validation rejects cycles and dangling deps, so an empty ready set
    while tasks remain means the wire payload was tampered with; fail
    loudly so the workflow ends up Failed with a clear cause.
    """
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
    return ready[:MAX_PARALLELISM]


def _resolve_wave_template(tid: str, value: Any, results: dict[str, Any]) -> Any:
    """Resolve template refs for a wave task, surfacing failures as RuntimeError."""
    try:
        return resolve_template_value(value, results)
    except TemplateResolutionError as exc:
        raise RuntimeError(
            f"task {tid!r}: template resolution failed: {exc}"
        ) from exc


def _build_tool_wave_item(
    context: df.DurableOrchestrationContext,
    tid: str,
    task: dict[str, Any],
    results: dict[str, Any],
) -> tuple[dict[str, Any], Any]:
    """Schedule a tool activity and return its (spec, task-handle) pair."""
    resolved_args = _resolve_wave_template(tid, task.get("args") or {}, results)
    handle = context.call_activity(
        _ACTIVITY_NAME,
        {
            "id": tid,
            "tool": task["tool"],
            "args": resolved_args,
        },
    )
    return {"id": tid, "type": TOOL_TASK_TYPE}, handle


def _build_sub_agent_wave_item(
    context: df.DurableOrchestrationContext,
    tid: str,
    task: dict[str, Any],
    results: dict[str, Any],
) -> tuple[dict[str, Any], Any]:
    """Schedule a sub-agent activity and return its (spec, task-handle) pair."""
    resolved_task = _resolve_wave_template(tid, task["task"], results)
    if not isinstance(resolved_task, str):
        raise RuntimeError(
            f"task {tid!r}: resolved Sub Agent task must be a string"
        )
    handle = context.call_activity(
        SUB_AGENT_ACTIVITY_NAME,
        {
            "id": tid,
            "agent": task["agent"],
            "task": resolved_task,
            "workflow_id": context.instance_id,
        },
    )
    return {"id": tid, "type": SUB_AGENT_TASK_TYPE}, handle


def _build_wait_wave_item(
    context: df.DurableOrchestrationContext,
    tid: str,
    task: dict[str, Any],
) -> tuple[dict[str, Any], Any]:
    """Schedule a durable timer and return its (spec, task-handle) pair."""
    deadline = _wait_deadline(context, task)
    handle = context.create_timer(deadline)
    spec = {
        "id": tid,
        "type": WAIT_TASK_TYPE,
        "deadline": deadline.isoformat(),
    }
    return spec, handle


def _build_wave(
    context: df.DurableOrchestrationContext,
    wave: list[str],
    by_id: dict[str, dict[str, Any]],
    results: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[Any]]:
    """Translate a ready wave into aligned result-spec and task-handle lists."""
    wave_specs: list[dict[str, Any]] = []
    wave_tasks: list[Any] = []
    for tid in wave:
        task = by_id[tid]
        ttype = task.get("type") or TOOL_TASK_TYPE
        if ttype == TOOL_TASK_TYPE:
            spec, handle = _build_tool_wave_item(context, tid, task, results)
        elif ttype == SUB_AGENT_TASK_TYPE:
            spec, handle = _build_sub_agent_wave_item(context, tid, task, results)
        elif ttype == WAIT_TASK_TYPE:
            spec, handle = _build_wait_wave_item(context, tid, task)
        else:
            # Validator should have rejected this; defend anyway.
            raise RuntimeError(
                f"task {tid!r}: unsupported task type {ttype!r}"
            )
        wave_specs.append(spec)
        wave_tasks.append(handle)
    return wave_specs, wave_tasks


def _build_cancel_result(
    context: df.DurableOrchestrationContext,
    cancel_task: Any,
    wave_specs: list[dict[str, Any]],
    wave_tasks: list[Any],
    results: dict[str, Any],
    total: int,
) -> dict[str, Any]:
    """Cancel in-flight wave timers and build the cooperative-cancel envelope."""
    reason = cancel_task.result
    # Durable requires every pending timer to be cancelled before the
    # orchestration can complete; otherwise the instance stays Running until
    # the timer naturally fires.
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


def _apply_wave_results(
    results: dict[str, Any],
    remaining: set[str],
    wave_specs: list[dict[str, Any]],
    wave_results: list[Any],
) -> None:
    """Fold completed wave outputs into ``results`` and drop them from ``remaining``."""
    for spec, raw in zip(wave_specs, wave_results, strict=True):
        tid = spec["id"]
        if spec["type"] in {TOOL_TASK_TYPE, SUB_AGENT_TASK_TYPE}:
            results[tid] = raw["result"]
        else:
            # Timer tasks resolve to None; synthesize a result so downstream
            # template refs to ``${tid.result}`` are useful.
            results[tid] = {"waited_until": spec["deadline"]}
        remaining.discard(tid)


def _set_progress_status(
    context: df.DurableOrchestrationContext,
    results: dict[str, Any],
    remaining: set[str],
    deps: dict[str, set[str]],
    total: int,
) -> None:
    """Publish the between-wave custom status naming the next ready task, if any."""
    next_ready = sorted(
        tid for tid in remaining if not (deps[tid] - results.keys())
    )
    done = len(results)
    if next_ready:
        context.set_custom_status(
            f"{done}/{total} tasks done, next={next_ready[0]}"
        )
    else:
        context.set_custom_status(f"{done}/{total} tasks done")


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
        return _run_tool_activity(task)

    @bp.activity_trigger(input_name="task")  # type: ignore[untyped-decorator]
    async def agents_workflow_run_sub_agent(task) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        return await _run_sub_agent_activity(task, catalog)

    @bp.orchestration_trigger(context_name="context")  # type: ignore[untyped-decorator]
    def agents_workflow_orchestrator(context: df.DurableOrchestrationContext) -> Any:
        """Execute an arbitrary-DAG workflow plan in deterministic waves.

        Input: ``{"tasks": [{"id", "type", "tool"?, "args"?, "duration"?,
        "until"?, "depends_on"}, ...]}``.

        Return on success: ``{"results": {task_id: result, ...}}``.
        Return on cooperative cancel: ``{"results": ..., "canceled": True,
        "reason": <event payload>, "completed_count": N, "total_count": M}``.

        Determinism contract:
        - ``ready`` set sorted by task id before each ``task_all`` wave.
        - Templates resolved against the JSON-normalized ``results`` dict
          using only deterministic Python.
        - Time read only via ``context.current_utc_datetime``.
        - No I/O outside ``call_activity`` / ``create_timer`` /
          ``wait_for_external_event``.
        """
        payload: dict[str, Any] = context.get_input() or {}
        tasks: list[dict[str, Any]] = list(payload.get("tasks") or [])

        by_id: dict[str, dict[str, Any]] = {t["id"]: t for t in tasks}
        deps: dict[str, set[str]] = {
            t["id"]: set(t.get("depends_on") or []) for t in tasks
        }
        results: dict[str, Any] = {}
        remaining: set[str] = set(by_id)
        total = len(tasks)

        # Single long-lived cancel listener: reusing one Task across task_any
        # iterations is the canonical Durable pattern. Unlike an in-flight
        # timer, an unfired external-event listener needs no .cancel() before
        # the orchestrator completes.
        cancel_task = context.wait_for_external_event(CANCEL_EVENT_NAME)

        while remaining:
            wave = _next_wave(remaining, deps, results)
            wave_specs, wave_tasks = _build_wave(context, wave, by_id, results)

            context.set_custom_status(
                f"{len(results)}/{total} tasks done, running={','.join(wave)}"
            )
            wave_task = context.task_all(wave_tasks)
            winner = yield context.task_any([cancel_task, wave_task])
            if winner is cancel_task:
                return _build_cancel_result(
                    context, cancel_task, wave_specs, wave_tasks, results, total
                )

            _apply_wave_results(results, remaining, wave_specs, wave_task.result)
            _set_progress_status(context, results, remaining, deps, total)

        return {"results": results}

    app.register_blueprint(bp)


__all__ = [
    "CANCEL_EVENT_NAME",
    "ORCHESTRATOR_NAME",
    "SUB_AGENT_ACTIVITY_NAME",
    "WORKFLOW_SAFE_ECHO_TOOL",
    "register_workflows",
]
