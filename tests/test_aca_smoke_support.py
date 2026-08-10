from __future__ import annotations

import asyncio

import pytest

from tests.live import aca_smoke_support


class _EmptySandboxAdapter:
    def __init__(self) -> None:
        self.closed = False
        self.label_queries: list[dict[str, str]] = []

    async def list_sandboxes(self, *, labels: dict[str, str]) -> tuple[object, ...]:
        self.label_queries.append(labels)
        return ()

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_cleanup_requires_confirmation_when_a_create_may_have_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _EmptySandboxAdapter()
    monkeypatch.setattr(aca_smoke_support, "_LABEL_RECONCILIATION_DELAY_SECONDS", 0.0)

    with pytest.raises(RuntimeError, match="label cleanup did not find"):
        await aca_smoke_support.cleanup_sandbox(
            adapter=adapter,
            handle=None,
            sandbox_id=None,
            labels={
                "owner_kind": "aca_smoke_ci",
                "owner_hash": "owner",
                "app_hash": "app",
                "session_id": "session",
            },
            creation_attempted=True,
        )

    assert adapter.closed is True
    assert len(adapter.label_queries) == 3


@pytest.mark.asyncio
async def test_shielded_cleanup_finishes_after_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_started = asyncio.Event()
    permit_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def delayed_cleanup(**_kwargs: object) -> None:
        cleanup_started.set()
        await permit_cleanup.wait()
        cleanup_finished.set()

    monkeypatch.setattr(aca_smoke_support, "cleanup_sandbox", delayed_cleanup)
    cleanup_task = asyncio.create_task(
        aca_smoke_support._cleanup_sandbox_shielded(
            adapter=None,
            handle=None,
            sandbox_id=None,
            labels={},
            creation_attempted=False,
        )
    )
    await cleanup_started.wait()
    cleanup_task.cancel()
    permit_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await cleanup_task

    assert cleanup_finished.is_set()
