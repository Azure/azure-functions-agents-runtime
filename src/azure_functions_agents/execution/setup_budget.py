"""Deadline helpers for the bounded synchronous sandbox setup path."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field

SYNCHRONOUS_RUN_CAP_SECONDS = 180.0
SETUP_BUDGET_SECONDS = 30.0
MINIMUM_EXECUTION_BUDGET_SECONDS = SYNCHRONOUS_RUN_CAP_SECONDS - SETUP_BUDGET_SECONDS


class SetupBudgetExpiredError(TimeoutError):
    """The controller exhausted setup time before a sandbox run could launch."""


def synchronous_wait_seconds(authored_timeout: float | None) -> float:
    """Return the controller's synchronous wait cap for one authored timeout."""
    if authored_timeout is None:
        return SYNCHRONOUS_RUN_CAP_SECONDS
    if not math.isfinite(authored_timeout) or authored_timeout <= 0:
        raise ValueError("authored_timeout must be positive and finite when specified")
    return min(authored_timeout, SYNCHRONOUS_RUN_CAP_SECONDS)


@dataclass(frozen=True, slots=True)
class SetupBudget:
    """One absolute deadline shared by all pre-submit sandbox operations."""

    deadline: float
    _clock: Callable[[], float] = field(repr=False, compare=False)

    @classmethod
    def create(
        cls,
        *,
        deadline: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> SetupBudget:
        if not math.isfinite(deadline):
            raise ValueError("deadline must be finite")
        return cls(deadline=deadline, _clock=clock)

    @classmethod
    def start(
        cls,
        *,
        setup_seconds: float = SETUP_BUDGET_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> SetupBudget:
        if not math.isfinite(setup_seconds) or setup_seconds <= 0:
            raise ValueError("setup_seconds must be positive and finite")
        return cls.create(deadline=clock() + setup_seconds, clock=clock)

    def remaining_setup_seconds(self) -> float:
        """Return the remaining shared setup time or raise before launching work."""
        remaining = self.deadline - self._clock()
        if remaining <= 0:
            raise SetupBudgetExpiredError("Sandbox setup budget expired before run launch.")
        return remaining
