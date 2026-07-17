"""Dynamic workflows for markdown agents (M1).

Public API:

- :func:`register_workflows` — register the Durable Functions engine
  (orchestrator + activities) on a :class:`azure.functions.FunctionApp`.
- :func:`build_workflow_integration` — one-shot helper for
  :func:`azure_functions_agents.create_function_app` that registers the
  Durable engine when enabled, builds the agent-facing tools, and returns
  the system-prompt addendum to append. HTTP workflow routes are registered
  by the app factory.
- :class:`WorkflowPlan` / :func:`validate_plan` — the LLM-authored plan
  shape and its validator.
"""

from .context import (
    WorkflowSessionContext,
    get_workflow_session,
    register_workflow_session,
    unregister_workflow_session,
)
from .engine import ORCHESTRATOR_NAME, WORKFLOW_SAFE_ECHO_TOOL, register_workflows
from .integration import (
    WORKFLOW_SYSTEM_ADDENDUM,
    WorkflowIntegrationResult,
    build_workflow_integration,
)
from .registry import register_workflow_tool
from .schema import PlanValidationError, WorkflowPlan, WorkflowTask, validate_plan

__all__ = [
    "ORCHESTRATOR_NAME",
    "WORKFLOW_SAFE_ECHO_TOOL",
    "WORKFLOW_SYSTEM_ADDENDUM",
    "PlanValidationError",
    "WorkflowIntegrationResult",
    "WorkflowPlan",
    "WorkflowSessionContext",
    "WorkflowTask",
    "build_workflow_integration",
    "get_workflow_session",
    "register_workflow_session",
    "register_workflow_tool",
    "register_workflows",
    "unregister_workflow_session",
    "validate_plan",
]
