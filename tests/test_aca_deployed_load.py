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
from tests.live.aca_deployed_agent_support import SseEvent


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


def test_provision_concurrency_cli_wins_and_local_default_is_four(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_ACA_PROVISION_CONCURRENCY", "2")

    assert support.provision_concurrency_from_option_or_environment(_config("4")) == 4
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_ACA_PROVISION_CONCURRENCY")
    assert support.provision_concurrency_from_option_or_environment(_config(None)) == 4


def test_dual_runtime_pipeline_limits_shared_group_provisioning_to_one_per_leg() -> None:
    root = Path(__file__).parent.parent
    source = (root / "eng" / "templates" / "official" / "jobs" / "e2e-tests.yml").read_text()
    runbook = (root / "tests" / "live" / "README.md").read_text()

    assert "default: '1'" in source
    assert 'ACA_DEPLOYED_CONFIGURED_PROVISION_CONCURRENCY}" -gt 1' in source
    assert "acaProvisionConcurrency above 1 requires acaRuntimeTarget=python313 or python314." in source
    assert "at most one session per runtime leg (two total)" in runbook
    assert "parallel Python 3.13/3.14 validation" in runbook


@pytest.mark.parametrize("value", ["0", "5", "three", "2.5"])
def test_provision_concurrency_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(AcaSmokeEnvironmentError, match="provision-concurrency"):
        support.provision_concurrency_from_option_or_environment(_config(value))


def test_agent_and_ci_load_policy_keeps_n5_diagnostic_and_human_n100_formal_only() -> None:
    root = Path(__file__).parent.parent
    runbook = (root / "tests" / "live" / "README.md").read_text()
    pipeline = (root / "eng" / "templates" / "official" / "jobs" / "e2e-tests.yml").read_text()

    assert "`N=5` is the sole agent/CI diagnostic validation size." in runbook
    assert "`N=100` is formal Decision #29 acceptance and is **human-only**" in runbook
    assert "acaLoadConcurrency=100" in runbook
    assert "ACA_DEPLOYED_LOAD_CONCURRENCY` pipeline variable does not control this job" in runbook
    assert "N=10/25/50/100" not in runbook
    assert 'ACA_DEPLOYED_LOAD_CONCURRENCY="100"' not in pipeline
    assert "ACA_DEPLOYED_CONFIGURED_LOAD_CONCURRENCY: ${{ parameters.acaLoadConcurrency }}" in pipeline
    assert "acaLoadConcurrency=100 requires acaRuntimeTarget" in pipeline


def test_deployed_agent_preflight_acquires_but_never_decodes_or_logs_easy_auth_tokens() -> None:
    root = Path(__file__).parent.parent
    pipeline = (root / "eng" / "templates" / "official" / "jobs" / "e2e-tests.yml").read_text()
    agent_job = pipeline.split('- job: "ACADeployedAgentTurn"', maxsplit=1)[1].split(
        '- job: "ACADeployedColdStart"', maxsplit=1
    )[0]
    preflight = agent_job.split("python - <<'PY'", maxsplit=1)[1].split("\n            PY", maxsplit=1)[0]

    assert "await credential.get_token(" in preflight
    assert 'print("Easy Auth token acquired")' in preflight
    assert preflight.count("print(") == 1
    for forbidden in ("base64", "json", "split(", "urlsafe_b64decode", "claim", "token.token"):
        assert forbidden not in preflight


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
        prepared_count=4,
        provision_concurrency=4,
        provisioning_duration_seconds=12.5,
        provisioning_attempt_count=5,
        provisioning_retry_count=1,
        suspended_prepared_count=2,
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
        admission_failure_categories=(("ambiguous_public_admission_http_500", 6),),
    )

    assert metrics.submission_ms == (2000, 4000, 4000)
    assert "N=4" in report
    assert "prepared=4" in report
    assert "provision_concurrency=4" in report
    assert "provisioning_attempts=5" in report
    assert "suspended_prepared=2" in report
    assert "p50=2000.0" in report
    assert "session" not in report
    assert "run_id" not in report
    assert "unclassified_service_throttles=0" in report
    assert "unresolved_idempotencies=0" in report
    assert "admission_failure_categories=ambiguous_public_admission_http_500=6" in report


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
async def test_phase_a_batch_preserves_prepared_candidates_before_aggregate_failure(
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

    async def submit_one(*_: object, **__: object) -> object:
        return next(outcomes)

    monkeypatch.setattr(module, "_submit_one", submit_one)
    submitted: list[object] = []
    with pytest.raises(module._AdmissionFailureError) as failure:  # type: ignore[attr-defined]
        await module._submit_session_batch(  # type: ignore[attr-defined]
            object(),
            SimpleNamespace(),
            {},
            object(),
            "partition",
            session_ids=[None, None],
            prompt="readiness",
            submitted=submitted,
            attempted_idempotency_keys=[],
        )

    assert submitted == [candidate]
    assert failure.value.retries == 1
    assert failure.value.unresolved_idempotencies == 1
    assert failure.value.attempted_idempotency_keys == ("a", "b")
    assert failure.value.failure_categories == (("setup_deadline_exceeded", 1),)


@pytest.mark.asyncio
async def test_batch_cancellation_retains_completed_candidate_immediately(
    monkeypatch: pytest.MonkeyPatch,
    load_module: object,
) -> None:
    module = load_module
    candidate = module._SubmittedRun(  # type: ignore[attr-defined]
        accepted=SimpleNamespace(session_id="session-a", run_id="run-a"),
        idempotency_key="accepted-key",
        submitted_at=1.0,
        accepted_at=2.0,
    )
    blocked = module.asyncio.Event()  # type: ignore[attr-defined]
    calls = 0

    async def submit_one(*_: object, **__: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            return module._AdmissionOutcome(  # type: ignore[attr-defined]
                "accepted-key", candidate, 0, 0, None, False
            )
        await blocked.wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(module, "_submit_one", submit_one)
    retained: list[object] = []
    task = module.asyncio.create_task(  # type: ignore[attr-defined]
        module._submit_session_batch(
            object(),
            SimpleNamespace(),
            {},
            object(),
            "partition",
            session_ids=[None, None],
            prompt="readiness",
            submitted=retained,
            attempted_idempotency_keys=[],
        )
    )
    for _ in range(10):
        if retained:
            break
        await module.asyncio.sleep(0)  # type: ignore[attr-defined]
    task.cancel()
    with pytest.raises(module.asyncio.CancelledError):  # type: ignore[attr-defined]
        await task

    assert retained == [candidate]


@pytest.mark.asyncio
async def test_phase_a_batch_deadline_retains_candidates_for_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    load_module: object,
) -> None:
    module = load_module
    candidate = module._SubmittedRun(  # type: ignore[attr-defined]
        accepted=SimpleNamespace(session_id="session-a", run_id="run-a"),
        idempotency_key="accepted-key",
        submitted_at=1.0,
        accepted_at=2.0,
    )
    never = module.asyncio.Event()  # type: ignore[attr-defined]

    async def submit_one(*_: object, **__: object) -> object:
        return module._AdmissionOutcome(  # type: ignore[attr-defined]
            "accepted-key", candidate, 0, 0, None, False
        )

    async def wait_forever(*_: object) -> None:
        await never.wait()

    monkeypatch.setattr(module, "_SETUP_HTTP_ATTEMPT_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(module, "_PROVISION_BATCH_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(module, "_submit_one", submit_one)
    monkeypatch.setattr(module, "_assert_prepared_sessions", wait_forever)
    retained: list[object] = []

    with pytest.raises(module.AcaSmokeEnvironmentError, match="Phase A provisioning batch"):  # type: ignore[attr-defined]
        await module._prepare_sessions(  # type: ignore[attr-defined]
            object(),
            object(),
            SimpleNamespace(),
            {},
            1,
            retained,
            object(),
            "partition",
            "redacted",
            [],
        )

    assert retained == [candidate]


@pytest.mark.asyncio
async def test_provisioning_batches_wait_for_prepared_idle_before_next_posts(
    monkeypatch: pytest.MonkeyPatch,
    load_module: object,
) -> None:
    module = load_module
    server_inflight: set[str] = set()
    completed_idle: set[str] = set()
    maximum_inflight = 0
    sequence = 0

    async def submit_one(*_: object, **kwargs: object) -> object:
        nonlocal maximum_inflight, sequence
        assert kwargs["prompt"] == module._READINESS_PROMPT  # type: ignore[attr-defined]
        assert kwargs["session_id"] is None
        if sequence == 2:
            assert completed_idle == {"session-1", "session-2"}
        if sequence == 4:
            assert completed_idle == {"session-1", "session-2", "session-3", "session-4"}
        await module.asyncio.sleep(0)  # type: ignore[attr-defined]
        sequence += 1
        session_id = f"session-{sequence}"
        server_inflight.add(session_id)
        maximum_inflight = max(maximum_inflight, len(server_inflight))
        return module._AdmissionOutcome(  # type: ignore[attr-defined]
            f"key-{sequence}",
            module._SubmittedRun(  # type: ignore[attr-defined]
                accepted=SimpleNamespace(session_id=session_id, run_id=f"run-{sequence}"),
                idempotency_key=f"key-{sequence}",
                submitted_at=1.0,
                accepted_at=2.0,
            ),
            0,
            0,
            None,
            False,
        )

    async def prepared_idle(*args: object) -> None:
        prepared = args[5]
        assert isinstance(prepared, list)
        session_ids = {item.accepted.session_id for item in prepared}
        assert len(session_ids) <= 2
        assert session_ids <= server_inflight
        completed_idle.update(session_ids)
        server_inflight.difference_update(session_ids)

    monkeypatch.setattr(module, "_submit_one", submit_one)
    monkeypatch.setattr(module, "_assert_prepared_sessions", prepared_idle)
    submitted: list[object] = []
    await module._prepare_sessions(  # type: ignore[attr-defined]
        object(),
        object(),
        SimpleNamespace(),
        {},
        6,
        submitted,
        object(),
        "partition",
        "redacted",
        [],
        provision_concurrency=2,
    )

    assert maximum_inflight == 2
    assert completed_idle == {f"session-{index}" for index in range(1, 7)}


@pytest.mark.asyncio
async def test_existing_session_phase_submits_each_held_run_with_its_session_header(
    monkeypatch: pytest.MonkeyPatch,
    load_module: object,
) -> None:
    module = load_module
    observed: list[tuple[str, str]] = []
    active = 0
    maximum_active = 0

    async def submit_one(*_: object, **kwargs: object) -> object:
        nonlocal active, maximum_active
        session_id = kwargs["session_id"]
        prompt = kwargs["prompt"]
        assert isinstance(session_id, str)
        assert prompt == module._LOAD_PROMPT  # type: ignore[attr-defined]
        observed.append((session_id, prompt))
        active += 1
        maximum_active = max(maximum_active, active)
        await module.asyncio.sleep(0)  # type: ignore[attr-defined]
        active -= 1
        return module._AdmissionOutcome(  # type: ignore[attr-defined]
            session_id,
            module._SubmittedRun(  # type: ignore[attr-defined]
                accepted=SimpleNamespace(session_id=session_id, run_id=f"run-{session_id}"),
                idempotency_key=f"key-{session_id}",
                submitted_at=1.0,
                accepted_at=2.0,
                session_id_header=session_id,
            ),
            0,
            0,
            None,
            False,
        )

    monkeypatch.setattr(module, "_submit_one", submit_one)
    prepared = [
        module._SubmittedRun(  # type: ignore[attr-defined]
            accepted=SimpleNamespace(session_id=session_id, run_id=f"prepared-{session_id}"),
            idempotency_key=f"prepared-{session_id}",
            submitted_at=1.0,
            accepted_at=2.0,
        )
        for session_id in ("one", "two", "three")
    ]
    held: list[object] = []
    await module._submit_existing_sessions(  # type: ignore[attr-defined]
        object(), SimpleNamespace(), {}, prepared, held, object(), "partition", []
    )

    assert {session_id for session_id, _ in observed} == {"one", "two", "three"}
    assert len(held) == 3
    assert maximum_active == 3


@pytest.mark.asyncio
async def test_phase_b_timeout_preserves_accepted_candidates_and_attempted_keys(
    monkeypatch: pytest.MonkeyPatch,
    load_module: object,
) -> None:
    module = load_module
    blocked = module.asyncio.Event()  # type: ignore[attr-defined]
    attempted_keys: list[str] = []
    held: list[object] = []

    async def submit_one(*args: object, **kwargs: object) -> object:
        session_id = kwargs["session_id"]
        assert isinstance(session_id, str)
        keys = args[5]
        assert isinstance(keys, list)
        keys.append(f"key-{session_id}")
        if session_id == "blocked":
            await blocked.wait()
        return module._AdmissionOutcome(  # type: ignore[attr-defined]
            f"key-{session_id}",
            module._SubmittedRun(  # type: ignore[attr-defined]
                accepted=SimpleNamespace(session_id=session_id, run_id=f"run-{session_id}"),
                idempotency_key=f"key-{session_id}",
                submitted_at=1.0,
                accepted_at=2.0,
                session_id_header=session_id,
            ),
            0,
            0,
            None,
            False,
        )

    prepared = [
        module._SubmittedRun(  # type: ignore[attr-defined]
            accepted=SimpleNamespace(session_id=session_id, run_id=f"prepared-{session_id}"),
            idempotency_key=f"prepared-{session_id}",
            submitted_at=1.0,
            accepted_at=2.0,
        )
        for session_id in ("retained", "blocked")
    ]
    monkeypatch.setattr(module, "_PHASE_B_ADMISSION_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(module, "_submit_one", submit_one)

    with pytest.raises(module.AcaSmokeEnvironmentError, match="Phase B admission"):
        await module._submit_existing_sessions(  # type: ignore[attr-defined]
            object(), SimpleNamespace(), {}, prepared, held, object(), "partition", attempted_keys
        )

    assert [item.accepted.session_id for item in held] == ["retained"]
    assert attempted_keys == ["key-retained", "key-blocked"]


@pytest.mark.asyncio
async def test_existing_session_response_retains_a_different_session_identity_as_failure(
    monkeypatch: pytest.MonkeyPatch,
    load_module: object,
) -> None:
    module = load_module
    accepted = SimpleNamespace(session_id="different-session", run_id="run-a")

    async def accepted_response(*_: object, **__: object) -> tuple[int, dict[str, str], dict[str, str]]:
        return 202, {"session_id": "different-session", "run_id": "run-a"}, {}

    monkeypatch.setattr(module, "json_request", accepted_response)
    monkeypatch.setattr(module, "parse_accepted_run", lambda *_: accepted)

    outcome = await module._submit_one(  # type: ignore[attr-defined]
        object(),
        SimpleNamespace(deployed=SimpleNamespace(chat_url="https://example.test/chat")),
        {},
        object(),
        "partition",
        [],
        session_id="prepared-session",
    )

    assert outcome.failure == "phase_b_session_mismatch"
    assert outcome.submitted is not None
    assert outcome.submitted.accepted is accepted


def test_phase_b_session_set_must_exactly_match_prepared_sessions(load_module: object) -> None:
    module = load_module

    def submitted(session_id: str) -> object:
        return module._SubmittedRun(  # type: ignore[attr-defined]
            accepted=SimpleNamespace(session_id=session_id, run_id=f"run-{session_id}"),
            idempotency_key=f"key-{session_id}",
            submitted_at=1.0,
            accepted_at=2.0,
            session_id_header=session_id,
        )

    module._assert_phase_b_session_identity([submitted("one"), submitted("two")], [submitted("two"), submitted("one")])  # type: ignore[attr-defined]
    with pytest.raises(AssertionError, match="prepared session set"):
        module._assert_phase_b_session_identity([submitted("one"), submitted("two")], [submitted("one"), submitted("other")])  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_phase_b_mismatch_is_retained_and_aggregated_for_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    load_module: object,
) -> None:
    module = load_module
    candidate = module._SubmittedRun(  # type: ignore[attr-defined]
        accepted=SimpleNamespace(session_id="unexpected", run_id="run-unexpected"),
        idempotency_key="key",
        submitted_at=1.0,
        accepted_at=2.0,
        session_id_header="requested",
    )

    async def mismatch(*_: object, **__: object) -> object:
        return module._AdmissionOutcome(  # type: ignore[attr-defined]
            "key", candidate, 0, 0, "phase_b_session_mismatch", False
        )

    monkeypatch.setattr(module, "_submit_one", mismatch)
    retained: list[object] = []
    with pytest.raises(module._AdmissionFailureError) as failure:  # type: ignore[attr-defined]
        await module._submit_session_batch(  # type: ignore[attr-defined]
            object(),
            SimpleNamespace(),
            {},
            object(),
            "partition",
            session_ids=["requested"],
            prompt="held",
            submitted=retained,
            attempted_idempotency_keys=[],
        )

    assert retained == [candidate]
    assert failure.value.failure_categories == (("phase_b_session_mismatch", 1),)
    assert "phase_b_session_mismatch=1" in str(failure.value)
    assert "unexpected" not in str(failure.value)


@pytest.mark.asyncio
async def test_swapped_owner_idempotency_recovery_retains_candidate_as_phase_b_failure(
    monkeypatch: pytest.MonkeyPatch,
    load_module: object,
) -> None:
    module = load_module
    deployed = SimpleNamespace(
        chat_url="https://example.test/chat",
        management_urls=lambda *, session_id, run_id: {"events_url": f"/{session_id}/{run_id}"},
    )

    async def service_error(*_: object, **__: object) -> tuple[int, dict[str, str], dict[str, str]]:
        return 503, {}, {}

    async def swapped_record(*_: object, **__: object) -> SimpleNamespace:
        return SimpleNamespace(session_id="swapped-session", run_id="swapped-run")

    monkeypatch.setattr(module, "json_request", service_error)
    monkeypatch.setattr(module, "read_owner_idempotency", swapped_record)

    outcome = await module._submit_one(  # type: ignore[attr-defined]
        object(),
        SimpleNamespace(deployed=deployed),
        {},
        object(),
        "partition",
        [],
        session_id="requested-session",
    )

    assert outcome.failure == "phase_b_session_mismatch"
    assert outcome.submitted is not None
    assert outcome.submitted.accepted.session_id == "swapped-session"
    assert not outcome.unresolved_idempotency


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "suspending", "resuming", "canceling"])
async def test_prepared_idle_observation_rejects_running_and_transitional_sessions(
    monkeypatch: pytest.MonkeyPatch,
    load_module: object,
    status: str,
) -> None:
    module = load_module
    prepared = module._SubmittedRun(  # type: ignore[attr-defined]
        accepted=SimpleNamespace(session_id="session-a", run_id="run-a"),
        idempotency_key="key-a",
        submitted_at=1.0,
        accepted_at=2.0,
    )

    async def read_session(*_: object, **__: object) -> object:
        return SimpleNamespace(
            status=status,
            idle_policy_armed=True,
            active_run_id=None,
            active_operation_id=None,
        )

    async def read_run(*_: object, **__: object) -> object:
        return SimpleNamespace(status="succeeded", result_available=True)

    async def read_operations(*_: object, **__: object) -> tuple[()]:
        return ()

    monkeypatch.setattr(module, "read_authoritative_session", read_session)
    monkeypatch.setattr(module, "read_authoritative_run", read_run)
    monkeypatch.setattr(module, "read_session_operations", read_operations)
    monkeypatch.setattr(module, "assert_session_belongs_to_deployment", lambda *_: None)

    with pytest.raises(AssertionError):
        await module._read_prepared_idle_observation(  # type: ignore[attr-defined]
            object(), object(), "partition", prepared
        )


@pytest.mark.asyncio
async def test_prepared_idle_observation_requires_an_armed_idle_policy(
    monkeypatch: pytest.MonkeyPatch,
    load_module: object,
) -> None:
    module = load_module
    prepared = module._SubmittedRun(  # type: ignore[attr-defined]
        accepted=SimpleNamespace(session_id="session-a", run_id="run-a"),
        idempotency_key="key-a",
        submitted_at=1.0,
        accepted_at=2.0,
    )

    async def read_session(*_: object, **__: object) -> object:
        return SimpleNamespace(
            status="ready",
            idle_policy_armed=False,
            active_run_id=None,
            active_operation_id=None,
        )

    async def read_run(*_: object, **__: object) -> object:
        return SimpleNamespace(status="succeeded", result_available=True)

    async def read_operations(*_: object, **__: object) -> tuple[()]:
        return ()

    monkeypatch.setattr(module, "read_authoritative_session", read_session)
    monkeypatch.setattr(module, "read_authoritative_run", read_run)
    monkeypatch.setattr(module, "read_session_operations", read_operations)
    monkeypatch.setattr(module, "assert_session_belongs_to_deployment", lambda *_: None)

    with pytest.raises(AssertionError):
        await module._read_prepared_idle_observation(  # type: ignore[attr-defined]
            object(), object(), "partition", prepared
        )


@pytest.mark.asyncio
async def test_setup_request_timeout_recovers_an_ambiguous_candidate(
    monkeypatch: pytest.MonkeyPatch,
    load_module: object,
) -> None:
    module = load_module
    deployed = SimpleNamespace(
        chat_url="https://example.test/chat",
        management_urls=lambda *, session_id, run_id: {"events_url": f"/{session_id}/{run_id}"},
    )
    never = module.asyncio.Event()  # type: ignore[attr-defined]

    async def blocking_request(*_: object, **__: object) -> tuple[int, dict[str, str], dict[str, str]]:
        await never.wait()
        raise AssertionError("unreachable")

    async def recovered(*_: object, **__: object) -> SimpleNamespace:
        return SimpleNamespace(session_id="recovered-session", run_id="recovered-run")

    monkeypatch.setattr(module, "_SETUP_HTTP_ATTEMPT_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(module, "json_request", blocking_request)
    monkeypatch.setattr(module, "read_owner_idempotency", recovered)

    outcome = await module._submit_one(  # type: ignore[attr-defined]
        object(), SimpleNamespace(deployed=deployed), {}, object(), "partition", []
    )

    assert outcome.failure == "public_admission_request_ambiguous"
    assert outcome.submitted is not None
    assert outcome.submitted.accepted.session_id == "recovered-session"


@pytest.mark.asyncio
async def test_n100_suspension_evidence_waits_at_one_hz_for_exact_label_backing(
    monkeypatch: pytest.MonkeyPatch,
    load_module: object,
) -> None:
    module = load_module
    prepared = [
        module._SubmittedRun(  # type: ignore[attr-defined]
            accepted=SimpleNamespace(session_id="prepared-session", run_id="prepared-run"),
            idempotency_key="prepared-key",
            submitted_at=1.0,
            accepted_at=2.0,
        )
    ]
    states = iter(("Running", "Stopped"))
    delays: list[float] = []

    async def read_session(*_: object, **__: object) -> object:
        return SimpleNamespace()

    async def sandbox(*_: object, **__: object) -> object:
        return SimpleNamespace(state=next(states))

    async def sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(module, "read_authoritative_session", read_session)
    monkeypatch.setattr(module, "owned_sandbox", sandbox)
    monkeypatch.setattr(module, "assert_session_belongs_to_deployment", lambda *_: None)
    monkeypatch.setattr(module.asyncio, "sleep", sleep)

    assert module._requires_prepared_suspension(100)  # type: ignore[attr-defined]
    assert not module._requires_prepared_suspension(10)  # type: ignore[attr-defined]
    assert await module._wait_for_suspended_prepared_backing(  # type: ignore[attr-defined]
        object(), object(), "partition", prepared
    ) == 1
    assert delays == [1.0]


def test_readiness_events_reject_the_hold_tool(load_module: object) -> None:
    module = load_module
    module._assert_no_public_hold_events([SseEvent(1, {"type": "done"})])  # type: ignore[attr-defined]
    with pytest.raises(AssertionError):
        module._assert_no_public_hold_events(  # type: ignore[attr-defined]
            [SseEvent(1, {"type": "tool_start", "tool_name": "qualification_hold"})]
        )


def test_public_hold_events_correlate_an_unnamed_maf_tool_result(load_module: object) -> None:
    module = load_module
    module._assert_public_hold_events(  # type: ignore[attr-defined]
        [
            SseEvent(
                1,
                {
                    "type": "tool_start",
                    "tool_call_id": "call-1",
                    "tool_name": "qualification_hold",
                },
            ),
            SseEvent(
                2,
                {
                    "type": "tool_end",
                    "tool_call_id": "call-1",
                    "tool_name": None,
                },
            ),
        ]
    )


def test_public_hold_events_accept_a_named_tool_result(load_module: object) -> None:
    module = load_module
    module._assert_public_hold_events(  # type: ignore[attr-defined]
        [
            SseEvent(
                1,
                {
                    "type": "tool_start",
                    "tool_call_id": "call-1",
                    "tool_name": "qualification_hold",
                },
            ),
            SseEvent(
                2,
                {
                    "type": "tool_end",
                    "tool_call_id": "call-1",
                    "tool_name": "qualification_hold",
                },
            ),
        ]
    )


def test_public_hold_events_allow_an_unrelated_tool_lifecycle(load_module: object) -> None:
    module = load_module
    module._assert_public_hold_events(  # type: ignore[attr-defined]
        [
            SseEvent(
                1,
                {
                    "type": "tool_start",
                    "tool_call_id": "call-1",
                    "tool_name": "qualification_hold",
                },
            ),
            SseEvent(
                2,
                {
                    "type": "tool_start",
                    "tool_call_id": "call-2",
                    "tool_name": "unrelated_tool",
                },
            ),
            SseEvent(
                3,
                {
                    "type": "tool_end",
                    "tool_call_id": "call-2",
                    "tool_name": "unrelated_tool",
                },
            ),
            SseEvent(
                4,
                {
                    "type": "tool_end",
                    "tool_call_id": "call-1",
                    "tool_name": None,
                },
            ),
        ]
    )


@pytest.mark.parametrize(
    "events",
    [
        [
            SseEvent(1, {"type": "tool_start", "tool_call_id": "call-1", "tool_name": "qualification_hold"}),
            SseEvent(2, {"type": "tool_start", "tool_call_id": "call-1", "tool_name": "qualification_hold"}),
            SseEvent(3, {"type": "tool_end", "tool_call_id": "call-1", "tool_name": None}),
        ],
        [
            SseEvent(1, {"type": "tool_start", "tool_call_id": "call-1", "tool_name": "qualification_hold"}),
            SseEvent(2, {"type": "tool_end", "tool_call_id": "call-1", "tool_name": None}),
            SseEvent(3, {"type": "tool_end", "tool_call_id": "call-1", "tool_name": None}),
        ],
        [
            SseEvent(1, {"type": "tool_start", "tool_call_id": "call-1", "tool_name": "qualification_hold"}),
            SseEvent(2, {"type": "tool_end", "tool_call_id": "call-2", "tool_name": None}),
        ],
        [
            SseEvent(1, {"type": "tool_start", "tool_call_id": "call-1", "tool_name": "qualification_hold"}),
            SseEvent(2, {"type": "tool_end", "tool_call_id": "call-1", "tool_name": "other_tool"}),
        ],
        [SseEvent(1, {"type": "tool_start", "tool_call_id": "call-1", "tool_name": "qualification_hold"})],
    ],
)
def test_public_hold_events_reject_invalid_tool_correlation(
    load_module: object,
    events: list[SseEvent],
) -> None:
    module = load_module

    with pytest.raises(AssertionError):
        module._assert_public_hold_events(events)  # type: ignore[attr-defined]


def test_admission_failure_categories_are_aggregated_and_redacted(load_module: object) -> None:
    module = load_module
    categories = module._admission_failure_categories(  # type: ignore[attr-defined]
        [
            "ambiguous_public_admission_http_500",
            "ambiguous_public_admission_http_500",
            "session-id-should-not-appear",
        ]
    )
    error = module._AdmissionFailureError(  # type: ignore[attr-defined]
        failures=3,
        retries=0,
        throttles=0,
        unresolved_idempotencies=0,
        attempted_idempotency_keys=(),
        failure_categories=categories,
        attempt_count=3,
    )

    assert categories == (
        ("ambiguous_public_admission_http_500", 2),
        ("other_admission_failure", 1),
    )
    assert error.failure_categories == categories
    assert "ambiguous_public_admission_http_500=2" in str(error)
    assert "session-id-should-not-appear" not in str(error)


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

    retry_delays: list[float] = []

    async def no_sleep(delay: float) -> None:
        retry_delays.append(delay)

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
    assert retry_delays == [120.0]


@pytest.mark.asyncio
async def test_provisioning_deadline_before_retry_preserves_attempt_metrics(
    monkeypatch: pytest.MonkeyPatch,
    load_module: object,
) -> None:
    module = load_module

    async def setup_deadline(*_: object, **__: object) -> tuple[int, dict[str, str], dict[str, str]]:
        return 504, {"error": "setup_deadline_exceeded"}, {"Retry-After": "120"}

    monkeypatch.setattr(module, "json_request", setup_deadline)

    with pytest.raises(module._AdmissionDeadlineError) as failure:  # type: ignore[attr-defined]
        await module._submit_session_batch(  # type: ignore[attr-defined]
            object(),
            SimpleNamespace(deployed=SimpleNamespace(chat_url="https://example.test/chat")),
            {},
            object(),
            "partition",
            session_ids=[None],
            prompt="readiness",
            submitted=[],
            attempted_idempotency_keys=[],
            deadline=module.time.perf_counter() + 0.01,  # type: ignore[attr-defined]
        )

    assert failure.value.retries == 1
    assert failure.value.attempt_count == 2
    assert failure.value.failure_categories == (("setup_deadline_exceeded", 1),)
    assert "setup retry deadline was exhausted" in str(failure.value)
@pytest.mark.asyncio
async def test_load_submission_retries_one_setup_lease_with_the_same_key(
    monkeypatch: pytest.MonkeyPatch,
    load_module: object,
) -> None:
    module = load_module
    deployed = SimpleNamespace(chat_url="https://example.test/chat")
    responses = iter(
        [
            (504, {"error": "setup_deadline_exceeded"}, {"Retry-After": "120"}),
            (202, {"session_id": "session-accepted", "run_id": "run-accepted"}, {}),
        ]
    )
    headers_seen: list[dict[str, str]] = []
    payloads_seen: list[dict[str, object]] = []
    retry_delays: list[float] = []
    accepted = SimpleNamespace(session_id="session-accepted", run_id="run-accepted")

    async def request(*_: object, **kwargs: object) -> tuple[int, dict[str, str], dict[str, str]]:
        headers_seen.append(dict(kwargs["headers"]))  # type: ignore[arg-type,index]
        payloads_seen.append(dict(kwargs["payload"]))  # type: ignore[arg-type,index]
        return next(responses)

    async def no_sleep(delay: float) -> None:
        retry_delays.append(delay)

    monkeypatch.setattr(module, "json_request", request)
    monkeypatch.setattr(module, "parse_accepted_run", lambda *_: accepted)
    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)

    keys: list[str] = []
    outcome = await module._submit_one(  # type: ignore[attr-defined]
        object(),
        SimpleNamespace(deployed=deployed),
        {},
        object(),
        "partition",
        keys,
        prompt=module._READINESS_PROMPT,  # type: ignore[attr-defined]
    )

    assert outcome.submitted is not None
    assert outcome.submitted.accepted is accepted
    assert outcome.retries == 1
    assert len(headers_seen) == 2
    assert {headers["Idempotency-Key"] for headers in headers_seen} == {keys[0]}
    assert all("x-ms-session-id" not in headers for headers in headers_seen)
    assert payloads_seen == [{"prompt": module._READINESS_PROMPT}] * 2  # type: ignore[attr-defined]
    assert retry_delays == [120.0]


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
async def test_final_recovery_retains_late_owner_idempotency_candidate(
    monkeypatch: pytest.MonkeyPatch,
    load_module: object,
) -> None:
    module = load_module
    deployed = SimpleNamespace(
        management_urls=lambda *, session_id, run_id: {"events_url": f"/{session_id}/{run_id}"}
    )
    reads = 0
    delays: list[float] = []

    async def read_record(*_: object, **__: object) -> object:
        nonlocal reads
        reads += 1
        return None if reads == 1 else SimpleNamespace(session_id="late-session", run_id="late-run")

    async def sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(module, "read_owner_idempotency", read_record)
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    recovered, unresolved = await module._recover_final_cleanup_candidates(  # type: ignore[attr-defined]
        object(),
        SimpleNamespace(deployed=deployed),
        "partition",
        ["late-key"],
        [],
    )

    assert unresolved == 0
    assert len(recovered) == 1
    assert recovered[0].accepted.session_id == "late-session"
    assert delays == [1.0]


@pytest.mark.asyncio
async def test_final_recovery_deduplicates_candidates_and_counts_unresolved(
    monkeypatch: pytest.MonkeyPatch,
    load_module: object,
) -> None:
    module = load_module
    deployed = SimpleNamespace(
        management_urls=lambda *, session_id, run_id: {"events_url": f"/{session_id}/{run_id}"}
    )

    async def read_record(*_: object, **kwargs: object) -> object:
        key = kwargs["idempotency_key"]
        if key in {"first-key", "second-key"}:
            return SimpleNamespace(session_id="same-session", run_id="same-run")
        return None

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(module, "_FINAL_RECOVERY_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(module, "read_owner_idempotency", read_record)
    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    recovered, unresolved = await module._recover_final_cleanup_candidates(  # type: ignore[attr-defined]
        object(),
        SimpleNamespace(deployed=deployed),
        "partition",
        ["first-key", "second-key", "missing-key", "first-key"],
        [],
    )

    assert len(recovered) == 1
    assert unresolved == 1


def test_admission_errors_and_reports_never_render_attempted_idempotency_keys(
    load_module: object,
) -> None:
    module = load_module
    error = module._AdmissionFailureError(  # type: ignore[attr-defined]
        failures=1,
        retries=0,
        throttles=0,
        unresolved_idempotencies=1,
        attempted_idempotency_keys=("sensitive-attempted-key",),
        failure_categories=(("setup_deadline_exceeded", 1),),
        attempt_count=1,
    )

    assert "sensitive-attempted-key" not in str(error)


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
        ),
        module._SubmittedRun(  # type: ignore[attr-defined]
            accepted=SimpleNamespace(session_id="session-a", run_id="run-b"),
            idempotency_key="b",
            submitted_at=1.0,
            accepted_at=2.0,
            session_id_header="session-a",
        ),
    ]
    session = SimpleNamespace()
    cleanup_calls = 0

    async def read_session(*_: object, **__: object) -> object:
        return session

    async def clean(*_: object, **__: object) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
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
    assert cleanup_calls == 1

    async def cleanup_failure(*_: object, **__: object) -> None:
        raise AcaSmokeEnvironmentError("tombstone failed")

    monkeypatch.setattr(module, "cleanup_owned_lifecycle_session", cleanup_failure)
    with pytest.raises(AcaSmokeEnvironmentError, match="Controller tombstone"):
        await module._cleanup_load_sessions(  # type: ignore[attr-defined]
            object(), object(), "partition", submitted
        )


@pytest.mark.asyncio
async def test_creating_session_is_not_an_active_observation(
    monkeypatch: pytest.MonkeyPatch,
    load_module: object,
) -> None:
    module = load_module
    submitted = module._SubmittedRun(  # type: ignore[attr-defined]
        accepted=SimpleNamespace(session_id="session-a", run_id="run-a"),
        idempotency_key="key-a",
        submitted_at=1.0,
        accepted_at=2.0,
    )

    async def read_session(*_: object, **__: object) -> object:
        return SimpleNamespace(status="creating", active_run_id="run-a", active_operation_id=None)

    async def read_run(*_: object, **__: object) -> object:
        return SimpleNamespace(status="accepted")

    async def read_operations(*_: object, **__: object) -> tuple[()]:
        return ()

    monkeypatch.setattr(module, "read_authoritative_session", read_session)
    monkeypatch.setattr(module, "read_authoritative_run", read_run)
    monkeypatch.setattr(module, "read_session_operations", read_operations)
    monkeypatch.setattr(module, "assert_session_belongs_to_deployment", lambda *_: None)

    assert (
        await module._read_active_observation(  # type: ignore[attr-defined]
            object(), object(), "partition", submitted
        )
        is None
    )


@pytest.mark.asyncio
async def test_active_race_replay_and_conflict_both_preserve_existing_session_header(
    monkeypatch: pytest.MonkeyPatch,
    load_module: object,
) -> None:
    module = load_module
    accepted = SimpleNamespace(session_id="session-a", run_id="run-a")
    submitted = module._SubmittedRun(  # type: ignore[attr-defined]
        accepted=accepted,
        idempotency_key="original-key",
        submitted_at=1.0,
        accepted_at=2.0,
        session_id_header="session-a",
    )
    captured_headers: list[dict[str, str]] = []

    async def request(*_: object, **kwargs: object) -> tuple[int, dict[str, str], dict[str, str]]:
        captured_headers.append(dict(kwargs["headers"]))  # type: ignore[arg-type,index]
        if len(captured_headers) == 1:
            return 202, {"session_id": "session-a", "run_id": "run-a"}, {}
        return 409, {"error": "active_run_exists"}, {}

    monkeypatch.setattr(module, "json_request", request)
    monkeypatch.setattr(module, "parse_accepted_run", lambda *_: accepted)

    assert await module._exercise_one_active_race(  # type: ignore[attr-defined]
        object(),
        SimpleNamespace(deployed=SimpleNamespace(chat_url="https://example.test/chat")),
        {"Authorization": "redacted"},
        submitted,
    ) == (1, 1)
    assert captured_headers[0]["Idempotency-Key"] == "original-key"
    assert captured_headers[0]["x-ms-session-id"] == "session-a"
    assert captured_headers[1]["x-ms-session-id"] == "session-a"
    assert captured_headers[1]["Idempotency-Key"] != "original-key"


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_status", ["running", "creating"])
async def test_failed_run_settlement_waits_then_cancels_and_requires_idle_state(
    monkeypatch: pytest.MonkeyPatch,
    load_module: object,
    initial_status: str,
) -> None:
    module = load_module
    submitted = module._SubmittedRun(  # type: ignore[attr-defined]
        accepted=SimpleNamespace(
            session_id="session-a", run_id="run-a", management_urls={"cancel_url": "/cancel"}
        ),
        idempotency_key="key-a",
        submitted_at=1.0,
        accepted_at=2.0,
    )
    state = {"phase": "initial", "cancel_phase": None}
    cancel_calls: list[str] = []

    async def read_session(*_: object, **__: object) -> object:
        if state["phase"] == "terminal":
            return SimpleNamespace(status="ready", active_run_id=None, active_operation_id=None)
        return SimpleNamespace(
            status=initial_status if state["phase"] == "initial" else "running",
            active_run_id="run-a",
            active_operation_id="operation-a",
        )

    async def read_run(*_: object, **__: object) -> object:
        return SimpleNamespace(status="canceled" if state["phase"] == "terminal" else "accepted")

    async def read_operations(*_: object, **__: object) -> tuple[object, ...]:
        if state["phase"] == "terminal":
            return ()
        return (SimpleNamespace(state="active"),)

    async def request(*_: object, **__: object) -> tuple[int, dict[str, str], dict[str, str]]:
        state["cancel_phase"] = state["phase"]
        cancel_calls.append("cancel")
        return 200, {"state": "canceled"}, {}

    async def advance(_: float) -> None:
        if cancel_calls:
            state["phase"] = "terminal"
        elif state["phase"] == "initial":
            state["phase"] = "running"

    monkeypatch.setattr(module, "read_authoritative_session", read_session)
    monkeypatch.setattr(module, "read_authoritative_run", read_run)
    monkeypatch.setattr(module, "read_session_operations", read_operations)
    monkeypatch.setattr(module, "assert_session_belongs_to_deployment", lambda *_: None)
    monkeypatch.setattr(module, "json_request", request)
    monkeypatch.setattr(module.asyncio, "sleep", advance)

    await module._settle_one_failed_run(  # type: ignore[attr-defined]
        object(), object(), "partition", submitted, object(), "redacted"
    )

    assert cancel_calls == ["cancel"]
    if initial_status == "creating":
        assert state["cancel_phase"] == "running"


@pytest.mark.asyncio
async def test_failed_run_settlement_waits_for_all_candidates_before_reporting_failure(
    monkeypatch: pytest.MonkeyPatch,
    load_module: object,
) -> None:
    module = load_module
    completed: list[str] = []

    async def settle_one(*args: object) -> None:
        candidate = args[3]
        if candidate == "first":
            raise AcaSmokeEnvironmentError("first failed")
        completed.append("second")

    monkeypatch.setattr(module, "_settle_one_failed_run", settle_one)
    with pytest.raises(AcaSmokeEnvironmentError, match="did not settle"):
        await module._settle_failed_runs(  # type: ignore[attr-defined]
            object(), object(), "partition", ["first", "second"], object(), "redacted"
        )

    assert completed == ["second"]


@pytest.mark.asyncio
async def test_terminal_run_settlement_waits_for_idle_without_public_cancel(
    monkeypatch: pytest.MonkeyPatch,
    load_module: object,
) -> None:
    module = load_module
    submitted = module._SubmittedRun(  # type: ignore[attr-defined]
        accepted=SimpleNamespace(
            session_id="session-a", run_id="run-a", management_urls={"cancel_url": "/cancel"}
        ),
        idempotency_key="key-a",
        submitted_at=1.0,
        accepted_at=2.0,
    )
    settled = False
    cancel_requests = 0

    async def read_session(*_: object, **__: object) -> object:
        return SimpleNamespace(
            status="running",
            active_run_id=None if settled else "run-a",
            active_operation_id=None if settled else "operation-a",
        )

    async def read_run(*_: object, **__: object) -> object:
        return SimpleNamespace(status="succeeded")

    async def read_operations(*_: object, **__: object) -> tuple[object, ...]:
        return () if settled else (SimpleNamespace(state="active"),)

    async def unexpected_cancel(*_: object, **__: object) -> tuple[int, dict[str, str], dict[str, str]]:
        nonlocal cancel_requests
        cancel_requests += 1
        return 200, {"state": "canceled"}, {}

    async def settle_after_poll(_: float) -> None:
        nonlocal settled
        settled = True

    monkeypatch.setattr(module, "read_authoritative_session", read_session)
    monkeypatch.setattr(module, "read_authoritative_run", read_run)
    monkeypatch.setattr(module, "read_session_operations", read_operations)
    monkeypatch.setattr(module, "assert_session_belongs_to_deployment", lambda *_: None)
    monkeypatch.setattr(module, "json_request", unexpected_cancel)
    monkeypatch.setattr(module.asyncio, "sleep", settle_after_poll)

    await module._settle_one_failed_run(  # type: ignore[attr-defined]
        object(), object(), "partition", submitted, object(), "redacted"
    )

    assert settled
    assert cancel_requests == 0


@pytest.mark.asyncio
async def test_last_resort_cleanup_deletes_owned_snapshots_before_sandbox(
    monkeypatch: pytest.MonkeyPatch,
    load_module: object,
) -> None:
    module = load_module
    submitted = [
        module._SubmittedRun(  # type: ignore[attr-defined]
            accepted=SimpleNamespace(session_id="session-a", run_id="run-a"),
            idempotency_key="key-a",
            submitted_at=1.0,
            accepted_at=2.0,
        )
    ]
    deleted: list[tuple[str, str]] = []

    class Adapter:
        async def delete_snapshot(self, snapshot_id: str) -> None:
            deleted.append(("snapshot", snapshot_id))

        async def delete_sandbox(self, sandbox_id: str) -> None:
            deleted.append(("sandbox", sandbox_id))

    async def read_session(*_: object, **__: object) -> object:
        return SimpleNamespace(sandbox_id="sandbox-a")

    async def snapshots(*_: object, **__: object) -> tuple[object, ...]:
        return (
            ()
            if ("snapshot", "snapshot-a") in deleted
            else (SimpleNamespace(snapshot_id="snapshot-a"),)
        )

    async def sandbox(*_: object, **__: object) -> object:
        return (
            None
            if ("sandbox", "sandbox-a") in deleted
            else SimpleNamespace(sandbox_id="sandbox-a")
        )

    monkeypatch.setattr(module, "read_authoritative_session", read_session)
    monkeypatch.setattr(module, "owned_snapshots", snapshots)
    monkeypatch.setattr(module, "owned_sandbox", sandbox)
    monkeypatch.setattr(module, "assert_session_belongs_to_deployment", lambda *_: None)

    await module._provider_cleanup_last_resort(  # type: ignore[attr-defined]
        SimpleNamespace(adapter=Adapter()), object(), "partition", submitted
    )

    assert deleted == [("snapshot", "snapshot-a"), ("sandbox", "sandbox-a")]


@pytest.mark.asyncio
async def test_last_resort_cleanup_does_not_match_nullable_snapshot_ownership(
    monkeypatch: pytest.MonkeyPatch,
    load_module: object,
) -> None:
    module = load_module
    submitted = [
        module._SubmittedRun(  # type: ignore[attr-defined]
            accepted=SimpleNamespace(session_id="session-a", run_id="run-a"),
            idempotency_key="key-a",
            submitted_at=1.0,
            accepted_at=2.0,
        )
    ]
    inventory_reads = 0

    async def read_session(*_: object, **__: object) -> object:
        return SimpleNamespace(sandbox_id=None)

    async def unexpected_inventory(*_: object, **__: object) -> object:
        nonlocal inventory_reads
        inventory_reads += 1
        return ()

    monkeypatch.setattr(module, "read_authoritative_session", read_session)
    monkeypatch.setattr(module, "owned_snapshots", unexpected_inventory)
    monkeypatch.setattr(module, "owned_sandbox", unexpected_inventory)
    monkeypatch.setattr(module, "assert_session_belongs_to_deployment", lambda *_: None)

    await module._provider_cleanup_last_resort(  # type: ignore[attr-defined]
        SimpleNamespace(adapter=object()), object(), "partition", submitted
    )

    assert inventory_reads == 0


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


def test_primary_product_failure_stays_primary_when_settlement_and_cleanup_fail(
    load_module: object,
) -> None:
    module = load_module

    def product_failure() -> None:
        raise AssertionError("product failure")

    with pytest.raises(AssertionError, match="product failure") as failure:
        try:
            product_failure()
        except AssertionError:
            primary = sys.exception()
            assert primary is not None
            module._note_settlement_failure(primary)  # type: ignore[attr-defined]
            module._raise_or_note_cleanup_failure(  # type: ignore[attr-defined]
                primary, AcaSmokeEnvironmentError("cleanup failure")
            )
            raise

    assert any("settlement also failed" in note for note in failure.value.__notes__)
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


def test_deployed_job_uses_queue_parameters_for_n5_n100_and_provisioning() -> None:
    source = (
        Path(__file__).parents[1] / "eng" / "templates" / "official" / "jobs" / "e2e-tests.yml"
    ).read_text()

    assert 'ACA_DEPLOYED_CONFIGURED_LOAD_CONCURRENCY:-' in source
    assert "$(ACA_DEPLOYED_LOAD_CONCURRENCY)" not in source
    assert "sole automated N=5 diagnostic" in source
    assert "ACA_DEPLOYED_CONFIGURED_LOAD_CONCURRENCY: ${{ parameters.acaLoadConcurrency }}" in source
    assert "ACA_DEPLOYED_CONFIGURED_PROVISION_CONCURRENCY: ${{ parameters.acaProvisionConcurrency }}" in source
    assert "acaLoadConcurrency=100 requires acaRuntimeTarget=python313 or python314." in source
    assert "acaProvisionConcurrency above 1 requires acaRuntimeTarget=python313 or python314." in source
    assert "AZURE_FUNCTIONS_AGENTS_ACA_PROVISION_CONCURRENCY" in source
    assert 'job: "ACADeployedAgentTurn"' in source
    assert "timeoutInMinutes: 360" in source
    assert "continueOnError: true" in source


def test_setup_attempt_and_job_bounds_match_the_runbook() -> None:
    root = Path(__file__).parents[1]
    load_source = (root / "tests" / "live" / "test_aca_deployed_load.py").read_text()
    loss_source = (root / "tests" / "live" / "test_aca_deployed_loss.py").read_text()
    job = (root / "eng" / "templates" / "official" / "jobs" / "e2e-tests.yml").read_text()
    runbook = (root / "tests" / "live" / "README.md").read_text()

    assert "_SETUP_HTTP_ATTEMPT_TIMEOUT_SECONDS = 105.0" in load_source
    assert "_SETUP_DEADLINE_ATTEMPTS = 2" in load_source
    assert "_SETUP_HTTP_ATTEMPT_TIMEOUT_SECONDS = 105.0" in loss_source
    assert "_SETUP_RETRY_ATTEMPTS = 2" in loss_source
    assert "_PROVISION_BATCH_TIMEOUT_SECONDS = 330.0" in load_source
    assert "_PHASE_B_ADMISSION_TIMEOUT_SECONDS = 330.0" in load_source
    assert "_HELD_RUN_SETUP_TIMEOUT_SECONDS = 330.0" in loss_source
    assert "timeoutInMinutes: 360" in job
    assert "two 105-second attempts plus one 120-second retry wait" in runbook
    assert "1,710 seconds" in runbook
    assert "8,310 seconds" in runbook
    assert "360-minute safety cap" in runbook
