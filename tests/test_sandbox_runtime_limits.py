from __future__ import annotations

from azure_functions_agents.sandbox_runtime_limits import (
    DEFAULT_RECONCILER_CADENCE_SECONDS,
    MAX_RECONCILER_CADENCE_SECONDS,
    RECLAIM_SAFETY_GRACE_SECONDS,
    lifecycle_auto_delete_seconds,
    reclaim_exceeds_auto_delete_backstop,
)


def test_lifecycle_limits_expose_the_single_canonical_values() -> None:
    assert DEFAULT_RECONCILER_CADENCE_SECONDS == 3600
    assert MAX_RECONCILER_CADENCE_SECONDS == 3600
    assert RECLAIM_SAFETY_GRACE_SECONDS == 300


def test_lifecycle_auto_delete_preserves_the_reclaim_backstop() -> None:
    reclaim_idle_seconds = 86_400
    auto_delete_seconds = lifecycle_auto_delete_seconds(reclaim_idle_seconds)

    assert auto_delete_seconds == 90_300
    assert not reclaim_exceeds_auto_delete_backstop(
        reclaim_idle_seconds=reclaim_idle_seconds,
        auto_delete_seconds=auto_delete_seconds,
    )
    assert reclaim_exceeds_auto_delete_backstop(
        reclaim_idle_seconds=reclaim_idle_seconds,
        auto_delete_seconds=auto_delete_seconds - 1,
    )
