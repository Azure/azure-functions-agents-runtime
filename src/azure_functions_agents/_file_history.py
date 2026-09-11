"""Agent-scoped local file history."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_framework import FileHistoryProvider

from ._history_identity import validate_agent_slug


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
        super().__init__(
            storage_path=Path(storage_root) / self._agent_slug,
            **kwargs,
        )
