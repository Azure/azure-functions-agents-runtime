"""Agent-facing workflow tools (M1 step 2c).

Five tools:

- ``start_workflow`` — validate + start a workflow; return ``{"workflow_id"}``.
- ``get_workflow_status`` — return the status envelope for a workflow.
- ``list_workflows`` — list this session's recent workflows.
- ``cancel_workflow`` — cooperatively cancel a workflow and preserve partial results.
- ``terminate_workflow`` — hard-stop a workflow; no cooperative cleanup.

All five call the Durable client captured by the per-session MAF tool
wrappers built in ``build_workflow_tools``.
Isolation is enforced by prefix-matching the Durable instance ID against a
128-bit SHA-256 prefix for ``(workflow_agent_slug, session_id)``; a mismatch
returns 404 (same shape as "not found") to avoid leaking another agent's workflows.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from azure.durable_functions import DurableFunctionsClient
from pydantic import BaseModel, ConfigDict, Field, field_validator

from azure_functions_agents._function_tool import tool as define_tool
from azure_functions_agents._logger import logger

from . import registry
from .context import (
    WorkflowSessionContext,
    new_workflow_instance_id,
    workflow_matches_agent_session,
)
from .engine import CANCEL_EVENT_NAME, ORCHESTRATOR_NAME
from .schema import (
    EffectiveWorkflowTaskExecution,
    PlanValidationError,
    WorkflowPlanPolicy,
    WorkflowTaskExecution,
    plan_to_activity_inputs,
    resolve_workflow_task_execution,
    validate_plan,
)

MAX_ACTIVE_WORKFLOWS_PER_SESSION = 10
MAX_WORKFLOW_STATUS_RESULTS = 25
_TERMINAL_RUNTIME_STATUSES = frozenset({
    "Completed",
    "Failed",
    "Terminated",
    "Canceled",
})


class _ConditionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str
    operator: Literal["equals", "not_equals"]
    value: str | int | float | bool | None


class _TaskSpecBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Unique identifier for this task within the plan.")
    depends_on: list[str] = Field(
        default_factory=list,
        description=(
            "IDs of upstream tasks that must complete before this one. Tasks form an "
            "arbitrary DAG: tasks with disjoint depends_on chains run in parallel; "
            "multiple roots are allowed. Cycles, unknown task references, and "
            "self-references are rejected at validation time."
        ),
    )
    when: _ConditionSpec | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "Optional predicate with ref, operator ('equals' or 'not_equals'), and a "
            "JSON-scalar value. The ref must be one full upstream-result reference; "
            "for_each tasks may instead use ${item}, ${item.path}, or ${index}."
        ),
    )


class _ToolTaskSpec(_TaskSpecBase):
    type: Literal["tool"] = "tool"
    tool: str = Field(
        description=(
            "Name of a workflow-safe tool to invoke. "
            "The set of allowed tool names is configured per-agent and listed "
            "in the system prompt under 'Available workflow tools'; an unknown "
            "or disallowed name causes the plan to be rejected."
        ),
    )
    args: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON-serializable arguments passed to the tool.",
    )
    for_each: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "Optional full upstream-result reference resolving to a JSON array. "
            "One task instance is created for each array item."
        ),
    )
    execution: WorkflowTaskExecution | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "Optional bounded execution policy: 'timeout' (ISO-8601, PT1S-PT10M) "
            "for one attempt, 'retry', and 'continue_on_error'. A timeout or retry "
            "declared on the tool itself overrides whatever is set here."
        ),
    )


class _WaitTaskSpec(_TaskSpecBase):
    type: Literal["wait"]
    duration: str | None = Field(
        default=None,
        description=(
            "Relative pause. ISO-8601 duration in the PnDTnHnMnS subset, e.g. "
            "'PT30S' (30 seconds), 'PT5M' (5 minutes), 'PT1H30M', or 'P1D'. "
            "Capped at 24 hours."
        ),
    )
    until: str | None = Field(
        default=None,
        description=(
            "Alternative to 'duration' for type='wait' tasks: schedule a pause "
            "that ends at an absolute moment in time. ISO-8601 datetime with an "
            "explicit timezone, e.g. '2026-04-25T17:30:00Z' or "
            "'2026-04-25T10:30:00-07:00'."
        ),
    )


class _SubAgentTaskSpec(_TaskSpecBase):
    type: Literal["sub_agent"]
    agent: str = Field(
        description=(
            "Authorized specialist slug listed under 'Available workflow Sub Agents'."
        ),
    )
    task: str = Field(
        description=(
            "Complete self-contained instruction for the specialist; may reference "
            "upstream results."
        ),
    )
    for_each: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "Optional full upstream-result reference resolving to a JSON array. "
            "One Sub Agent task is created for each array item."
        ),
    )
    execution: WorkflowTaskExecution | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "Optional bounded execution policy for this Sub Agent task: "
            "'timeout' (ISO-8601, PT1S-PT10M) for one attempt, 'retry', and "
            "'continue_on_error'."
        ),
    )


type _TaskSpec = Annotated[
    _ToolTaskSpec | _WaitTaskSpec | _SubAgentTaskSpec,
    Field(discriminator="type"),
]


class StartWorkflowParams(BaseModel):
    tasks: list[_TaskSpec] = Field(
        description=(
            "Tasks making up the workflow plan. Tasks form a DAG via their depends_on "
            "edges; tasks whose dependencies are all satisfied run in parallel. Argument "
            "values may reference upstream task results using ${node_id.result} or "
            "${node_id.result.path.to.field} — the ref must point to a transitive "
            "predecessor, otherwise the plan is rejected."
        ),
        min_length=1,
    )

    @field_validator("tasks", mode="before")
    @classmethod
    def default_tool_task_type(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        return [
            {"type": "tool", **item}
            if isinstance(item, dict) and "type" not in item
            else item
            for item in value
        ]


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


def _effective_runtime_status(status: Any) -> str:
    """Return the tool-facing runtime status for a Durable instance.

    Durable has no first-class cooperative-cancel or controlled-failure
    terminal state: the orchestrator returns a normal ``Completed`` output
    whose payload signals the outcome. We translate a ``canceled`` output to
    ``Canceled`` and a controlled ``failed`` output to ``Failed`` so callers
    can branch on ``runtime_status`` alone. A native Durable ``Failed`` (raised
    invariant / Activity/provider failure) keeps its opaque output untouched.
    """
    runtime_status = _runtime_status_name(status)
    output = getattr(status, "output", None)
    if runtime_status == "Completed" and isinstance(output, dict):
        if output.get("canceled") is True:
            return "Canceled"
        if output.get("failed") is True:
            return "Failed"
    return runtime_status


def status_envelope(status: Any) -> dict[str, Any]:
    """Normalize a Durable instance status into the tool-facing envelope.

    Translates a successfully-returned cooperative-cancel output into
    ``runtime_status="Canceled"`` and a controlled runtime failure output
    (``output.failed is True``) into ``runtime_status="Failed"`` so callers
    (LLM tools, UI cards, drain endpoint) can distinguish them from clean
    success without inspecting the output payload. Hard ``terminate`` is left
    as Durable's native ``Terminated`` status.
    """
    if status is None:
        return {"workflow_id": None, "runtime_status": "not_found"}
    runtime_status = _effective_runtime_status(status)
    output = status.output
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


def _runtime_status_name(status: Any) -> str:
    return getattr(status.runtime_status, "name", str(status.runtime_status))


def _is_active_status(status: Any) -> bool:
    return _effective_runtime_status(status) not in _TERMINAL_RUNTIME_STATUSES


async def fetch_session_workflows(
    durable_client: DurableFunctionsClient,
    workflow_agent_slug: str,
    session_id: str,
) -> list[dict[str, Any]]:
    """Return status envelopes for workflows matching the agent and session.

    Shared between the ``list_workflows`` tool (LLM-facing) and the
    ``/agent/workflows`` HTTP endpoint (UI polling). See the note in
    ``list_workflows`` about the SDK's missing ``instance_id_prefix``
    filter (FU-7) — until that lands we pull the full instance list
    and filter in memory.
    """
    statuses = await durable_client.get_status_all()
    envelopes: list[dict[str, Any]] = []
    for status in statuses or []:
        instance_id = getattr(status, "instance_id", None)
        if not instance_id or not workflow_matches_agent_session(
            workflow_agent_slug, session_id, instance_id
        ):
            continue
        envelopes.append(status_envelope(status))
    envelopes.sort(
        key=lambda env: env.get("last_updated_time") or "",
        reverse=True,
    )
    return envelopes[:MAX_WORKFLOW_STATUS_RESULTS]


async def count_active_session_workflows(
    durable_client: DurableFunctionsClient,
    workflow_agent_slug: str,
    session_id: str,
) -> int:
    statuses = await durable_client.get_status_all()
    active = 0
    for status in statuses or []:
        instance_id = getattr(status, "instance_id", None)
        if (
            instance_id
            and workflow_matches_agent_session(
                workflow_agent_slug, session_id, instance_id
            )
            and _is_active_status(status)
        ):
            active += 1
            if active >= MAX_ACTIVE_WORKFLOWS_PER_SESSION:
                return active
    return active


async def fetch_session_workflow_status(
    durable_client: DurableFunctionsClient,
    workflow_agent_slug: str,
    session_id: str,
    workflow_id: str,
) -> dict[str, Any] | None:
    """Return the status if it matches the workflow agent and session.
    """
    if not workflow_matches_agent_session(
        workflow_agent_slug, session_id, workflow_id
    ):
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


START_WORKFLOW_DESCRIPTION = (
    "Author and launch a long-running workflow. The workflow runs as a durable "
    "background orchestration; this tool returns as soon as the workflow is "
    "scheduled. Use it when work should survive across chat turns, run steps "
    "in parallel, or exceed a typical tool-call budget. Tasks form a DAG; "
    "supported task types are 'tool', 'wait', and authorized 'sub_agent' tasks. "
    "This tool is fire-and-forget: "
    "after receiving the workflow_id, end your turn promptly and do not poll "
    "get_workflow_status unless the user asks, or the chat client later injects "
    "a <workflow-notification> envelope containing a <workflow-id> and <status>."
)

GET_WORKFLOW_STATUS_DESCRIPTION = (
    "Return the current status of a previously-started workflow. Call this only "
    "when the user explicitly asks about a workflow, or when a synthetic "
    "<workflow-notification> message names a workflow_id."
)

LIST_WORKFLOWS_DESCRIPTION = (
    "List workflows started by this agent session. Call this only when the user "
    "explicitly asks about their workflows; the chat client surfaces in-flight "
    "workflow progress without model polling."
)

TERMINATE_WORKFLOW_DESCRIPTION = (
    "Hard-stop a running workflow. Prefer this only when a workflow is clearly "
    "wrong and must not continue. Only workflows started by the same agent "
    "session can be terminated."
)

CANCEL_WORKFLOW_DESCRIPTION = (
    "Cooperatively cancel a running workflow. Prefer cancel over terminate when "
    "partial output is useful. Only workflows started by the same agent session "
    "can be canceled."
)


async def start_workflow(
    params: StartWorkflowParams,
    session: WorkflowSessionContext | None,
    *,
    policy: WorkflowPlanPolicy | None = None,
) -> str:
    if session is None:
        return _error(_NO_CLIENT_MESSAGE)

    allowed_tools = registry.get_app_config() if policy is None else None
    if policy is None and allowed_tools is None:
        # Should be unreachable: build_workflow_integration sets the
        # allowlist whenever workflows are enabled, and start_workflow
        # is only registered as a tool in that case. Surface explicitly
        # rather than passing None into validate_plan.
        return _error(
            "workflow tools are registered but the per-app allowlist was "
            "never configured (build_workflow_integration was not called)"
        )
    effective_policies: dict[str, EffectiveWorkflowTaskExecution] = {}
    try:
        if policy is None:
            assert allowed_tools is not None
            policy = WorkflowPlanPolicy(
                allowed_tools=frozenset(allowed_tools),
                allowed_subagents=frozenset(),
            )
        plan = validate_plan(
            params.model_dump(exclude_unset=True),
            policy=policy,
        )
        for task in plan.tasks:
            declarations = (
                policy.tool_execution.get(task.tool or "")
                if task.type == "tool"
                else None
            )
            effective = resolve_workflow_task_execution(
                task,
                decorator_retry=declarations.retry if declarations is not None else None,
                decorator_timeout=(
                    declarations.timeout if declarations is not None else None
                ),
            )
            if effective is not None:
                effective_policies[task.id] = effective
    except PlanValidationError as exc:
        metadata: dict[str, str | None] = {}
        if exc.error_code is not None:
            metadata["error_code"] = exc.error_code
            metadata["node_id"] = exc.node_id
            metadata["path"] = exc.path
        return _error(str(exc), **metadata)
    except ValueError as exc:
        return _error(str(exc))

    workflow_agent = {
        "workflow_agent_slug": session.workflow_agent_slug,
        "session_id": session.session_id,
        "agent_name": session.agent_name,
    }
    instance_id = new_workflow_instance_id(
        session.workflow_agent_slug,
        session.session_id,
    )

    try:
        active_count = await count_active_session_workflows(
            session.durable_client,
            session.workflow_agent_slug,
            session.session_id,
        )
    except Exception:
        logger.exception(
            "start_workflow: client.get_status_all failed "
            "workflow_agent=%s session=%s",
            session.workflow_agent_slug,
            session.session_id,
        )
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
                "tasks": plan_to_activity_inputs(plan, effective_policies),
                "workflow_agent_slug": session.workflow_agent_slug,
                "workflow_agent": workflow_agent,
                "policy": {
                    "allowed_tools": sorted(policy.allowed_tools),
                    "allowed_subagents": sorted(policy.allowed_subagents),
                },
            },
        )
    except Exception:
        logger.exception(
            "start_workflow: client.start_new failed workflow_agent=%s session=%s",
            session.workflow_agent_slug,
            session.session_id,
        )
        return _error("failed to start workflow")

    # Durable echoes back the instance ID we supplied; defend against SDK
    # drift by logging a mismatch but trusting our own value.
    if returned_id != instance_id:
        logger.warning(
            "start_workflow: Durable returned instance_id=%r but we supplied %r",
            returned_id,
            instance_id,
        )
    logger.info(
        "workflow started: id=%s workflow_agent=%s session=%s",
        instance_id,
        session.workflow_agent_slug,
        session.session_id,
    )
    return json.dumps({"workflow_id": instance_id})


async def get_workflow_status(
    params: GetWorkflowStatusParams,
    session: WorkflowSessionContext | None,
) -> str:
    if session is None:
        return _error(_NO_CLIENT_MESSAGE)

    # Agent/session check via instance-ID prefix. Any workflow whose ID does
    # not start with this session's hash is treated as nonexistent — same
    # shape as "not found" so existence cannot be probed.
    if not workflow_matches_agent_session(
        session.workflow_agent_slug,
        session.session_id,
        params.workflow_id,
    ):
        return _error(
            f"workflow {params.workflow_id!r} not found",
            status=_NOT_FOUND_ERROR_STATUS,
        )

    try:
        status = await session.durable_client.get_status(params.workflow_id)
    except Exception:
        logger.exception(
            "get_workflow_status: client.get_status failed "
            "workflow_agent=%s session=%s",
            session.workflow_agent_slug,
            session.session_id,
        )
        return _error("failed to fetch workflow status")

    envelope = status_envelope(status)
    if envelope["runtime_status"] == "not_found":
        return _error(
            f"workflow {params.workflow_id!r} not found",
            status=_NOT_FOUND_ERROR_STATUS,
        )
    return json.dumps(envelope)


async def list_workflows(
    params: ListWorkflowsParams,
    session: WorkflowSessionContext | None,
) -> str:
    if session is None:
        return _error(_NO_CLIENT_MESSAGE)

    try:
        envelopes = await fetch_session_workflows(
            session.durable_client,
            session.workflow_agent_slug,
            session.session_id,
        )
    except Exception:
        logger.exception(
            "list_workflows: fetch_session_workflows failed "
            "workflow_agent=%s session=%s",
            session.workflow_agent_slug,
            session.session_id,
        )
        return _error("failed to list workflows")

    return json.dumps({"workflows": envelopes})


async def terminate_workflow(
    params: TerminateWorkflowParams,
    session: WorkflowSessionContext | None,
) -> str:
    if session is None:
        return _error(_NO_CLIENT_MESSAGE)

    if not workflow_matches_agent_session(
        session.workflow_agent_slug,
        session.session_id,
        params.workflow_id,
    ):
        return _error(
            f"workflow {params.workflow_id!r} not found",
            status=_NOT_FOUND_ERROR_STATUS,
        )

    try:
        await session.durable_client.terminate(params.workflow_id, params.reason)
    except Exception:
        logger.exception(
            "terminate_workflow: client.terminate failed "
            "workflow_agent=%s session=%s",
            session.workflow_agent_slug,
            session.session_id,
        )
        return _error("failed to terminate workflow")

    logger.info(
        "workflow terminated: id=%s workflow_agent=%s session=%s reason=%r",
        params.workflow_id,
        session.workflow_agent_slug,
        session.session_id,
        params.reason,
    )
    return json.dumps({"workflow_id": params.workflow_id, "terminated": True})


async def cancel_workflow(
    params: CancelWorkflowParams,
    session: WorkflowSessionContext | None,
) -> str:
    if session is None:
        return _error(_NO_CLIENT_MESSAGE)

    if not workflow_matches_agent_session(
        session.workflow_agent_slug,
        session.session_id,
        params.workflow_id,
    ):
        return _error(
            f"workflow {params.workflow_id!r} not found",
            status=_NOT_FOUND_ERROR_STATUS,
        )

    try:
        await session.durable_client.raise_event(
            params.workflow_id, CANCEL_EVENT_NAME, params.reason
        )
    except Exception:
        logger.exception(
            "cancel_workflow: client.raise_event failed "
            "workflow_agent=%s session=%s",
            session.workflow_agent_slug,
            session.session_id,
        )
        return _error("failed to cancel workflow")

    logger.info(
        "workflow cancel requested: id=%s workflow_agent=%s session=%s reason=%r",
        params.workflow_id,
        session.workflow_agent_slug,
        session.session_id,
        params.reason,
    )
    return json.dumps(
        {"workflow_id": params.workflow_id, "cancel_requested": True}
    )


def _build_session(
    workflow_agent_slug: str,
    session_id: str | None,
    agent_name: str,
    durable_client: DurableFunctionsClient | None,
) -> WorkflowSessionContext | None:
    if not session_id or durable_client is None:
        return None
    return WorkflowSessionContext(
        workflow_agent_slug=workflow_agent_slug,
        session_id=session_id,
        agent_name=agent_name,
        durable_client=durable_client,
    )


def build_workflow_tools(
    *,
    session_id: str | None = None,
    workflow_agent_slug: str = "main",
    agent_name: str = "main",
    durable_client: DurableFunctionsClient | None = None,
    policy: WorkflowPlanPolicy | None = None,
) -> list[Any]:
    """Return the list of workflow tool objects to inject for an agent."""
    session = _build_session(
        workflow_agent_slug, session_id, agent_name, durable_client
    )

    @define_tool(
        name="start_workflow",
        description=START_WORKFLOW_DESCRIPTION,
        schema=StartWorkflowParams,
    )
    async def _start_workflow(params: StartWorkflowParams) -> str:
        return await start_workflow(params, session, policy=policy)

    @define_tool(
        name="get_workflow_status",
        description=GET_WORKFLOW_STATUS_DESCRIPTION,
        schema=GetWorkflowStatusParams,
    )
    async def _get_workflow_status(params: GetWorkflowStatusParams) -> str:
        return await get_workflow_status(params, session)

    @define_tool(
        name="list_workflows",
        description=LIST_WORKFLOWS_DESCRIPTION,
        schema=ListWorkflowsParams,
    )
    async def _list_workflows(params: ListWorkflowsParams) -> str:
        return await list_workflows(params, session)

    @define_tool(
        name="cancel_workflow",
        description=CANCEL_WORKFLOW_DESCRIPTION,
        schema=CancelWorkflowParams,
    )
    async def _cancel_workflow(params: CancelWorkflowParams) -> str:
        return await cancel_workflow(params, session)

    @define_tool(
        name="terminate_workflow",
        description=TERMINATE_WORKFLOW_DESCRIPTION,
        schema=TerminateWorkflowParams,
    )
    async def _terminate_workflow(params: TerminateWorkflowParams) -> str:
        return await terminate_workflow(params, session)

    return [
        _start_workflow,
        _get_workflow_status,
        _list_workflows,
        _cancel_workflow,
        _terminate_workflow,
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
