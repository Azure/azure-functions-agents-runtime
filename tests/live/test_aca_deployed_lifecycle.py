"""Manual live qualification of real ACA auto-suspend, resume/reuse, and reclaim."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from aiohttp import ClientSession
from tests.aca_smoke_diagnostics import AcaSmokeEnvironmentError
from tests.live.aca_deployed_agent_support import (
    AcceptedRun,
    acquire_default_authorization_header,
    client_timeout,
    deployed_aca_smoke_enabled,
    json_request,
    parse_accepted_run,
    read_sse_events,
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
    read_authoritative_session,
    wait_for_idle_session,
    wait_for_reclaimed_session,
    wait_for_suspended_sandbox,
    wait_until_reclaim_due,
)

if not deployed_aca_smoke_enabled():
    pytest.skip(
        "Set AZURE_FUNCTIONS_AGENTS_RUN_DEPLOYED_ACA_SMOKE=1 after authorization to qualify "
        "a deployed ACA session lifecycle.",
        allow_module_level=True,
    )


@pytest.mark.live_aca
@pytest.mark.asyncio
async def test_deployed_aca_session_auto_suspends_resumes_reuses_and_reclaims() -> None:
    """Use public turns and read-only Table/ACA observations of the deployed timer lifecycle."""

    config = deployed_aca_lifecycle_config_from_environment()
    resources: DeployedAcaLifecycleResources | None = None
    last_session_id: str | None = None
    cleanup_session = None
    try:
        resources = await open_deployed_aca_lifecycle_resources(config)
        authorization = await acquire_default_authorization_header(config.deployed.token_scope)
        headers = {
            "Authorization": authorization,
            "Content-Type": "application/json",
            "Prefer": "respond-async",
        }
        async with ClientSession(timeout=client_timeout(config.deployed)) as client:
            first_run = await _submit_and_wait(
                client,
                config,
                headers,
                session_id=None,
            )
            last_session_id = first_run.session_id
            first_session = await wait_for_idle_session(
                resources,
                session_id=first_run.session_id,
                timeout_seconds=config.deployed.timeout_seconds,
            )
            cleanup_session = first_session
            assert_session_belongs_to_deployment(first_session, config)
            assert first_session.generation >= 1
            assert first_session.sandbox_id is not None
            assert first_session.expires_at - first_session.last_activity_at == timedelta(seconds=120)
            first_sandbox_id = first_session.sandbox_id
            first_generation = first_session.generation

            suspended = await wait_for_suspended_sandbox(
                resources,
                first_session,
                timeout_seconds=105.0,
            )
            assert suspended.sandbox_id == first_sandbox_id

            resumed_run = await _submit_and_wait(
                client,
                config,
                headers,
                session_id=first_run.session_id,
            )
            assert resumed_run.session_id == first_run.session_id
            resumed_session = await wait_for_idle_session(
                resources,
                session_id=resumed_run.session_id,
                timeout_seconds=config.deployed.timeout_seconds,
            )
            cleanup_session = resumed_session
            assert resumed_session.sandbox_id == first_sandbox_id
            assert resumed_session.generation == first_generation
            assert resumed_session.expires_at - resumed_session.last_activity_at == timedelta(
                seconds=120
            )

            resumed_suspended = await wait_for_suspended_sandbox(
                resources,
                resumed_session,
                timeout_seconds=105.0,
            )
            assert resumed_suspended.sandbox_id == first_sandbox_id
            snapshots_before_reclaim = await owned_snapshots(resources, resumed_session)
            assert snapshots_before_reclaim
            await wait_until_reclaim_due(resumed_session)
            reclaimed = await wait_for_reclaimed_session(
                resources,
                session_id=resumed_run.session_id,
            )
            cleanup_session = reclaimed
            assert reclaimed.status == "tombstoned"
            assert reclaimed.tombstone_reason == "reclaimed_idle_session"
            assert reclaimed.active_run_id is None
            assert reclaimed.active_operation_id is None
            assert await owned_sandbox(resources, reclaimed) is None
            snapshots_after_reclaim = await owned_snapshots(resources, reclaimed)
            assert not {
                snapshot.snapshot_id for snapshot in snapshots_before_reclaim
            }.intersection(snapshot.snapshot_id for snapshot in snapshots_after_reclaim)
            assert not set(resumed_session.snapshot_ids).intersection(
                snapshot.snapshot_id for snapshot in snapshots_after_reclaim
            )

            status_code, status, _ = await json_request(
                client,
                "GET",
                resumed_run.management_urls["status_url"],
                headers={"Authorization": authorization},
            )
            assert status_code == 200
            assert status.get("session_id") == resumed_run.session_id
            assert status.get("run_id") == resumed_run.run_id
            assert status.get("state") == "succeeded"

            result_code, result, _ = await json_request(
                client,
                "GET",
                resumed_run.management_urls["result_url"],
                headers={"Authorization": authorization},
            )
            assert result_code == 410
            assert result.get("error") in {"result_unavailable", "session_gone"}
    finally:
        if resources is not None:
            try:
                if cleanup_session is not None:
                    await cleanup_owned_lifecycle_session(
                        resources,
                        session=cleanup_session,
                        config=config,
                    )
                elif last_session_id is not None:
                    session = await read_authoritative_session(resources, session_id=last_session_id)
                    await cleanup_owned_lifecycle_session(resources, session=session, config=config)
            finally:
                await resources.close()


async def _submit_and_wait(
    client: ClientSession,
    config: DeployedAcaLifecycleConfig,
    headers: dict[str, str],
    *,
    session_id: str | None,
) -> AcceptedRun:
    request_headers = {**headers, "Idempotency-Key": uuid.uuid4().hex}
    if session_id is not None:
        request_headers["x-ms-session-id"] = session_id
    accepted_status, accepted, response_headers = await json_request(
        client,
        "POST",
        config.deployed.chat_url,
        headers=request_headers,
        payload=submission_payload("Return a brief acknowledgement."),
    )
    if accepted_status in {401, 403, 404}:
        raise AcaSmokeEnvironmentError(
            "The protected deployed chat route rejected the app-only token or is missing."
        )
    assert accepted_status == 202
    accepted_run = parse_accepted_run(accepted, config.deployed)
    assert _header(response_headers, "x-ms-session-id") == accepted_run.session_id
    if session_id is not None:
        assert accepted_run.session_id == session_id

    events_status, events, _ = await read_sse_events(
        client,
        accepted_run.management_urls["events_url"],
        headers={"Authorization": headers["Authorization"]},
    )
    assert events_status == 200
    assert events and events[-1].payload.get("type") == "done"
    status_code, status, _ = await json_request(
        client,
        "GET",
        accepted_run.management_urls["status_url"],
        headers={"Authorization": headers["Authorization"]},
    )
    assert status_code == 200
    assert status.get("state") == "succeeded"
    result_code, result, _ = await json_request(
        client,
        "GET",
        accepted_run.management_urls["result_url"],
        headers={"Authorization": headers["Authorization"]},
    )
    assert result_code == 200
    assert isinstance(result.get("result"), dict)
    return accepted_run


def _header(headers: object, name: str) -> str | None:
    if not hasattr(headers, "items"):
        return None
    return next(
        (
            value
            for key, value in headers.items()  # type: ignore[union-attr]
            if isinstance(key, str) and key.casefold() == name
        ),
        None,
    )
