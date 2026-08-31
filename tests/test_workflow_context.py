from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from azure_functions_agents import WorkflowTaskContext, current_workflow_task_context
from azure_functions_agents.workflows.context import (
    _reset_workflow_task_context,
    _set_workflow_task_context,
    _workflow_task_idempotency_key,
)


def test_workflow_task_context_is_frozen_and_scoped() -> None:
    context = WorkflowTaskContext(
        workflow_id="workflow",
        task_id="task",
        node_instance_id="task[0]",
        attempt=1,
        max_attempts=3,
        idempotency_key="key",
        deadline=datetime(2026, 8, 24, tzinfo=UTC),
    )
    assert current_workflow_task_context() is None

    token = _set_workflow_task_context(context)
    try:
        assert current_workflow_task_context() is context
    finally:
        _reset_workflow_task_context(token)

    assert current_workflow_task_context() is None


def test_workflow_task_idempotency_key_is_stable_and_length_delimited() -> None:
    digest = hashlib.sha256()
    for value in ("ab", "c"):
        encoded = value.encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)

    assert _workflow_task_idempotency_key("ab", "c") == (
        f"af-wf-task-v1:{digest.hexdigest()}"
    )
    assert _workflow_task_idempotency_key("ab", "c") != (
        _workflow_task_idempotency_key("a", "bc")
    )
