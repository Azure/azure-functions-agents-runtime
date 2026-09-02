"""Shared ACA sandbox lifecycle limits."""

from __future__ import annotations

DEFAULT_RECONCILER_CADENCE_SECONDS = 3600
MAX_RECONCILER_CADENCE_SECONDS = 3600
RECLAIM_SAFETY_GRACE_SECONDS = 300
RESULT_HOLD_SECONDS = 300


def lifecycle_auto_delete_seconds(reclaim_idle_seconds: int) -> int:
    """Return the per-sandbox lifecycle backstop interval."""
    return (
        reclaim_idle_seconds
        + MAX_RECONCILER_CADENCE_SECONDS
        + RECLAIM_SAFETY_GRACE_SECONDS
    )


def reclaim_exceeds_auto_delete_backstop(
    *,
    reclaim_idle_seconds: int,
    auto_delete_seconds: int,
    reconciler_cadence_seconds: int = DEFAULT_RECONCILER_CADENCE_SECONDS,
    grace_seconds: int = RECLAIM_SAFETY_GRACE_SECONDS,
) -> bool:
    """Return whether reclaim can miss the lifecycle backstop."""
    return reclaim_idle_seconds > (
        auto_delete_seconds - reconciler_cadence_seconds - grace_seconds
    )
