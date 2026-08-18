"""Deadline helpers for the bounded synchronous sandbox setup path."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

SYNCHRONOUS_RUN_CAP_SECONDS = 180.0
SETUP_BUDGET_SECONDS = 90.0
MINIMUM_EXECUTION_BUDGET_SECONDS = SYNCHRONOUS_RUN_CAP_SECONDS - SETUP_BUDGET_SECONDS


class SetupPhase(StrEnum):
    """Closed classifications for bounded setup work."""

    REQUEST_LOCK = "request_lock"
    STATE_STORE = "state_store"
    PACKAGE_CAPTURE = "package_capture"
    PROVIDER_BIND = "provider_bind"
    SESSION_LOOKUP = "session_lookup"
    OPERATION_STATE = "operation_state"
    IDEMPOTENCY_LOOKUP = "idempotency_lookup"
    PROVISION_CREATE = "provision_create"
    PROVISION_RECONCILE = "provision_reconcile"
    CAPACITY_REAP = "capacity_reap"
    LIFECYCLE = "lifecycle"
    CONTENT = "content"
    MANIFEST = "manifest"
    JOURNAL = "journal"
    SUBMIT_ADMISSION = "submit_admission"
    PRE_SUBMIT_VALIDATION = "pre_submit_validation"
    POST_CREATE_RECONCILE = "post_create_reconcile"
    SESSION_ATTACH = "session_attach"
    SESSION_RESUME = "session_resume"


class SetupTimeoutReason(StrEnum):
    """Closed reasons exposed by setup timeout telemetry."""

    DEADLINE_ELAPSED = "deadline_elapsed"
    OPERATION_TIMEOUT = "operation_timeout"
    PROVISION_LEASE_LIVE = "provision_lease_live"
    PROVISION_INDETERMINATE = "provision_indeterminate"


class SetupTimeoutExceptionType(StrEnum):
    """Closed exception kinds exposed by setup timeout telemetry."""

    SETUP_BUDGET_EXPIRED = "setup_budget_expired"
    SESSION_ACTIVATION_SETUP_TIMEOUT = "session_activation_setup_timeout"


@dataclass(frozen=True, slots=True)
class SetupTimeoutMetadata:
    """Safe timeout context suitable for internal telemetry only."""

    stage: str
    phase: SetupPhase
    reason: SetupTimeoutReason
    exception_type: SetupTimeoutExceptionType
    configured_budget_seconds: float | None
    elapsed_seconds: float | None
    remaining_seconds: float | None
    request_mode: str | None = None
    session_present: bool | None = None

    @classmethod
    def create(
        cls,
        *,
        phase: SetupPhase,
        reason: SetupTimeoutReason,
        exception_type: SetupTimeoutExceptionType,
        configured_budget_seconds: float | None,
        elapsed_seconds: float | None,
        remaining_seconds: float | None,
        request_mode: str | None = None,
        session_present: bool | None = None,
    ) -> SetupTimeoutMetadata:
        if request_mode not in {None, "synchronous", "respond_async"}:
            raise ValueError("request_mode must be synchronous or respond_async")
        if configured_budget_seconds is not None and (
            not math.isfinite(configured_budget_seconds) or configured_budget_seconds <= 0
        ):
            raise ValueError("configured_budget_seconds must be positive and finite")
        for name, value in (
            ("elapsed_seconds", elapsed_seconds),
            ("remaining_seconds", remaining_seconds),
        ):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError(f"{name} must be non-negative and finite")
        return cls(
            stage="setup",
            phase=phase,
            reason=reason,
            exception_type=exception_type,
            configured_budget_seconds=configured_budget_seconds,
            elapsed_seconds=elapsed_seconds,
            remaining_seconds=remaining_seconds,
            request_mode=request_mode,
            session_present=session_present,
        )


class SetupBudgetExpiredError(TimeoutError):
    """The controller exhausted setup time before a sandbox run could launch."""

    def __init__(self, metadata: SetupTimeoutMetadata) -> None:
        self.metadata = metadata
        super().__init__("Sandbox setup deadline exceeded.")


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
    origin: float | None = None
    configured_budget_seconds: float | None = None

    @classmethod
    def create(
        cls,
        *,
        deadline: float,
        clock: Callable[[], float] = time.monotonic,
        origin: float | None = None,
        configured_budget_seconds: float | None = None,
    ) -> SetupBudget:
        if not math.isfinite(deadline):
            raise ValueError("deadline must be finite")
        if (origin is None) != (configured_budget_seconds is None):
            raise ValueError("origin and configured_budget_seconds must be specified together")
        if origin is not None and (
            not math.isfinite(origin)
            or configured_budget_seconds is None
            or not math.isfinite(configured_budget_seconds)
            or configured_budget_seconds <= 0
        ):
            raise ValueError("configured budget origin must be finite and positive")
        return cls(
            deadline=deadline,
            _clock=clock,
            origin=origin,
            configured_budget_seconds=configured_budget_seconds,
        )

    @classmethod
    def start(
        cls,
        *,
        setup_seconds: float = SETUP_BUDGET_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> SetupBudget:
        if not math.isfinite(setup_seconds) or setup_seconds <= 0:
            raise ValueError("setup_seconds must be positive and finite")
        origin = clock()
        return cls.create(
            deadline=origin + setup_seconds,
            clock=clock,
            origin=origin,
            configured_budget_seconds=setup_seconds,
        )

    def remaining_setup_seconds(self, *, phase: SetupPhase) -> float:
        """Return the remaining shared setup time or raise before launching work."""
        remaining = self.deadline - self._clock()
        if remaining <= 0:
            raise SetupBudgetExpiredError(
                self.timeout_metadata(
                    phase=phase,
                    reason=SetupTimeoutReason.DEADLINE_ELAPSED,
                    exception_type=SetupTimeoutExceptionType.SETUP_BUDGET_EXPIRED,
                )
            )
        return remaining

    def timeout_metadata(
        self,
        *,
        phase: SetupPhase,
        reason: SetupTimeoutReason,
        exception_type: SetupTimeoutExceptionType,
    ) -> SetupTimeoutMetadata:
        """Build redacted timeout metadata without leaking a provider failure."""
        now = self._clock()
        elapsed = None if self.origin is None else max(0.0, now - self.origin)
        remaining = None if self.origin is None else max(0.0, self.deadline - now)
        return SetupTimeoutMetadata.create(
            phase=phase,
            reason=reason,
            exception_type=exception_type,
            configured_budget_seconds=self.configured_budget_seconds,
            elapsed_seconds=None if elapsed is None else round(elapsed, 3),
            remaining_seconds=None if remaining is None else round(remaining, 3),
        )
