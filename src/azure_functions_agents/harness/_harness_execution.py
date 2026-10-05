"""Shared invocation accounting and process-local agent/session serialization."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .._logger import logger
from ..client_manager import InferenceTarget

if TYPE_CHECKING:
    from ._harness_binding import ExecutionRole

_USAGE_FIELD_NAMES: dict[str, str] = {
    "input_token_count": "input_tokens",
    "output_token_count": "output_tokens",
}


def _normalize_usage_details(usage_details: Any) -> dict[str, int]:
    """Return the valid canonical token counts reported by MAF."""
    if not isinstance(usage_details, Mapping):
        return {}

    normalized: dict[str, int] = {}
    for source_name, record_name in _USAGE_FIELD_NAMES.items():
        value = usage_details.get(source_name)
        if (
            record_name not in normalized
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
        ):
            normalized[record_name] = value
    return normalized


def _model_publisher(provider: str | None) -> str | None:
    return "openai" if provider in {"openai", "azure_openai"} else None


@dataclass
class _AgentUsageRecorder:
    """Attempt at most one internal token-usage record per invocation."""

    agent_name: str
    execution_role: ExecutionRole
    inference_target: InferenceTarget = field(default_factory=InferenceTarget)
    _emission_attempted: bool = field(default=False, init=False)

    def emit(self, usage_details: Any = None) -> None:
        try:
            usage = _normalize_usage_details(usage_details)
        except Exception:
            usage = {}
        self.emit_counts(
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
        )

    def emit_counts(self, *, input_tokens: int | None, output_tokens: int | None) -> None:
        """Record backend-neutral token counts without a MAF-shaped intermediate."""
        if self._emission_attempted:
            return
        self._emission_attempted = True

        try:
            payload: dict[str, Any] = {
                "agent_name": self.agent_name,
                "event_name": "agent_token_usage",
                "execution_role": self.execution_role,
                "input_tokens": input_tokens,
                "model": self.inference_target.model,
                "model_publisher": _model_publisher(self.inference_target.provider),
                "output_tokens": output_tokens,
                "provider": self.inference_target.provider,
            }
            logger.info(
                "Agent token usage: %s",
                json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
            )
        except Exception:
            return


_SESSION_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}
_SESSION_LOCKS_GUARD = asyncio.Lock()


async def _get_session_lock(session_id: str, agent_slug: str = "main") -> asyncio.Lock:
    key = (agent_slug, session_id)
    async with _SESSION_LOCKS_GUARD:
        lock = _SESSION_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _SESSION_LOCKS[key] = lock
        return lock


@contextlib.asynccontextmanager
async def _session_lock_bounded_by(
    session_id: str,
    deadline: float,
    *,
    agent_slug: str = "main",
) -> AsyncIterator[None]:
    """Bound the existing same-agent/session lock wait by the invocation deadline."""
    lock = await _get_session_lock(session_id, agent_slug)
    loop = asyncio.get_running_loop()
    await asyncio.wait_for(lock.acquire(), timeout=max(0.0, deadline - loop.time()))
    try:
        yield
    finally:
        lock.release()
