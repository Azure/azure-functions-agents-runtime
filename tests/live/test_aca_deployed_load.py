"""Manual/Scheduled real ACA load qualification through the deployed Function endpoint."""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
import uuid
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
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
    LoadLatencyMetrics,
    latency_metrics,
    provision_concurrency_from_option_or_environment,
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
_SETUP_DEADLINE_ATTEMPTS = 2
_SETUP_HTTP_ATTEMPT_TIMEOUT_SECONDS = 105.0
_PROVISION_BATCH_TIMEOUT_SECONDS = 330.0
_PHASE_B_ADMISSION_TIMEOUT_SECONDS = 330.0
_RECOVERY_ATTEMPTS = 5
_RECOVERY_POLL_SECONDS = 1.0
_FINAL_RECOVERY_TIMEOUT_SECONDS = 60.0
_OVERLAP_BUDGET_MARGIN_SECONDS = 15.0
_SETTLEMENT_TIMEOUT_SECONDS = 900.0
_RACE_SAMPLE_LIMIT = 5
_CONNECTION_HEADROOM = 10
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
    deadline_exhausted: bool = False


@dataclass(frozen=True, slots=True)
class _AdmissionSummary:
    retries: int
    unclassified_service_throttles: int
    unresolved_idempotencies: int
    attempted_idempotency_keys: tuple[str, ...]
    attempt_count: int


@dataclass(frozen=True, slots=True)
class _LoadQualificationContext:
    config: DeployedAcaLifecycleConfig
    partition_key: str
    authorization: str
    concurrency: int
    provision_concurrency: int


@dataclass(slots=True)
class _LoadQualificationState:
    prepared: list[_SubmittedRun] = field(default_factory=list)
    held: list[_SubmittedRun] = field(default_factory=list)
    recovered_cleanup_candidates: list[_SubmittedRun] = field(default_factory=list)
    attempted_idempotency_keys: list[str] = field(default_factory=list)
    event_tasks: list[asyncio.Task[_EventEvidence]] = field(default_factory=list)
    common_interval: CommonActiveInterval | None = None
    succeeded_count: int = 0
    replay_count: int = 0
    active_run_conflict_count: int = 0
    retry_count: int = 0
    unclassified_service_throttle_count: int = 0
    unresolved_idempotency_count: int = 0
    admission_failure_categories: tuple[tuple[str, int], ...] = ()
    provisioning_duration_seconds: float | None = None
    provisioning_attempt_count: int = 0
    provisioning_retry_count: int = 0
    suspended_prepared_count: int = 0
    cleanup_complete: bool = False
    settlement_complete: bool = True
    metrics: LoadLatencyMetrics | None = None


@dataclass(frozen=True, slots=True)
class _AdmissionRequest:
    idempotency_key: str
    submitted_at: float
    session_id_header: str | None


@dataclass(frozen=True, slots=True)
class _AdmissionResponse:
    status: int
    payload: dict[str, object]
    response_headers: Mapping[str, str]
    accepted_at: float


@dataclass(frozen=True, slots=True)
class _SetupRetry:
    retry_after_seconds: float


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
        category_text = (
            ",".join(f"{category}={count}" for category, count in failure_categories) or "none"
        )
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


class _AdmissionDeadlineError(_AdmissionFailureError):
    """Aggregate admission failure caused by a bounded setup retry deadline."""

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
        super().__init__(
            failures=failures,
            retries=retries,
            throttles=throttles,
            unresolved_idempotencies=unresolved_idempotencies,
            attempted_idempotency_keys=attempted_idempotency_keys,
            failure_categories=failure_categories,
            attempt_count=attempt_count,
        )
        self.args = (f"A load-admission setup retry deadline was exhausted; {self.args[0]}",)


@pytest.mark.live_aca
@pytest.mark.asyncio
async def test_deployed_aca_load_has_a_common_durable_active_interval(
    request: pytest.FixtureRequest,
) -> None:
    """Qualify N real held model turns with public races and read-only durable evidence."""
    concurrency = require_load_concurrency(request.config)
    provision_concurrency = provision_concurrency_from_option_or_environment(request.config)
    config = _load_config(deployed_aca_lifecycle_config_from_environment())
    authorization_evidence = await acquire_default_authorization_evidence(
        config.deployed.token_scope
    )
    partition_key = owner_partition(
        EntraUserOwnerContext.create(
            config.app_identity,
            _LOAD_AGENT_SLUG,
            authorization_evidence.tenant_id,
            authorization_evidence.object_id,
        )
    ).partition_key
    context = _LoadQualificationContext(
        config=config,
        partition_key=partition_key,
        authorization=authorization_evidence.authorization_header,
        concurrency=concurrency,
        provision_concurrency=provision_concurrency,
    )
    resources: DeployedAcaLifecycleResources | None = None
    state = _LoadQualificationState()
    try:
        resources = await open_deployed_aca_lifecycle_resources(config)
        await _run_load_qualification(resources, context, state)
    finally:
        await _finalize_load_qualification(resources, context, state, primary_error=sys.exception())
    assert state.cleanup_complete


async def _run_load_qualification(
    resources: DeployedAcaLifecycleResources,
    context: _LoadQualificationContext,
    state: _LoadQualificationState,
) -> None:
    headers = {
        "Authorization": context.authorization,
        "Content-Type": "application/json",
        "Prefer": "respond-async",
    }
    timeout = ClientTimeout(
        total=context.config.deployed.timeout_seconds + _EVENT_STREAM_GRACE_SECONDS
    )
    connector_limit = context.concurrency + _CONNECTION_HEADROOM
    async with (
        ClientSession(timeout=timeout, connector=TCPConnector(limit=connector_limit)) as control,
        ClientSession(timeout=timeout, connector=TCPConnector(limit=connector_limit)) as events,
    ):
        try:
            await _provision_load_sessions(resources, control, events, context, state, headers)
            await _admit_held_load_sessions(resources, control, context, state, headers)
            await _verify_held_load_sessions(resources, control, events, context, state, headers)
        finally:
            await _preserve_load_cleanup_candidates(
                resources,
                control,
                context,
                state,
                primary_error=sys.exception(),
            )


async def _provision_load_sessions(
    resources: DeployedAcaLifecycleResources,
    control: ClientSession,
    events: ClientSession,
    context: _LoadQualificationContext,
    state: _LoadQualificationState,
    headers: dict[str, str],
) -> None:
    provisioning_started = time.perf_counter()
    try:
        try:
            provisioning = await _prepare_sessions(
                control,
                events,
                context.config,
                headers,
                context.concurrency,
                state.prepared,
                resources,
                context.partition_key,
                context.authorization,
                state.attempted_idempotency_keys,
                provision_concurrency=context.provision_concurrency,
            )
        except _AdmissionFailureError as exc:
            _record_provisioning_failure(state, exc)
            raise
        _record_provisioning_summary(state, provisioning)
        _assert_distinct_admissions(state.prepared, context.concurrency)
        if _requires_prepared_suspension(context.concurrency):
            state.suspended_prepared_count = await _wait_for_suspended_prepared_backing(
                resources,
                context.config,
                context.partition_key,
                state.prepared,
            )
    finally:
        state.provisioning_duration_seconds = time.perf_counter() - provisioning_started


def _record_provisioning_failure(
    state: _LoadQualificationState,
    error: _AdmissionFailureError,
) -> None:
    state.provisioning_retry_count += error.retries
    state.provisioning_attempt_count += error.attempt_count
    state.unclassified_service_throttle_count += error.throttles
    state.unresolved_idempotency_count += error.unresolved_idempotencies
    state.admission_failure_categories = error.failure_categories


def _record_provisioning_summary(
    state: _LoadQualificationState,
    summary: _AdmissionSummary,
) -> None:
    state.provisioning_attempt_count = summary.attempt_count
    state.provisioning_retry_count += summary.retries
    state.unclassified_service_throttle_count += summary.unclassified_service_throttles
    state.unresolved_idempotency_count += summary.unresolved_idempotencies


async def _admit_held_load_sessions(
    resources: DeployedAcaLifecycleResources,
    control: ClientSession,
    context: _LoadQualificationContext,
    state: _LoadQualificationState,
    headers: dict[str, str],
) -> None:
    try:
        admission = await _submit_existing_sessions(
            control,
            context.config,
            headers,
            state.prepared,
            state.held,
            resources,
            context.partition_key,
            state.attempted_idempotency_keys,
        )
    except _AdmissionFailureError as exc:
        _record_held_admission_failure(state, exc)
        raise
    _record_held_admission_summary(state, admission)
    _assert_distinct_admissions(state.held, context.concurrency)
    _assert_remaining_hold_budget(state.held)


def _record_held_admission_failure(
    state: _LoadQualificationState,
    error: _AdmissionFailureError,
) -> None:
    state.retry_count += error.retries
    state.unclassified_service_throttle_count += error.throttles
    state.unresolved_idempotency_count += error.unresolved_idempotencies
    state.admission_failure_categories = error.failure_categories


def _record_held_admission_summary(
    state: _LoadQualificationState,
    summary: _AdmissionSummary,
) -> None:
    state.retry_count += summary.retries
    state.unclassified_service_throttle_count += summary.unclassified_service_throttles
    state.unresolved_idempotency_count += summary.unresolved_idempotencies


async def _verify_held_load_sessions(
    resources: DeployedAcaLifecycleResources,
    control: ClientSession,
    events: ClientSession,
    context: _LoadQualificationContext,
    state: _LoadQualificationState,
    headers: dict[str, str],
) -> None:
    state.event_tasks = [
        asyncio.create_task(_read_events(events, item, context.authorization))
        for item in state.held
    ]
    (
        state.common_interval,
        state.replay_count,
        state.active_run_conflict_count,
    ) = await _establish_common_active_interval(
        resources,
        context.config,
        context.partition_key,
        state.held,
        control,
        headers,
    )
    event_evidence = await asyncio.gather(*state.event_tasks)
    _assert_hold_duration(state.held, event_evidence)
    await _assert_terminal_public_results(control, state.held, context.authorization)
    await _assert_terminal_durable_state(
        resources,
        context.config,
        context.partition_key,
        state.held,
    )
    state.succeeded_count = len(state.held)
    state.metrics = latency_metrics(
        [item.accepted_at - item.submitted_at for item in state.held],
        [
            event.first_event_at - item.submitted_at
            for item, event in zip(state.held, event_evidence, strict=True)
        ],
        [
            event.terminal_at - item.submitted_at
            for item, event in zip(state.held, event_evidence, strict=True)
        ],
    )


async def _preserve_load_cleanup_candidates(
    resources: DeployedAcaLifecycleResources,
    control: ClientSession,
    context: _LoadQualificationContext,
    state: _LoadQualificationState,
    *,
    primary_error: BaseException | None,
) -> None:
    for task in state.event_tasks:
        if not task.done():
            task.cancel()
    if state.event_tasks:
        await asyncio.gather(*state.event_tasks, return_exceptions=True)
    try:
        recovered, unresolved = await _recover_final_cleanup_candidates(
            resources,
            context.config,
            context.partition_key,
            state.attempted_idempotency_keys,
            [*state.prepared, *state.held],
        )
        state.recovered_cleanup_candidates.extend(recovered)
        state.unresolved_idempotency_count = unresolved
        if unresolved and primary_error is not None:
            primary_error.add_note(
                "ACA load cleanup has unresolved owner-idempotency reservations."
            )
    except AcaSmokeEnvironmentError:
        state.unresolved_idempotency_count = len(set(state.attempted_idempotency_keys))
        if primary_error is not None:
            primary_error.add_note(
                "ACA load cleanup could not recover all owner-idempotency reservations."
            )
        else:
            raise
    if primary_error is not None and (
        state.prepared or state.held or state.recovered_cleanup_candidates
    ):
        try:
            await _settle_failed_runs(
                resources,
                context.config,
                context.partition_key,
                [*state.prepared, *state.held, *state.recovered_cleanup_candidates],
                control,
                context.authorization,
            )
        except (AcaSmokeEnvironmentError, AssertionError):
            state.settlement_complete = False
            _note_settlement_failure(primary_error)


async def _finalize_load_qualification(
    resources: DeployedAcaLifecycleResources | None,
    context: _LoadQualificationContext,
    state: _LoadQualificationState,
    *,
    primary_error: BaseException | None,
) -> None:
    cleanup_error: BaseException | None = None
    try:
        tracked = [*state.prepared, *state.held, *state.recovered_cleanup_candidates]
        if resources is not None and tracked:
            if state.settlement_complete:
                state.cleanup_complete = await _cleanup_load_sessions(
                    resources,
                    context.config,
                    context.partition_key,
                    tracked,
                )
            else:
                await _provider_cleanup_last_resort(
                    resources,
                    context.config,
                    context.partition_key,
                    tracked,
                )
                state.cleanup_complete = False
        if state.unresolved_idempotency_count:
            state.cleanup_complete = False
    except (AcaSmokeEnvironmentError, AssertionError) as exc:
        cleanup_error = exc
    finally:
        if resources is not None:
            await resources.close()
    _LOGGER.info(
        "%s",
        render_load_report(
            concurrency=context.concurrency,
            prepared_count=len(state.prepared),
            provision_concurrency=context.provision_concurrency,
            provisioning_duration_seconds=state.provisioning_duration_seconds,
            provisioning_attempt_count=state.provisioning_attempt_count,
            provisioning_retry_count=state.provisioning_retry_count,
            suspended_prepared_count=state.suspended_prepared_count,
            common_interval=state.common_interval,
            admitted_count=len(state.held),
            succeeded_count=state.succeeded_count,
            metrics=state.metrics,
            replay_count=state.replay_count,
            active_run_conflict_count=state.active_run_conflict_count,
            retry_count=state.retry_count,
            unclassified_service_throttle_count=state.unclassified_service_throttle_count,
            unresolved_idempotency_count=state.unresolved_idempotency_count,
            cleanup_complete=state.cleanup_complete,
            admission_failure_categories=state.admission_failure_categories,
        ),
    )
    if cleanup_error is not None:
        _raise_or_note_cleanup_failure(primary_error, cleanup_error)


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
    *,
    provision_concurrency: int = 4,
) -> _AdmissionSummary:
    """Create and prove idle each configured provisioning batch before the next batch."""
    retries = 0
    throttles = 0
    unresolved = 0
    for start in range(0, concurrency, provision_concurrency):
        before_batch = len(prepared)
        batch_size = min(provision_concurrency, concurrency - start)
        deadline = time.perf_counter() + _PROVISION_BATCH_TIMEOUT_SECONDS
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
                    deadline=deadline,
                )
                async with asyncio.timeout(_remaining_timeout_seconds(deadline)):
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
    deadline = time.perf_counter() + _PHASE_B_ADMISSION_TIMEOUT_SECONDS
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
                deadline=deadline,
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
    deadline: float | None = None,
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
            deadline=deadline,
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
        error_type: type[_AdmissionFailureError] = (
            _AdmissionDeadlineError
            if any(outcome.deadline_exhausted for outcome in outcomes)
            else _AdmissionFailureError
        )
        error = error_type(
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
        raise error_type(
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
        failure
        if _SAFE_ADMISSION_FAILURE_CATEGORY.fullmatch(failure)
        else "other_admission_failure"
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


def _new_admission_request(
    attempted_idempotency_keys: list[str],
    session_id_header: str | None,
) -> _AdmissionRequest:
    idempotency_key = uuid.uuid4().hex
    attempted_idempotency_keys.append(idempotency_key)
    return _AdmissionRequest(
        idempotency_key=idempotency_key,
        submitted_at=time.perf_counter(),
        session_id_header=session_id_header,
    )


def _admission_deadline_elapsed(deadline: float | None) -> bool:
    return deadline is not None and time.perf_counter() >= deadline


def _admission_request_headers(
    headers: dict[str, str],
    request: _AdmissionRequest,
) -> dict[str, str]:
    request_headers = {**headers, "Idempotency-Key": request.idempotency_key}
    if request.session_id_header is not None:
        request_headers["x-ms-session-id"] = request.session_id_header
    return request_headers


def _admission_attempt_timeout(deadline: float | None) -> float:
    if deadline is None:
        return _SETUP_HTTP_ATTEMPT_TIMEOUT_SECONDS
    return _remaining_timeout_seconds(
        deadline,
        maximum=_SETUP_HTTP_ATTEMPT_TIMEOUT_SECONDS,
    )


async def _post_admission_request(
    client: ClientSession,
    config: DeployedAcaLifecycleConfig,
    headers: dict[str, str],
    request: _AdmissionRequest,
    prompt: str,
    deadline: float | None,
) -> _AdmissionResponse:
    async with asyncio.timeout(_admission_attempt_timeout(deadline)):
        status, payload, response_headers = await json_request(
            client,
            "POST",
            config.deployed.chat_url,
            headers=_admission_request_headers(headers, request),
            payload=submission_payload(prompt),
        )
    return _AdmissionResponse(
        status=status,
        payload=payload,
        response_headers=response_headers,
        accepted_at=time.perf_counter(),
    )


async def _recover_admission_outcome(
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    partition_key: str,
    request: _AdmissionRequest,
    retries: int,
    *,
    failure: str,
    unclassified_service_throttles: int,
) -> _AdmissionOutcome:
    recovered = await _recover_submitted_run(
        resources,
        config,
        partition_key,
        request.idempotency_key,
        request.submitted_at,
        session_id_header=request.session_id_header,
    )
    return _recovered_admission_outcome(
        idempotency_key=request.idempotency_key,
        recovered=recovered,
        retries=retries,
        unclassified_service_throttles=unclassified_service_throttles,
        failure=failure,
        session_id_header=request.session_id_header,
    )


async def _accepted_admission_outcome(
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    partition_key: str,
    request: _AdmissionRequest,
    response: _AdmissionResponse,
    retries: int,
) -> _AdmissionOutcome:
    try:
        accepted = parse_accepted_run(response.payload, config.deployed)
    except (AssertionError, ValueError):
        return await _recover_admission_outcome(
            resources,
            config,
            partition_key,
            request,
            retries,
            failure="public_admission_response_invalid",
            unclassified_service_throttles=0,
        )
    submitted = _SubmittedRun(
        accepted,
        request.idempotency_key,
        request.submitted_at,
        response.accepted_at,
        request.session_id_header,
    )
    failure = (
        "phase_b_session_mismatch"
        if request.session_id_header is not None
        and accepted.session_id != request.session_id_header
        else None
    )
    return _AdmissionOutcome(
        request.idempotency_key,
        submitted,
        retries,
        0,
        failure,
        False,
    )


async def _setup_deadline_response_outcome(
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    partition_key: str,
    request: _AdmissionRequest,
    response: _AdmissionResponse,
    retries: int,
    *,
    is_final_attempt: bool,
) -> _AdmissionOutcome | _SetupRetry:
    if not is_final_attempt:
        return _SetupRetry(setup_retry_after_seconds(response.response_headers))
    return await _recover_admission_outcome(
        resources,
        config,
        partition_key,
        request,
        retries,
        failure="setup_deadline_exceeded",
        unclassified_service_throttles=0,
    )


async def _admission_response_outcome(
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    partition_key: str,
    request: _AdmissionRequest,
    response: _AdmissionResponse,
    retries: int,
    *,
    is_final_attempt: bool,
) -> _AdmissionOutcome | _SetupRetry:
    if response.status == 202:
        return await _accepted_admission_outcome(
            resources,
            config,
            partition_key,
            request,
            response,
            retries,
        )
    if response.status in {429, 503}:
        return await _recover_ambiguous_http_outcome(
            resources,
            config,
            partition_key,
            request.idempotency_key,
            request.submitted_at,
            retries,
            response.status,
            session_id_header=request.session_id_header,
            unclassified_service_throttles=1,
        )
    if response.status == 504 and response.payload.get("error") == "setup_deadline_exceeded":
        return await _setup_deadline_response_outcome(
            resources,
            config,
            partition_key,
            request,
            response,
            retries,
            is_final_attempt=is_final_attempt,
        )
    if response.status in {400, 401, 403, 404}:
        return _AdmissionOutcome(
            request.idempotency_key,
            None,
            retries,
            0,
            f"public_admission_http_{response.status}",
            False,
        )
    return await _recover_ambiguous_http_outcome(
        resources,
        config,
        partition_key,
        request.idempotency_key,
        request.submitted_at,
        retries,
        response.status,
        session_id_header=request.session_id_header,
        unclassified_service_throttles=0,
    )


def _retry_would_exceed_setup_deadline(
    deadline: float | None,
    retry_after_seconds: float,
) -> bool:
    return deadline is not None and time.perf_counter() + retry_after_seconds >= deadline


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
    deadline: float | None = None,
) -> _AdmissionOutcome:
    request = _new_admission_request(attempted_idempotency_keys, session_id)
    for retries in range(_SETUP_DEADLINE_ATTEMPTS):
        if _admission_deadline_elapsed(deadline):
            return _setup_deadline_outcome(request.idempotency_key, retries)
        try:
            response = await _post_admission_request(
                client,
                config,
                headers,
                request,
                prompt,
                deadline,
            )
        except (AcaSmokeEnvironmentError, TimeoutError):
            return await _recover_admission_outcome(
                resources,
                config,
                partition_key,
                request,
                retries,
                failure="public_admission_request_ambiguous",
                unclassified_service_throttles=0,
            )
        outcome = await _admission_response_outcome(
            resources,
            config,
            partition_key,
            request,
            response,
            retries,
            is_final_attempt=retries + 1 == _SETUP_DEADLINE_ATTEMPTS,
        )
        if isinstance(outcome, _AdmissionOutcome):
            return outcome
        retry_count = retries + 1
        if _retry_would_exceed_setup_deadline(deadline, outcome.retry_after_seconds):
            return _setup_deadline_outcome(request.idempotency_key, retry_count)
        await asyncio.sleep(outcome.retry_after_seconds)
    raise AssertionError("setup-deadline admission loop must return an outcome")


def _setup_deadline_outcome(idempotency_key: str, retries: int) -> _AdmissionOutcome:
    """Preserve retry evidence when the enclosing setup budget expires before retry."""
    return _AdmissionOutcome(
        idempotency_key=idempotency_key,
        submitted=None,
        retries=retries,
        unclassified_service_throttles=0,
        failure="setup_deadline_exceeded",
        unresolved_idempotency=True,
        deadline_exhausted=True,
    )


def _remaining_timeout_seconds(deadline: float, *, maximum: float | None = None) -> float:
    """Return a positive remaining timeout, optionally capped for one request."""
    remaining = deadline - time.perf_counter()
    if remaining <= 0:
        raise TimeoutError
    return min(remaining, maximum) if maximum is not None else remaining


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
    return _ActiveObservation(started_monotonic, completed_monotonic, started_utc, completed_utc)


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
    assert not [event for event in events if event.payload.get("tool_name") == "qualification_hold"]


def _assert_hold_duration(submitted: list[_SubmittedRun], evidence: list[_EventEvidence]) -> None:
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
        client,
        "GET",
        accepted.management_urls["status_url"],
        headers={"Authorization": authorization},
    )
    assert status_code == 200
    assert status.get("state") == "succeeded"
    result_code, result, _ = await json_request(
        client,
        "GET",
        accepted.management_urls["result_url"],
        headers={"Authorization": authorization},
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
        *(_read_terminal_observation(resources, config, partition_key, item) for item in submitted)
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
