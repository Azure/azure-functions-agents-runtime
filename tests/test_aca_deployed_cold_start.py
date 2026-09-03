from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.aca_smoke_diagnostics import AcaSmokeEnvironmentError
from tests.live import aca_deployed_cold_start_support as support


def _config(value: object) -> SimpleNamespace:
    return SimpleNamespace(getoption=lambda _: value)


def test_cold_start_sample_option_wins_and_default_is_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_ACA_COLD_START_SAMPLES", "5")

    assert support.cold_start_samples_from_option_or_environment(_config("2")) == 2
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_ACA_COLD_START_SAMPLES")
    assert support.cold_start_samples_from_option_or_environment(_config(None)) == 3


@pytest.mark.parametrize("value", ["0", "6", "three", "2.5"])
def test_cold_start_sample_option_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(AcaSmokeEnvironmentError, match="aca-cold-start-samples"):
        support.cold_start_samples_from_option_or_environment(_config(value))


def test_cold_start_report_is_aggregate_and_redacted() -> None:
    metrics = support.cold_start_metrics([1, 2, 3], [2, 3, 4], [3, 4, 5], [4, 5, 6])
    report = support.render_cold_start_report(
        sample_count=3,
        retries=1,
        metrics=metrics,
        cleanup_complete=True,
    )

    assert metrics.first_attempt_acceptance_ms == (2000, 3000, 3000)
    assert "samples=3" in report
    assert "p50=2000.0,p95=3000.0,max=3000.0" in report
    assert "cleanup=complete" in report
    assert "provenance=verified" in report
    assert all(forbidden not in report for forbidden in ("session", "run_id", "prompt", "result"))


def test_unverified_provenance_suppresses_latency_metrics() -> None:
    metrics = support.cold_start_metrics([1], [2], [3], [4])

    report = support.render_cold_start_report(
        sample_count=1,
        retries=0,
        metrics=metrics,
        cleanup_complete=True,
        provenance_verified=False,
    )

    assert "not-available" in report
    assert "p50=" not in report
    assert "provenance=unverified" in report


@pytest.mark.parametrize(
    ("status", "elapsed", "typed_deadline", "expected"),
    [
        (202, 105.0, False, None),
        (202, 105.1, False, "first_attempt_acceptance_exceeded"),
        (504, 30.0, True, "typed_setup_deadline_exceeded"),
        (503, 1.0, False, "first_attempt_http_503"),
    ],
)
def test_first_attempt_slo_classifies_pass_and_failure(
    status: int,
    elapsed: float,
    typed_deadline: bool,
    expected: str | None,
) -> None:
    assert (
        support.first_attempt_slo_failure(
            status=status,
            elapsed_seconds=elapsed,
            typed_setup_deadline=typed_deadline,
        )
        == expected
    )


@pytest.fixture
def cold_start_module(monkeypatch: pytest.MonkeyPatch) -> object:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_RUN_DEPLOYED_ACA_SMOKE", "1")
    sys.modules.pop("tests.live.test_aca_deployed_cold_start", None)
    return importlib.import_module("tests.live.test_aca_deployed_cold_start")


@pytest.mark.asyncio
async def test_typed_setup_retry_reuses_one_idempotency_key_and_keeps_first_attempt_failure(
    monkeypatch: pytest.MonkeyPatch,
    cold_start_module: object,
) -> None:
    module = cold_start_module
    posted_headers: list[dict[str, str]] = []
    responses = iter(
        [
            (504, {"error": "setup_deadline_exceeded"}, {"Retry-After": "1"}),
            (202, {"session_id": "session-a", "run_id": "run-a"}, {}),
        ]
    )

    async def request(
        _: object, method: str, __: str, *, headers: dict[str, str], payload: object = None
    ) -> tuple[int, dict[str, object], dict[str, str]]:
        if method == "POST":
            posted_headers.append(headers)
            return next(responses)
        if method == "GET":
            if __.endswith("/result"):
                return 200, {"session_id": "session-a", "run_id": "run-a", "result": {}}, {}
            return 200, {
                "session_id": "session-a",
                "run_id": "run-a",
                "state": "succeeded",
                "result_available": True,
            }, {}
        raise AssertionError("unexpected method")

    async def events(*_: object, **__: object) -> tuple[int, list[object], dict[str, str], float]:
        return 200, [SimpleNamespace(payload={"type": "done"})], {}, 2.0

    monkeypatch.setattr(module, "json_request", request)
    monkeypatch.setattr(
        module,
        "parse_accepted_run",
        lambda *_: SimpleNamespace(
            session_id="session-a",
            run_id="run-a",
            management_urls={
                "events_url": "/events",
                "status_url": "/status",
                "result_url": "/result",
            },
        ),
    )
    monkeypatch.setattr(module, "read_sse_events_with_first_event_time", events)

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)

    candidates: list[object] = []
    attempted: list[str] = []
    sample = await module._run_cold_start_sample(
        object(),
        SimpleNamespace(deployed=SimpleNamespace(chat_url="/chat", timeout_seconds=10)),
        object(),
        "partition",
        {"Authorization": "redacted"},
        candidates,
        attempted,
        module._Progress(samples=[]),
    )

    assert len(posted_headers) == 2
    assert posted_headers[0]["Idempotency-Key"] == posted_headers[1]["Idempotency-Key"]
    assert attempted == [posted_headers[0]["Idempotency-Key"]]
    assert sample.retries == 1
    assert sample.first_attempt_failure == "typed_setup_deadline_exceeded"
    assert len(candidates) == 1


@pytest.mark.asyncio
async def test_provenance_is_checked_after_measured_turn_without_value_leakage(
    monkeypatch: pytest.MonkeyPatch,
    cold_start_module: object,
) -> None:
    module = cold_start_module
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EXPECTED_BUILD_ID", "expected-build")
    monkeypatch.setenv(
        "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EXPECTED_COMMIT_SHA", "expected-commit"
    )
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EXPECTED_PYTHON_VERSION", "3.13")

    async def mismatched(*_: object) -> dict[str, object]:
        return {
            "build": {
                "marker": "present",
                "schema": 1,
                "build_id": "wrong-build",
                "commit_sha": "wrong-commit",
            },
            "runtime": {"python_version": "3.14"},
        }

    monkeypatch.setattr(module, "fetch_build_info", mismatched)
    with pytest.raises(AssertionError, match="cold_start_provenance_mismatch") as error:
        await module._verify_deployed_build("https://example.invalid", "scope/.default")

    rendered = str(error.value)
    assert "build_id,commit_sha,python_version" in rendered
    assert all(
        value not in rendered
        for value in ("expected-build", "expected-commit", "wrong-build", "wrong-commit", "3.14")
    )


def test_cold_test_calls_attestation_only_after_timing_collection() -> None:
    source = (
        Path(__file__).resolve().parent / "live" / "test_aca_deployed_cold_start.py"
    ).read_text(encoding="utf-8")
    test_body = source[
        source.index("async def test_deployed_aca_cold_start_acceptance_is_bounded_and_cleaned")
        : source.index("\n\nasync def _verify_deployed_build")
    ]

    assert test_body.index("await _run_samples_sequentially(") < test_body.index(
        "await _verify_deployed_build("
    )
    assert "fetch_build_info(" not in test_body


@pytest.mark.asyncio
async def test_samples_are_orchestrated_strictly_sequentially(
    monkeypatch: pytest.MonkeyPatch,
    cold_start_module: object,
) -> None:
    module = cold_start_module
    active = 0
    maximum_active = 0

    async def sample(*_: object) -> object:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await module.asyncio.sleep(0)
        active -= 1
        return object()

    monkeypatch.setattr(module, "_run_cold_start_sample", sample)

    progress = module._Progress(samples=[])
    result = await module._run_samples_sequentially(
        object(), object(), object(), "partition", {}, 3, [], [], progress
    )

    assert result is None
    assert len(progress.samples) == 3
    assert maximum_active == 1


@pytest.mark.asyncio
async def test_cleanup_recovery_collects_unresolved_keys_and_cleans_known_candidates(
    monkeypatch: pytest.MonkeyPatch,
    cold_start_module: object,
) -> None:
    module = cold_start_module
    existing = module._Candidate(SimpleNamespace(session_id="one", run_id="one"), "known")
    recovered = module._Candidate(SimpleNamespace(session_id="two", run_id="two"), "missing")

    async def recover(*_: object, **kwargs: object) -> object:
        return recovered if kwargs.get("deadline") is not None else None

    monkeypatch.setattr(module, "_recover_candidate", recover)
    candidates = [existing]

    unresolved = await module._recover_cleanup_candidates(
        object(), object(), "partition", ["known", "missing"], candidates
    )

    assert candidates == [existing, recovered]
    assert unresolved == ()


@pytest.mark.asyncio
async def test_final_recovery_waits_for_a_late_owner_idempotency_record(
    monkeypatch: pytest.MonkeyPatch,
    cold_start_module: object,
) -> None:
    module = cold_start_module
    records = iter([None, None, SimpleNamespace(session_id="late", run_id="run")])
    calls = 0

    async def read_record(*_: object, **__: object) -> object:
        nonlocal calls
        calls += 1
        return next(records)

    delays: list[float] = []

    async def no_sleep(delay: float) -> None:
        delays.append(delay)
        return None

    monkeypatch.setattr(module, "read_owner_idempotency", read_record)
    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    candidate = await module._recover_candidate(
        object(),
        SimpleNamespace(
            deployed=SimpleNamespace(
                management_urls=lambda *, session_id, run_id: {
                    "status_url": f"/{session_id}/{run_id}"
                }
            )
        ),
        "partition",
        "key",
        deadline=module.time.perf_counter() + 60,
    )

    assert candidate is not None
    assert calls == 3
    assert delays == [1.0, 1.0]


@pytest.mark.asyncio
async def test_unresolved_final_recovery_is_collected_without_key_leakage(
    monkeypatch: pytest.MonkeyPatch,
    cold_start_module: object,
) -> None:
    module = cold_start_module

    async def missing(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(module, "_recover_candidate", missing)
    unresolved = await module._recover_cleanup_candidates(
        object(), object(), "partition", ["secret-idempotency-key"], []
    )

    assert unresolved == ("secret-idempotency-key",)


@pytest.mark.asyncio
async def test_mixed_recovery_still_cleans_every_known_candidate(
    monkeypatch: pytest.MonkeyPatch,
    cold_start_module: object,
) -> None:
    module = cold_start_module
    existing = module._Candidate(SimpleNamespace(session_id="known", run_id="known"), "known")
    recovered = module._Candidate(SimpleNamespace(session_id="recovered", run_id="recovered"), "recover")

    async def recover(*args: object, **__: object) -> object:
        return recovered if args[3] == "recover" else None

    async def read_session(*_: object, **kwargs: object) -> object:
        return SimpleNamespace(session_id=kwargs["session_id"])

    cleaned: list[str] = []

    async def cleanup(_: object, *, session: object, **__: object) -> None:
        cleaned.append(session.session_id)

    async def no_sandbox(*_: object, **__: object) -> None:
        return None

    async def no_snapshots(*_: object, **__: object) -> tuple[()]:
        return ()

    monkeypatch.setattr(module, "_recover_candidate", recover)
    monkeypatch.setattr(module, "read_authoritative_session", read_session)
    monkeypatch.setattr(module, "assert_session_belongs_to_deployment", lambda *_: None)
    monkeypatch.setattr(module, "cleanup_owned_lifecycle_session", cleanup)
    monkeypatch.setattr(module, "owned_sandbox", no_sandbox)
    monkeypatch.setattr(module, "owned_snapshots", no_snapshots)
    candidates = [existing]

    unresolved = await module._recover_cleanup_candidates(
        object(), object(), "partition", ["known", "recover", "missing"], candidates
    )
    await module._cleanup_candidates(object(), object(), "partition", candidates)

    assert unresolved == ("missing",)
    assert cleaned == ["known", "recovered"]


@pytest.mark.asyncio
async def test_hanging_candidate_cleanup_becomes_sanitized_timeout(
    monkeypatch: pytest.MonkeyPatch,
    cold_start_module: object,
) -> None:
    module = cold_start_module
    never = module.asyncio.Event()
    candidate = module._Candidate(SimpleNamespace(session_id="secret", run_id="secret"), "key")

    async def hanging_read(*_: object, **__: object) -> object:
        await never.wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(module, "_CLEANUP_CANDIDATE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(module, "read_authoritative_session", hanging_read)
    with pytest.raises(AcaSmokeEnvironmentError, match="cold_start_cleanup_candidate_timeout") as error:
        await module._cleanup_candidates(object(), object(), "partition", [candidate])

    assert "secret" not in str(error.value)


def test_sanitized_public_validator_does_not_print_response_values(cold_start_module: object) -> None:
    module = cold_start_module
    candidate = module._Candidate(
        SimpleNamespace(session_id="secret-session", run_id="secret-run"), "secret-key"
    )

    with pytest.raises(AssertionError, match=r"^cold_start_public_status_invalid$") as error:
        module._validate_public_status(
            500,
            {"session_id": "secret-session", "error": "secret-model-output"},
            candidate,
        )

    assert "secret" not in str(error.value)


def test_cleanup_error_retains_safe_category_without_cause_details(cold_start_module: object) -> None:
    module = cold_start_module
    sanitized = module._sanitized_cleanup_error(
        AcaSmokeEnvironmentError("cold_start_cleanup_unresolved_idempotency")
    )

    assert str(sanitized) == "ACA-SMOKE-ENV: cold_start_cleanup_incomplete_unresolved_idempotency"


@pytest.mark.asyncio
async def test_cleanup_wrapper_suppresses_selector_bearing_cause(
    monkeypatch: pytest.MonkeyPatch,
    cold_start_module: object,
) -> None:
    module = cold_start_module
    candidate = module._Candidate(
        SimpleNamespace(session_id="secret-session", run_id="secret-run"), "secret-key"
    )

    async def bad_read(*_: object, **__: object) -> object:
        raise AcaSmokeEnvironmentError("selector=secret-session")

    monkeypatch.setattr(module, "read_authoritative_session", bad_read)
    with pytest.raises(AcaSmokeEnvironmentError, match="cold_start_cleanup_controller_failed") as error:
        await module._cleanup_candidates(object(), object(), "partition", [candidate])

    assert "secret-session" not in str(error.value)


@pytest.mark.asyncio
async def test_later_sample_failure_preserves_partial_metrics_and_retries(
    monkeypatch: pytest.MonkeyPatch,
    cold_start_module: object,
) -> None:
    module = cold_start_module
    candidate = module._Candidate(SimpleNamespace(session_id="one", run_id="one"), "key")
    completed = module._Sample(candidate, 202, 1.0, 2.0, 3.0, 4.0, 2, True, None)
    calls = 0

    async def sample(*_: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            return completed
        raise AcaSmokeEnvironmentError("cold_start_sample_timeout")

    monkeypatch.setattr(module, "_run_cold_start_sample", sample)
    progress = module._Progress(samples=[], retries=2)
    with pytest.raises(AcaSmokeEnvironmentError, match="cold_start_sample_timeout"):
        await module._run_samples_sequentially(
            object(), object(), object(), "partition", {}, 3, [], [], progress
        )

    report = support.render_cold_start_report(
        sample_count=3,
        retries=progress.retries,
        metrics=support.cold_start_metrics(
            [item.first_attempt_acceptance_seconds for item in progress.samples],
            [item.total_acceptance_seconds for item in progress.samples],
            [item.first_event_seconds for item in progress.samples],
            [item.terminal_seconds for item in progress.samples],
        ),
        cleanup_complete=False,
    )
    assert progress.samples == [completed]
    assert "samples=3 retries=2" in report
    assert "first_attempt_acceptance_ms=p50=1000.0" in report
