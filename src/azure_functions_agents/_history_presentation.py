"""Pure decoding and presentation helpers for persisted agent history."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping

from agent_framework import Message

MAX_HISTORY_REPLAY_MESSAGES = 200


def decode_history_jsonl(
    content: str | bytes,
    *,
    source: str | None = None,
) -> list[Message]:
    """Decode strict UTF-8 JSONL history into MAF messages."""
    text = content if isinstance(content, str) else content.decode("utf-8")
    messages: list[Message] = []
    for line_number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError as exc:
            detail = (
                f"Failed to deserialize history line {line_number}."
                if source is None
                else f"Failed to deserialize history line {line_number} from {source}."
            )
            raise ValueError(detail) from exc
        if not isinstance(payload, Mapping):
            detail = (
                f"History line {line_number} did not deserialize to a mapping."
                if source is None
                else f"History line {line_number} in {source} did not deserialize to a mapping."
            )
            raise ValueError(detail)
        messages.append(Message.from_dict(dict(payload)))
    return messages


def filter_excluded_history_messages(messages: Iterable[Message]) -> list[Message]:
    """Exclude messages marked as internal by the history provider."""
    return [
        message
        for message in messages
        if not message.additional_properties.get("_excluded", False)
    ]


def present_history_messages(
    messages: Iterable[Message],
    *,
    limit: int = MAX_HISTORY_REPLAY_MESSAGES,
) -> tuple[list[dict[str, str]], bool]:
    """Render the user-visible message transcript in source order."""
    rendered: list[dict[str, str]] = []
    for message in messages:
        role = str(getattr(message, "role", "") or "").strip().lower()
        if role not in ("user", "assistant"):
            continue
        text = getattr(message, "text", "")
        if not isinstance(text, str) or not text:
            continue
        rendered.append({"role": role, "text": text})

    truncated = len(rendered) > limit
    return (rendered[-limit:] if truncated else rendered, truncated)
