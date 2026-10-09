"""Shared invocation accounting and process-local agent/session serialization."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .._logger import logger
from ._provider_config import InferenceTarget, ProviderKind

if TYPE_CHECKING:
    from ._harness_binding import ExecutionRole


def _model_publisher(provider: ProviderKind | str | None) -> str | None:
    return "openai" if provider in {
        ProviderKind.OPENAI,
        ProviderKind.AZURE_OPENAI,
        ProviderKind.OPENAI.value,
        ProviderKind.AZURE_OPENAI.value,
    } else None


@dataclass
class _AgentUsageRecorder:
    """Attempt at most one internal token-usage record per invocation."""

    agent_name: str
    execution_role: ExecutionRole
    inference_target: InferenceTarget = field(default_factory=InferenceTarget)
    _emission_attempted: bool = field(default=False, init=False)

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
