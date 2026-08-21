"""Manual live proof that reconciliation terminalizes an active run after ACA backing loss."""

from __future__ import annotations

import asyncio
import sys
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
    read_sse_until_matching_event,
    setup_retry_after_seconds,
    submission_payload,
)
from tests.live.aca_deployed_lifecycle_support import (
    DeployedAcaLifecycleConfig,
    DeployedAcaLifecycleResources,
    assert_session_belongs_to_deployment,
    deployed_aca_lifecycle_config_from_environment,
    open_deployed_aca_lifecycle_resources,
    owned_sandbox,
    owned_snapshots,
    read_authoritative_run,
    read_authoritative_session,
    read_owner_idempotency,
    read_session_operations,
    session_labels,
)
from tests.live.aca_deployed_loss_support import (
    assert_public_backing_loss_contract,
    deployed_partition_key,
    has_active_owned_backing,
    has_lost_backing_projection,
)

from azure_functions_agents.session_state import (
    DurableRunRecord,
    DurableSessionOperation,
    DurableSessionRecord,
)
from azure_functions_agents.transport.transport_models import SandboxSummary, SandboxTransportError

_LOAD_AGENT_SLUG = "deployed_load"
_HOLD_PROMPT = "Call qualification_hold exactly once, then return a brief acknowledgement."
_SETUP_RETRY_ATTEMPTS = 2
_SETUP_HTTP_ATTEMPT_TIMEOUT_SECONDS = 105.0
_HELD_RUN_SETUP_TIMEOUT_SECONDS = 330.0
_HELD_RUN_RECOVERY_RESERVE_SECONDS = 60.0
_RECOVERY_ATTEMPTS = 5
_POLL_SECONDS = 1.0
_CONTROLLER_WAIT_SECONDS = 300.0

if not deployed_aca_smoke_enabled():
    pytest.skip(
        "Set AZURE_FUNCTIONS_AGENTS_RUN_DEPLOYED_ACA_SMOKE=1 after authorization to qualify "
        "deployed ACA backing-loss reconciliation.",
        allow_module_level=True,
    )


@dataclass(frozen=True, slots=True)
class _ActiveBacking:
    session_id: str
    run_id: str
    sandbox: SandboxSummary


@dataclass(frozen=True, slots=True)
class _LossProjection:
    status_code: int
    status: dict[str, object]
    run: DurableRunRecord
    operations: tuple[DurableSessionOperation, ...]


@pytest.mark.live_aca
@pytest.mark.asyncio
async def test_deployed_aca_reconciles_lost_active_backing_without_table_mutation() -> None:
    """Delete one exact-label held backing and observe only controller-written durable loss state."""
    config = _load_config(deployed_aca_lifecycle_config_from_environment())
    authorization_evidence = await acquire_default_authorization_evidence(config.deployed.token_scope)
    partition_key = deployed_partition_key(
        config,
        authorization_evidence,
        agent_slug=_LOAD_AGENT_SLUG,
    )
    resources: DeployedAcaLifecycleResources | None = None
    accepted: AcceptedRun | None = None
    reconciled = False
    idempotency_key = uuid.uuid4().hex
    try:
        resources = await open_deployed_aca_lifecycle_resources(config)
        headers = {
            "Authorization": authorization_evidence.authorization_header,
            "Content-Type": "application/json",
            "Prefer": "respond-async",
        }
        timeout = ClientTimeout(total=config.deployed.timeout_seconds)
        async with ClientSession(timeout=timeout) as client:
            admission_deadline = _setup_deadline_now() + _HELD_RUN_SETUP_TIMEOUT_SECONDS
            try:
                accepted = await _submit_held_run(
                    client,
                    resources,
                    config,
                    headers,
                    partition_key,
                    idempotency_key,
                    admission_deadline=admission_deadline,
                )
                await _wait_for_qualification_hold_start(
                    client,
                    accepted,
                    authorization_evidence.authorization_header,
                )
                active = await _wait_for_active_backing(
                    resources,
                    config,
                    partition_key,
                    accepted,
                )
                await resources.adapter.delete_sandbox(active.sandbox.sandbox_id)
                assert (
                    await owned_sandbox(
                        resources,
                        await read_authoritative_session(
                            resources,
                            session_id=accepted.session_id,
                            partition_key=partition_key,
                        ),
                    )
                    is None
                )
                projection = await _wait_for_loss_projection(
                    client,
                    resources,
                    config,
                    authorization_evidence.authorization_header,
                    partition_key,
                    accepted,
                )
                assert await owned_sandbox(
                    resources,
                    await read_authoritative_session(
                        resources,
                        session_id=accepted.session_id,
                        partition_key=partition_key,
                    ),
                ) is None
                assert not await owned_snapshots(
                    resources,
                    await read_authoritative_session(
                        resources,
                        session_id=accepted.session_id,
                        partition_key=partition_key,
                    ),
                )
                result_code, result, _ = await json_request(
                    client,
                    "GET",
                    accepted.management_urls["result_url"],
                    headers={"Authorization": authorization_evidence.authorization_header},
                )
                assert_public_backing_loss_contract(
                    status_code=projection.status_code,
                    status=projection.status,
                    result_code=result_code,
                    result=result,
                )
                reconciled = True
            finally:
                await _finalize_backing_loss(
                    client,
                    resources,
                    config,
                    authorization_evidence.authorization_header,
                    partition_key,
                    idempotency_key,
                    admission_deadline=admission_deadline,
                    accepted=accepted,
                    reconciled=reconciled,
                    primary_error=sys.exception(),
                )
    finally:
        if resources is not None:
            await resources.close()
    assert reconciled


async def _finalize_backing_loss(
    client: ClientSession,
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    authorization: str,
    partition_key: str,
    idempotency_key: str,
    *,
    admission_deadline: float,
    accepted: AcceptedRun | None,
    reconciled: bool,
    primary_error: BaseException | None,
) -> None:
    cleanup_error: BaseException | None = None
    if accepted is None:
        try:
            accepted = await _recover_before_admission_deadline(
                resources,
                config,
                partition_key,
                idempotency_key,
                admission_deadline=admission_deadline,
            )
        except (AcaSmokeEnvironmentError, AssertionError, SandboxTransportError) as exc:
            cleanup_error = exc
        if accepted is None and cleanup_error is None:
            cleanup_error = AcaSmokeEnvironmentError(
                "ACA backing-loss cleanup has an unresolved admission after its "
                "bounded recovery deadline."
            )
    if accepted is not None and not reconciled:
        try:
            await _cleanup_failed_loss_candidate(
                client,
                resources,
                config,
                authorization,
                partition_key,
                accepted,
            )
        except (AcaSmokeEnvironmentError, AssertionError, SandboxTransportError) as exc:
            cleanup_error = exc
    if cleanup_error is not None:
        if primary_error is not None:
            primary_error.add_note(
                "ACA backing-loss cleanup could not confirm controller-owned tombstoning."
            )
        else:
            raise cleanup_error


def _load_config(config: DeployedAcaLifecycleConfig) -> DeployedAcaLifecycleConfig:
    return replace(config, deployed=replace(config.deployed, agent_slug=_LOAD_AGENT_SLUG))


async def _submit_held_run(
    client: ClientSession,
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    headers: dict[str, str],
    partition_key: str,
    idempotency_key: str,
    *,
    admission_deadline: float,
) -> AcceptedRun:
    retry_budget = max(
        0.0,
        admission_deadline - _setup_deadline_now() - _HELD_RUN_RECOVERY_RESERVE_SECONDS,
    )
    try:
        async with asyncio.timeout(retry_budget):
            return await _submit_held_run_with_retries(
                client,
                resources,
                config,
                headers,
                partition_key,
                idempotency_key,
            )
    except TimeoutError as exc:
        recovered = await _recover_before_admission_deadline(
            resources,
            config,
            partition_key,
            idempotency_key,
            admission_deadline=admission_deadline,
        )
        if recovered is not None:
            return recovered
        raise AcaSmokeEnvironmentError(
            "The backing-loss held public admission exceeded its bounded setup deadline."
        ) from exc


def _setup_deadline_now() -> float:
    return asyncio.get_running_loop().time()


async def _recover_before_admission_deadline(
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    partition_key: str,
    idempotency_key: str,
    *,
    admission_deadline: float,
) -> AcceptedRun | None:
    remaining = max(0.0, admission_deadline - _setup_deadline_now())
    if not remaining:
        return None
    try:
        async with asyncio.timeout(remaining):
            return await _recover_candidate(
                resources,
                config,
                partition_key,
                idempotency_key,
            )
    except TimeoutError:
        return None


async def _submit_held_run_with_retries(
    client: ClientSession,
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    headers: dict[str, str],
    partition_key: str,
    idempotency_key: str,
) -> AcceptedRun:
    for attempt in range(_SETUP_RETRY_ATTEMPTS):
        try:
            async with asyncio.timeout(_SETUP_HTTP_ATTEMPT_TIMEOUT_SECONDS):
                status, payload, response_headers = await json_request(
                    client,
                    "POST",
                    config.deployed.chat_url,
                    headers={**headers, "Idempotency-Key": idempotency_key},
                    payload=submission_payload(_HOLD_PROMPT),
                )
        except (AcaSmokeEnvironmentError, TimeoutError) as exc:
            recovered = await _recover_candidate(
                resources,
                config,
                partition_key,
                idempotency_key,
            )
            if recovered is not None:
                return recovered
            if isinstance(exc, TimeoutError):
                raise AcaSmokeEnvironmentError(
                    "The backing-loss public setup request exceeded its bounded attempt timeout."
                ) from exc
            raise
        if status == 202:
            return parse_accepted_run(payload, config.deployed)
        if (
            status == 504
            and payload.get("error") == "setup_deadline_exceeded"
            and attempt + 1 < _SETUP_RETRY_ATTEMPTS
        ):
            await asyncio.sleep(setup_retry_after_seconds(response_headers))
            continue
        recovered = await _recover_candidate(
            resources,
            config,
            partition_key,
            idempotency_key,
        )
        if recovered is not None:
            return recovered
        if status in {401, 403, 404}:
            raise AcaSmokeEnvironmentError(
                "The protected deployed load route rejected the app-only token or is missing."
            )
        raise AssertionError(f"The held public submission returned unexpected HTTP {status}.")
    raise AssertionError("The held public submission exhausted its bounded setup retry window.")


async def _wait_for_qualification_hold_start(
    client: ClientSession,
    accepted: AcceptedRun,
    authorization: str,
) -> None:
    status, event, _ = await read_sse_until_matching_event(
        client,
        accepted.management_urls["events_url"],
        headers={"Authorization": authorization},
        matches=_is_qualification_hold_start,
    )
    assert status == 200
    assert event is not None
    assert _is_qualification_hold_start(event)


def _is_qualification_hold_start(event: object) -> bool:
    payload = getattr(event, "payload", None)
    return isinstance(payload, dict) and (
        payload.get("type") == "tool_start" and payload.get("tool_name") == "qualification_hold"
    )


async def _recover_candidate(
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    partition_key: str,
    idempotency_key: str,
) -> AcceptedRun | None:
    for attempt in range(_RECOVERY_ATTEMPTS):
        reservation = await read_owner_idempotency(
            resources,
            partition_key=partition_key,
            idempotency_key=idempotency_key,
        )
        if reservation is not None:
            return AcceptedRun(
                session_id=reservation.session_id,
                run_id=reservation.run_id,
                management_urls=config.deployed.management_urls(
                    session_id=reservation.session_id,
                    run_id=reservation.run_id,
                ),
            )
        if attempt + 1 < _RECOVERY_ATTEMPTS:
            await asyncio.sleep(_POLL_SECONDS)
    return None


async def _wait_for_active_backing(
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    partition_key: str,
    accepted: AcceptedRun,
) -> _ActiveBacking:
    deadline = asyncio.get_running_loop().time() + config.deployed.timeout_seconds
    while True:
        session = await read_authoritative_session(
            resources,
            session_id=accepted.session_id,
            partition_key=partition_key,
        )
        assert_session_belongs_to_deployment(session, config)
        run = await read_authoritative_run(
            resources,
            session_id=accepted.session_id,
            run_id=accepted.run_id,
            partition_key=partition_key,
        )
        sandbox = await owned_sandbox(resources, session)
        if has_active_owned_backing(
            session,
            run,
            sandbox,
            expected_session_id=accepted.session_id,
            expected_run_id=accepted.run_id,
        ):
            assert sandbox is not None
            return _ActiveBacking(accepted.session_id, accepted.run_id, sandbox)
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("The held run did not reach active exact-label ACA backing state.")
        await asyncio.sleep(_POLL_SECONDS)


async def _wait_for_loss_projection(
    client: ClientSession,
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    authorization: str,
    partition_key: str,
    accepted: AcceptedRun,
) -> _LossProjection:
    deadline = asyncio.get_running_loop().time() + _CONTROLLER_WAIT_SECONDS
    while True:
        status_code, status, _ = await json_request(
            client,
            "GET",
            accepted.management_urls["status_url"],
            headers={"Authorization": authorization},
        )
        session = await read_authoritative_session(
            resources,
            session_id=accepted.session_id,
            partition_key=partition_key,
        )
        run = await read_authoritative_run(
            resources,
            session_id=accepted.session_id,
            run_id=accepted.run_id,
            partition_key=partition_key,
        )
        operations = await read_session_operations(
            resources,
            session_id=accepted.session_id,
            partition_key=partition_key,
        )
        if has_lost_backing_projection(
            session,
            run,
            operations,
            expected_session_id=accepted.session_id,
            expected_run_id=accepted.run_id,
        ) and status_code == 200:
            # Re-read the public status. The one above was captured before the
            # table reads, so it can predate the terminal write the gate just
            # observed and report a pre-loss state. The public projection is
            # derived from the run record, so once the table is terminal this
            # read is deterministic rather than another race.
            status_code, status, _ = await json_request(
                client,
                "GET",
                accepted.management_urls["status_url"],
                headers={"Authorization": authorization},
            )
            if status_code == 200:
                return _LossProjection(status_code, status, run, operations)
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                "The deployed controller did not terminalize the lost backing within the "
                "operation lease and controller cadence bound."
            )
        await asyncio.sleep(_POLL_SECONDS)


async def _cleanup_failed_loss_candidate(
    client: ClientSession,
    resources: DeployedAcaLifecycleResources,
    config: DeployedAcaLifecycleConfig,
    authorization: str,
    partition_key: str,
    accepted: AcceptedRun,
) -> None:
    """Use exact labels for last-resort provider cleanup, then require controller durable cleanup."""
    session = await read_authoritative_session(
        resources,
        session_id=accepted.session_id,
        partition_key=partition_key,
    )
    assert_session_belongs_to_deployment(session, config)
    try:
        await _delete_exact_provider_backing(resources, session)
        await _wait_for_loss_projection(
            client,
            resources,
            config,
            authorization,
            partition_key,
            accepted,
        )
        await _assert_exact_provider_resources_removed(resources, session)
    except (AcaSmokeEnvironmentError, AssertionError, SandboxTransportError) as exc:
        selector = ",".join(sorted(session_labels(session)))
        raise AcaSmokeEnvironmentError(
            "ACA-SMOKE-ENV cleanup could not confirm controller tombstoning after exact-label "
            f"provider cleanup (selector keys={selector})."
        ) from exc


async def _delete_exact_provider_backing(
    resources: DeployedAcaLifecycleResources,
    session: DurableSessionRecord,
) -> None:
    sandbox = await owned_sandbox(resources, session)
    expected_sandbox_id = sandbox.sandbox_id if sandbox is not None else session.sandbox_id
    if expected_sandbox_id is None:
        return
    snapshots = await owned_snapshots(resources, session)
    for snapshot in snapshots:
        if snapshot.sandbox_id != expected_sandbox_id:
            raise AssertionError("The exact-label snapshot did not belong to the selected sandbox.")
    for snapshot in snapshots:
        await resources.adapter.delete_snapshot(snapshot.snapshot_id)
    if sandbox is not None:
        await resources.adapter.delete_sandbox(sandbox.sandbox_id)


async def _assert_exact_provider_resources_removed(
    resources: DeployedAcaLifecycleResources,
    session: DurableSessionRecord,
) -> None:
    if await owned_sandbox(resources, session) is not None or await owned_snapshots(resources, session):
        raise AssertionError("The exact-label provider backing was not fully removed.")
