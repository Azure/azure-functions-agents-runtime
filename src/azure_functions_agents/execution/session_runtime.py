"""Provider-neutral app-scoped execution runtime types."""

from __future__ import annotations

from typing import TypeGuard

from ..controller.readiness import SessionRuntimeBinding
from .foundry_responses_runtime import FoundryResponsesRuntime

type SessionExecutionRuntime = SessionRuntimeBinding | FoundryResponsesRuntime


def is_aca_sandbox_runtime(runtime: SessionExecutionRuntime) -> TypeGuard[SessionRuntimeBinding]:
    """Return whether a runtime owns ACA sandbox lifecycle semantics."""
    return isinstance(runtime, SessionRuntimeBinding)


def is_foundry_responses_runtime(
    runtime: SessionExecutionRuntime,
) -> TypeGuard[FoundryResponsesRuntime]:
    """Return whether a runtime owns Foundry Hosted Agent Responses semantics."""
    return isinstance(runtime, FoundryResponsesRuntime)
