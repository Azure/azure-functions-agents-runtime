"""Dependency-free canonical model-visible names for runtime-owned tools."""

from __future__ import annotations

FHS_START_WORKFLOW_TOOL_NAME = "fhs_start_workflow"
FHS_GET_WORKFLOW_STATUS_TOOL_NAME = "fhs_get_workflow_status"
FHS_LIST_WORKFLOWS_TOOL_NAME = "fhs_list_workflows"
FHS_CANCEL_WORKFLOW_TOOL_NAME = "fhs_cancel_workflow"
FHS_TERMINATE_WORKFLOW_TOOL_NAME = "fhs_terminate_workflow"
FHS_EXECUTE_PYTHON_TOOL_NAME = "fhs_execute_python"

RUNTIME_WORKFLOW_TOOL_NAMES: frozenset[str] = frozenset(
    {
        FHS_START_WORKFLOW_TOOL_NAME,
        FHS_GET_WORKFLOW_STATUS_TOOL_NAME,
        FHS_LIST_WORKFLOWS_TOOL_NAME,
        FHS_CANCEL_WORKFLOW_TOOL_NAME,
        FHS_TERMINATE_WORKFLOW_TOOL_NAME,
    }
)

__all__ = [
    "FHS_CANCEL_WORKFLOW_TOOL_NAME",
    "FHS_EXECUTE_PYTHON_TOOL_NAME",
    "FHS_GET_WORKFLOW_STATUS_TOOL_NAME",
    "FHS_LIST_WORKFLOWS_TOOL_NAME",
    "FHS_START_WORKFLOW_TOOL_NAME",
    "FHS_TERMINATE_WORKFLOW_TOOL_NAME",
    "RUNTIME_WORKFLOW_TOOL_NAMES",
]
