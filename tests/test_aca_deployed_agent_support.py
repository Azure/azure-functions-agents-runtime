"""Unit tests for SSE retry behavior in aca_deployed_agent_support."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping

import pytest

from tests.aca_smoke_diagnostics import AcaSmokeEnvironmentError
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

    @pytest.mark.parametrize("status", [502, 503])
    def test_frontend_unavailable_without_retry_after_uses_fixed_delay(
        self,
        status: int,
    ) -> None:
        assert support.sse_throttle_retry_delay(
            status,
            {"Content-Type": "text/html; charset=utf-8", "Connection": "close"},
            is_final_attempt=False,
        ) == pytest.approx(support._FRONTEND_UNAVAILABLE_RETRY_SECONDS)

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
    async def test_frontend_unavailable_without_header_retries_then_succeeds(self) -> None:
        session = _FakeSseSession(
            [
                _FakeSseResponse(503, {"Content-Type": "text/html"}, b"Site Unavailable"),
                _FakeSseResponse(200, {}, _sse_body(1)),
            ]
        )

        status, events, _, _, _ = await support._read_sse_response(
            session,  # type: ignore[arg-type]
            "https://example.invalid/events",
            headers={"Authorization": "******", "Last-Event-ID": "0"},
        )

        assert status == 200
        assert [event.sequence for event in events] == [1]
        assert self.slept == [support._FRONTEND_UNAVAILABLE_RETRY_SECONDS]
        assert [headers["Last-Event-ID"] for headers in session.request_headers] == ["0", "0"]

    @pytest.mark.asyncio
    async def test_persistent_frontend_unavailability_still_raises(self) -> None:
        session = _FakeSseSession(
            [
                _FakeSseResponse(503, {"Content-Type": "text/html"}, b"Site Unavailable")
                for _ in range(support._SSE_THROTTLE_MAX_ATTEMPTS)
            ]
        )

        with pytest.raises(support.SseResponseStatusError):
            await support._read_sse_response(
                session,  # type: ignore[arg-type]
                "https://example.invalid/events",
                headers={"Authorization": "******"},
            )

        assert len(session.request_headers) == support._SSE_THROTTLE_MAX_ATTEMPTS
        assert self.slept == [
            support._FRONTEND_UNAVAILABLE_RETRY_SECONDS
        ] * (support._SSE_THROTTLE_MAX_ATTEMPTS - 1)

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


# ---------------------------------------------------------------------------
# json_request throttle-retry tests
# ---------------------------------------------------------------------------


class _FakeJsonResponse:
    """One aiohttp-shaped response for json_request faking."""

    def __init__(self, status: int, headers: dict[str, str], body: dict[str, object]) -> None:
        self.status = status
        self.headers = headers
        self._body = body

    async def __aenter__(self) -> _FakeJsonResponse:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def json(self, content_type: object = None) -> dict[str, object]:
        return self._body

    async def read(self) -> bytes:
        import json as _json

        return _json.dumps(self._body).encode()


class _FakeFrontendResponse(_FakeJsonResponse):
    """An App Service front-end response that never reached the JSON app."""

    def __init__(self, status: int = 503) -> None:
        super().__init__(
            status,
            {"Content-Type": "text/html; charset=utf-8", "Connection": "close"},
            {},
        )
        self.reads = 0

    async def json(self, content_type: object = None) -> dict[str, object]:
        del content_type
        raise json.JSONDecodeError("not JSON", "Site Unavailable", 0)

    async def read(self) -> bytes:
        self.reads += 1
        return b"Site Unavailable"


class _FakeJsonSession:
    """Serve scripted JSON responses and record requests made."""

    def __init__(self, responses: list[_FakeJsonResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[tuple[str, str]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: object = None,
        json: object = None,
    ) -> _FakeJsonResponse:
        self.requests.append((method, url))
        return self._responses.pop(0)


class TestJsonRequestThrottleRetry:
    """Cover the json_request retry loop for transient backpressure."""

    @pytest.fixture(autouse=True)
    def _no_real_sleeping(self, monkeypatch: pytest.MonkeyPatch) -> list[float]:
        slept: list[float] = []

        async def _record(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr(support.asyncio, "sleep", _record)
        self.slept = slept
        return slept

    @pytest.mark.asyncio
    async def test_503_with_valid_retry_after_then_200_succeeds(self) -> None:
        """A single transient 503 with Retry-After is retried and the success returned."""
        session = _FakeJsonSession(
            [
                _FakeJsonResponse(503, {"Retry-After": "5"}, {}),
                _FakeJsonResponse(200, {}, {"result": "ok"}),
            ]
        )
        status, body, _headers = await support.json_request(
            session,  # type: ignore[arg-type]
            "GET",
            "https://example.invalid/status",
        )
        assert status == 200
        assert body == {"result": "ok"}
        assert self.slept == [5.0], "must sleep exactly the server-requested delay"
        assert len(session.requests) == 2

    @pytest.mark.asyncio
    async def test_503_exhausts_all_attempts_returns_last_response(self) -> None:
        """When all attempts are throttled, the last 503 is returned (not raised)."""
        responses = [
            _FakeJsonResponse(503, {"Retry-After": "1"}, {"error": "busy"})
            for _ in range(support._JSON_THROTTLE_MAX_ATTEMPTS)
        ]
        session = _FakeJsonSession(responses)
        status, body, _ = await support.json_request(
            session,  # type: ignore[arg-type]
            "GET",
            "https://example.invalid/status",
        )
        assert status == 503
        assert body == {"error": "busy"}
        assert len(session.requests) == support._JSON_THROTTLE_MAX_ATTEMPTS
        assert len(self.slept) == support._JSON_THROTTLE_MAX_ATTEMPTS - 1

    @pytest.mark.asyncio
    async def test_429_handled_same_as_503(self) -> None:
        session = _FakeJsonSession(
            [
                _FakeJsonResponse(429, {"Retry-After": "3"}, {}),
                _FakeJsonResponse(200, {}, {"ok": True}),
            ]
        )
        status, _body, _ = await support.json_request(
            session,  # type: ignore[arg-type]
            "GET",
            "https://example.invalid/status",
        )
        assert status == 200
        assert self.slept == [3.0]

    @pytest.mark.asyncio
    async def test_504_setup_deadline_exceeded_is_not_retried(self) -> None:
        """504 with setup_deadline_exceeded is a meaningful assertion target."""
        session = _FakeJsonSession(
            [
                _FakeJsonResponse(
                    504,
                    {"Retry-After": "120"},
                    {"error": "setup_deadline_exceeded"},
                ),
            ]
        )
        status, body, _ = await support.json_request(
            session,  # type: ignore[arg-type]
            "GET",
            "https://example.invalid/status",
        )
        assert status == 504
        assert body["error"] == "setup_deadline_exceeded"
        assert len(session.requests) == 1
        assert self.slept == []

    @pytest.mark.asyncio
    async def test_500_is_not_retried(self) -> None:
        session = _FakeJsonSession([_FakeJsonResponse(500, {"Retry-After": "2"}, {})])
        status, _, _ = await support.json_request(
            session,  # type: ignore[arg-type]
            "GET",
            "https://example.invalid/status",
        )
        assert status == 500
        assert len(session.requests) == 1
        assert self.slept == []

    @pytest.mark.asyncio
    async def test_404_is_not_retried(self) -> None:
        session = _FakeJsonSession([_FakeJsonResponse(404, {}, {})])
        status, _, _ = await support.json_request(
            session,  # type: ignore[arg-type]
            "GET",
            "https://example.invalid/status",
        )
        assert status == 404
        assert len(session.requests) == 1

    @pytest.mark.asyncio
    async def test_409_is_not_retried(self) -> None:
        session = _FakeJsonSession([_FakeJsonResponse(409, {}, {})])
        status, _, _ = await support.json_request(
            session,  # type: ignore[arg-type]
            "GET",
            "https://example.invalid/status",
        )
        assert status == 409
        assert len(session.requests) == 1

    @pytest.mark.asyncio
    async def test_missing_retry_after_on_503_is_not_retried(self) -> None:
        """No Retry-After means no server backpressure signal — do not invent one."""
        session = _FakeJsonSession([_FakeJsonResponse(503, {}, {"error": "busy"})])
        status, _, _ = await support.json_request(
            session,  # type: ignore[arg-type]
            "GET",
            "https://example.invalid/status",
        )
        assert status == 503
        assert len(session.requests) == 1
        assert self.slept == []

    @pytest.mark.asyncio
    async def test_idempotent_post_retries_frontend_unavailable_then_succeeds(self) -> None:
        unavailable = _FakeFrontendResponse()
        session = _FakeJsonSession(
            [
                unavailable,
                _FakeJsonResponse(202, {}, {"run_id": "run-1"}),
            ]
        )

        status, body, _ = await support.json_request(
            session,  # type: ignore[arg-type]
            "POST",
            "https://example.invalid/chat",
            headers={"Idempotency-Key": "same-attempt"},
        )

        assert status == 202
        assert body == {"run_id": "run-1"}
        assert unavailable.reads == 1
        assert len(session.requests) == 2
        assert self.slept == [support._FRONTEND_UNAVAILABLE_RETRY_SECONDS]

    @pytest.mark.asyncio
    async def test_unsafe_post_does_not_retry_frontend_unavailable(self) -> None:
        unavailable = _FakeFrontendResponse()
        session = _FakeJsonSession([unavailable])

        with pytest.raises(AcaSmokeEnvironmentError, match="HTTP 503"):
            await support.json_request(
                session,  # type: ignore[arg-type]
                "POST",
                "https://example.invalid/chat",
            )

        assert unavailable.reads == 1
        assert len(session.requests) == 1
        assert self.slept == []

    @pytest.mark.asyncio
    async def test_persistent_frontend_unavailability_still_fails(self) -> None:
        responses = [
            _FakeFrontendResponse()
            for _ in range(support._JSON_THROTTLE_MAX_ATTEMPTS)
        ]
        session = _FakeJsonSession(responses)

        with pytest.raises(AcaSmokeEnvironmentError, match="HTTP 503"):
            await support.json_request(
                session,  # type: ignore[arg-type]
                "GET",
                "https://example.invalid/status",
            )

        assert len(session.requests) == support._JSON_THROTTLE_MAX_ATTEMPTS
        assert self.slept == [
            support._FRONTEND_UNAVAILABLE_RETRY_SECONDS
        ] * (support._JSON_THROTTLE_MAX_ATTEMPTS - 1)

    @pytest.mark.asyncio
    async def test_malformed_retry_after_on_503_is_not_retried(self) -> None:
        session = _FakeJsonSession(
            [_FakeJsonResponse(503, {"Retry-After": "abc"}, {})]
        )
        status, _, _ = await support.json_request(
            session,  # type: ignore[arg-type]
            "GET",
            "https://example.invalid/status",
        )
        assert status == 503
        assert len(session.requests) == 1
        assert self.slept == []

    @pytest.mark.asyncio
    async def test_out_of_range_retry_after_on_503_is_not_retried(self) -> None:
        session = _FakeJsonSession(
            [_FakeJsonResponse(503, {"Retry-After": "999"}, {})]
        )
        status, _, _ = await support.json_request(
            session,  # type: ignore[arg-type]
            "GET",
            "https://example.invalid/status",
        )
        assert status == 503
        assert len(session.requests) == 1
        assert self.slept == []

    @pytest.mark.asyncio
    async def test_retry_disabled_returns_503_immediately(self) -> None:
        """Callers with their own retry logic can opt out."""
        session = _FakeJsonSession(
            [_FakeJsonResponse(503, {"Retry-After": "5"}, {"error": "busy"})]
        )
        status, _body, _ = await support.json_request(
            session,  # type: ignore[arg-type]
            "GET",
            "https://example.invalid/status",
            retry_throttled=False,
        )
        assert status == 503
        assert len(session.requests) == 1
        assert self.slept == []

    def test_max_attempts_constant_is_bounded(self) -> None:
        """Guard: attempt limit is positive and not accidentally huge."""
        assert 2 <= support._JSON_THROTTLE_MAX_ATTEMPTS <= 10


def test_json_retry_budget_cannot_exhaust_a_caller_deadline() -> None:
    """Bound the retry budget itself, not just the loop that spends it.

    The sleep is patched out in every other retry test, so nothing else notices
    if the ceiling grows. Every 503 this service emits asks for two seconds, so
    a ten second cap is generous; admitting the 120 second setup-timeout value
    here would let one request sleep for minutes and exhaust the very budgets
    these suites stopped asserting on.
    """
    worst_case = (
        support._JSON_THROTTLE_MAX_ATTEMPTS - 1
    ) * support._JSON_THROTTLE_RETRY_AFTER_MAXIMUM_SECONDS
    assert worst_case <= 30.0, (
        f"json_request can now sleep {worst_case}s across its retries."
    )
