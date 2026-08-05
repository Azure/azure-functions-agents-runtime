"""Canonical controller idempotency hashing and replay helpers."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass

from ..session_state import hash_idempotency_key


class IdempotencyInputError(ValueError):
    """A caller supplied an invalid idempotency attempt input."""


class IdempotencyResultUnavailableError(RuntimeError):
    """A replay identified a completed run whose retained result was evicted."""


@dataclass(frozen=True, slots=True)
class IdempotencyAttempt:
    """Hashed request identity safe to persist in a durable control record."""

    key_hash: str
    request_hash: str


def build_idempotency_attempt(
    *,
    agent_slug: str,
    prompt: str,
    timeout: float | None,
    idempotency_key: str | None,
) -> IdempotencyAttempt | None:
    """Hash one raw client key and its canonical logical submission exactly once."""
    if idempotency_key is None:
        return None
    if not isinstance(agent_slug, str) or not agent_slug:
        raise IdempotencyInputError("agent_slug must be a non-empty string")
    if not isinstance(prompt, str):
        raise IdempotencyInputError("prompt must be a string")
    if timeout is not None and (
        isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout)
    ):
        raise IdempotencyInputError("timeout must be finite when specified")
    canonical = json.dumps(
        {
            "agent_slug": agent_slug,
            "prompt": prompt,
            "timeout": timeout,
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return IdempotencyAttempt(
        key_hash=hash_idempotency_key(idempotency_key),
        request_hash=hashlib.sha256(canonical).hexdigest(),
    )
