"""MAF history provider that commits each hosted run's delta exactly once."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from agent_framework import HistoryProvider, Message

from .fha_private_history import (
    FhaHistoryFactory,
    FhaPrivateHistoryError,
    FhaResponsesRequestEnvelope,
)

_FHA_HISTORY_SOURCE_ID = "fha_private_history"


class FhaRunIdempotentHistoryProvider(HistoryProvider):
    """Persist MAF history in private run records before a Responses checkpoint."""

    def __init__(
        self,
        history_factory: FhaHistoryFactory,
        envelope: FhaResponsesRequestEnvelope,
    ) -> None:
        super().__init__(_FHA_HISTORY_SOURCE_ID)
        self._history_factory = history_factory
        self._envelope = envelope

    async def get_messages(
        self,
        session_id: str | None,
        *,
        state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[Message]:
        """Load all durable private MAF messages for this runtime session."""
        del state, kwargs
        self._validate_session_id(session_id)
        documents = await asyncio.to_thread(
            self._history_factory.read_history_messages,
            self._envelope,
        )
        try:
            return [Message.from_dict(document) for document in documents]
        except (TypeError, ValueError) as exc:
            raise FhaPrivateHistoryError("Hosted Responses history messages are invalid.") from exc

    async def save_messages(
        self,
        session_id: str | None,
        messages: Sequence[Message],
        *,
        state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Commit the complete MAF turn delta once under the opaque run ID."""
        del state, kwargs
        self._validate_session_id(session_id)
        if not messages:
            return
        try:
            documents = [message.to_dict() for message in messages]
        except (TypeError, ValueError) as exc:
            raise FhaPrivateHistoryError("Hosted Responses history messages are invalid.") from exc
        await asyncio.to_thread(
            self._history_factory.commit_history_messages,
            self._envelope,
            documents,
        )

    def _validate_session_id(self, session_id: str | None) -> None:
        if session_id != self._envelope.runtime_session_id:
            raise FhaPrivateHistoryError("Hosted Responses history session does not match.")
