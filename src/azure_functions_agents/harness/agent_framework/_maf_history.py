"""MAF-owned projection of persisted session history for the chat UI."""

from __future__ import annotations

from .._harness_binding import HistoryMessage, SessionHistory
from .._history_identity import validate_agent_slug

_MAX_HISTORY_REPLAY_MESSAGES = 200


async def get_session_history(agent_slug: str, session_id: str) -> SessionHistory:
    """Load the persisted MAF transcript and project visible chat messages."""
    slug = validate_agent_slug(agent_slug)
    from ._maf_blob_history import build_blob_provider_from_environment

    provider = build_blob_provider_from_environment(agent_slug=slug)
    if provider is None:
        return SessionHistory()

    provider.skip_excluded = True
    messages = await provider.get_messages(session_id)
    rendered: list[HistoryMessage] = []
    for message in messages:
        role = str(message.role or "").strip().lower()
        text = message.text
        if role not in {"user", "assistant"} or not isinstance(text, str) or not text:
            continue
        rendered.append({"role": role, "text": text})  # type: ignore[typeddict-item]

    truncated = len(rendered) > _MAX_HISTORY_REPLAY_MESSAGES
    if truncated:
        rendered = rendered[-_MAX_HISTORY_REPLAY_MESSAGES:]
    return SessionHistory(tuple(rendered), truncated)
