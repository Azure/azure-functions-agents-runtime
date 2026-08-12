"""Pure contracts for the manual deployed ACA backing-loss qualification."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from tests.live.aca_deployed_lifecycle_support import DeployedAcaLifecycleConfig

from azure_functions_agents.session_state import (
    DurableRunRecord,
    DurableSessionOperation,
    DurableSessionRecord,
    EntraUserOwnerContext,
    owner_partition,
)
from azure_functions_agents.transport.transport_models import SandboxSummary

_LOSS_REASON = "sandbox_backing_lost"
_ACTIVE_RUN_STATES = frozenset({"accepted", "running"})


class _AuthorizationEvidence(Protocol):
    tenant_id: str
    object_id: str


def deployed_partition_key(
    config: DeployedAcaLifecycleConfig,
    authorization: _AuthorizationEvidence,
    *,
    agent_slug: str,
) -> str:
    """Derive the exact deployed user partition from the Easy-Auth token evidence."""
    return owner_partition(
        EntraUserOwnerContext.create(
            config.app_identity,
            agent_slug,
            authorization.tenant_id,
            authorization.object_id,
        )
    ).partition_key


def has_active_owned_backing(
    session: DurableSessionRecord,
    run: DurableRunRecord,
    sandbox: SandboxSummary | None,
    *,
    expected_session_id: str,
    expected_run_id: str,
) -> bool:
    """Recognize the live setup state before the test deletes its exact backing."""
    return (
        session.session_id == expected_session_id
        and run.session_id == expected_session_id
        and run.run_id == expected_run_id
        and session.status == "running"
        and run.status in _ACTIVE_RUN_STATES
        and session.active_run_id == run.run_id
        and session.sandbox_id is not None
        and sandbox is not None
        and sandbox.sandbox_id == session.sandbox_id
    )


def has_lost_backing_projection(
    session: DurableSessionRecord,
    run: DurableRunRecord,
    operations: Sequence[DurableSessionOperation],
    *,
    expected_session_id: str,
    expected_run_id: str,
) -> bool:
    """Recognize the fenced terminal projection written only by reconciliation."""
    return (
        session.session_id == expected_session_id
        and run.session_id == expected_session_id
        and run.run_id == expected_run_id
        and run.status == "abandoned"
        and run.status_reason == _LOSS_REASON
        and session.status == "tombstoned"
        and session.tombstone_reason == _LOSS_REASON
        and session.active_run_id is None
        and session.active_operation_id is None
        and any(
            operation.target.run_id == expected_run_id and operation.state == "completed"
            for operation in operations
        )
    )


def assert_public_backing_loss_contract(
    *,
    status_code: int,
    status: dict[str, object],
    result_code: int,
    result: dict[str, object],
) -> None:
    """Verify public terminal status stays readable while the unavailable result is gone."""
    assert status_code == 200
    assert status.get("state") == "abandoned"
    error = status.get("error")
    if error is not None:
        assert isinstance(error, dict)
        assert error.get("code") == "session_tombstoned"
    assert result_code == 410
    assert result.get("error") in {"result_unavailable", "session_gone"}
