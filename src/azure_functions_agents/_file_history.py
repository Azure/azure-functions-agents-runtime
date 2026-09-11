"""Agent-scoped local file history with legacy path detection."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from agent_framework import FileHistoryProvider, Message

from ._history_identity import validate_agent_slug, warn_legacy_history_detected


class ScopedFileHistoryProvider(FileHistoryProvider):
    """Store local history under ``{storage_root}/{agent_slug}/{session_id}.jsonl``."""

    def __init__(
        self,
        storage_root: str | Path,
        *,
        agent_slug: str,
        **kwargs: Any,
    ) -> None:
        self._agent_slug = validate_agent_slug(agent_slug)
        self._legacy_provider = FileHistoryProvider(storage_path=storage_root)
        super().__init__(
            storage_path=Path(storage_root) / self._agent_slug,
            **kwargs,
        )

    async def get_messages(
        self,
        session_id: str | None,
        *,
        state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[Message]:
        scoped_path = self._session_file_path(session_id)
        messages = await super().get_messages(session_id, state=state, **kwargs)
        if scoped_path.exists():
            return messages

        legacy_path = self._legacy_provider._session_file_path(session_id)
        try:
            legacy_exists = await asyncio.to_thread(legacy_path.is_file)
        except OSError:
            return messages
        if legacy_exists:
            warn_legacy_history_detected(
                agent_slug=self._agent_slug,
                backend="file",
            )
        return messages
