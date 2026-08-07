from __future__ import annotations

import json
from pathlib import Path

import pytest

from azure_functions_agents.conformance.diff import semantic_diff
from azure_functions_agents.conformance.trace import (
    TraceValidationError,
    normalize_trace,
    parse_trace,
)

_TRACE_DIRECTORY = Path(__file__).parent / "conformance" / "traces"


def test_all_golden_traces_are_valid_semantic_documents() -> None:
    traces = [parse_trace(path.read_bytes()) for path in sorted(_TRACE_DIRECTORY.glob("*.json"))]

    assert len(traces) == 13
    assert {trace.name for trace in traces} >= {"bootstrap_ready", "delegation", "egress"}


def test_semantic_normalization_ignores_model_wording_and_timing() -> None:
    expected = parse_trace(
        {
            "name": "sample",
            "capabilities": [],
            "events": [
                {
                    "type": "message",
                    "data": {
                        "content": "first answer",
                        "timestamp": "earlier",
                        "stable": True,
                    },
                }
            ],
            "terminal_state": "succeeded",
        }
    )
    actual = parse_trace(
        {
            "name": "sample",
            "capabilities": [],
            "events": [
                {
                    "type": "message",
                    "data": {
                        "content": "different answer",
                        "timestamp": "later",
                        "stable": True,
                    },
                }
            ],
            "terminal_state": "succeeded",
        }
    )

    assert normalize_trace(expected).events[0].data == {"stable": True}
    assert semantic_diff(expected, actual) == ()


def test_semantic_diff_preserves_event_order_and_terminal_state() -> None:
    expected = parse_trace(
        {
            "name": "sample",
            "capabilities": [],
            "events": [{"type": "message", "data": {}}],
            "terminal_state": "succeeded",
        }
    )
    actual = parse_trace(
        {
            "name": "sample",
            "capabilities": [],
            "events": [{"type": "error", "data": {}}],
            "terminal_state": "failed",
        }
    )

    differences = semantic_diff(expected, actual)

    assert {difference.path for difference in differences} == {
        "events[0].type",
        "terminal_state",
    }


def test_trace_parser_rejects_duplicate_keys() -> None:
    payload = (
        b'{"name":"first","name":"second","capabilities":[],"events":[],'
        b'"terminal_state":"succeeded"}'
    )

    with pytest.raises(TraceValidationError):
        parse_trace(payload)


def test_trace_fixtures_are_valid_json() -> None:
    for path in _TRACE_DIRECTORY.glob("*.json"):
        assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict)
