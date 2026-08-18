from __future__ import annotations

import pytest

from azure_functions_agents.execution.setup_budget import (
    MINIMUM_EXECUTION_BUDGET_SECONDS,
    SETUP_BUDGET_SECONDS,
    SYNCHRONOUS_RUN_CAP_SECONDS,
    SetupBudget,
    SetupBudgetExpiredError,
    SetupPhase,
    SetupTimeoutExceptionType,
    SetupTimeoutReason,
    synchronous_wait_seconds,
)


class _Clock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def test_budget_reserves_ninety_seconds_of_the_sync_cap_for_setup() -> None:
    assert SETUP_BUDGET_SECONDS == 90.0
    assert MINIMUM_EXECUTION_BUDGET_SECONDS == 90.0
    assert SETUP_BUDGET_SECONDS + MINIMUM_EXECUTION_BUDGET_SECONDS == SYNCHRONOUS_RUN_CAP_SECONDS


def test_budget_uses_one_absolute_deadline_across_operations() -> None:
    clock = _Clock(10.0)
    budget = SetupBudget.start(clock=clock)

    assert budget.remaining_setup_seconds(phase=SetupPhase.PROVISION_CREATE) == 90.0
    clock.value = 27.5
    assert budget.remaining_setup_seconds(phase=SetupPhase.PROVISION_CREATE) == 72.5
    clock.value = 100.0
    with pytest.raises(SetupBudgetExpiredError) as expired:
        budget.remaining_setup_seconds(phase=SetupPhase.PROVISION_CREATE)
    assert expired.value.metadata.phase is SetupPhase.PROVISION_CREATE
    assert expired.value.metadata.reason is SetupTimeoutReason.DEADLINE_ELAPSED
    assert expired.value.metadata.exception_type is SetupTimeoutExceptionType.SETUP_BUDGET_EXPIRED
    assert expired.value.metadata.configured_budget_seconds == 90.0
    assert expired.value.metadata.elapsed_seconds == 90.0
    assert expired.value.metadata.remaining_seconds == 0.0


def test_budget_metadata_tracks_origin_elapsed_and_remaining() -> None:
    clock = _Clock(10.0)
    budget = SetupBudget.start(clock=clock)
    clock.value = 31.2349

    metadata = budget.timeout_metadata(
        phase=SetupPhase.MANIFEST,
        reason=SetupTimeoutReason.OPERATION_TIMEOUT,
        exception_type=SetupTimeoutExceptionType.SESSION_ACTIVATION_SETUP_TIMEOUT,
    )

    assert metadata.stage == "setup"
    assert metadata.phase is SetupPhase.MANIFEST
    assert metadata.configured_budget_seconds == 90.0
    assert metadata.elapsed_seconds == 21.235
    assert metadata.remaining_seconds == 68.765


def test_legacy_absolute_deadline_keeps_unknown_timing_metadata() -> None:
    budget = SetupBudget.create(deadline=100.0, clock=lambda: 10.0)

    metadata = budget.timeout_metadata(
        phase=SetupPhase.CONTENT,
        reason=SetupTimeoutReason.OPERATION_TIMEOUT,
        exception_type=SetupTimeoutExceptionType.SESSION_ACTIVATION_SETUP_TIMEOUT,
    )

    assert metadata.configured_budget_seconds is None
    assert metadata.elapsed_seconds is None
    assert metadata.remaining_seconds is None


@pytest.mark.parametrize("phase", list(SetupPhase))
def test_every_setup_phase_is_accepted_by_metadata(phase: SetupPhase) -> None:
    metadata = SetupBudget.start(clock=_Clock()).timeout_metadata(
        phase=phase,
        reason=SetupTimeoutReason.DEADLINE_ELAPSED,
        exception_type=SetupTimeoutExceptionType.SETUP_BUDGET_EXPIRED,
    )

    assert metadata.phase is phase


@pytest.mark.parametrize(
    ("authored_timeout", "expected"),
    [
        (None, 180.0),
        (300.0, 180.0),
        (120.0, 120.0),
    ],
)
def test_synchronous_wait_is_capped_without_changing_authored_watchdog(
    authored_timeout: float | None,
    expected: float,
) -> None:
    assert synchronous_wait_seconds(authored_timeout) == expected


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("inf")])
def test_synchronous_wait_rejects_invalid_authored_timeout(timeout: float) -> None:
    with pytest.raises(ValueError, match="positive and finite"):
        synchronous_wait_seconds(timeout)
