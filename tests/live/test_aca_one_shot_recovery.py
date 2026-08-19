"""Opt-in one-shot recovery coverage against the controlled deployed ACA fixture."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

import pytest
import pytest_asyncio
from aiohttp import ClientSession
from tests.aca_smoke_diagnostics import AcaSmokeEnvironmentError
from tests.live.aca_deployed_agent_support import (
    AcceptedRun,
    DeployedAcaSmokeConfig,
    acquire_default_authorization_header,
    cancel_retry_after_seconds,
    client_timeout,
    deployed_aca_smoke_enabled,
    deployed_aca_timeout_recovery_config_from_environment,
    json_request,
    parse_accepted_run,
    response_header,
    submission_payload,
    timeout_recovery_submission_headers,
)

_TERMINAL_SETTLEMENT_TIMEOUT_SECONDS = 300.0
_STATUS_POLL_SECONDS = 1.0
_TERMINAL_STATES = frozenset({"abandoned", "canceled", "failed", "succeeded", "timed_out"})
_ENVIRONMENT_UNREADY_STATUSES = frozenset({401, 403, 404, 429, 502, 503})

if not deployed_aca_smoke_enabled():
    pytest.skip(
        "Set AZURE_FUNCTIONS_AGENTS_RUN_DEPLOYED_ACA_SMOKE=1 after authorization to run "
        "the controlled deployed ACA setup-timeout fixture.",
        allow_module_level=True,
    )


@dataclass(frozen=True, slots=True)
class _AdmissionResponse:
    status_code: int
    payload: dict[str, object]
    headers: Mapping[str, str]
    authorization: str


@pytest.fixture
def timeout_recovery_config() -> DeployedAcaSmokeConfig:
    return deployed_aca_timeout_recovery_config_from_environment()


@pytest_asyncio.fixture
async def timeout_recovery_admission(
    timeout_recovery_config: DeployedAcaSmokeConfig,
) -> _AdmissionResponse:
    authorization = await acquire_default_authorization_header(timeout_recovery_config.token_scope)
    async with ClientSession(timeout=client_timeout(timeout_recovery_config)) as session:
        return await _submit_timeout_recovery_once(
            session,
            timeout_recovery_config,
            authorization,
        )


async def _submit_timeout_recovery_once(
    session: ClientSession,
    config: DeployedAcaSmokeConfig,
    authorization: str,
) -> _AdmissionResponse:
    request_headers = timeout_recovery_submission_headers(
        authorization,
        f"aca-one-shot-{uuid.uuid4().hex}",
    )
    status_code, payload, response_headers = await json_request(
        session,
        "POST",
        config.chat_url,
        headers=request_headers,
        payload=submission_payload("Trigger the controlled setup-timeout recovery fixture."),
    )
    if status_code in _ENVIRONMENT_UNREADY_STATUSES:
        detail = payload.get("error")
        suffix = f" ({detail})" if isinstance(detail, str) and detail else ""
        raise AcaSmokeEnvironmentError(
            "The controlled deployed setup-timeout route is unavailable, unauthorized, or "
            f"capacity constrained (HTTP {status_code}){suffix}."
        )
    return _AdmissionResponse(
        status_code=status_code,
        payload=payload,
        headers=response_headers,
        authorization=authorization,
    )


@pytest.mark.live_aca
@pytest.mark.asyncio
async def test_live_aca_first_response_is_a_linked_setup_timeout_recovery_handle(
    timeout_recovery_config: DeployedAcaSmokeConfig,
    timeout_recovery_admission: _AdmissionResponse,
) -> None:
    """Require the fixture's only submission to time out after durable admission."""
    response = timeout_recovery_admission
    cleanup_ticket = _ticket_from_identifiers(response.payload, timeout_recovery_config)
    primary_error: BaseException | None = None
    terminal: dict[str, object] | None = None
    try:
        assert response.status_code == 504
        assert response.payload.get("error") == "setup_deadline_exceeded"
        assert response.payload.get("admission") == "committed"
        assert response.payload.get("retry_with") == "respond-async"
        assert response.payload.get("phase") == "provisioning"
        assert cleanup_ticket is not None

        ticket = parse_accepted_run(response.payload, timeout_recovery_config)
        assert ticket == cleanup_ticket
        assert response_header(response.headers, "Location") == response.payload["status_url"]
        assert response_header(response.headers, "x-ms-session-id") == ticket.session_id
        assert response_header(response.headers, "x-ms-retry-with") == "respond-async"
        retry_after = response_header(response.headers, "Retry-After")
        assert retry_after is not None and retry_after.isdecimal()
        assert 1 <= int(retry_after) <= 120
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if cleanup_ticket is not None:
            try:
                async with ClientSession(timeout=client_timeout(timeout_recovery_config)) as session:
                    terminal = await _cancel_and_wait_for_terminal(
                        session,
                        timeout_recovery_config,
                        cleanup_ticket,
                        response.authorization,
                    )
            except BaseException:
                if primary_error is not None:
                    primary_error.add_note(
                        "Recovery fixture cleanup also failed after the first submission."
                    )
                else:
                    raise

    assert terminal is not None
    assert terminal.get("phase") == "terminal"
    assert terminal.get("status") in _TERMINAL_STATES


def _ticket_from_identifiers(
    payload: Mapping[str, object],
    config: DeployedAcaSmokeConfig,
) -> AcceptedRun | None:
    session_id = payload.get("session_id")
    run_id = payload.get("run_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    if not isinstance(run_id, str) or not run_id:
        return None
    return AcceptedRun(
        session_id=session_id,
        run_id=run_id,
        management_urls=config.management_urls(session_id=session_id, run_id=run_id),
    )


async def _cancel_and_wait_for_terminal(
    session: ClientSession,
    config: DeployedAcaSmokeConfig,
    ticket: AcceptedRun,
    authorization: str,
) -> dict[str, object]:
    headers = {"Authorization": authorization}
    cancel_status, canceled, cancel_headers = await json_request(
        session,
        "POST",
        ticket.management_urls["cancel_url"],
        headers=headers,
    )
    assert cancel_status in {200, 202}
    assert canceled.get("session_id") == ticket.session_id
    assert canceled.get("run_id") == ticket.run_id
    if cancel_status == 202:
        retry_after = response_header(cancel_headers, "Retry-After")
        assert retry_after is not None and retry_after.isdecimal()
        assert 1 <= int(retry_after) <= 120
        await asyncio.sleep(cancel_retry_after_seconds(cancel_headers))
    return await _wait_for_terminal_status(session, config, ticket, headers)


async def _wait_for_terminal_status(
    session: ClientSession,
    config: DeployedAcaSmokeConfig,
    ticket: AcceptedRun,
    headers: Mapping[str, str],
) -> dict[str, object]:
    deadline = time.monotonic() + _TERMINAL_SETTLEMENT_TIMEOUT_SECONDS
    latest: dict[str, object] | None = None
    while time.monotonic() < deadline:
        status_code, latest, _ = await json_request(
            session,
            "GET",
            ticket.management_urls["status_url"],
            headers=headers,
        )
        assert status_code == 200
        assert latest.get("session_id") == ticket.session_id
        assert latest.get("run_id") == ticket.run_id
        if latest.get("phase") == "terminal":
            return latest
        await asyncio.sleep(_STATUS_POLL_SECONDS)
    raise AssertionError(f"Timed-out recovery run did not reach a terminal phase: {latest!r}")
