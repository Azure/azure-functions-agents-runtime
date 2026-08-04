from __future__ import annotations

import pytest

from azure_functions_agents.execution.setup_budget import (
    MINIMUM_EXECUTION_BUDGET_SECONDS,
    SETUP_BUDGET_SECONDS,
    SYNCHRONOUS_RUN_CAP_SECONDS,
    SetupBudget,
    SetupBudgetExpiredError,
    synchronous_wait_seconds,
)


class _Clock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def test_budget_reserves_thirty_seconds_of_the_sync_cap_for_setup() -> None:
    assert SETUP_BUDGET_SECONDS == 30.0
    assert MINIMUM_EXECUTION_BUDGET_SECONDS == 150.0
    assert SETUP_BUDGET_SECONDS + MINIMUM_EXECUTION_BUDGET_SECONDS == SYNCHRONOUS_RUN_CAP_SECONDS


def test_budget_uses_one_absolute_deadline_across_operations() -> None:
    clock = _Clock(10.0)
    budget = SetupBudget.start(clock=clock)

    assert budget.remaining_setup_seconds() == 30.0
    clock.value = 27.5
    assert budget.remaining_setup_seconds() == 12.5
    clock.value = 40.0
    with pytest.raises(SetupBudgetExpiredError):
        budget.remaining_setup_seconds()


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
