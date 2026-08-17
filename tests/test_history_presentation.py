"""Tests for :mod:`azure_functions_agents._history_presentation`."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from azure_functions_agents import _history_presentation
from azure_functions_agents._history_presentation import (
    MAX_HISTORY_REPLAY_MESSAGES,
    decode_history_jsonl,
    filter_excluded_history_messages,
    present_history_messages,
)


class _MessageFromDictSpy:
    @staticmethod
    def from_dict(payload: dict[str, Any]) -> dict[str, Any]:
        return payload


def test_decode_history_jsonl_rejects_malformed_jsonl() -> None:
    with pytest.raises(ValueError, match="history line 2"):
        decode_history_jsonl(b'{"role": "user"}\n{not json}\n')


def test_decode_history_jsonl_rejects_non_utf8_bytes() -> None:
    with pytest.raises(UnicodeDecodeError):
        decode_history_jsonl(b"\xff")


def test_decode_history_jsonl_rejects_non_mapping_payload() -> None:
    with pytest.raises(ValueError, match="did not deserialize to a mapping"):
        decode_history_jsonl('["not", "a", "mapping"]\n')


def test_decode_history_jsonl_uses_message_from_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_history_presentation, "Message", _MessageFromDictSpy)

    messages = decode_history_jsonl(b'{"role": "user", "contents": []}\n')

    assert messages == [{"role": "user", "contents": []}]


def test_filter_and_present_history_messages_preserve_source_order() -> None:
    messages = [
        SimpleNamespace(
            role="user",
            text="first",
            additional_properties={},
        ),
        SimpleNamespace(
            role="assistant",
            text="hidden",
            additional_properties={"_excluded": True},
        ),
        SimpleNamespace(
            role="tool",
            text="tool output",
            additional_properties={},
        ),
        SimpleNamespace(
            role="assistant",
            text="",
            additional_properties={},
        ),
        SimpleNamespace(
            role="assistant",
            text="second",
            additional_properties={},
        ),
    ]

    rendered, truncated = present_history_messages(filter_excluded_history_messages(messages))

    assert rendered == [
        {"role": "user", "text": "first"},
        {"role": "assistant", "text": "second"},
    ]
    assert truncated is False


def test_present_history_messages_caps_after_filtering() -> None:
    messages = [
        SimpleNamespace(
            role="user",
            text=f"excluded-{index}",
            additional_properties={"_excluded": True},
        )
        for index in range(3)
    ] + [
        SimpleNamespace(role="tool", text=f"tool-{index}", additional_properties={})
        for index in range(3)
    ] + [
        SimpleNamespace(
            role="user" if index % 2 == 0 else "assistant",
            text=f"message-{index:03d}",
            additional_properties={},
        )
        for index in range(MAX_HISTORY_REPLAY_MESSAGES + 1)
    ]

    rendered, truncated = present_history_messages(filter_excluded_history_messages(messages))

    assert truncated is True
    assert len(rendered) == MAX_HISTORY_REPLAY_MESSAGES
    assert rendered[0]["text"] == "message-001"
    assert rendered[-1]["text"] == f"message-{MAX_HISTORY_REPLAY_MESSAGES:03d}"
