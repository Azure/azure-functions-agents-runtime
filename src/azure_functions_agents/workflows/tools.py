"""Agent-facing workflow tools (M1 step 2c).

Five tools:

- ``start_workflow`` — validate + start a workflow; return ``{"workflow_id"}``.
- ``get_workflow_status`` — return the status envelope for a workflow.
- ``list_workflows`` — list this session's recent workflows.
- ``cancel_workflow`` — cooperatively cancel a workflow and preserve partial results.
- ``terminate_workflow`` — hard-stop a workflow; no cooperative cleanup.

All five call the Durable client directly via the per-session registry
populated by the chat handler's ``durable_client_input`` binding.
Ownership is enforced by prefix-matching the Durable instance ID
against ``sha256(session_id)[:12]``; a mismatch returns 404 (same shape
as "not found") to avoid leaking existence of other sessions'
workflows.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from copilot import define_tool
from copilot.tools import ToolInvocation
from pydantic import BaseModel, Field

from . import registry
from .context import (
    get_workflow_session,
    new_workflow_instance_id,
    session_owns_workflow,
)
from .engine import CANCEL_EVENT_NAME, ORCHESTRATOR_NAME
from .schema import PlanValidationError, plan_to_activity_inputs, validate_plan

log = logging.getLogger(__name__)

MAX_ACTIVE_WORKFLOWS_PER_SESSION = 10
MAX_WORKFLOW_STATUS_RESULTS = 25
_TERMINAL_RUNTIME_STATUSES = frozenset({
    "Completed",
    "Failed",
    "Terminated",
    "Canceled",
})


class _TaskSpec(BaseModel):
    id: str = Field(description="Unique identifier for this task within the plan.")
    type: str = Field(
        default="tool",
        description=(
            "Task type. 'tool' invokes a workflow-safe tool; 'wait' pauses the "
            "workflow until a deadline (use the 'duration' or 'until' field)."
        ),
    )
    tool: Optional[str] = Field(
        default=None,
        description=(
            "Required for type='tool'. Name of a workflow-safe tool to invoke. "
            "The set of allowed tool names is configured per-agent and listed "
            "in the system prompt under 'Available workflow tools'; an unknown "
            "or disallowed name causes the plan to be rejected. Must be omitted "
            "for type='wait'."
        ),
    )
    args: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "JSON-serializable arguments passed to the tool (type='tool' only)."
        ),
    )
    depends_on: List[str] = Field(
        default_factory=list,
        description=(
            "IDs of upstream tasks that must complete before this one. Tasks form an "
            "arbitrary DAG: tasks with disjoint depends_on chains run in parallel; "
            "multiple roots are allowed. Cycles, unknown task references, and "
            "self-references are rejected at validation time."
        ),
    )
    duration: Optional[str] = Field(
        default=None,
        description=(
            "Required for type='wait' (and only valid then) when scheduling a "
            "relative pause. ISO-8601 duration in the PnDTnHnMnS subset, e.g. "
            "'PT30S' (30 seconds), 'PT5M' (5 minutes), 'PT1H30M', or 'P1D'. "
            "Capped at 24 hours."
        ),
    )
    until: Optional[str] = Field(
        default=None,
        description=(
            "Alternative to 'duration' for type='wait' tasks: schedule a pause "
            "that ends at an absolute moment in time. ISO-8601 datetime with an "
            "explicit timezone, e.g. '2026-04-25T17:30:00Z' or "
            "'2026-04-25T10:30:00-07:00'."
        ),
    )


class StartWorkflowParams(BaseModel):
    tasks: List[_TaskSpec] = Field(
        description=(
            "Tasks making up the workflow plan. Tasks form a DAG via their depends_on "
            "edges; tasks whose dependencies are all satisfied run in parallel. Argument "
            "values may reference upstream task results using ${node_id.result} or "
            "${node_id.result.path.to.field} — the ref must point to a transitive "
            "predecessor, otherwise the plan is rejected."
        ),
        min_length=1,
    )


class GetWorkflowStatusParams(BaseModel):
    workflow_id: str = Field(
        description="Workflow ID returned by a prior call to start_workflow."
    )


class ListWorkflowsParams(BaseModel):
    """No parameters — lists recent workflows owned by the calling session."""


class TerminateWorkflowParams(BaseModel):
    workflow_id: str = Field(
        description="Workflow ID returned by a prior call to start_workflow."
    )
    reason: str = Field(
        default="terminated by agent",
        description="Short human-readable reason recorded on the Durable instance.",
    )


class CancelWorkflowParams(BaseModel):
    workflow_id: str = Field(
        description="Workflow ID returned by a prior call to start_workflow."
    )
    reason: str = Field(
        default="canceled by agent",
        description=(
            "Short human-readable reason delivered to the orchestrator as the "
            "cancel event payload."
        ),
    )


def status_envelope(status: Any) -> dict:
    """Normalize a Durable instance status into the tool-facing envelope.

    Translates a successfully-returned cooperative-cancel output into
    ``runtime_status="Canceled"`` so callers (LLM tools, UI cards, drain
    endpoint) can distinguish cooperative cancel from clean success
    without inspecting the output payload. Hard ``terminate`` is left as
    Durable's native ``Terminated`` status.
    """
    if status is None:
        return {"workflow_id": None, "runtime_status": "not_found"}
    runtime_status = _runtime_status_name(status)
    output = status.output
    if (
        runtime_status == "Completed"
        and isinstance(output, dict)
        and output.get("canceled") is True
    ):
        runtime_status = "Canceled"
    return {
        "workflow_id": status.instance_id,
        "runtime_status": runtime_status,
        "custom_status": status.custom_status,
        "output": output,
        "created_time": status.created_time.isoformat() if status.created_time else None,
        "last_updated_time": (
            status.last_updated_time.isoformat() if status.last_updated_time else None
        ),
    }


# Backwards-compatible alias for in-module call sites; do not export.
_status_envelope = status_envelope


def _runtime_status_name(status: Any) -> str:
    return getattr(status.runtime_status, "name", str(status.runtime_status))


def _is_active_status(status: Any) -> bool:
    runtime_status = _runtime_status_name(status)
    output = getattr(status, "output", None)
    if (
        runtime_status == "Completed"
        and isinstance(output, dict)
        and output.get("canceled") is True
    ):
        runtime_status = "Canceled"
    return runtime_status not in _TERMINAL_RUNTIME_STATUSES


async def fetch_session_workflows(
    durable_client: Any, session_id: str
) -> List[Dict[str, Any]]:
    """Return status envelopes for all workflows owned by ``session_id``.

    Shared between the ``list_workflows`` tool (LLM-facing) and the
    ``/agent/workflows`` HTTP endpoint (UI polling). See the note in
    ``list_workflows`` about the SDK's missing ``instance_id_prefix``
    filter (FU-7) — until that lands we pull the full instance list
    and filter in memory.
    """
    statuses = await durable_client.get_status_all()
    envelopes: List[Dict[str, Any]] = []
    for status in statuses or []:
        instance_id = getattr(status, "instance_id", None)
        if not instance_id or not session_owns_workflow(session_id, instance_id):
            continue
        envelopes.append(status_envelope(status))
    envelopes.sort(
        key=lambda env: env.get("last_updated_time") or "",
        reverse=True,
    )
    return envelopes[:MAX_WORKFLOW_STATUS_RESULTS]


async def count_active_session_workflows(durable_client: Any, session_id: str) -> int:
    statuses = await durable_client.get_status_all()
    active = 0
    for status in statuses or []:
        instance_id = getattr(status, "instance_id", None)
        if (
            instance_id
            and session_owns_workflow(session_id, instance_id)
            and _is_active_status(status)
        ):
            active += 1
            if active >= MAX_ACTIVE_WORKFLOWS_PER_SESSION:
                return active
    return active


async def fetch_session_workflow_status(
    durable_client: Any, session_id: str, workflow_id: str
) -> Optional[Dict[str, Any]]:
    """Return the status envelope for ``workflow_id`` if owned by
    ``session_id``; otherwise ``None`` (404 semantics).
    """
    if not session_owns_workflow(session_id, workflow_id):
        return None
    status = await durable_client.get_status(workflow_id)
    envelope = status_envelope(status)
    if envelope["runtime_status"] == "not_found":
        return None
    return envelope


def _error(message: str, **extra: Any) -> str:
    return json.dumps({"error": message, **extra})


_NO_CLIENT_MESSAGE = (
    "workflow tools are not available in this request context (the enclosing "
    "chat handler did not register a Durable client for this session)"
)

_NOT_FOUND_ERROR_STATUS = 404


@define_tool(
    description=(
        "Author and launch a long-running workflow. The workflow runs as a durable "
        "background orchestration; this tool returns as soon as the workflow is "
        "scheduled, so the conversation can continue. Use it when the work needs to "
        "survive across chat turns, when you want steps to run in parallel, or when "
        "the total work would exceed a typical tool-call budget. Tasks form a DAG: "
        "use depends_on to express dependencies; tasks whose dependencies are all "
        "satisfied run concurrently. Two task types are supported: 'tool' (invokes "
        "a workflow-safe tool) and 'wait' (durable timer using 'duration' or "
        "'until'). Argument values may reference prior task results via "
        "${node_id.result} or ${node_id.result.path}. Returns {workflow_id} "
        "immediately. **This tool is fire-and-forget**: the workflow runs in the "
        "background and its progress is rendered to the user by the chat client. "
        "After receiving the workflow_id, end your turn promptly — do not poll "
        "get_workflow_status. When the workflow reaches a terminal state, the "
        "chat client will inject a synthetic user message containing a "
        "`<workflow-notification>` envelope (with `<workflow-id>` and "
        "`<status>`); call get_workflow_status once with that id and "
        "summarize the result for the user. "
        "Use cancel_workflow to stop cooperatively, or terminate_workflow to stop "
        "abruptly, only if the user asks."
    )
)
async def start_workflow(params: StartWorkflowParams, invocation: ToolInvocation) -> str:
    session = get_workflow_session(invocation.session_id)
    if session is None:
        return _error(_NO_CLIENT_MESSAGE)

    allowed_tools = registry.get_app_config()
    if allowed_tools is None:
        # Should be unreachable: build_workflow_integration sets the
        # allowlist whenever workflows are enabled, and start_workflow
        # is only registered as a tool in that case. Surface explicitly
        # rather than passing None into validate_plan.
        return _error(
            "workflow tools are registered but the per-app allowlist was "
            "never configured (build_workflow_integration was not called)"
        )
    try:
        plan = validate_plan(params.model_dump(), allowed_tools=allowed_tools)
    except PlanValidationError as exc:
        return _error(str(exc))

    owner = {
        "session_id": session.session_id,
        "agent_name": session.agent_name,
    }
    instance_id = new_workflow_instance_id(session.session_id)

    try:
        active_count = await count_active_session_workflows(
            session.durable_client, session.session_id
        )
    except Exception:  # noqa: BLE001
        log.exception("start_workflow: client.get_status_all failed")
        return _error("failed to start workflow")
    if active_count >= MAX_ACTIVE_WORKFLOWS_PER_SESSION:
        return _error(
            "too many active workflows for this session",
            active=active_count,
            limit=MAX_ACTIVE_WORKFLOWS_PER_SESSION,
        )

    try:
        returned_id = await session.durable_client.start_new(
            ORCHESTRATOR_NAME,
            instance_id=instance_id,
            client_input={
                "tasks": plan_to_activity_inputs(plan),
                "owner": owner,
            },
        )
    except Exception:  # noqa: BLE001
        log.exception("start_workflow: client.start_new failed")
        return _error("failed to start workflow")

    # Durable echoes back the instance ID we supplied; defend against SDK
    # drift by logging a mismatch but trusting our own value.
    if returned_id != instance_id:
        log.warning(
            "start_workflow: Durable returned instance_id=%r but we supplied %r",
            returned_id,
            instance_id,
        )
    log.info("workflow started: id=%s owner=%s", instance_id, owner["session_id"])
    return json.dumps({"workflow_id": instance_id})


@define_tool(
    description=(
        "Return the current status of a previously-started workflow. **Call this "
        "only when the user explicitly asks** about a workflow's progress or "
        "result (for example, 'what did the incident workflow find?' or 'is X "
        "still running?'), **or when you receive a synthetic user message "
        "containing one or more `<workflow-notification>` envelopes** posted "
        "by the chat client (call this once per `<workflow-id>` listed, then "
        "summarize). Do not call this on your own initiative "
        "after start_workflow — workflow progress is rendered to the user by the "
        "chat client, and the chat client will notify you when the workflow "
        "reaches a terminal state. The returned envelope includes runtime_status "
        "(one of: Running, Completed, Failed, Terminated, Canceled, Pending), an "
        "optional short custom_status string with progress, and — once the "
        "workflow reaches a terminal state — the output. Only workflows started "
        "by the same agent session are visible."
    )
)
async def get_workflow_status(
    params: GetWorkflowStatusParams, invocation: ToolInvocation
) -> str:
    session = get_workflow_session(invocation.session_id)
    if session is None:
        return _error(_NO_CLIENT_MESSAGE)

    # Ownership check via instance-ID prefix. Any workflow whose ID does
    # not start with this session's hash is treated as nonexistent — same
    # shape as "not found" so existence cannot be probed.
    if not session_owns_workflow(session.session_id, params.workflow_id):
        return _error(
            f"workflow {params.workflow_id!r} not found",
            status=_NOT_FOUND_ERROR_STATUS,
        )

    try:
        status = await session.durable_client.get_status(params.workflow_id)
    except Exception:  # noqa: BLE001
        log.exception("get_workflow_status: client.get_status failed")
        return _error("failed to fetch workflow status")

    envelope = _status_envelope(status)
    if envelope["runtime_status"] == "not_found":
        return _error(
            f"workflow {params.workflow_id!r} not found",
            status=_NOT_FOUND_ERROR_STATUS,
        )
    return json.dumps(envelope)


@define_tool(
    description=(
        "List workflows started by this agent session. Returns an array of "
        "status envelopes, newest first, in the same shape as "
        "get_workflow_status. Includes active workflows regardless of age "
        "and terminal workflows that have not yet been purged from Durable "
        "history. **Call this only when the user explicitly asks** about "
        "their workflows (for example, 'what workflows are still running?' "
        "or 'do you have any workflow IDs from earlier?'). Do not call this "
        "to check on workflows you started yourself — the chat client "
        "surfaces in-flight workflows to the user without your assistance."
    )
)
async def list_workflows(
    params: ListWorkflowsParams, invocation: ToolInvocation
) -> str:
    session = get_workflow_session(invocation.session_id)
    if session is None:
        return _error(_NO_CLIENT_MESSAGE)

    try:
        envelopes = await fetch_session_workflows(
            session.durable_client, session.session_id
        )
    except Exception:  # noqa: BLE001
        log.exception("list_workflows: fetch_session_workflows failed")
        return _error("failed to list workflows")

    return json.dumps({"workflows": envelopes})


@define_tool(
    description=(
        "Hard-stop a running workflow. The Durable instance stops abruptly; no "
        "completion push-back is guaranteed. Prefer this when a workflow is "
        "clearly wrong and must not continue; for clean shutdown of workflows "
        "that could leave partial state, wait for them to finish on their own. "
        "Only workflows started by the same agent session can be terminated."
    )
)
async def terminate_workflow(
    params: TerminateWorkflowParams, invocation: ToolInvocation
) -> str:
    session = get_workflow_session(invocation.session_id)
    if session is None:
        return _error(_NO_CLIENT_MESSAGE)

    if not session_owns_workflow(session.session_id, params.workflow_id):
        return _error(
            f"workflow {params.workflow_id!r} not found",
            status=_NOT_FOUND_ERROR_STATUS,
        )

    try:
        await session.durable_client.terminate(params.workflow_id, params.reason)
    except Exception:  # noqa: BLE001
        log.exception("terminate_workflow: client.terminate failed")
        return _error("failed to terminate workflow")

    log.info(
        "workflow terminated: id=%s reason=%r", params.workflow_id, params.reason
    )
    return json.dumps({"workflow_id": params.workflow_id, "terminated": True})


@define_tool(
    description=(
        "Cooperatively cancel a running workflow. Unlike terminate_workflow, "
        "cancel signals the orchestrator via an event and lets it return a clean "
        "result (status will be 'Canceled' with the partial results so far). "
        "Cancellation is observed when the orchestrator receives the cancel "
        "event; completed prior-wave results are preserved, but in-flight "
        "activities from the current wave are not waited on or included if the "
        "cancel event wins first. Pending wait timers are canceled before the "
        "workflow returns. "
        "Prefer cancel over terminate when the user changes their mind and "
        "partial output is useful. Only workflows started by the same agent "
        "session can be canceled."
    )
)
async def cancel_workflow(
    params: CancelWorkflowParams, invocation: ToolInvocation
) -> str:
    session = get_workflow_session(invocation.session_id)
    if session is None:
        return _error(_NO_CLIENT_MESSAGE)

    if not session_owns_workflow(session.session_id, params.workflow_id):
        return _error(
            f"workflow {params.workflow_id!r} not found",
            status=_NOT_FOUND_ERROR_STATUS,
        )

    try:
        await session.durable_client.raise_event(
            params.workflow_id, CANCEL_EVENT_NAME, params.reason
        )
    except Exception:  # noqa: BLE001
        log.exception("cancel_workflow: client.raise_event failed")
        return _error("failed to cancel workflow")

    log.info(
        "workflow cancel requested: id=%s reason=%r",
        params.workflow_id,
        params.reason,
    )
    return json.dumps(
        {"workflow_id": params.workflow_id, "cancel_requested": True}
    )


def build_workflow_tools() -> list:
    """Return the list of workflow tool objects to inject for an agent."""
    return [
        start_workflow,
        get_workflow_status,
        list_workflows,
        cancel_workflow,
        terminate_workflow,
    ]


__all__ = [
    "CancelWorkflowParams",
    "GetWorkflowStatusParams",
    "ListWorkflowsParams",
    "StartWorkflowParams",
    "TerminateWorkflowParams",
    "build_workflow_tools",
    "cancel_workflow",
    "fetch_session_workflow_status",
    "fetch_session_workflows",
    "get_workflow_status",
    "list_workflows",
    "start_workflow",
    "status_envelope",
    "terminate_workflow",
]
