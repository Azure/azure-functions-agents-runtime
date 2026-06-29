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

import json
import logging
from typing import Any

import azure.durable_functions as df
import azure.functions as func

from . import registry
from .schema import (
    ECHO_TOOL_NAME,
    MAX_PARALLELISM,
    MAX_WAIT_DURATION,
    TOOL_TASK_TYPE,
    WAIT_TASK_TYPE,
    TemplateResolutionError,
    parse_iso8601_datetime,
    parse_iso8601_duration,
    resolve_template_value,
)

ORCHESTRATOR_NAME = "agents_workflow_orchestrator"
CANCEL_EVENT_NAME = "cancel"
_ACTIVITY_NAME = "agents_workflow_run_tool"

WORKFLOW_SAFE_ECHO_TOOL = ECHO_TOOL_NAME

log = logging.getLogger(__name__)


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


def register_workflows(app: func.FunctionApp) -> None:
    """Register the workflow orchestrator + activities on ``app``.

    Expected to be invoked exactly once during app construction.
    Registering twice would double-register Durable bindings and fail
    at worker index time.
    """
    bp = df.Blueprint()

    @bp.activity_trigger(input_name="task")  # type: ignore[untyped-decorator]
    def agents_workflow_run_tool(task: dict[str, Any]) -> dict[str, Any]:
        task_id = task["id"]
        tool_name = task["tool"]
        args = task.get("args") or {}
        handler = registry.get_handler(tool_name)
        if handler is None:
            raise ValueError(
                f"task {task_id!r}: tool {tool_name!r} is not registered "
                "in the workflow-safe tool registry"
            )
        log.info("workflow activity running: id=%s tool=%s", task_id, tool_name)
        try:
            result = handler(args)
        except Exception:
            log.exception(
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

        # Single long-lived cancel listener. Reusing the same Task across
        # iterations of task_any is the canonical Durable pattern; once the
        # event fires, every subsequent task_any sees it as already-complete.
        # Note (asymmetry with timers): a wait_for_external_event Task does
        # NOT need to be .cancel()-ed before the orchestrator returns — that
        # method is only defined on TimerTask in the Durable Python SDK, and
        # an unfired external-event listener does not block completion. Only
        # in-flight timers must be explicitly cancelled (see below).
        cancel_task = context.wait_for_external_event(CANCEL_EVENT_NAME)

        while remaining:
            ready = sorted(
                tid for tid in remaining if not (deps[tid] - results.keys())
            )
            if not ready:
                # Validation rejects cycles, so this means the wire payload
                # was tampered with or has dangling deps. Fail loudly so the
                # workflow ends up in Failed state with a clear cause.
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
                    # Validator should have rejected this; defend anyway.
                    raise RuntimeError(
                        f"task {tid!r}: unsupported task type {ttype!r}"
                    )

            wave_task = context.task_all(wave_tasks)
            winner = yield context.task_any([cancel_task, wave_task])
            if winner is cancel_task:
                reason = cancel_task.result
                # Durable requires every pending timer to be cancelled before
                # the orchestration can complete; otherwise the instance stays
                # in Running until the timer naturally fires. Cancel any timers
                # in the current wave that haven't completed yet.
                for spec, t in zip(wave_specs, wave_tasks, strict=True):
                    if spec["type"] == WAIT_TASK_TYPE and not t.is_completed:
                        t.cancel()
                context.set_custom_status(
                    f"canceled at {len(results)}/{total} tasks done"
                )
                log.info("workflow canceled: instance=%s reason=%r",
                         context.instance_id, reason)
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
                if spec["type"] == TOOL_TASK_TYPE:
                    results[tid] = raw["result"]
                else:
                    # Timer tasks resolve to None; we synthesize a result so
                    # downstream template refs to ``${tid.result}`` are useful.
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

    app.register_blueprint(bp)


__all__ = [
    "CANCEL_EVENT_NAME",
    "ORCHESTRATOR_NAME",
    "WORKFLOW_SAFE_ECHO_TOOL",
    "register_workflows",
]
