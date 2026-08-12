from __future__ import annotations

import ast
import importlib
import runpy
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.aca_smoke_diagnostics import AcaSmokeEnvironmentError
from tests.live import aca_deployed_load_support as support


def _config(value: object) -> SimpleNamespace:
    return SimpleNamespace(getoption=lambda _: value)


def test_load_concurrency_cli_option_wins_over_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_ACA_LOAD_CONCURRENCY", "100")

    assert support.load_concurrency_from_option_or_environment(_config("3")) == 3


@pytest.mark.parametrize("value", ["0", "101", "three", "2.5"])
def test_load_concurrency_rejects_invalid_explicit_values(value: str) -> None:
    with pytest.raises(AcaSmokeEnvironmentError, match="aca-load-concurrency"):
        support.load_concurrency_from_option_or_environment(_config(value))


def test_load_concurrency_uses_environment_and_omission_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_ACA_LOAD_CONCURRENCY", "100")
    assert support.load_concurrency_from_option_or_environment(_config(None)) == 100
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_ACA_LOAD_CONCURRENCY")
    with pytest.raises(pytest.skip.Exception):
        support.require_load_concurrency(_config(None))


def test_load_concurrency_rejects_invalid_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_ACA_LOAD_CONCURRENCY", "101")

    with pytest.raises(AcaSmokeEnvironmentError, match="ACA_LOAD_CONCURRENCY"):
        support.load_concurrency_from_option_or_environment(_config(None))


def test_load_orchestration_preserves_public_and_read_only_boundaries() -> None:
    source = (Path(__file__).parent / "live" / "test_aca_deployed_load.py").read_text()

    assert "SandboxRunControl" not in source
    assert "AcaSandboxAdapter" not in source
    assert "FunctionAppOwnerContext" not in source
    assert "EntraUserOwnerContext" in source
    assert "create_entity" not in source
    assert "upsert_entity" not in source
    assert "_POLL_SECONDS = 1.0" in source
    assert "active_run_exists" in source
    assert "Idempotency-Key" in source


def test_load_percentiles_and_report_are_aggregate_and_redacted() -> None:
    metrics = support.latency_metrics([1, 2, 3, 4], [2, 3, 4, 5], [3, 4, 5, 6])
    report = support.render_load_report(
        concurrency=4,
        common_interval=support.CommonActiveInterval(
            started_at=support.utc_now(),
            ended_at=support.utc_now(),
        ),
        admitted_count=4,
        succeeded_count=4,
        metrics=metrics,
        replay_count=4,
        active_run_conflict_count=4,
        retry_count=0,
        unclassified_service_throttle_count=0,
        unresolved_idempotency_count=0,
        cleanup_complete=True,
    )

    assert metrics.submission_ms == (2000, 4000, 4000)
    assert "N=4" in report
    assert "p50=2000.0" in report
    assert "session" not in report
    assert "run_id" not in report
    assert "unclassified_service_throttles=0" in report
    assert "unresolved_idempotencies=0" in report


def test_load_module_compiles_and_skips_when_the_deployed_opt_in_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_RUN_DEPLOYED_ACA_SMOKE", raising=False)
    source_path = Path(__file__).parent / "live" / "test_aca_deployed_load.py"
    compile(source_path.read_text(), str(source_path), "exec")
    sys.modules.pop("tests.live.test_aca_deployed_load", None)
    with pytest.raises(pytest.skip.Exception):
        importlib.import_module("tests.live.test_aca_deployed_load")


def test_common_interval_call_matches_its_function_signature() -> None:
    source_path = Path(__file__).parent / "live" / "test_aca_deployed_load.py"
    tree = ast.parse(source_path.read_text())
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_establish_common_active_interval"
    )
    call = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_establish_common_active_interval"
    )

    assert len(call.args) == len(function.args.args)


def test_load_admission_preserves_successes_before_aggregate_failure() -> None:
    source = (Path(__file__).parent / "live" / "test_aca_deployed_load.py").read_text()
    assert "SandboxRunControl" not in source
    assert "AcaSandboxAdapter" not in source


@pytest.fixture
def load_module(monkeypatch: pytest.MonkeyPatch) -> object:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_RUN_DEPLOYED_ACA_SMOKE", "1")
    sys.modules.pop("tests.live.test_aca_deployed_load", None)
    return importlib.import_module("tests.live.test_aca_deployed_load")


@pytest.mark.asyncio
async def test_admission_aggregate_preserves_mixed_candidates(
    monkeypatch: pytest.MonkeyPatch,
    load_module: object,
) -> None:
    module = load_module
    candidate = module._SubmittedRun(  # type: ignore[attr-defined]
        accepted=SimpleNamespace(session_id="session-a", run_id="run-a"),
        idempotency_key="a",
        submitted_at=1.0,
        accepted_at=2.0,
    )
    outcomes = iter(
        [
            module._AdmissionOutcome("a", candidate, 0, 0, None, False),  # type: ignore[attr-defined]
            module._AdmissionOutcome("b", None, 1, 0, "setup_deadline_exceeded", True),  # type: ignore[attr-defined]
        ]
    )

    async def submit_one(*_: object) -> object:
        return next(outcomes)

    monkeypatch.setattr(module, "_submit_one", submit_one)
    submitted: list[object] = []
    with pytest.raises(module._AdmissionFailureError) as failure:  # type: ignore[attr-defined]
        await module._submit_distinct_sessions(  # type: ignore[attr-defined]
            object(), SimpleNamespace(), {}, 2, submitted, object(), "partition"
        )

    assert submitted == [candidate]
    assert failure.value.retries == 1
    assert failure.value.unresolved_idempotencies == 1
    assert failure.value.attempted_idempotency_keys == ("a", "b")


@pytest.mark.asyncio
async def test_final_setup_deadline_recovers_cleanup_candidate(
    monkeypatch: pytest.MonkeyPatch,
    load_module: object,
) -> None:
    module = load_module
    deployed = SimpleNamespace(
        chat_url="https://example.test/chat",
        management_urls=lambda *, session_id, run_id: {"events_url": f"/{session_id}/{run_id}"},
    )

    async def json_504(*_: object, **__: object) -> tuple[int, dict[str, str], dict[str, str]]:
        return 504, {"error": "setup_deadline_exceeded"}, {}

    async def recovered(*_: object, **__: object) -> SimpleNamespace:
        return SimpleNamespace(session_id="session-recovered", run_id="run-recovered")

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(module, "json_request", json_504)
    monkeypatch.setattr(module, "read_owner_idempotency", recovered)
    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    keys: list[str] = []
    outcome = await module._submit_one(  # type: ignore[attr-defined]
        object(), SimpleNamespace(deployed=deployed), {}, object(), "partition", keys
    )

    assert outcome.failure == "setup_deadline_exceeded"
    assert outcome.submitted is not None
    assert outcome.submitted.accepted.session_id == "session-recovered"
    assert not outcome.unresolved_idempotency
    assert len(keys) == 1


@pytest.mark.asyncio
async def test_unresolved_ambiguous_admission_is_counted(
    monkeypatch: pytest.MonkeyPatch,
    load_module: object,
) -> None:
    module = load_module

    async def no_record(*_: object, **__: object) -> None:
        return None

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(module, "read_owner_idempotency", no_record)
    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    recovered = await module._recover_submitted_run(  # type: ignore[attr-defined]
        object(),
        SimpleNamespace(deployed=SimpleNamespace(management_urls=lambda **_: {})),
        "partition",
        "raw-key",
        1.0,
    )

    assert recovered is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 503, 500, 504])
async def test_ambiguous_service_admissions_recover_or_mark_unresolved(
    monkeypatch: pytest.MonkeyPatch,
    load_module: object,
    status: int,
) -> None:
    module = load_module
    deployed = SimpleNamespace(
        chat_url="https://example.test/chat",
        management_urls=lambda *, session_id, run_id: {"events_url": f"/{session_id}/{run_id}"},
    )

    async def recovered(*_: object, **__: object) -> SimpleNamespace:
        return SimpleNamespace(session_id="session-recovered", run_id="run-recovered")

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(module, "read_owner_idempotency", recovered)
    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    outcome = await module._recover_ambiguous_http_outcome(  # type: ignore[attr-defined]
        object(),
        SimpleNamespace(deployed=deployed),
        "partition",
        "raw-key",
        1.0,
        0,
        status,
        unclassified_service_throttles=int(status in {429, 503}),
    )
    assert outcome.submitted is not None
    assert not outcome.unresolved_idempotency
    assert outcome.unclassified_service_throttles == int(status in {429, 503})

    async def no_record(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(module, "read_owner_idempotency", no_record)
    unresolved = await module._recover_ambiguous_http_outcome(  # type: ignore[attr-defined]
        object(),
        SimpleNamespace(deployed=deployed),
        "partition",
        "raw-key",
        1.0,
        0,
        status,
        unclassified_service_throttles=int(status in {429, 503}),
    )
    assert unresolved.submitted is None
    assert unresolved.unresolved_idempotency


@pytest.mark.asyncio
async def test_cleanup_accepts_empty_snapshot_tuple_and_propagates_errors(
    monkeypatch: pytest.MonkeyPatch,
    load_module: object,
) -> None:
    module = load_module
    submitted = [
        module._SubmittedRun(  # type: ignore[attr-defined]
            accepted=SimpleNamespace(session_id="session-a", run_id="run-a"),
            idempotency_key="a",
            submitted_at=1.0,
            accepted_at=2.0,
        )
    ]
    session = SimpleNamespace()

    async def read_session(*_: object, **__: object) -> object:
        return session

    async def clean(*_: object, **__: object) -> None:
        return None

    async def no_sandbox(*_: object, **__: object) -> None:
        return None

    async def empty_snapshots(*_: object, **__: object) -> tuple[()]:
        return ()

    monkeypatch.setattr(module, "read_authoritative_session", read_session)
    monkeypatch.setattr(module, "cleanup_owned_lifecycle_session", clean)
    monkeypatch.setattr(module, "owned_sandbox", no_sandbox)
    monkeypatch.setattr(module, "owned_snapshots", empty_snapshots)
    assert await module._cleanup_load_sessions(  # type: ignore[attr-defined]
        object(), object(), "partition", submitted
    )

    async def cleanup_failure(*_: object, **__: object) -> None:
        raise AcaSmokeEnvironmentError("tombstone failed")

    monkeypatch.setattr(module, "cleanup_owned_lifecycle_session", cleanup_failure)
    with pytest.raises(AcaSmokeEnvironmentError, match="Controller tombstone"):
        await module._cleanup_load_sessions(  # type: ignore[attr-defined]
            object(), object(), "partition", submitted
        )


def test_primary_product_failure_is_not_masked_by_cleanup_failure(load_module: object) -> None:
    module = load_module

    def product_failure() -> None:
        raise AssertionError("product failure")

    def cleanup_failure() -> None:
        raise AcaSmokeEnvironmentError("cleanup failure")

    with pytest.raises(AssertionError, match="product failure") as failure:
        try:
            product_failure()
        except AssertionError:
            primary = sys.exception()
            try:
                cleanup_failure()
            except AcaSmokeEnvironmentError as cleanup_error:
                module._raise_or_note_cleanup_failure(primary, cleanup_error)  # type: ignore[attr-defined]
            raise

    assert any("cleanup also failed" in note for note in failure.value.__notes__)


def test_overlap_math_and_hold_budget_margin(load_module: object) -> None:
    module = load_module
    now = datetime.now(UTC)
    first = [
        module._ActiveObservation(0, 2, now, now),  # type: ignore[attr-defined]
        module._ActiveObservation(0, 3, now, now),  # type: ignore[attr-defined]
    ]
    second = [
        module._ActiveObservation(5, 6, now, now),  # type: ignore[attr-defined]
        module._ActiveObservation(4, 6, now, now),  # type: ignore[attr-defined]
    ]
    assert module._overlapping_interval(first, second) is not None  # type: ignore[attr-defined]
    assert module._overlapping_interval(second, first) is None  # type: ignore[attr-defined]

    module._assert_remaining_hold_budget(  # type: ignore[attr-defined]
        [SimpleNamespace(accepted_at=0.0), SimpleNamespace(accepted_at=100.0)]
    )
    with pytest.raises(AssertionError, match="insufficient remaining"):
        module._assert_remaining_hold_budget(  # type: ignore[attr-defined]
            [SimpleNamespace(accepted_at=0.0), SimpleNamespace(accepted_at=200.0)]
        )


def test_hold_constant_and_sse_continuation_guard(load_module: object) -> None:
    module = load_module
    fixture = runpy.run_path(
        str(
            Path(__file__).parent
            / "fixtures"
            / "live_aca_deployed_agent_turn"
            / "tools"
            / "qualification_hold.py"
        )
    )
    from tests.live.aca_deployed_agent_support import SseEvent, append_contiguous_sse_events

    assert fixture["QUALIFICATION_HOLD_SECONDS"] == module._HOLD_SECONDS  # type: ignore[attr-defined]
    initial = [SseEvent(1, {"type": "session"})]
    continued = append_contiguous_sse_events(
        initial,
        [SseEvent(2, {"type": "tool_start"}), SseEvent(3, {"type": "done"})],
    )
    assert [event.sequence for event in continued] == [1, 2, 3]
    with pytest.raises(AssertionError, match="contiguous"):
        append_contiguous_sse_events(initial, [SseEvent(3, {"type": "done"})])


def test_manual_job_reads_optional_load_variable_from_the_script_environment() -> None:
    source = (
        Path(__file__).parents[1] / "eng" / "templates" / "official" / "jobs" / "e2e-tests.yml"
    ).read_text()

    assert 'ACA_DEPLOYED_LOAD_CONCURRENCY:-' in source
    assert "$(ACA_DEPLOYED_LOAD_CONCURRENCY)" not in source
    assert "auto-injects non-secret pipeline variables" in source
