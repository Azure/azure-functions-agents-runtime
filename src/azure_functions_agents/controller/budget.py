"""One request-scoped clock for sandbox setup and synchronous response waiting."""

from __future__ import annotations

import asyncio
import inspect
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from ..execution.setup_budget import (
    SETUP_BUDGET_SECONDS,
    SetupBudget,
    synchronous_wait_seconds,
)


class RunDeadlineExceededError(TimeoutError):
    """The synchronous controller wall deadline elapsed after a run was accepted."""


class RunCleanupDeadlineExceededError(RunDeadlineExceededError):
    """Bounded post-deadline cleanup exceeded the reserved Functions headroom."""


CLEANUP_HEADROOM_SECONDS = 45.0


@dataclass(frozen=True, slots=True)
class RequestBudget:
    """Absolute request deadlines anchored once after auth and input validation."""

    wall_deadline: float
    setup: SetupBudget
    _clock: Callable[[], float] = field(repr=False, compare=False)

    @classmethod
    def start(
        cls,
        *,
        authored_timeout: float | None,
        clock: Callable[[], float] = time.monotonic,
    ) -> RequestBudget:
        wall_seconds = synchronous_wait_seconds(authored_timeout)
        now = clock()
        setup_seconds = min(SETUP_BUDGET_SECONDS, wall_seconds)
        return cls(
            wall_deadline=now + wall_seconds,
            setup=SetupBudget.create(deadline=now + setup_seconds, clock=clock),
            _clock=clock,
        )

    @property
    def wall_seconds(self) -> float:
        """Return the remaining synchronous wait duration before response delivery."""
        return self.remaining_wall_seconds()

    def remaining_wall_seconds(self) -> float:
        """Return remaining wall time or raise before another synchronous wait."""
        remaining = self.wall_deadline - self._clock()
        if remaining <= 0:
            raise RunDeadlineExceededError("Synchronous run deadline exceeded.")
        return remaining

    def remaining_cleanup_seconds(self) -> float:
        """Return cleanup headroom measured from the same original monotonic anchor."""
        remaining = self.wall_deadline + CLEANUP_HEADROOM_SECONDS - self._clock()
        if remaining <= 0:
            raise RunCleanupDeadlineExceededError(
                "Synchronous cleanup deadline exceeded."
            )
        return remaining

    async def wait_for[T](self, operation: Awaitable[T]) -> T:
        """Await work within the shared wall deadline without creating a second clock."""
        try:
            remaining = self.remaining_wall_seconds()
        except RunDeadlineExceededError:
            if inspect.iscoroutine(operation):
                operation.close()
            raise
        try:
            async with asyncio.timeout(remaining):
                return await operation
        except TimeoutError:
            raise RunDeadlineExceededError("Synchronous run deadline exceeded.") from None

    async def wait_for_cleanup[T](self, operation: Awaitable[T]) -> T:
        """Await bounded post-deadline cleanup without exceeding platform headroom."""
        try:
            remaining = self.remaining_cleanup_seconds()
        except RunCleanupDeadlineExceededError:
            if inspect.iscoroutine(operation):
                operation.close()
            raise
        try:
            async with asyncio.timeout(remaining):
                return await operation
        except TimeoutError:
            raise RunCleanupDeadlineExceededError(
                "Synchronous cleanup deadline exceeded."
            ) from None


def validate_async_setup_timeout(authored_timeout: float | None) -> float:
    """Return the bounded acceptance setup allowance for an async submission."""
    if authored_timeout is not None and (
        not math.isfinite(authored_timeout) or authored_timeout <= 0
    ):
        raise ValueError("authored_timeout must be positive and finite when specified")
    return SETUP_BUDGET_SECONDS
