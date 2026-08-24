"""Unit tests for SSE retry behavior in aca_deployed_agent_support."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping

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


class _FakeSseResponse:
    """One aiohttp-shaped response: status, headers, and a byte-chunk body."""

    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self._body = body

    async def __aenter__(self) -> _FakeSseResponse:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    @property
    def content(self) -> AsyncIterator[bytes]:
        async def _chunks() -> AsyncIterator[bytes]:
            yield self._body

        return _chunks()


class _FakeSseSession:
    """Serve a scripted sequence of responses and record the requests made."""

    def __init__(self, responses: list[_FakeSseResponse]) -> None:
        self._responses = list(responses)
        self.request_headers: list[Mapping[str, str]] = []

    def get(self, url: str, headers: Mapping[str, str]) -> _FakeSseResponse:
        del url
        self.request_headers.append(dict(headers))
        return self._responses.pop(0)


def _sse_body(sequence: int) -> bytes:
    return f"id: {sequence}\ndata: {{\"type\": \"token\"}}\n\n".encode()


class TestSseReaderRetryLoop:
    """Cover the reader loop, not just the decision it consults.

    The decision function alone was covered, which is the same layer seam that
    let earlier defects reach a live run: the loop could stop sleeping, stop
    raising, or ignore the helper entirely and every test stayed green.
    """

    @pytest.fixture(autouse=True)
    def _no_real_sleeping(self, monkeypatch: pytest.MonkeyPatch) -> list[float]:
        slept: list[float] = []

        async def _record(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr(support.asyncio, "sleep", _record)
        self.slept = slept
        return slept

    @pytest.mark.asyncio
    async def test_a_throttled_response_is_retried_then_succeeds(self) -> None:
        session = _FakeSseSession(
            [
                _FakeSseResponse(503, {"Retry-After": "2"}, b""),
                _FakeSseResponse(200, {}, _sse_body(1)),
            ]
        )
        status, events, _, _, _ = await support._read_sse_response(
            session,  # type: ignore[arg-type]
            "https://example.invalid/events",
            headers={"Authorization": "Bearer x"},
        )
        assert status == 200
        assert len(events) == 1
        assert self.slept == [2.0], "the server's delay must be honored exactly once"

    @pytest.mark.asyncio
    async def test_persistent_throttling_still_raises(self) -> None:
        """Bounding the retry is what preserves the suite's ability to fail."""
        responses = [
            _FakeSseResponse(503, {"Retry-After": "1"}, b"")
            for _ in range(support._SSE_THROTTLE_MAX_ATTEMPTS)
        ]
        session = _FakeSseSession(responses)
        with pytest.raises(support.SseResponseStatusError):
            await support._read_sse_response(
                session,  # type: ignore[arg-type]
                "https://example.invalid/events",
                headers={"Authorization": "Bearer x"},
            )
        assert len(session.request_headers) == support._SSE_THROTTLE_MAX_ATTEMPTS
        assert len(self.slept) == support._SSE_THROTTLE_MAX_ATTEMPTS - 1

    @pytest.mark.asyncio
    async def test_a_non_throttled_status_is_not_retried(self) -> None:
        session = _FakeSseSession([_FakeSseResponse(500, {"Retry-After": "2"}, b"")])
        with pytest.raises(support.SseResponseStatusError):
            await support._read_sse_response(
                session,  # type: ignore[arg-type]
                "https://example.invalid/events",
                headers={"Authorization": "Bearer x"},
            )
        assert len(session.request_headers) == 1
        assert self.slept == []

    @pytest.mark.asyncio
    async def test_a_throttle_without_a_header_is_not_retried(self) -> None:
        session = _FakeSseSession([_FakeSseResponse(503, {}, b"")])
        with pytest.raises(support.SseResponseStatusError):
            await support._read_sse_response(
                session,  # type: ignore[arg-type]
                "https://example.invalid/events",
                headers={"Authorization": "Bearer x"},
            )
        assert len(session.request_headers) == 1

    @pytest.mark.asyncio
    async def test_resume_header_survives_a_throttled_attempt(self) -> None:
        """A retry must not drop the caller's resume position."""
        session = _FakeSseSession(
            [
                _FakeSseResponse(503, {"Retry-After": "1"}, b""),
                _FakeSseResponse(200, {}, _sse_body(4)),
            ]
        )
        await support._read_sse_response(
            session,  # type: ignore[arg-type]
            "https://example.invalid/events",
            headers={"Authorization": "Bearer x", "Last-Event-ID": "3"},
        )
        assert [h.get("Last-Event-ID") for h in session.request_headers] == ["3", "3"]
