"""Manual-only real ACA load qualification through the deployed Function endpoint."""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
import uuid
from collections import Counter
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
_SETUP_DEADLINE_ATTEMPTS = 12
_SETUP_HTTP_ATTEMPT_TIMEOUT_SECONDS = 45.0
_PROVISION_BATCH_TIMEOUT_SECONDS = 660.0
_PHASE_B_ADMISSION_TIMEOUT_SECONDS = 660.0
_RECOVERY_ATTEMPTS = 5
_RECOVERY_POLL_SECONDS = 1.0
_FINAL_RECOVERY_TIMEOUT_SECONDS = 60.0
_OVERLAP_BUDGET_MARGIN_SECONDS = 15.0
_SETTLEMENT_TIMEOUT_SECONDS = 900.0
_RACE_SAMPLE_LIMIT = 5
_CONNECTION_HEADROOM = 10
_PROVISION_CONCURRENCY = 4
_FORMAL_LOAD_CONCURRENCY = 100
_PREPARED_SUSPENSION_TIMEOUT_SECONDS = 120.0
_LOAD_PROMPT = "Call qualification_hold exactly once, then return a brief acknowledgement."
_READINESS_PROMPT = "Return a brief readiness acknowledgement without tools."
_SAFE_ADMISSION_FAILURE_CATEGORY = re.compile(
    r"^(?:"
    r"setup_deadline_exceeded|"
    r"phase_b_session_mismatch|"
    r"public_admission_request_ambiguous|"
    r"public_admission_response_invalid|"
    r"public_admission_http_(?:400|401|403|404)|"
    r"ambiguous_public_admission_http_\d{3}"
    r")$"
)

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
    session_id_header: str | None = None


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
    attempt_count: int


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
        failure_categories: tuple[tuple[str, int], ...],
        attempt_count: int,
    ) -> None:
        category_text = ",".join(
            f"{category}={count}" for category, count in failure_categories
        ) or "none"
        super().__init__(
            f"{failures} load admissions failed; "
            f"unclassified_service_throttles={throttles}; retries={retries}; "
            f"unresolved_idempotencies={unresolved_idempotencies}; "
            f"admission_failure_categories={category_text}."
        )
        self.retries = retries
        self.throttles = throttles
        self.unresolved_idempotencies = unresolved_idempotencies
        self.attempted_idempotency_keys = attempted_idempotency_keys
        self.failure_categories = failure_categories
        self.attempt_count = attempt_count


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
    prepared: list[_SubmittedRun] = []
    held: list[_SubmittedRun] = []
    recovered_cleanup_candidates: list[_SubmittedRun] = []
    attempted_idempotency_keys: list[str] = []
    event_tasks: list[asyncio.Task[_EventEvidence]] = []
    common_interval: CommonActiveInterval | None = None
    succeeded_count = 0
    replay_count = 0
    active_run_conflict_count = 0
    retry_count = 0
    unclassified_service_throttle_count = 0
    unresolved_idempotency_count = 0
    admission_failure_categories: tuple[tuple[str, int], ...] = ()
    provisioning_duration_seconds: float | None = None
    provisioning_attempt_count = 0
    provisioning_retry_count = 0
    suspended_prepared_count = 0
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
                provisioning_started = time.perf_counter()
                try:
                    provisioning = await _prepare_sessions(
                        control,
                        events,
                        config,
                        headers,
                        concurrency,
                        prepared,
                        resources,
                        partition_key,
                        authorization,
                        attempted_idempotency_keys,
                    )
                except _AdmissionFailureError as exc:
                    provisioning_retry_count += exc.retries
                    provisioning_attempt_count += exc.attempt_count
                    unclassified_service_throttle_count += exc.throttles
                    unresolved_idempotency_count += exc.unresolved_idempotencies
                    admission_failure_categories = exc.failure_categories
                    raise
                else:
                    provisioning_attempt_count = provisioning.attempt_count
                    provisioning_retry_count += provisioning.retries
                    unclassified_service_throttle_count += provisioning.unclassified_service_throttles
                    unresolved_idempotency_count += provisioning.unresolved_idempotencies
                    _assert_distinct_admissions(prepared, concurrency)
                    if _requires_prepared_suspension(concurrency):
                        suspended_prepared_count = await _wait_for_suspended_prepared_backing(
                            resources,
                            config,
                            partition_key,
                            prepared,
                        )
                finally:
                    provisioning_duration_seconds = time.perf_counter() - provisioning_started
                try:
                    admission = await _submit_existing_sessions(
                        control,
                        config,
                        headers,
                        prepared,
                        held,
                        resources,
                        partition_key,
                        attempted_idempotency_keys,
                    )
                except _AdmissionFailureError as exc:
                    retry_count += exc.retries
                    unclassified_service_throttle_count += exc.throttles
                    unresolved_idempotency_count += exc.unresolved_idempotencies
                    admission_failure_categories = exc.failure_categories
                    raise
                retry_count += admission.retries
                unclassified_service_throttle_count += admission.unclassified_service_throttles
                unresolved_idempotency_count += admission.unresolved_idempotencies
                _assert_distinct_admissions(held, concurrency)
                _assert_remaining_hold_budget(held)
                event_tasks = [
                    asyncio.create_task(_read_events(events, item, authorization)) for item in held
                ]
                common_interval, replay_count, active_run_conflict_count = (
                    await _establish_common_active_interval(
                        resources,
                        config,
                        partition_key,
                        held,
                        control,
                        headers,
                    )
                )
                event_evidence = await asyncio.gather(*event_tasks)
                _assert_hold_duration(held, event_evidence)
                await _assert_terminal_public_results(control, held, authorization)
                await _assert_terminal_durable_state(resources, config, partition_key, held)
                succeeded_count = len(held)
                metrics = latency_metrics(
                    [item.accepted_at - item.submitted_at for item in held],
                    [
                        event.first_event_at - item.submitted_at
                        for item, event in zip(held, event_evidence, strict=True)
                    ],
                    [
                        event.terminal_at - item.submitted_at
                        for item, event in zip(held, event_evidence, strict=True)
                    ],
                )
            finally:
                inner_primary_error = sys.exception()
                for task in event_tasks:
                    if not task.done():
                        task.cancel()
                if event_tasks:
                    await asyncio.gather(*event_tasks, return_exceptions=True)
                try:
                    recovered, unresolved = await _recover_final_cleanup_candidates(
                        resources,
                        config,
                        partition_key,
                        attempted_idempotency_keys,
                        [*prepared, *held],
                    )
                    recovered_cleanup_candidates.extend(recovered)
                    unresolved_idempotency_count = unresolved
                    if unresolved and inner_primary_error is not None:
                        inner_primary_error.add_note(
                            "ACA load cleanup has unresolved owner-idempotency reservations."
                        )
                except AcaSmokeEnvironmentError:
                    unresolved_idempotency_count = len(set(attempted_idempotency_keys))
                    if inner_primary_error is not None:
                        inner_primary_error.add_note(
                            "ACA load cleanup could not recover all owner-idempotency reservations."
                        )
                    else:
                        raise
                if inner_primary_error is not None and (prepared or held or recovered_cleanup_candidates):
                    try:
                        await _settle_failed_runs(
                            resources,
                            config,
                            partition_key,
                            [*prepared, *held, *recovered_cleanup_candidates],
                            control,
                            authorization,
                        )
                    except (AcaSmokeEnvironmentError, AssertionError):
                        settlement_complete = False
                        _note_settlement_failure(inner_primary_error)
    finally:
        primary_error = sys.exception()
        try:
            tracked = [*prepared, *held, *recovered_cleanup_candidates]
            if resources is not None and tracked:
                if settlement_complete:
                    cleanup_complete = await _cleanup_load_sessions(
                        resources, config, partition_key, tracked
                    )
                else:
                    await _provider_cleanup_last_resort(resources, config, partition_key, tracked)
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
                prepared_count=len(prepared),
                provision_concurrency=_PROVISION_CONCURRENCY,
                provisioning_duration_seconds=provisioning_duration_seconds,
                provisioning_attempt_count=provisioning_attempt_count,
                provisioning_retry_count=provisioning_retry_count,
                suspended_prepared_count=suspended_prepared_count,
                common_interval=common_interval,
                admitted_count=len(held),
                succeeded_count=succeeded_count,
                metrics=metrics,
                replay_count=replay_count,
                active_run_conflict_count=active_run_conflict_count,
                retry_count=retry_count,
                unclassified_service_throttle_count=unclassified_service_throttle_count,
                unresolved_idempotency_count=unresolved_idempotency_count,
                cleanup_complete=cleanup_complete,
                admission_failure_categories=admission_failure_categories,
            ),
        )
        if cleanup_error is not None:
            _raise_or_note_cleanup_failure(primary_error, cleanup_error)
    assert cleanup_complete


def _load_config(config: DeployedAcaLifecycleConfig) -> DeployedAcaLifecycleConfig:
    """Use the fixture's fixed load-only agent without adding a deployment setting."""
    return replace(config, deployed=replace(config.deployed, agent_slug=_LOAD_AGENT_SLUG))


def _requires_prepared_suspension(concurrency: int) -> bool:
    return concurrency == _FORMAL_LOAD_CONCURRENCY


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


async def _prepare_sessions(
    client: ClientSession,
    events: ClientSession,
    config: DeployedAcaLifecycleConfig,
    headers: dict[str, str],
    concurrency: int,
    prepared: list[_SubmittedRun],
    resources: DeployedAcaLifecycleResources,
    partition_key: str,
    authorization: str,
    attempted_idempotency_keys: list[str],
) -> _AdmissionSummary:
    """Create and prove idle each four-session batch before posting the next batch."""
    retries = 0
    throttles = 0
    unresolved = 0
    for start in range(0, concurrency, _PROVISION_CONCURRENCY):
        before_batch = len(prepared)
        batch_size = min(_PROVISION_CONCURRENCY, concurrency - start)
        try:
            async with asyncio.timeout(_PROVISION_BATCH_TIMEOUT_SECONDS):
                admission = await _submit_session_batch(
                    client,
                    config,
                    headers,
                    resources,
                    partition_key,
                    session_ids=[None] * batch_size,
                    prompt=_READINESS_PROMPT,
                    submitted=prepared,
                    attempted_idempotency_keys=attempted_idempotency_keys,
                )
                batch = prepared[before_batch:]
                _assert_distinct_admissions(batch, batch_size)
                await _assert_prepared_sessions(
                    events,
                    client,
                    resources,
                    config,
                    partition_key,
                    batch,
                    authorization,
                )
        except TimeoutError as exc:
            raise AcaSmokeEnvironmentError(
                "A Phase A provisioning batch did not reach public terminal durable idle state "
                "within its bounded deadline."
            ) from exc
        retries += admission.retries
        throttles += admission.unclassified_service_throttles
        unresolved += admission.unresolved_idempotencies
    return _AdmissionSummary(
        retries=retries,
        unclassified_service_throttles=throttles,
        unresolved_idempotencies=unresolved,
        attempted_idempotency_keys=tuple(attempted_idempotency_keys),
        attempt_count=concurrency + retries,
    )


async def _submit_existing_sessions(
    client: ClientSession,
    config: DeployedAcaLifecycleConfig,
    headers: dict[str, str],
    prepared: list[_SubmittedRun],
    held: list[_SubmittedRun],
    resources: DeployedAcaLifecycleResources,
    partition_key: str,
    attempted_idempotency_keys: list[str],
) -> _AdmissionSummary:
    """Launch the formal held turns concurrently against the already prepared sessions."""
    try:
        async with asyncio.timeout(_PHASE_B_ADMISSION_TIMEOUT_SECONDS):
            admission = await _submit_session_batch(
                client,
                config,
                headers,
                resources,
                partition_key,
                session_ids=[item.accepted.session_id for item in prepared],
                prompt=_LOAD_PROMPT,
                submitted=held,
                attempted_idempotency_keys=attempted_idempotency_keys,
            )
    except TimeoutError as exc:
        raise AcaSmokeEnvironmentError(
            "The formal Phase B admission did not complete within its bounded setup deadline."
        ) from exc
    _assert_phase_b_session_identity(prepared, held)
    return admission


async def _assert_prepared_sessions(
    events: ClientSession,
    control: ClientSession,
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    partition_key: str,
    prepared: list[_SubmittedRun],
    authorization: str,
) -> None:
    """Require public terminal readiness and a durable idle slot before the held-run phase."""
    await asyncio.gather(
        *(
            _assert_prepared_session(
                events,
                control,
                resources,
                config,
                partition_key,
                item,
                authorization,
            )
            for item in prepared
        )
    )


async def _assert_prepared_session(
    events: ClientSession,
    control: ClientSession,
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    partition_key: str,
    prepared: _SubmittedRun,
    authorization: str,
) -> None:
    status, stream, _, _ = await read_sse_events_with_first_event_time(
        events,
        prepared.accepted.management_urls["events_url"],
        headers={"Authorization": authorization},
        overall_timeout_seconds=config.deployed.timeout_seconds + _EVENT_STREAM_GRACE_SECONDS,
    )
    assert status == 200
    assert stream
    assert [event.sequence for event in stream] == list(range(1, len(stream) + 1))
    assert stream[-1].payload.get("type") == "done"
    _assert_no_public_hold_events(stream)
    assert await _read_terminal_result(control, prepared, authorization)
    assert await _read_prepared_idle_observation(resources, config, partition_key, prepared)


async def _wait_for_suspended_prepared_backing(
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    partition_key: str,
    prepared: list[_SubmittedRun],
) -> int:
    """Observe one exact-label prepared backing stopped or suspended before the N=100 phase."""
    deadline = time.perf_counter() + _PREPARED_SUSPENSION_TIMEOUT_SECONDS
    while True:
        sessions = await asyncio.gather(
            *(
                read_authoritative_session(
                    resources,
                    session_id=item.accepted.session_id,
                    partition_key=partition_key,
                )
                for item in prepared
            )
        )
        for session in sessions:
            assert_session_belongs_to_deployment(session, config)
        sandboxes = await asyncio.gather(
            *(owned_sandbox(resources, session) for session in sessions)
        )
        suspended_count = sum(
            sandbox is not None and sandbox.state in {"Stopped", "Suspended"}
            for sandbox in sandboxes
        )
        if suspended_count:
            return suspended_count
        if time.perf_counter() >= deadline:
            raise AssertionError(
                "No prepared session's exact-label ACA backing reached Stopped or Suspended "
                "before the formal N=100 held-run phase."
            )
        await asyncio.sleep(_POLL_SECONDS)


async def _submit_session_batch(
    client: ClientSession,
    config: DeployedAcaLifecycleConfig,
    headers: dict[str, str],
    resources: DeployedAcaLifecycleResources,
    partition_key: str,
    *,
    session_ids: list[str | None],
    prompt: str,
    submitted: list[_SubmittedRun],
    attempted_idempotency_keys: list[str],
) -> _AdmissionSummary:
    first_key_index = len(attempted_idempotency_keys)

    async def submit(session_id: str | None) -> _AdmissionOutcome:
        outcome = await _submit_one(
            client,
            config,
            headers,
            resources,
            partition_key,
            attempted_idempotency_keys,
            prompt=prompt,
            session_id=session_id,
        )
        if outcome.submitted is not None:
            submitted.append(outcome.submitted)
        return outcome

    results = await asyncio.gather(
        *(submit(session_id) for session_id in session_ids),
        return_exceptions=True,
    )
    outcomes = [result for result in results if isinstance(result, _AdmissionOutcome)]
    retries = sum(outcome.retries for outcome in outcomes)
    throttles = sum(outcome.unclassified_service_throttles for outcome in outcomes)
    unresolved = sum(outcome.unresolved_idempotency for outcome in outcomes)
    attempt_count = len(outcomes) + retries
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
    failure_categories = _admission_failure_categories(
        [*failures, *(["other_admission_failure"] * len(exceptions))]
    )
    retained_keys = tuple(attempted_idempotency_keys[first_key_index:]) or tuple(
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
            failure_categories=failure_categories,
            attempt_count=attempt_count,
        )
        if cause is not None:
            raise error from cause
        raise _AdmissionFailureError(
            failures=len(failures),
            retries=retries,
            throttles=throttles,
            unresolved_idempotencies=unresolved,
            attempted_idempotency_keys=retained_keys,
            failure_categories=failure_categories,
            attempt_count=attempt_count,
        )
    return _AdmissionSummary(
        retries=retries,
        unclassified_service_throttles=throttles,
        unresolved_idempotencies=unresolved,
        attempted_idempotency_keys=retained_keys,
        attempt_count=attempt_count,
    )


def _admission_failure_categories(
    failures: list[str],
) -> tuple[tuple[str, int], ...]:
    """Aggregate only safe categories so live diagnostics cannot expose request data."""
    categories = Counter(
        failure if _SAFE_ADMISSION_FAILURE_CATEGORY.fullmatch(failure) else "other_admission_failure"
        for failure in failures
    )
    return tuple(sorted(categories.items()))


def _recovered_admission_outcome(
    *,
    idempotency_key: str,
    recovered: _SubmittedRun | None,
    retries: int,
    unclassified_service_throttles: int,
    failure: str,
    session_id_header: str | None,
) -> _AdmissionOutcome:
    """Retain a recovered candidate even when an existing-session identity is unsafe."""
    if (
        recovered is not None
        and session_id_header is not None
        and recovered.accepted.session_id != session_id_header
    ):
        failure = "phase_b_session_mismatch"
    return _AdmissionOutcome(
        idempotency_key,
        recovered,
        retries,
        unclassified_service_throttles,
        failure,
        recovered is None,
    )


async def _submit_one(
    client: ClientSession,
    config: DeployedAcaLifecycleConfig,
    headers: dict[str, str],
    resources: DeployedAcaLifecycleResources,
    partition_key: str,
    attempted_idempotency_keys: list[str],
    *,
    prompt: str = _LOAD_PROMPT,
    session_id: str | None = None,
) -> _AdmissionOutcome:
    idempotency_key = uuid.uuid4().hex
    attempted_idempotency_keys.append(idempotency_key)
    submitted_at = time.perf_counter()
    retries = 0
    for attempt in range(_SETUP_DEADLINE_ATTEMPTS):
        try:
            request_headers = {**headers, "Idempotency-Key": idempotency_key}
            if session_id is not None:
                request_headers["x-ms-session-id"] = session_id
            async with asyncio.timeout(_SETUP_HTTP_ATTEMPT_TIMEOUT_SECONDS):
                status, payload, response_headers = await json_request(
                    client,
                    "POST",
                    config.deployed.chat_url,
                    headers=request_headers,
                    payload=submission_payload(prompt),
                )
        except (AcaSmokeEnvironmentError, TimeoutError):
            recovered = await _recover_submitted_run(
                resources,
                config,
                partition_key,
                idempotency_key,
                submitted_at,
                session_id_header=session_id,
            )
            return _recovered_admission_outcome(
                idempotency_key=idempotency_key,
                recovered=recovered,
                retries=retries,
                unclassified_service_throttles=0,
                failure="public_admission_request_ambiguous",
                session_id_header=session_id,
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
                    session_id_header=session_id,
                )
                return _recovered_admission_outcome(
                    idempotency_key=idempotency_key,
                    recovered=recovered,
                    retries=retries,
                    unclassified_service_throttles=0,
                    failure="public_admission_response_invalid",
                    session_id_header=session_id,
                )
            submitted = _SubmittedRun(
                accepted,
                idempotency_key,
                submitted_at,
                accepted_at,
                session_id,
            )
            if session_id is not None and accepted.session_id != session_id:
                return _AdmissionOutcome(
                    idempotency_key,
                    submitted,
                    retries,
                    0,
                    "phase_b_session_mismatch",
                    False,
                )
            return _AdmissionOutcome(
                idempotency_key,
                submitted,
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
                session_id_header=session_id,
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
                session_id_header=session_id,
            )
            return _recovered_admission_outcome(
                idempotency_key=idempotency_key,
                recovered=recovered,
                retries=retries,
                unclassified_service_throttles=0,
                failure="setup_deadline_exceeded",
                session_id_header=session_id,
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
            session_id_header=session_id,
            unclassified_service_throttles=0,
        )
    raise AssertionError("setup-deadline admission loop must return an outcome")


async def _recover_submitted_run(
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    partition_key: str,
    idempotency_key: str,
    submitted_at: float,
    *,
    session_id_header: str | None = None,
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
                session_id_header=session_id_header,
            )
        if attempt + 1 < _RECOVERY_ATTEMPTS:
            await asyncio.sleep(_RECOVERY_POLL_SECONDS)
    return None


async def _recover_final_cleanup_candidates(
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    partition_key: str,
    attempted_idempotency_keys: list[str],
    retained: list[_SubmittedRun],
) -> tuple[list[_SubmittedRun], int]:
    """Recover every unrepresented reservation before durable settlement and cleanup."""
    represented = {item.idempotency_key for item in retained}
    missing_keys = [
        key for key in dict.fromkeys(attempted_idempotency_keys) if key not in represented
    ]
    recovered = await asyncio.gather(
        *(
            _recover_cleanup_candidate(resources, config, partition_key, idempotency_key)
            for idempotency_key in missing_keys
        )
    )
    candidates = _unique_run_submissions(
        [candidate for candidate in recovered if candidate is not None]
    )
    return candidates, sum(candidate is None for candidate in recovered)


async def _recover_cleanup_candidate(
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    partition_key: str,
    idempotency_key: str,
) -> _SubmittedRun | None:
    deadline = time.perf_counter() + _FINAL_RECOVERY_TIMEOUT_SECONDS
    while True:
        try:
            record = await read_owner_idempotency(
                resources,
                partition_key=partition_key,
                idempotency_key=idempotency_key,
            )
        except AcaSmokeEnvironmentError:
            return None
        if record is not None:
            accepted = AcceptedRun(
                session_id=record.session_id,
                run_id=record.run_id,
                management_urls=config.deployed.management_urls(
                    session_id=record.session_id,
                    run_id=record.run_id,
                ),
            )
            now = time.perf_counter()
            return _SubmittedRun(accepted, idempotency_key, now, now)
        if time.perf_counter() >= deadline:
            return None
        await asyncio.sleep(_POLL_SECONDS)


def _unique_run_submissions(submitted: list[_SubmittedRun]) -> list[_SubmittedRun]:
    return list(
        {(item.accepted.session_id, item.accepted.run_id): item for item in submitted}.values()
    )


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
    session_id_header: str | None = None,
) -> _AdmissionOutcome:
    recovered = await _recover_submitted_run(
        resources,
        config,
        partition_key,
        idempotency_key,
        submitted_at,
        session_id_header=session_id_header,
    )
    return _recovered_admission_outcome(
        idempotency_key=idempotency_key,
        recovered=recovered,
        retries=retries,
        unclassified_service_throttles=unclassified_service_throttles,
        failure=f"ambiguous_public_admission_http_{status}",
        session_id_header=session_id_header,
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


def _assert_phase_b_session_identity(
    prepared: list[_SubmittedRun], held: list[_SubmittedRun]
) -> None:
    assert {item.accepted.session_id for item in held} == {
        item.accepted.session_id for item in prepared
    }, "Phase B admissions did not preserve the prepared session set."


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
    assert submitted.session_id_header == accepted.session_id
    replay_status, replay_payload, _ = await json_request(
        client,
        "POST",
        config.deployed.chat_url,
        headers={
            **headers,
            "Idempotency-Key": submitted.idempotency_key,
            "x-ms-session-id": submitted.session_id_header,
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
    tool_starts = [
        event.payload
        for event in events
        if event.payload.get("type") == "tool_start"
        and event.payload.get("tool_name") == "qualification_hold"
    ]
    assert len(tool_starts) == 1
    tool_call_id = tool_starts[0].get("tool_call_id")
    assert isinstance(tool_call_id, str) and tool_call_id

    matching_tool_ends = [
        event.payload
        for event in events
        if event.payload.get("type") == "tool_end"
        and event.payload.get("tool_call_id") == tool_call_id
    ]
    assert len(matching_tool_ends) == 1
    tool_end = matching_tool_ends[0]
    assert tool_end.get("tool_name") in {None, "qualification_hold"}


def _assert_no_public_hold_events(events: list[SseEvent]) -> None:
    assert not [
        event
        for event in events
        if event.payload.get("tool_name") == "qualification_hold"
    ]


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


async def _read_prepared_idle_observation(
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    partition_key: str,
    prepared: _SubmittedRun,
) -> bool:
    accepted = prepared.accepted
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
    assert session.status in {"ready", "suspended"}
    assert session.idle_policy_armed
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
    submitted = _unique_session_submissions(submitted)
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
    submitted = _unique_session_submissions(submitted)
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


def _unique_session_submissions(submitted: list[_SubmittedRun]) -> list[_SubmittedRun]:
    """Keep one cleanup descriptor per session while retaining every run for settlement."""
    return list({item.accepted.session_id: item for item in submitted}.values())
