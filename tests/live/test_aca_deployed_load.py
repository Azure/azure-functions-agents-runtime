"""Manual-only real ACA load qualification through the deployed Function endpoint."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime

import pytest
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from azure.core.exceptions import AzureError
from tests.aca_smoke_diagnostics import AcaSmokeEnvironmentError
from tests.live.aca_deployed_agent_support import (
    AcceptedRun,
    SseEvent,
    acquire_default_authorization_evidence,
    deployed_aca_smoke_enabled,
    json_request,
    parse_accepted_run,
    read_sse_events_with_first_event_time,
    setup_retry_after_seconds,
    submission_payload,
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
    read_authoritative_run,
    read_authoritative_session,
    read_owner_idempotency,
    read_session_operations,
)
from tests.live.aca_deployed_load_support import (
    CommonActiveInterval,
    latency_metrics,
    render_load_report,
    require_load_concurrency,
    utc_now,
)

from azure_functions_agents.session_state import (
    TERMINAL_RUN_STATUSES,
    DurableSessionOperation,
    DurableSessionRecord,
    EntraUserOwnerContext,
    owner_partition,
)
from azure_functions_agents.transport.transport_models import SandboxTransportError

_LOGGER = logging.getLogger(__name__)
_ACTIVE_STATES = frozenset({"accepted", "running"})
_LOAD_AGENT_SLUG = "deployed_load"
_POLL_SECONDS = 1.0
_COMMON_ACTIVE_WAIT_SECONDS = 1.0
_ACTIVE_PROOF_TIMEOUT_SECONDS = 120.0
_EVENT_STREAM_GRACE_SECONDS = 360.0
_HOLD_SECONDS = 300.0
_MINIMUM_HOLD_TERMINAL_SECONDS = _HOLD_SECONDS - 1.0
_SETUP_DEADLINE_ATTEMPTS = 6
_RECOVERY_ATTEMPTS = 5
_RECOVERY_POLL_SECONDS = 1.0
_OVERLAP_BUDGET_MARGIN_SECONDS = 15.0
_SETTLEMENT_TIMEOUT_SECONDS = 900.0
_RACE_SAMPLE_LIMIT = 5
_CONNECTION_HEADROOM = 10
_LOAD_PROMPT = "Call qualification_hold exactly once, then return a brief acknowledgement."

if not deployed_aca_smoke_enabled():
    pytest.skip(
        "Set AZURE_FUNCTIONS_AGENTS_RUN_DEPLOYED_ACA_SMOKE=1 after authorization to qualify "
        "the deployed ACA load path.",
        allow_module_level=True,
    )


@dataclass(frozen=True, slots=True)
class _SubmittedRun:
    accepted: AcceptedRun
    idempotency_key: str
    submitted_at: float
    accepted_at: float


@dataclass(frozen=True, slots=True)
class _EventEvidence:
    first_event_at: float
    terminal_at: float


@dataclass(frozen=True, slots=True)
class _ActiveObservation:
    started_monotonic: float
    completed_monotonic: float
    started_utc: datetime
    completed_utc: datetime


@dataclass(frozen=True, slots=True)
class _AdmissionOutcome:
    idempotency_key: str
    submitted: _SubmittedRun | None
    retries: int
    unclassified_service_throttles: int
    failure: str | None
    unresolved_idempotency: bool


@dataclass(frozen=True, slots=True)
class _AdmissionSummary:
    retries: int
    unclassified_service_throttles: int
    unresolved_idempotencies: int
    attempted_idempotency_keys: tuple[str, ...]


class _AdmissionFailureError(AcaSmokeEnvironmentError):
    """Sanitized aggregate admission failure that retains all prior admissions for cleanup."""

    def __init__(
        self,
        *,
        failures: int,
        retries: int,
        throttles: int,
        unresolved_idempotencies: int,
        attempted_idempotency_keys: tuple[str, ...],
    ) -> None:
        super().__init__(
            f"{failures} load admissions failed; "
            f"unclassified_service_throttles={throttles}; retries={retries}; "
            f"unresolved_idempotencies={unresolved_idempotencies}."
        )
        self.retries = retries
        self.throttles = throttles
        self.unresolved_idempotencies = unresolved_idempotencies
        self.attempted_idempotency_keys = attempted_idempotency_keys


@pytest.mark.live_aca
@pytest.mark.asyncio
async def test_deployed_aca_load_has_a_common_durable_active_interval(
    request: pytest.FixtureRequest,
) -> None:
    """Qualify N real held model turns with public races and read-only durable evidence."""
    concurrency = require_load_concurrency(request.config)
    config = _load_config(deployed_aca_lifecycle_config_from_environment())
    authorization_evidence = await acquire_default_authorization_evidence(config.deployed.token_scope)
    partition_key = owner_partition(
        EntraUserOwnerContext.create(
            config.app_identity,
            _LOAD_AGENT_SLUG,
            authorization_evidence.tenant_id,
            authorization_evidence.object_id,
        )
    ).partition_key
    resources: DeployedAcaLifecycleResources | None = None
    submitted: list[_SubmittedRun] = []
    event_tasks: list[asyncio.Task[_EventEvidence]] = []
    common_interval: CommonActiveInterval | None = None
    succeeded_count = 0
    replay_count = 0
    active_run_conflict_count = 0
    retry_count = 0
    unclassified_service_throttle_count = 0
    unresolved_idempotency_count = 0
    cleanup_complete = False
    settlement_complete = True
    metrics = None
    cleanup_error: BaseException | None = None
    try:
        resources = await open_deployed_aca_lifecycle_resources(config)
        authorization = authorization_evidence.authorization_header
        headers = {
            "Authorization": authorization,
            "Content-Type": "application/json",
            "Prefer": "respond-async",
        }
        timeout = ClientTimeout(total=config.deployed.timeout_seconds + _EVENT_STREAM_GRACE_SECONDS)
        connector_limit = concurrency + _CONNECTION_HEADROOM
        async with (
            ClientSession(timeout=timeout, connector=TCPConnector(limit=connector_limit)) as control,
            ClientSession(timeout=timeout, connector=TCPConnector(limit=connector_limit)) as events,
        ):
            try:
                try:
                    admission = await _submit_distinct_sessions(
                        control,
                        config,
                        headers,
                        concurrency,
                        submitted,
                        resources,
                        partition_key,
                    )
                except _AdmissionFailureError as exc:
                    retry_count += exc.retries
                    unclassified_service_throttle_count += exc.throttles
                    unresolved_idempotency_count += exc.unresolved_idempotencies
                    raise
                retry_count += admission.retries
                unclassified_service_throttle_count += admission.unclassified_service_throttles
                unresolved_idempotency_count += admission.unresolved_idempotencies
                _assert_distinct_admissions(submitted, concurrency)
                _assert_remaining_hold_budget(submitted)
                event_tasks = [
                    asyncio.create_task(_read_events(events, item, authorization)) for item in submitted
                ]
                common_interval, replay_count, active_run_conflict_count = (
                    await _establish_common_active_interval(
                        resources,
                        config,
                        partition_key,
                        submitted,
                        control,
                        headers,
                    )
                )
                event_evidence = await asyncio.gather(*event_tasks)
                _assert_hold_duration(submitted, event_evidence)
                await _assert_terminal_public_results(control, submitted, authorization)
                await _assert_terminal_durable_state(resources, config, partition_key, submitted)
                succeeded_count = len(submitted)
                metrics = latency_metrics(
                    [item.accepted_at - item.submitted_at for item in submitted],
                    [
                        event.first_event_at - item.submitted_at
                        for item, event in zip(submitted, event_evidence, strict=True)
                    ],
                    [
                        event.terminal_at - item.submitted_at
                        for item, event in zip(submitted, event_evidence, strict=True)
                    ],
                )
            finally:
                inner_primary_error = sys.exception()
                for task in event_tasks:
                    if not task.done():
                        task.cancel()
                if event_tasks:
                    await asyncio.gather(*event_tasks, return_exceptions=True)
                if inner_primary_error is not None and submitted:
                    try:
                        await _settle_failed_runs(
                            resources,
                            config,
                            partition_key,
                            submitted,
                            control,
                            authorization,
                        )
                    except (AcaSmokeEnvironmentError, AssertionError):
                        settlement_complete = False
                        _note_settlement_failure(inner_primary_error)
    finally:
        primary_error = sys.exception()
        try:
            if resources is not None and submitted:
                if settlement_complete:
                    cleanup_complete = await _cleanup_load_sessions(
                        resources, config, partition_key, submitted
                    )
                else:
                    await _provider_cleanup_last_resort(resources, config, partition_key, submitted)
                    cleanup_complete = False
            if unresolved_idempotency_count:
                cleanup_complete = False
        except (AcaSmokeEnvironmentError, AssertionError) as exc:
            cleanup_error = exc
        finally:
            if resources is not None:
                await resources.close()
        _LOGGER.info(
            "%s",
            render_load_report(
                concurrency=concurrency,
                common_interval=common_interval,
                admitted_count=len(submitted),
                succeeded_count=succeeded_count,
                metrics=metrics,
                replay_count=replay_count,
                active_run_conflict_count=active_run_conflict_count,
                retry_count=retry_count,
                unclassified_service_throttle_count=unclassified_service_throttle_count,
                unresolved_idempotency_count=unresolved_idempotency_count,
                cleanup_complete=cleanup_complete,
            ),
        )
        if cleanup_error is not None:
            _raise_or_note_cleanup_failure(primary_error, cleanup_error)
    assert cleanup_complete


def _load_config(config: DeployedAcaLifecycleConfig) -> DeployedAcaLifecycleConfig:
    """Use the fixture's fixed load-only agent without adding a deployment setting."""
    return replace(config, deployed=replace(config.deployed, agent_slug=_LOAD_AGENT_SLUG))


def _raise_or_note_cleanup_failure(
    primary_error: BaseException | None,
    cleanup_error: BaseException,
) -> None:
    """Keep a product failure primary while retaining a redacted cleanup diagnostic."""
    if primary_error is not None:
        primary_error.add_note("ACA load cleanup also failed after admitted-session preservation.")
        return
    raise AcaSmokeEnvironmentError(
        "ACA load cleanup did not complete after preserving admitted session candidates."
    ) from cleanup_error


def _note_settlement_failure(primary_error: BaseException) -> None:
    primary_error.add_note("ACA load durable settlement also failed before provider cleanup.")


async def _submit_distinct_sessions(
    client: ClientSession,
    config: DeployedAcaLifecycleConfig,
    headers: dict[str, str],
    concurrency: int,
    submitted: list[_SubmittedRun],
    resources: DeployedAcaLifecycleResources,
    partition_key: str,
) -> _AdmissionSummary:
    attempted_idempotency_keys: list[str] = []
    results = await asyncio.gather(
        *(
            _submit_one(
                client,
                config,
                headers,
                resources,
                partition_key,
                attempted_idempotency_keys,
            )
            for _ in range(concurrency)
        ),
        return_exceptions=True,
    )
    outcomes = [result for result in results if isinstance(result, _AdmissionOutcome)]
    submitted.extend(outcome.submitted for outcome in outcomes if outcome.submitted is not None)
    retries = sum(outcome.retries for outcome in outcomes)
    throttles = sum(outcome.unclassified_service_throttles for outcome in outcomes)
    unresolved = sum(outcome.unresolved_idempotency for outcome in outcomes)
    failures = [outcome.failure for outcome in outcomes if outcome.failure is not None]
    unexpected = [
        result
        for result in results
        if isinstance(result, BaseException)
        and not isinstance(result, (AcaSmokeEnvironmentError, AssertionError))
    ]
    if unexpected:
        raise unexpected[0]
    exceptions = [
        result
        for result in results
        if isinstance(result, (AcaSmokeEnvironmentError, AssertionError))
    ]
    retained_keys = tuple(attempted_idempotency_keys) or tuple(
        outcome.idempotency_key for outcome in outcomes
    )
    if failures or exceptions:
        cause = exceptions[0] if exceptions else None
        error = _AdmissionFailureError(
            failures=len(failures) + len(exceptions),
            retries=retries,
            throttles=throttles,
            unresolved_idempotencies=unresolved,
            attempted_idempotency_keys=retained_keys,
        )
        if cause is not None:
            raise error from cause
        raise _AdmissionFailureError(
            failures=len(failures),
            retries=retries,
            throttles=throttles,
            unresolved_idempotencies=unresolved,
            attempted_idempotency_keys=retained_keys,
        )
    return _AdmissionSummary(
        retries=retries,
        unclassified_service_throttles=throttles,
        unresolved_idempotencies=unresolved,
        attempted_idempotency_keys=retained_keys,
    )


async def _submit_one(
    client: ClientSession,
    config: DeployedAcaLifecycleConfig,
    headers: dict[str, str],
    resources: DeployedAcaLifecycleResources,
    partition_key: str,
    attempted_idempotency_keys: list[str],
) -> _AdmissionOutcome:
    idempotency_key = uuid.uuid4().hex
    attempted_idempotency_keys.append(idempotency_key)
    submitted_at = time.perf_counter()
    retries = 0
    for attempt in range(_SETUP_DEADLINE_ATTEMPTS):
        try:
            status, payload, response_headers = await json_request(
                client,
                "POST",
                config.deployed.chat_url,
                headers={**headers, "Idempotency-Key": idempotency_key},
                payload=submission_payload(_LOAD_PROMPT),
            )
        except AcaSmokeEnvironmentError:
            recovered = await _recover_submitted_run(
                resources,
                config,
                partition_key,
                idempotency_key,
                submitted_at,
            )
            return _AdmissionOutcome(
                idempotency_key,
                recovered,
                retries,
                0,
                "public_admission_request_ambiguous",
                recovered is None,
            )
        accepted_at = time.perf_counter()
        if status == 202:
            try:
                accepted = parse_accepted_run(payload, config.deployed)
            except (AssertionError, ValueError):
                recovered = await _recover_submitted_run(
                    resources,
                    config,
                    partition_key,
                    idempotency_key,
                    submitted_at,
                )
                return _AdmissionOutcome(
                    idempotency_key,
                    recovered,
                    retries,
                    0,
                    "public_admission_response_invalid",
                    recovered is None,
                )
            return _AdmissionOutcome(
                idempotency_key,
                _SubmittedRun(accepted, idempotency_key, submitted_at, accepted_at),
                retries,
                0,
                None,
                False,
            )
        if status in {429, 503}:
            return await _recover_ambiguous_http_outcome(
                resources,
                config,
                partition_key,
                idempotency_key,
                submitted_at,
                retries,
                status,
                unclassified_service_throttles=1,
            )
        if status == 504 and payload.get("error") == "setup_deadline_exceeded":
            if attempt + 1 < _SETUP_DEADLINE_ATTEMPTS:
                retries += 1
                await asyncio.sleep(setup_retry_after_seconds(response_headers))
                continue
            recovered = await _recover_submitted_run(
                resources,
                config,
                partition_key,
                idempotency_key,
                submitted_at,
            )
            return _AdmissionOutcome(
                idempotency_key,
                recovered,
                retries,
                0,
                "setup_deadline_exceeded",
                recovered is None,
            )
        if status in {400, 401, 403, 404}:
            return _AdmissionOutcome(
                idempotency_key, None, retries, 0, f"public_admission_http_{status}", False
            )
        return await _recover_ambiguous_http_outcome(
            resources,
            config,
            partition_key,
            idempotency_key,
            submitted_at,
            retries,
            status,
            unclassified_service_throttles=0,
        )
    raise AssertionError("setup-deadline admission loop must return an outcome")


async def _recover_submitted_run(
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    partition_key: str,
    idempotency_key: str,
    submitted_at: float,
) -> _SubmittedRun | None:
    """Find an ambiguous admission through its owner-scoped durable reservation."""
    for attempt in range(_RECOVERY_ATTEMPTS):
        record = await read_owner_idempotency(
            resources,
            partition_key=partition_key,
            idempotency_key=idempotency_key,
        )
        if record is not None:
            accepted = AcceptedRun(
                session_id=record.session_id,
                run_id=record.run_id,
                management_urls=config.deployed.management_urls(
                    session_id=record.session_id,
                    run_id=record.run_id,
                ),
            )
            return _SubmittedRun(
                accepted=accepted,
                idempotency_key=idempotency_key,
                submitted_at=submitted_at,
                accepted_at=time.perf_counter(),
            )
        if attempt + 1 < _RECOVERY_ATTEMPTS:
            await asyncio.sleep(_RECOVERY_POLL_SECONDS)
    return None


async def _recover_ambiguous_http_outcome(
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    partition_key: str,
    idempotency_key: str,
    submitted_at: float,
    retries: int,
    status: int,
    *,
    unclassified_service_throttles: int,
) -> _AdmissionOutcome:
    recovered = await _recover_submitted_run(
        resources,
        config,
        partition_key,
        idempotency_key,
        submitted_at,
    )
    return _AdmissionOutcome(
        idempotency_key,
        recovered,
        retries,
        unclassified_service_throttles,
        f"ambiguous_public_admission_http_{status}",
        recovered is None,
    )


def _assert_remaining_hold_budget(submitted: list[_SubmittedRun]) -> None:
    """Reject runs that cannot leave the formal proof enough shared hold time."""
    accepted_times = [item.accepted_at for item in submitted]
    admission_spread = max(accepted_times) - min(accepted_times)
    required_budget = _ACTIVE_PROOF_TIMEOUT_SECONDS + _OVERLAP_BUDGET_MARGIN_SECONDS
    remaining_budget = _HOLD_SECONDS - admission_spread
    assert remaining_budget > required_budget, (
        "Admission spread leaves insufficient remaining qualification hold: "
        f"remaining={remaining_budget:.1f}s required>{required_budget:.1f}s."
    )


def _assert_distinct_admissions(submitted: list[_SubmittedRun], concurrency: int) -> None:
    assert len(submitted) == concurrency
    assert len({item.accepted.session_id for item in submitted}) == concurrency
    assert len({item.accepted.run_id for item in submitted}) == concurrency
    assert len({item.idempotency_key for item in submitted}) == concurrency


async def _establish_common_active_interval(
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    partition_key: str,
    submitted: list[_SubmittedRun],
    client: ClientSession,
    headers: dict[str, str],
) -> tuple[CommonActiveInterval, int, int]:
    deadline = time.perf_counter() + _ACTIVE_PROOF_TIMEOUT_SECONDS
    while True:
        first = await _active_observations(resources, config, partition_key, submitted)
        if first is not None:
            replay_count, conflict_count = await _exercise_active_races(
                client, config, headers, submitted[:_RACE_SAMPLE_LIMIT]
            )
            await asyncio.sleep(_COMMON_ACTIVE_WAIT_SECONDS)
            second = await _active_observations(resources, config, partition_key, submitted)
            if second is not None:
                interval = _overlapping_interval(first, second)
                if interval is not None:
                    return interval, replay_count, conflict_count
        if time.perf_counter() >= deadline:
            raise AssertionError(
                "All admitted runs never had conservative overlapping accepted/running observations."
            )
        await asyncio.sleep(_POLL_SECONDS)


async def _active_observations(
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    partition_key: str,
    submitted: list[_SubmittedRun],
) -> list[_ActiveObservation] | None:
    observations = await asyncio.gather(
        *(_read_active_observation(resources, config, partition_key, item) for item in submitted)
    )
    if any(observation is None for observation in observations):
        return None
    return [observation for observation in observations if observation is not None]


def _overlapping_interval(
    first: list[_ActiveObservation], second: list[_ActiveObservation]
) -> CommonActiveInterval | None:
    first_completion = max(first, key=lambda observation: observation.completed_monotonic)
    second_start = min(second, key=lambda observation: observation.started_monotonic)
    if first_completion.completed_monotonic >= second_start.started_monotonic:
        return None
    return CommonActiveInterval(
        started_at=first_completion.completed_utc,
        ended_at=second_start.started_utc,
    )


async def _read_active_observation(
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    partition_key: str,
    submitted: _SubmittedRun,
) -> _ActiveObservation | None:
    started_monotonic = time.perf_counter()
    started_utc = utc_now()
    accepted = submitted.accepted
    session, run, operations = await asyncio.gather(
        read_authoritative_session(
            resources, session_id=accepted.session_id, partition_key=partition_key
        ),
        read_authoritative_run(
            resources,
            session_id=accepted.session_id,
            run_id=accepted.run_id,
            partition_key=partition_key,
        ),
        read_session_operations(
            resources, session_id=accepted.session_id, partition_key=partition_key
        ),
    )
    completed_monotonic = time.perf_counter()
    completed_utc = utc_now()
    assert_session_belongs_to_deployment(session, config)
    if (
        session.status != "running"
        or run.status not in _ACTIVE_STATES
        or session.active_run_id != accepted.run_id
    ):
        return None
    _assert_active_operation_consistency(session, operations, accepted.run_id)
    return _ActiveObservation(
        started_monotonic, completed_monotonic, started_utc, completed_utc
    )


def _assert_active_operation_consistency(
    session: DurableSessionRecord,
    operations: tuple[DurableSessionOperation, ...],
    run_id: str,
) -> None:
    active_operations = [operation for operation in operations if operation.state == "active"]
    active_operation_id = session.active_operation_id
    if active_operation_id is None:
        assert not active_operations
        return
    assert len(active_operations) == 1
    active = active_operations[0]
    assert active.operation_id == active_operation_id
    assert active.target.run_id == run_id
    assert active.target.generation == session.generation


async def _exercise_active_races(
    client: ClientSession,
    config: DeployedAcaLifecycleConfig,
    headers: dict[str, str],
    sample: list[_SubmittedRun],
) -> tuple[int, int]:
    outcomes = await asyncio.gather(
        *(_exercise_one_active_race(client, config, headers, item) for item in sample)
    )
    return sum(replays for replays, _ in outcomes), sum(conflicts for _, conflicts in outcomes)


async def _exercise_one_active_race(
    client: ClientSession,
    config: DeployedAcaLifecycleConfig,
    headers: dict[str, str],
    submitted: _SubmittedRun,
) -> tuple[int, int]:
    accepted = submitted.accepted
    replay_status, replay_payload, _ = await json_request(
        client,
        "POST",
        config.deployed.chat_url,
        headers={
            **headers,
            "Idempotency-Key": submitted.idempotency_key,
        },
        payload=submission_payload(_LOAD_PROMPT),
    )
    assert replay_status == 202
    replay = parse_accepted_run(replay_payload, config.deployed)
    assert replay.session_id == accepted.session_id
    assert replay.run_id == accepted.run_id
    conflict_status, conflict_payload, _ = await json_request(
        client,
        "POST",
        config.deployed.chat_url,
        headers={
            **headers,
            "Idempotency-Key": uuid.uuid4().hex,
            "x-ms-session-id": accepted.session_id,
        },
        payload=submission_payload(_LOAD_PROMPT),
    )
    assert conflict_status == 409
    assert conflict_payload.get("error") == "active_run_exists"
    return 1, 1


async def _read_events(
    client: ClientSession,
    submitted: _SubmittedRun,
    authorization: str,
) -> _EventEvidence:
    status, events, _, first_event_at = await read_sse_events_with_first_event_time(
        client,
        submitted.accepted.management_urls["events_url"],
        headers={"Authorization": authorization},
        overall_timeout_seconds=_HOLD_SECONDS + _EVENT_STREAM_GRACE_SECONDS,
    )
    terminal_at = time.perf_counter()
    assert status == 200
    assert first_event_at is not None
    assert events
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert events[-1].payload.get("type") == "done"
    _assert_public_hold_events(events)
    return _EventEvidence(first_event_at=first_event_at, terminal_at=terminal_at)


def _assert_public_hold_events(events: list[SseEvent]) -> None:
    tool_events = [
        event.payload
        for event in events
        if event.payload.get("tool_name") == "qualification_hold"
    ]
    assert [event.get("type") for event in tool_events] == ["tool_start", "tool_end"]


def _assert_hold_duration(
    submitted: list[_SubmittedRun], evidence: list[_EventEvidence]
) -> None:
    terminal_latencies = [
        event.terminal_at - item.submitted_at
        for item, event in zip(submitted, evidence, strict=True)
    ]
    assert all(latency >= _MINIMUM_HOLD_TERMINAL_SECONDS for latency in terminal_latencies)


async def _assert_terminal_public_results(
    client: ClientSession, submitted: list[_SubmittedRun], authorization: str
) -> None:
    responses = await asyncio.gather(
        *(_read_terminal_result(client, item, authorization) for item in submitted)
    )
    assert all(responses)


async def _read_terminal_result(
    client: ClientSession, submitted: _SubmittedRun, authorization: str
) -> bool:
    accepted = submitted.accepted
    status_code, status, _ = await json_request(
        client, "GET", accepted.management_urls["status_url"], headers={"Authorization": authorization}
    )
    assert status_code == 200
    assert status.get("state") == "succeeded"
    result_code, result, _ = await json_request(
        client, "GET", accepted.management_urls["result_url"], headers={"Authorization": authorization}
    )
    assert result_code == 200
    assert isinstance(result.get("result"), dict)
    return True


async def _assert_terminal_durable_state(
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    partition_key: str,
    submitted: list[_SubmittedRun],
) -> None:
    observations = await asyncio.gather(
        *(
            _read_terminal_observation(resources, config, partition_key, item)
            for item in submitted
        )
    )
    assert all(observations)


async def _read_terminal_observation(
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    partition_key: str,
    submitted: _SubmittedRun,
) -> bool:
    accepted = submitted.accepted
    session, run, operations = await asyncio.gather(
        read_authoritative_session(
            resources, session_id=accepted.session_id, partition_key=partition_key
        ),
        read_authoritative_run(
            resources,
            session_id=accepted.session_id,
            run_id=accepted.run_id,
            partition_key=partition_key,
        ),
        read_session_operations(
            resources, session_id=accepted.session_id, partition_key=partition_key
        ),
    )
    assert_session_belongs_to_deployment(session, config)
    assert run.status == "succeeded"
    assert run.result_available
    assert session.active_run_id is None
    assert session.active_operation_id is None
    assert not [operation for operation in operations if operation.state == "active"]
    return True


async def _settle_failed_runs(
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    partition_key: str,
    submitted: list[_SubmittedRun],
    client: ClientSession,
    authorization: str,
) -> None:
    """Use only public cancellation, then wait for durable slots to become terminal and idle."""
    outcomes = await asyncio.gather(
        *(
            _settle_one_failed_run(
                resources,
                config,
                partition_key,
                item,
                client,
                authorization,
            )
            for item in submitted
        ),
        return_exceptions=True,
    )
    failures = [
        outcome
        for outcome in outcomes
        if isinstance(outcome, (AcaSmokeEnvironmentError, AssertionError))
    ]
    if failures:
        raise AcaSmokeEnvironmentError(
            "One or more failed load runs did not settle to a terminal idle durable state."
        ) from failures[0]
    unexpected = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    if unexpected:
        raise AcaSmokeEnvironmentError(
            "A failed load-run settlement could not complete its read-only observation."
        ) from unexpected[0]


async def _settle_one_failed_run(
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    partition_key: str,
    submitted: _SubmittedRun,
    client: ClientSession,
    authorization: str,
) -> None:
    """Allow provisioning to settle before a single public cancellation request."""
    deadline = time.perf_counter() + _SETTLEMENT_TIMEOUT_SECONDS
    cancel_requested = False
    accepted = submitted.accepted
    while True:
        session, run, operations = await asyncio.gather(
            read_authoritative_session(
                resources, session_id=accepted.session_id, partition_key=partition_key
            ),
            read_authoritative_run(
                resources,
                session_id=accepted.session_id,
                run_id=accepted.run_id,
                partition_key=partition_key,
            ),
            read_session_operations(
                resources, session_id=accepted.session_id, partition_key=partition_key
            ),
        )
        assert_session_belongs_to_deployment(session, config)
        if run.status in TERMINAL_RUN_STATUSES:
            if (
                session.active_run_id is None
                and session.active_operation_id is None
                and not [operation for operation in operations if operation.state == "active"]
            ):
                return
        elif session.status in {"running", "canceling"} and not cancel_requested:
            status, projection, _ = await json_request(
                client,
                "POST",
                accepted.management_urls["cancel_url"],
                headers={"Authorization": authorization},
            )
            assert status == 200
            assert projection.get("state") in TERMINAL_RUN_STATUSES
            cancel_requested = True
        if time.perf_counter() >= deadline:
            raise AcaSmokeEnvironmentError(
                "Failed load runs did not settle from provisioning to a terminal idle durable state."
            )
        await asyncio.sleep(_POLL_SECONDS)


async def _provider_cleanup_last_resort(
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    partition_key: str,
    submitted: list[_SubmittedRun],
) -> None:
    """Remove only exact-label provider backing after a failed durable settlement."""
    sessions = await asyncio.gather(
        *(
            read_authoritative_session(
                resources, session_id=item.accepted.session_id, partition_key=partition_key
            )
            for item in submitted
        )
    )
    sessions_with_backing = []
    for session in sessions:
        assert_session_belongs_to_deployment(session, config)
        if session.sandbox_id is None:
            continue
        sessions_with_backing.append(session)
        try:
            snapshots = await owned_snapshots(resources, session)
            for snapshot in snapshots:
                await resources.adapter.delete_snapshot(snapshot.snapshot_id)
            sandbox = await owned_sandbox(resources, session)
            if sandbox is not None:
                await resources.adapter.delete_sandbox(sandbox.sandbox_id)
        except (AzureError, SandboxTransportError) as exc:
            raise AcaSmokeEnvironmentError(
                "Last-resort exact-label provider cleanup could not delete owned ACA resources."
            ) from exc
    remaining = await asyncio.gather(
        *(owned_sandbox(resources, session) for session in sessions_with_backing),
        *(owned_snapshots(resources, session) for session in sessions_with_backing),
    )
    if not all(not item for item in remaining):
        raise AcaSmokeEnvironmentError(
            "Last-resort exact-label provider cleanup left owned sandbox or snapshot resources behind."
        )


async def _cleanup_load_sessions(
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    partition_key: str,
    submitted: list[_SubmittedRun],
) -> bool:
    sessions = await asyncio.gather(
        *(
            read_authoritative_session(
                resources, session_id=item.accepted.session_id, partition_key=partition_key
            )
            for item in submitted
        )
    )
    outcomes = await asyncio.gather(
        *(
            cleanup_owned_lifecycle_session(
                resources,
                session=session,
                config=config,
                partition_key=partition_key,
            )
            for session in sessions
        ),
        return_exceptions=True,
    )
    unexpected = [
        outcome
        for outcome in outcomes
        if isinstance(outcome, BaseException)
        and not isinstance(outcome, (AcaSmokeEnvironmentError, AssertionError))
    ]
    if unexpected:
        raise unexpected[0]
    cleanup_failures = [
        outcome
        for outcome in outcomes
        if isinstance(outcome, (AcaSmokeEnvironmentError, AssertionError))
    ]
    if cleanup_failures:
        raise AcaSmokeEnvironmentError(
            "Controller tombstone cleanup failed for one or more admitted sessions."
        ) from cleanup_failures[0]
    remaining = await asyncio.gather(
        *(owned_sandbox(resources, session) for session in sessions),
        *(owned_snapshots(resources, session) for session in sessions),
    )
    if not all(not item for item in remaining):
        raise AcaSmokeEnvironmentError(
            "Exact-label ACA sandbox or snapshot cleanup left owned resources behind."
        )
    return True
