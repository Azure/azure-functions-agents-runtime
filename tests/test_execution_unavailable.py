"""Tests for the fail-closed ``aca_sandbox`` execution backend (FRD 0008, P2).

``UnavailableBackend`` is defense in depth: application startup
(``config.validation.validate_session_runtime``) is expected to reject an
``aca_sandbox`` ``session_runtime`` configuration before this backend could
ever be constructed in practice. These tests exercise it directly so its
fail-closed behavior has explicit, first-class coverage rather than relying
solely on the startup-gate integration tests in
``test_session_runtime_validation.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from azure_functions_agents.execution import (
    ACA_SANDBOX_EXECUTION_PROVIDER,
    DEFAULT_EXECUTION_PROVIDER,
    BackendUnavailableError,
    RunContext,
    UnavailableBackend,
    create_execution_backend,
    unavailable_backend_message,
)
from azure_functions_agents.execution.backend import StartRunRequest
from tests.test_execution_in_lang_worker import _binding


def test_unavailable_backend_message_mentions_provider_and_frd() -> None:
    message = unavailable_backend_message("aca_sandbox")
    assert message.startswith("aca_sandbox backend not available in this build")
    assert "docs/frds/0008-aca-sandbox-session-runtime.md" in message


def test_unavailable_backend_raises_on_construction() -> None:
    with pytest.raises(BackendUnavailableError) as exc_info:
        UnavailableBackend(provider="aca_sandbox")
    message = str(exc_info.value)
    assert message == unavailable_backend_message("aca_sandbox")


def test_aca_sandbox_execution_provider_constant() -> None:
    assert ACA_SANDBOX_EXECUTION_PROVIDER == "aca_sandbox"
    assert ACA_SANDBOX_EXECUTION_PROVIDER != DEFAULT_EXECUTION_PROVIDER


def test_factory_resolves_aca_sandbox_to_unavailable_backend() -> None:
    """The factory recognizes ``aca_sandbox`` but fails closed immediately.

    This is the P2 extension to ``create_execution_backend``: unlike an
    unrecognized provider string (which raises a plain ``ValueError``,
    covered in ``test_execution_in_lang_worker.py``), ``aca_sandbox`` is a *known*
    provider that resolves to :class:`UnavailableBackend`, which raises
    :class:`BackendUnavailableError` from its own ``__init__``.
    """
    with pytest.raises(BackendUnavailableError) as exc_info:
        create_execution_backend(
            binding=_binding(), provider=ACA_SANDBOX_EXECUTION_PROVIDER
        )
    assert str(exc_info.value) == unavailable_backend_message(ACA_SANDBOX_EXECUTION_PROVIDER)


def test_unavailable_backend_seam_methods_never_return_or_simulate_a_result() -> None:
    """The four Protocol seam methods are unreachable but must still fail loudly.

    Construction always raises before an instance can exist in practice, so
    these methods exist purely for structural ``AgentExecutionBackend``
    Protocol conformance under ``mypy --strict``. Bypass ``__init__`` via
    ``object.__new__`` to prove that even if somehow reached, none of them
    return, yield, or otherwise fabricate a run result -- they all raise.
    """
    backend = object.__new__(UnavailableBackend)
    context = RunContext(run_id="run-1", session_id="session-1")
    request = StartRunRequest(prompt="hello", session_id="session-1")

    with pytest.raises(BackendUnavailableError, match="unreachable"):
        asyncio.run(backend.start_run(request))

    with pytest.raises(BackendUnavailableError, match="unreachable"):
        asyncio.run(backend.get_run(context))

    with pytest.raises(BackendUnavailableError, match="unreachable"):
        asyncio.run(backend.cancel_run(context))

    with pytest.raises(BackendUnavailableError, match="unreachable"):

        async def _drain() -> list[Any]:
            return [event async for event in backend.read_events(context, 0)]

        asyncio.run(_drain())
