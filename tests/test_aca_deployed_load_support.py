from __future__ import annotations

import pytest

from tests.live import aca_deployed_load_support as support


def test_realistic_burst_timestamps_are_grouped_into_one_batch() -> None:
    """Regression: events in one poll arrive microseconds apart, not identically.

    Grouping on exact float equality passed every synthetic test while producing
    all-size-1 batches on real data -- which silently empties the waiting-only
    series that answers the streaming-visibility question, and collapses the
    measured cadence to the cost of parsing two adjacent events. Both numbers
    still look plausible, which is what makes the failure dangerous.
    """
    timestamps: list[float] = []
    moment = 100.0
    for _ in range(4):
        # Three events delivered by one poll, parsed a fraction of a millisecond
        # apart, then roughly a second until the next poll.
        timestamps.extend(moment + offset * 0.0004 for offset in range(3))
        moment += 1.0

    assert support.events_per_batch(timestamps) == (3, 3, 3, 3)

    primary = support.visibility_gap_seconds(timestamps, waiting_only=True)
    assert len(primary) == 3
    assert all(abs(gap - 1.0) < 0.01 for gap in primary)

    cadence = support.observed_poll_cadence_seconds(timestamps)
    assert all(abs(spacing - 1.0) < 0.01 for spacing in cadence)


def test_slow_trickle_is_not_merged_into_one_batch() -> None:
    """The window must separate distinct polls, not swallow a steady trickle."""
    assert support.events_per_batch([0.0, 0.2, 0.4, 0.6]) == (1, 1, 1, 1)


def test_observed_event_visibility_requires_at_least_two_events() -> None:
    assert support.observed_event_batches([]) == ()
    assert support.visibility_gap_seconds([1.0], waiting_only=True) == ()
    assert support.visibility_gap_seconds([1.0], waiting_only=False) == ()
    assert support.observed_poll_cadence_seconds([1.0]) == ()
    assert support.events_per_batch([1.0]) == ((1,))


def test_waiting_visibility_gap_counts_only_gaps_ending_in_multi_event_batches() -> None:
    timestamps = [10.0, 11.0, 11.0, 12.5]

    assert support.visibility_gap_seconds(timestamps, waiting_only=True) == (1.0,)
    assert support.visibility_gap_seconds(timestamps, waiting_only=False) == (1.0, 0.0, 1.5)
    assert support.observed_poll_cadence_seconds(timestamps) == (1.0, 1.5)
    assert support.events_per_batch(timestamps) == (1, 2, 1)


def test_single_batch_has_no_primary_gap_or_cadence() -> None:
    timestamps = [4.0, 4.0, 4.0]

    assert support.visibility_gap_seconds(timestamps, waiting_only=True) == ()
    assert support.visibility_gap_seconds(timestamps, waiting_only=False) == (0.0, 0.0)
    assert support.observed_poll_cadence_seconds(timestamps) == ()
    assert support.events_per_batch(timestamps) == (3,)


def test_observed_event_helpers_sort_timestamps_before_grouping() -> None:
    timestamps = [5.0, 1.0, 3.0, 3.0]

    assert support.observed_event_batches(timestamps) == (
        support.ObservedEventBatch(observed_at=1.0, event_count=1),
        support.ObservedEventBatch(observed_at=3.0, event_count=2),
        support.ObservedEventBatch(observed_at=5.0, event_count=1),
    )
    assert support.visibility_gap_seconds(timestamps, waiting_only=True) == (2.0,)
    assert support.observed_poll_cadence_seconds(timestamps) == (2.0, 2.0)


def test_latency_metrics_reports_visibility_attribution_and_warning() -> None:
    metrics = support.latency_metrics(
        [0.1],
        [0.2],
        [4.0],
        [
            [1.0, 2.0, 2.0, 4.5, 4.5],
            [10.0, 11.0, 11.0],
        ],
    )
    report = support.render_load_report(
        concurrency=5,
        prepared_count=5,
        provision_concurrency=4,
        provisioning_duration_seconds=None,
        provisioning_attempt_count=5,
        provisioning_retry_count=0,
        suspended_prepared_count=0,
        common_interval=None,
        admitted_count=5,
        succeeded_count=5,
        metrics=metrics,
        replay_count=0,
        active_run_conflict_count=0,
        retry_count=0,
        unclassified_service_throttle_count=0,
        unresolved_idempotency_count=0,
        cleanup_complete=True,
    )

    assert metrics.visibility_gap_ms == (1000.0, 2500.0, 2500.0)
    assert metrics.visibility_gap_all_ms == (0.0, 2500.0, 2500.0)
    assert metrics.observed_poll_cadence_ms == (1000.0, 2500.0, 2500.0)
    assert metrics.events_per_batch == ((1, 2), (2, 3))
    assert "visibility_warning=p95_exceeds_2s" in report
    assert "visibility_attribution=poll_timing_dominates" in report
    assert "does not capture true sandbox-write-to-client-observe delta" in report
    assert "clock-skew correction would add error comparable to the 2s budget" in report


# ---------------------------------------------------------------------------
# throttle_retry_after_seconds
# ---------------------------------------------------------------------------


def test_throttle_retry_returns_seconds_for_valid_retry_after() -> None:
    """A 503/429 with Retry-After: 2 yields 2.0 seconds."""
    assert support.throttle_retry_after_seconds({"Retry-After": "2"}) == 2.0


def test_throttle_retry_returns_none_when_header_missing() -> None:
    """No Retry-After header means no throttle delay."""
    assert support.throttle_retry_after_seconds({}) is None


@pytest.mark.parametrize(
    "value",
    ["0", "-1", "11", "abc", "1.5"],
    ids=["zero", "negative", "exceeds_max", "non_numeric", "float"],
)
def test_throttle_retry_returns_none_for_out_of_bounds(value: str) -> None:
    """Out-of-range or non-integer Retry-After is ignored."""
    assert support.throttle_retry_after_seconds({"Retry-After": value}) is None


def test_throttle_retry_is_case_insensitive() -> None:
    """Header lookup is case-insensitive (delegated to response_header)."""
    assert support.throttle_retry_after_seconds({"retry-after": "3"}) == 3.0


class TestThrottledAdmissionRetryDelay:
    """Cover the branch that decides whether a throttled admission is retried.

    Mutating that branch to ignore Retry-After previously left every test green,
    so the client behavior honoring backpressure was unprotected.
    """

    def test_throttled_status_with_a_valid_header_retries(self) -> None:
        for status in (429, 503):
            assert support.throttled_admission_retry_delay(
                status, {"retry-after": "2"}, is_final_attempt=False
            ) == pytest.approx(2.0)

    def test_final_attempt_never_retries(self) -> None:
        """Bounding the retry is what preserves the run's ability to fail."""
        assert (
            support.throttled_admission_retry_delay(503, {"retry-after": "2"}, is_final_attempt=True)
            is None
        )

    def test_a_missing_header_declines_to_retry(self) -> None:
        assert support.throttled_admission_retry_delay(503, {}, is_final_attempt=False) is None

    def test_an_out_of_range_header_declines_to_retry(self) -> None:
        """A server asking for an implausible wait is not honored blindly."""
        assert (
            support.throttled_admission_retry_delay(503, {"retry-after": "600"}, is_final_attempt=False)
            is None
        )

    def test_non_throttled_statuses_are_not_retried(self) -> None:
        for status in (202, 400, 401, 403, 404, 500, 504):
            assert (
                support.throttled_admission_retry_delay(
                    status, {"retry-after": "2"}, is_final_attempt=False
                )
                is None
            ), f"status {status} must not be treated as throttling"
