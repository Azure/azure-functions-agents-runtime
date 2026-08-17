from __future__ import annotations

import asyncio
from urllib.error import URLError

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


def test_aca_smoke_run_id_uses_the_seeded_ci_identifier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(aca_smoke_support.ACA_SMOKE_RUN_ID_ENV_VAR, "123456")

    assert aca_smoke_support.aca_smoke_run_id() == "123456"


def test_aca_smoke_run_id_sanitizes_and_bounds_the_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(aca_smoke_support.ACA_SMOKE_RUN_ID_ENV_VAR, "-Build/ID_" + "9" * 40)

    run_id = aca_smoke_support.aca_smoke_run_id()

    assert run_id == "buildid9999999999"[: aca_smoke_support._MAX_RUN_ID_LENGTH]
    assert set(run_id) <= set("abcdefghijklmnopqrstuvwxyz0123456789-")
    assert not run_id.startswith("-")


def test_aca_smoke_run_id_falls_back_to_a_unique_local_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(aca_smoke_support.ACA_SMOKE_RUN_ID_ENV_VAR, raising=False)

    first = aca_smoke_support.aca_smoke_run_id()
    second = aca_smoke_support.aca_smoke_run_id()

    assert first and second
    assert first != second


def test_session_belongs_to_run_matches_only_its_own_run() -> None:
    assert aca_smoke_support.session_belongs_to_run(
        {"session_id": "123456-aca-harness-smoke-0011223344556677"}, "123456"
    )
    # A different run's sandbox must never be selected for deletion.
    assert not aca_smoke_support.session_belongs_to_run(
        {"session_id": "999999-aca-harness-smoke-0011223344556677"}, "123456"
    )
    # A run id that is a numeric prefix of another must not match across the delimiter.
    assert not aca_smoke_support.session_belongs_to_run(
        {"session_id": "1234567-aca-harness-smoke-0011223344556677"}, "123456"
    )
    assert not aca_smoke_support.session_belongs_to_run({}, "123456")


def test_history_smoke_config_rejects_missing_endpoint_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        aca_smoke_support,
        "aca_smoke_config_from_environment",
        lambda: aca_smoke_support.AcaSmokeConfig(group_resource_id="group", disk="python-3.13"),
    )
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_ACA_HISTORY_SMOKE_BASE_URL", raising=False)

    with pytest.raises(aca_smoke_support.AcaSmokeEnvironmentError, match="BASE_URL"):
        aca_smoke_support.aca_history_smoke_config_from_environment()


def test_history_smoke_config_validates_and_normalizes_deployed_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aca = aca_smoke_support.AcaSmokeConfig(group_resource_id="group", disk="python-3.13")
    monkeypatch.setattr(aca_smoke_support, "aca_smoke_config_from_environment", lambda: aca)
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_ACA_HISTORY_SMOKE_BASE_URL", "https://example.test/")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_ACA_HISTORY_SMOKE_AGENT_SLUG", "test-agent")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_ACA_HISTORY_SMOKE_FUNCTION_KEY", "key")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_ACA_HISTORY_SMOKE_RESUMED_SESSION_ID", "resumed")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_ACA_HISTORY_SMOKE_GONE_SESSION_ID", "gone")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_ACA_HISTORY_SMOKE_UNAVAILABLE_SESSION_ID", "unavailable")

    config = aca_smoke_support.aca_history_smoke_config_from_environment()

    assert config.aca is aca
    assert config.base_url == "https://example.test"
    assert config.agent_slug == "test-agent"
    assert config.resumed_session_id == "resumed"


@pytest.mark.asyncio
async def test_history_smoke_transport_failure_is_an_environment_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = aca_smoke_support.AcaHistorySmokeConfig(
        aca=aca_smoke_support.AcaSmokeConfig(group_resource_id="group", disk="python-3.13"),
        base_url="https://example.test",
        agent_slug="test-agent",
        function_key="key",
        resumed_session_id="resumed",
        gone_session_id="gone",
        unavailable_session_id="unavailable",
    )

    def fail_request(_request: object) -> aca_smoke_support.AcaHistorySmokeResponse:
        raise URLError("network unavailable")

    monkeypatch.setattr(aca_smoke_support, "_request_aca_history_smoke", fail_request)

    with pytest.raises(aca_smoke_support.AcaSmokeEnvironmentError, match="could not be reached"):
        await aca_smoke_support.request_aca_history_smoke(
            config,
            method="GET",
            path="/agents/test-agent/history",
        )
