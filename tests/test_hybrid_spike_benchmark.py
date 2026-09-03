import argparse

import pytest
from eng.scripts.hybrid_spike_benchmark import (
    _percentiles,
    _request_succeeded,
    _run,
    _SseTerminalTracker,
)


def test_hybrid_benchmark_percentiles_use_nearest_rank() -> None:
    values = _percentiles([0.001, 0.002, 0.003, 0.004])

    assert values.p50 == 2
    assert values.p95 == 4
    assert values.p99 == 4


@pytest.mark.asyncio
async def test_hybrid_benchmark_requires_explicit_25_approval() -> None:
    args = argparse.Namespace(
        concurrency=25,
        requests=1,
        allow_25=False,
    )

    with pytest.raises(ValueError, match="stable evidence"):
        await _run(args)


def test_hybrid_benchmark_accepts_split_sse_done_event() -> None:
    tracker = _SseTerminalTracker()

    tracker.feed(b'data: {\"type\":\"do')
    tracker.feed(b'ne\"}\r')
    tracker.feed(b"\n\r\n")
    tracker.finish()

    assert tracker.terminal_event == "done"
    assert _request_succeeded(200, stream=True, terminal_event=tracker.terminal_event)


@pytest.mark.parametrize(
    ("payload", "terminal_event"),
    [
        (b'data: {\"type\":\"error\",\"content\":\"failed\"}\n\n', "error"),
        (b'data: {\"type\":\"delta\",\"content\":\"partial\"}\n\n', None),
        (b"data: not-json\n\n", "error"),
        (b'data: {\"type\":\"delta\",\"content\":\"\xe2', "error"),
    ],
)
def test_hybrid_benchmark_rejects_unsuccessful_stream_terminal_state(
    payload: bytes,
    terminal_event: str | None,
) -> None:
    tracker = _SseTerminalTracker()

    tracker.feed(payload)
    tracker.finish()

    assert tracker.terminal_event == terminal_event
    assert not _request_succeeded(200, stream=True, terminal_event=tracker.terminal_event)
