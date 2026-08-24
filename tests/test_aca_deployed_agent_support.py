"""Unit tests for SSE retry decision logic in aca_deployed_agent_support."""

from __future__ import annotations

import pytest

from tests.live import aca_deployed_agent_support as support


class TestSseThrottleRetryDelay:
    """Cover the pure decision function that gates SSE stream retries on 503/429."""

    def test_throttled_status_with_valid_header_retries(self) -> None:
        for status in (429, 503):
            assert support.sse_throttle_retry_delay(
                status, {"Retry-After": "2"}, is_final_attempt=False
            ) == pytest.approx(2.0)

    def test_final_attempt_never_retries(self) -> None:
        """Bounding retries preserves the reader's ability to fail."""
        assert (
            support.sse_throttle_retry_delay(
                503, {"Retry-After": "2"}, is_final_attempt=True
            )
            is None
        )

    def test_missing_header_declines_to_retry(self) -> None:
        assert support.sse_throttle_retry_delay(503, {}, is_final_attempt=False) is None

    def test_malformed_header_declines_to_retry(self) -> None:
        assert (
            support.sse_throttle_retry_delay(503, {"Retry-After": "abc"}, is_final_attempt=False)
            is None
        )

    def test_out_of_range_header_declines_to_retry(self) -> None:
        """A server asking for an implausible wait is not honored blindly."""
        assert (
            support.sse_throttle_retry_delay(
                503, {"Retry-After": "600"}, is_final_attempt=False
            )
            is None
        )

    def test_zero_retry_after_declines(self) -> None:
        assert (
            support.sse_throttle_retry_delay(
                503, {"Retry-After": "0"}, is_final_attempt=False
            )
            is None
        )

    def test_non_throttled_statuses_are_not_retried(self) -> None:
        for status in (200, 400, 401, 403, 404, 500, 502, 504):
            assert (
                support.sse_throttle_retry_delay(
                    status, {"Retry-After": "2"}, is_final_attempt=False
                )
                is None
            ), f"status {status} must not be treated as SSE throttling"

    def test_429_is_retried_same_as_503(self) -> None:
        delay_429 = support.sse_throttle_retry_delay(
            429, {"Retry-After": "3"}, is_final_attempt=False
        )
        delay_503 = support.sse_throttle_retry_delay(
            503, {"Retry-After": "3"}, is_final_attempt=False
        )
        assert delay_429 == delay_503 == pytest.approx(3.0)

    def test_max_attempts_constant_is_positive_and_bounded(self) -> None:
        """Guard: the attempt limit is positive and not accidentally huge."""
        assert 2 <= support._SSE_THROTTLE_MAX_ATTEMPTS <= 10
