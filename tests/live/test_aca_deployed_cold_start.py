"""Manual/Scheduled deployed cold-start qualification through the real customer path."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, replace

import pytest
from aiohttp import ClientSession, ClientTimeout
from tests.aca_smoke_diagnostics import AcaSmokeEnvironmentError
from tests.live.aca_deployed_agent_support import (
    AcceptedRun,
    acquire_default_authorization_evidence,
    deployed_aca_smoke_enabled,
    json_request,
    parse_accepted_run,
    read_sse_events_with_first_event_time,
    setup_retry_after_seconds,
    submission_payload,
)
from tests.live.aca_deployed_cold_start_support import (
    ADMISSION_WINDOW_SECONDS,
    FINAL_RECOVERY_WINDOW_SECONDS,
    SAMPLE_WINDOW_SECONDS,
    SETUP_ATTEMPT_TIMEOUT_SECONDS,
    SSE_TERMINAL_WINDOW_SECONDS,
    cold_start_metrics,
    cold_start_samples_from_option_or_environment,
    first_attempt_slo_failure,
    render_cold_start_report,
)
from tests.live.aca_deployed_lifecycle_support import (
    DeployedAcaLifecycleConfig,
    DeployedAcaLifecycleResources,
    assert_session_belongs_to_deployment,
    cleanup_owned_lifecycle_session,
    deployed_aca_lifecycle_config_from_environment,
    open_deployed_aca_lifecycle_resources,
    owned_sandbox,
    owned_snapshots,
    read_authoritative_session,
    read_owner_idempotency,
)

from azure_functions_agents.session_state import EntraUserOwnerContext, owner_partition

_LOGGER = logging.getLogger(__name__)
_COLD_START_AGENT_SLUG = "deployed_turn"
_PROMPT = "Return a brief acknowledgement."
_SETUP_ATTEMPTS = 2
_RECOVERY_POLL_SECONDS = 1.0
_PUBLIC_TERMINAL_WINDOW_SECONDS = 45.0
_CLEANUP_CANDIDATE_TIMEOUT_SECONDS = 240.0

if not deployed_aca_smoke_enabled():
    pytest.skip(
        "Set AZURE_FUNCTIONS_AGENTS_RUN_DEPLOYED_ACA_SMOKE=1 after authorization to qualify "
        "deployed cold-start acceptance.",
        allow_module_level=True,
    )


@dataclass(frozen=True, slots=True)
class _Candidate:
    accepted: AcceptedRun
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class _Sample:
    candidate: _Candidate
    first_attempt_status: int
    first_attempt_acceptance_seconds: float
    total_acceptance_seconds: float
    first_event_seconds: float
    terminal_seconds: float
    retries: int
    result_available: bool
    first_attempt_failure: str | None


@dataclass(slots=True)
class _Progress:
    samples: list[_Sample]
    retries: int = 0


@dataclass(frozen=True, slots=True)
class _Admission:
    candidate: _Candidate
    first_status: int
    first_acceptance_seconds: float
    first_typed_setup_deadline: bool
    retries: int


@dataclass(frozen=True, slots=True)
class _AdmissionResponse:
    candidate: _Candidate | None
    retry: bool


@pytest.mark.live_aca
@pytest.mark.asyncio
async def test_deployed_aca_cold_start_acceptance_is_bounded_and_cleaned(
    request: pytest.FixtureRequest,
) -> None:
    """Run three fresh, sequential no-tools customer turns and retain cleanup candidates."""
    sample_count = cold_start_samples_from_option_or_environment(request.config)
    config = _cold_start_config(deployed_aca_lifecycle_config_from_environment())
    evidence = await acquire_default_authorization_evidence(config.deployed.token_scope)
    partition_key = owner_partition(
        EntraUserOwnerContext.create(
            config.app_identity,
            _COLD_START_AGENT_SLUG,
            evidence.tenant_id,
            evidence.object_id,
        )
    ).partition_key
    resources: DeployedAcaLifecycleResources | None = None
    candidates: list[_Candidate] = []
    attempted_keys: list[str] = []
    progress = _Progress(samples=[])
    cleanup_complete = False
    primary_error: BaseException | None = None
    try:
        resources = await open_deployed_aca_lifecycle_resources(config)
        headers = {
            "Authorization": evidence.authorization_header,
            "Content-Type": "application/json",
            "Prefer": "respond-async",
        }
        timeout = ClientTimeout(total=SAMPLE_WINDOW_SECONDS)
        async with ClientSession(timeout=timeout) as client:
            await _run_samples_sequentially(
                client,
                config,
                resources,
                partition_key,
                headers,
                sample_count,
                candidates,
                attempted_keys,
                progress,
            )
        _require(
            len({sample.candidate.accepted.session_id for sample in progress.samples}) == sample_count,
            "distinct_fresh_sessions_required",
        )
        _require(
            not any(sample.first_attempt_failure for sample in progress.samples),
            "first_attempt_slo_failed",
        )
    except BaseException as exc:
        primary_error = exc
    finally:
        if resources is not None:
            cleanup_complete, cleanup_error = await _finalize_cold_start_cleanup(
                resources, config, partition_key, attempted_keys, candidates
            )
            if primary_error is None and cleanup_error is not None:
                primary_error = cleanup_error
    _LOGGER.info(
        render_cold_start_report(
            sample_count=sample_count,
            retries=progress.retries,
            metrics=(
                cold_start_metrics(
                    [sample.first_attempt_acceptance_seconds for sample in progress.samples],
                    [sample.total_acceptance_seconds for sample in progress.samples],
                    [sample.first_event_seconds for sample in progress.samples],
                    [sample.terminal_seconds for sample in progress.samples],
                )
                if progress.samples
                else None
            ),
            cleanup_complete=cleanup_complete,
        )
    )
    if primary_error is not None:
        raise primary_error


def _cold_start_config(config: DeployedAcaLifecycleConfig) -> DeployedAcaLifecycleConfig:
    """Force the customer-facing no-tools fixture agent, never the load hold agent."""
    return replace(config, deployed=replace(config.deployed, agent_slug=_COLD_START_AGENT_SLUG))


async def _run_samples_sequentially(
    client: ClientSession,
    config: DeployedAcaLifecycleConfig,
    resources: DeployedAcaLifecycleResources,
    partition_key: str,
    headers: dict[str, str],
    sample_count: int,
    candidates: list[_Candidate],
    attempted_keys: list[str],
    progress: _Progress,
) -> None:
    for _ in range(sample_count):
        sample = await _run_cold_start_sample(
            client,
            config,
            resources,
            partition_key,
            headers,
            candidates,
            attempted_keys,
            progress,
        )
        progress.samples.append(sample)


async def _run_cold_start_sample(
    client: ClientSession,
    config: DeployedAcaLifecycleConfig,
    resources: DeployedAcaLifecycleResources,
    partition_key: str,
    headers: dict[str, str],
    candidates: list[_Candidate],
    attempted_keys: list[str],
    progress: _Progress,
) -> _Sample:
    try:
        async with asyncio.timeout(SAMPLE_WINDOW_SECONDS):
            return await _run_cold_start_sample_phases(
                client,
                config,
                resources,
                partition_key,
                headers,
                candidates,
                attempted_keys,
                progress,
            )
    except TimeoutError:
        raise AcaSmokeEnvironmentError("cold_start_sample_timeout") from None


async def _run_cold_start_sample_phases(
    client: ClientSession,
    config: DeployedAcaLifecycleConfig,
    resources: DeployedAcaLifecycleResources,
    partition_key: str,
    headers: dict[str, str],
    candidates: list[_Candidate],
    attempted_keys: list[str],
    progress: _Progress,
) -> _Sample:
    key = uuid.uuid4().hex
    attempted_keys.append(key)
    started_at = time.perf_counter()
    admission = await _admit_cold_start_candidate(
        client, config, resources, partition_key, headers, key, started_at, progress
    )
    candidates.append(admission.candidate)
    total_accepted_at = time.perf_counter()
    first_event_at, terminal_at = await _qualify_public_turn(
        client, admission.candidate, headers, started_at
    )
    return _Sample(
        candidate=admission.candidate,
        first_attempt_status=admission.first_status,
        first_attempt_acceptance_seconds=admission.first_acceptance_seconds,
        total_acceptance_seconds=total_accepted_at - started_at,
        first_event_seconds=first_event_at - started_at,
        terminal_seconds=terminal_at - started_at,
        retries=admission.retries,
        result_available=True,
        first_attempt_failure=first_attempt_slo_failure(
            status=admission.first_status,
            elapsed_seconds=admission.first_acceptance_seconds,
            typed_setup_deadline=admission.first_typed_setup_deadline,
        ),
    )


async def _admit_cold_start_candidate(
    client: ClientSession,
    config: DeployedAcaLifecycleConfig,
    resources: DeployedAcaLifecycleResources,
    partition_key: str,
    headers: dict[str, str],
    key: str,
    started_at: float,
    progress: _Progress,
) -> _Admission:
    first_status = 0
    first_elapsed = 0.0
    first_typed_setup_deadline = False
    retries = 0
    candidate: _Candidate | None = None
    admission_deadline = time.perf_counter() + ADMISSION_WINDOW_SECONDS
    for attempt in range(_SETUP_ATTEMPTS):
        try:
            async with asyncio.timeout(_remaining(admission_deadline, SETUP_ATTEMPT_TIMEOUT_SECONDS)):
                status, payload, response_headers = await json_request(
                    client,
                    "POST",
                    config.deployed.chat_url,
                    headers={**headers, "Idempotency-Key": key},
                    payload=submission_payload(_PROMPT),
                )
        except AcaSmokeEnvironmentError:
            candidate = await _recover_or_fail(
                resources,
                config,
                partition_key,
                key,
                admission_deadline=admission_deadline,
                category="cold_start_admission_ambiguous",
            )
            break
        except TimeoutError:
            candidate = await _recover_or_fail(
                resources,
                config,
                partition_key,
                key,
                admission_deadline=admission_deadline,
                category="cold_start_admission_timeout",
            )
            break
        if attempt == 0:
            first_status = status
            first_elapsed = time.perf_counter() - started_at
            first_typed_setup_deadline = (
                status == 504 and payload.get("error") == "setup_deadline_exceeded"
            )
        outcome = _classify_admission_response(status, payload, key, config)
        if outcome.retry and attempt + 1 < _SETUP_ATTEMPTS:
            retries += 1
            progress.retries += 1
            await asyncio.sleep(
                _remaining(admission_deadline, setup_retry_after_seconds(response_headers))
            )
            continue
        if outcome.candidate is not None:
            candidate = outcome.candidate
            break
        candidate = await _recover_or_fail(
            resources,
            config,
            partition_key,
            key,
            admission_deadline=admission_deadline,
            category="cold_start_admission_unresolved",
        )
        break
    if candidate is None:
        candidate = await _recover_or_fail(
            resources,
            config,
            partition_key,
            key,
            admission_deadline=admission_deadline,
            category="cold_start_admission_unresolved",
        )
    return _Admission(
        candidate=candidate,
        first_status=first_status,
        first_acceptance_seconds=first_elapsed,
        first_typed_setup_deadline=first_typed_setup_deadline,
        retries=retries,
    )


def _classify_admission_response(
    status: int,
    payload: dict[str, object],
    key: str,
    config: DeployedAcaLifecycleConfig,
) -> _AdmissionResponse:
    if status == 202:
        try:
            return _AdmissionResponse(
                candidate=_Candidate(parse_accepted_run(payload, config.deployed), key),
                retry=False,
            )
        except (AssertionError, ValueError):
            raise AssertionError("cold_start_accepted_response_invalid") from None
    if status == 504 and payload.get("error") == "setup_deadline_exceeded":
        return _AdmissionResponse(candidate=None, retry=True)
    return _AdmissionResponse(candidate=None, retry=False)


async def _recover_or_fail(
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    partition_key: str,
    key: str,
    *,
    admission_deadline: float,
    category: str,
) -> _Candidate:
    candidate = await _recover_candidate(
        resources, config, partition_key, key, deadline=_recovery_deadline(admission_deadline)
    )
    if candidate is None:
        raise AcaSmokeEnvironmentError(category) from None
    return candidate


async def _qualify_public_turn(
    client: ClientSession,
    candidate: _Candidate,
    headers: dict[str, str],
    started_at: float,
) -> tuple[float, float]:
    sse_deadline = time.perf_counter() + SSE_TERMINAL_WINDOW_SECONDS
    try:
        status, events, _, first_event_at = await read_sse_events_with_first_event_time(
            client,
            candidate.accepted.management_urls["events_url"],
            headers={"Authorization": headers["Authorization"]},
            overall_timeout_seconds=_remaining(sse_deadline, SSE_TERMINAL_WINDOW_SECONDS),
        )
    except AcaSmokeEnvironmentError:
        raise AcaSmokeEnvironmentError("cold_start_sse_unavailable") from None
    _validate_terminal_events(status, events, first_event_at)
    terminal_at = time.perf_counter()
    public_deadline = time.perf_counter() + _PUBLIC_TERMINAL_WINDOW_SECONDS
    async with asyncio.timeout(_remaining(public_deadline, _PUBLIC_TERMINAL_WINDOW_SECONDS)):
        try:
            status_code, public_status, _ = await json_request(
                client,
                "GET",
                candidate.accepted.management_urls["status_url"],
                headers={"Authorization": headers["Authorization"]},
            )
        except AcaSmokeEnvironmentError:
            raise AcaSmokeEnvironmentError("cold_start_public_status_unavailable") from None
        _validate_public_status(status_code, public_status, candidate)
        try:
            result_code, public_result, _ = await json_request(
                client,
                "GET",
                candidate.accepted.management_urls["result_url"],
                headers={"Authorization": headers["Authorization"]},
            )
        except AcaSmokeEnvironmentError:
            raise AcaSmokeEnvironmentError("cold_start_public_result_unavailable") from None
        _validate_public_result(result_code, public_result, candidate)
    return first_event_at, terminal_at


async def _recover_candidate(
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    partition_key: str,
    idempotency_key: str,
    *,
    deadline: float,
) -> _Candidate | None:
    """Recover only through the owner reservation; this test never writes Table rows."""
    while True:
        record = await read_owner_idempotency(
            resources, partition_key=partition_key, idempotency_key=idempotency_key
        )
        if record is not None:
            return _Candidate(
                AcceptedRun(
                    session_id=record.session_id,
                    run_id=record.run_id,
                    management_urls=config.deployed.management_urls(
                        session_id=record.session_id, run_id=record.run_id
                    ),
                ),
                idempotency_key,
            )
        if time.perf_counter() >= deadline:
            return None
        await asyncio.sleep(_remaining(deadline, _RECOVERY_POLL_SECONDS))


async def _recover_cleanup_candidates(
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    partition_key: str,
    attempted_keys: list[str],
    candidates: list[_Candidate],
) -> tuple[str, ...]:
    represented = {candidate.idempotency_key for candidate in candidates}
    deadline = time.perf_counter() + FINAL_RECOVERY_WINDOW_SECONDS
    unresolved: list[str] = []
    for key in dict.fromkeys(attempted_keys):
        if key not in represented:
            try:
                candidate = await _recover_candidate(
                    resources, config, partition_key, key, deadline=deadline
                )
            except (AcaSmokeEnvironmentError, AssertionError, TimeoutError):
                unresolved.append(key)
                continue
            if candidate is None:
                unresolved.append(key)
                continue
            candidates.append(candidate)
            represented.add(key)
    return tuple(unresolved)


async def _cleanup_candidates(
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    partition_key: str,
    candidates: list[_Candidate],
) -> None:
    """Use exact labels for provider cleanup and leave durable state to the controller."""
    failure_category: str | None = None
    for candidate in {
        (item.accepted.session_id, item.accepted.run_id): item for item in candidates
    }.values():
        try:
            async with asyncio.timeout(_CLEANUP_CANDIDATE_TIMEOUT_SECONDS):
                session = await read_authoritative_session(
                    resources,
                    session_id=candidate.accepted.session_id,
                    partition_key=partition_key,
                )
                assert_session_belongs_to_deployment(session, config)
                await cleanup_owned_lifecycle_session(
                    resources, session=session, config=config, partition_key=partition_key
                )
                _require(await owned_sandbox(resources, session) is None, "cleanup_backing_remaining")
                _require(not await owned_snapshots(resources, session), "cleanup_snapshot_remaining")
        except TimeoutError:
            failure_category = "cold_start_cleanup_candidate_timeout"
        except (AcaSmokeEnvironmentError, AssertionError):
            failure_category = "cold_start_cleanup_controller_failed"
    if failure_category is not None:
        raise AcaSmokeEnvironmentError(failure_category) from None


async def _finalize_cold_start_cleanup(
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    partition_key: str,
    attempted_keys: list[str],
    candidates: list[_Candidate],
) -> tuple[bool, BaseException | None]:
    """Recover, provider-clean, and close; leave durable Table state to the controller."""
    cleanup_complete = False
    cleanup_error: BaseException | None = None
    try:
        unresolved_keys = await _recover_cleanup_candidates(
            resources, config, partition_key, attempted_keys, candidates
        )
        cleanup_failure: AcaSmokeEnvironmentError | None = None
        try:
            await _cleanup_candidates(resources, config, partition_key, candidates)
        except AcaSmokeEnvironmentError as exc:
            cleanup_failure = exc
        if unresolved_keys:
            cleanup_error = AcaSmokeEnvironmentError(
                "cold_start_cleanup_incomplete_unresolved_idempotency"
            )
        elif cleanup_failure is not None:
            cleanup_error = _sanitized_cleanup_error(cleanup_failure)
        else:
            cleanup_complete = True
    except AcaSmokeEnvironmentError as exc:
        cleanup_error = _sanitized_cleanup_error(exc)
    except (AssertionError, TimeoutError):
        cleanup_error = AcaSmokeEnvironmentError("cold_start_cleanup_incomplete_controller_failed")
    finally:
        await resources.close()
    return cleanup_complete, cleanup_error


def _remaining(deadline: float, cap_seconds: float) -> float:
    remaining = deadline - time.perf_counter()
    if remaining <= 0:
        raise AcaSmokeEnvironmentError("cold_start_phase_timeout")
    return min(remaining, cap_seconds)


def _recovery_deadline(phase_deadline: float) -> float:
    return min(phase_deadline, time.perf_counter() + FINAL_RECOVERY_WINDOW_SECONDS)


def _sanitized_cleanup_error(error: AcaSmokeEnvironmentError) -> AcaSmokeEnvironmentError:
    if "cold_start_cleanup_unresolved_idempotency" in str(error):
        return AcaSmokeEnvironmentError("cold_start_cleanup_incomplete_unresolved_idempotency")
    if "cold_start_cleanup_candidate_timeout" in str(error):
        return AcaSmokeEnvironmentError("cold_start_cleanup_incomplete_candidate_timeout")
    return AcaSmokeEnvironmentError("cold_start_cleanup_incomplete_controller_failed")


def _require(condition: bool, category: str) -> None:
    if not condition:
        raise AssertionError(f"cold_start_{category}")


def _validate_terminal_events(
    status: int,
    events: list[object],
    first_event_at: float | None,
) -> None:
    terminal_type = (
        events[-1].payload.get("type")  # type: ignore[union-attr]
        if events and hasattr(events[-1], "payload")
        else None
    )
    _require(status == 200 and terminal_type == "done" and first_event_at is not None, "sse_invalid")


def _validate_public_status(
    status_code: int,
    status: dict[str, object],
    candidate: _Candidate,
) -> None:
    _require(
        status_code == 200
        and status.get("session_id") == candidate.accepted.session_id
        and status.get("run_id") == candidate.accepted.run_id
        and status.get("state") == "succeeded"
        and status.get("result_available") is True,
        "public_status_invalid",
    )


def _validate_public_result(
    result_code: int,
    result: dict[str, object],
    candidate: _Candidate,
) -> None:
    _require(
        result_code == 200
        and result.get("session_id") == candidate.accepted.session_id
        and result.get("run_id") == candidate.accepted.run_id
        and isinstance(result.get("result"), dict),
        "public_result_invalid",
    )
