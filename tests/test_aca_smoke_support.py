from __future__ import annotations

import asyncio

import pytest
from azure.core.exceptions import HttpResponseError

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


class _ForbiddenSandboxAdapter:
    def __init__(self) -> None:
        self.calls = 0

    async def list_sandboxes(self, *, labels: dict[str, str]) -> tuple[object, ...]:
        self.calls += 1
        error = HttpResponseError("Operation returned an invalid status 'Forbidden'")
        error.status_code = 403
        raise error


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


@pytest.mark.asyncio
async def test_label_cleanup_aborts_immediately_on_authorization_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _ForbiddenSandboxAdapter()
    monkeypatch.setattr(aca_smoke_support, "_LABEL_RECONCILIATION_DELAY_SECONDS", 0.0)

    with pytest.raises(
        aca_smoke_support.AcaSmokeEnvironmentError, match="data-plane authorization"
    ):
        await aca_smoke_support._delete_labelled_sandboxes(
            adapter,  # type: ignore[arg-type]
            {"owner_kind": "aca_smoke_ci"},
        )

    assert adapter.calls == 1


def test_setup_error_renders_empty_cause_as_type_name() -> None:
    result = aca_smoke_support._setup_error("ACA smoke setup failed", TimeoutError())

    message = str(result)
    assert "TimeoutError" in message
    assert not message.endswith(": ")


@pytest.mark.parametrize("status_code", [403, None])
def test_setup_error_does_not_double_prefix_a_wrapped_message(status_code: int | None) -> None:
    inner = HttpResponseError(
        "ACA smoke cleanup failed: Operation returned an invalid status 'Forbidden'."
    )
    inner.status_code = status_code

    result = aca_smoke_support._setup_error("ACA smoke cleanup failed", inner)

    message = str(result)
    assert "ACA smoke cleanup failed: ACA smoke cleanup failed:" not in message
    assert message.count("ACA smoke cleanup failed:") == 1
