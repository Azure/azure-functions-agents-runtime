"""Manual GA qualification through only an Easy-Auth-protected deployed Function."""

from __future__ import annotations

import uuid

import pytest
from aiohttp import ClientSession
from tests.aca_smoke_diagnostics import AcaSmokeEnvironmentError
from tests.live.aca_deployed_agent_support import (
    DeployedAcaSmokeConfig,
    acquire_default_authorization_header,
    client_timeout,
    deployed_aca_smoke_config_from_environment,
    deployed_aca_smoke_enabled,
    json_request,
    parse_accepted_run,
    read_sse_events,
    redact_deployed_aca_evidence,
    submission_payload,
)

if not deployed_aca_smoke_enabled():
    pytest.skip(
        "Set AZURE_FUNCTIONS_AGENTS_RUN_DEPLOYED_ACA_SMOKE=1 after authorization to qualify "
        "a deployed Easy-Auth-protected Function App.",
        allow_module_level=True,
    )


@pytest.fixture
def deployed_aca_smoke_config() -> DeployedAcaSmokeConfig:
    return deployed_aca_smoke_config_from_environment()


@pytest.mark.live_aca
@pytest.mark.asyncio
async def test_deployed_aca_agent_turn_uses_only_public_authenticated_routes(
    deployed_aca_smoke_config: DeployedAcaSmokeConfig,
) -> None:
    """Qualify submit, journal SSE, status, and result without direct ACA or Table access."""

    config = deployed_aca_smoke_config
    prompt = "Reply with a brief acknowledgement."
    async with ClientSession(timeout=client_timeout(config)) as session:
        unauthorized_status, _, _ = await json_request(
            session,
            "POST",
            config.chat_url,
            headers={"Prefer": "respond-async"},
            payload=submission_payload(prompt),
        )
        assert unauthorized_status in {401, 403}

        authorization = await acquire_default_authorization_header(config.token_scope)
        headers = {
            "Authorization": authorization,
            "Content-Type": "application/json",
            "Prefer": "respond-async",
            "Idempotency-Key": uuid.uuid4().hex,
        }
        accepted_status, accepted, _ = await json_request(
            session,
            "POST",
            config.chat_url,
            headers=headers,
            payload=submission_payload(prompt),
        )
        if accepted_status in {401, 403, 404}:
            raise AcaSmokeEnvironmentError(
                "The protected deployed chat route rejected the app-only token or is missing: "
                f"{redact_deployed_aca_evidence(config.chat_url)} (HTTP {accepted_status})."
            )
        assert accepted_status == 202
        accepted_run = parse_accepted_run(accepted, config)
        session_id = accepted_run.session_id
        run_id = accepted_run.run_id
        management = accepted_run.management_urls

        events_status, events, _ = await read_sse_events(
            session,
            management["events_url"],
            headers={"Authorization": authorization},
        )
        assert events_status == 200
        assert events
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
        assert events[-1].payload.get("type") == "done"

        status_code, status, _ = await json_request(
            session,
            "GET",
            management["status_url"],
            headers={"Authorization": authorization},
        )
        assert status_code == 200
        assert status.get("session_id") == session_id
        assert status.get("run_id") == run_id
        assert status.get("state") == "succeeded"
        assert status.get("last_event_id") == events[-1].sequence
        assert status.get("result_available") is True

        result_code, result, _ = await json_request(
            session,
            "GET",
            management["result_url"],
            headers={"Authorization": authorization},
        )
        assert result_code == 200
        assert result.get("session_id") == session_id
        assert result.get("run_id") == run_id
        response = result.get("result")
        assert isinstance(response, dict)
        content = response.get("content")
        assert isinstance(content, str) and content.strip()
