from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from azure_functions_agents.config.loader import load_agent_specs, load_global_config
from azure_functions_agents.config.merge import compose
from tests.aca_smoke_diagnostics import AcaSmokeEnvironmentError
from tests.live import aca_deployed_agent_support as support

_DEPLOYABLE_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "live_aca_deployed_agent_turn"


def _set_deployed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_FUNCTION_BASE_URL",
        "https://deployed-aca.azurewebsites.net/api",
    )
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_AGENT_SLUG", "deployed_turn")
    monkeypatch.setenv(
        "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EASY_AUTH_TOKEN_SCOPE",
        "api://deployed-aca/.default",
    )
    monkeypatch.setenv(
        "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EASY_AUTH_AUDIENCE",
        "deployed-aca",
    )
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_TIMEOUT_SECONDS", "180")


def _set_deployable_fixture_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "deployed-model")
    monkeypatch.setenv(
        "AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID",
        "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.App/sandboxGroups/group",
    )
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_ENTRA_TENANT_ID", "tenant-id")
    monkeypatch.setenv(
        "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EASY_AUTH_AUDIENCE",
        "api://deployed-aca",
    )
    monkeypatch.setenv(
        "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_TEST_INVOKER_CLIENT_ID",
        "test-invoker-client-id",
    )


def test_deployable_fixture_has_persistent_entra_no_tools_aca_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_deployable_fixture_environment(monkeypatch)

    global_config = load_global_config(_DEPLOYABLE_FIXTURE)
    [spec] = load_agent_specs(_DEPLOYABLE_FIXTURE, strict=True)
    resolved = compose(spec, global_config)

    assert (_DEPLOYABLE_FIXTURE / "function_app.py").is_file()
    assert (_DEPLOYABLE_FIXTURE / "host.json").is_file()
    assert (_DEPLOYABLE_FIXTURE / ".funcignore").is_file()
    assert global_config.session_runtime is not None
    assert global_config.session_runtime.aca_sandbox is not None
    assert global_config.session_runtime.aca_sandbox.retention is not None
    assert global_config.session_runtime.aca_sandbox.retention.auto_suspend_idle == 300
    assert global_config.session_runtime.aca_sandbox.retention.reclaim_idle == 900
    assert resolved.model == "deployed-model"
    assert resolved.builtin_endpoints.http_auth.mode == "entra"
    assert resolved.tools_disabled is True
    assert resolved.mcp_disabled is True
    assert resolved.web_request_config is None


def test_deployed_config_reads_only_safe_url_and_route_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_deployed_environment(monkeypatch)

    config = support.deployed_aca_smoke_config_from_environment()

    assert config.chat_url == "https://deployed-aca.azurewebsites.net/api/agents/deployed_turn/chat"
    assert config.management_urls(session_id="session-1", run_id="run-1") == {
        "status_url": "https://deployed-aca.azurewebsites.net/api/agents/deployed_turn/sessions/session-1/runs/run-1",
        "result_url": "https://deployed-aca.azurewebsites.net/api/agents/deployed_turn/sessions/session-1/runs/run-1/result",
        "events_url": "https://deployed-aca.azurewebsites.net/api/agents/deployed_turn/sessions/session-1/runs/run-1/events",
        "cancel_url": "https://deployed-aca.azurewebsites.net/api/agents/deployed_turn/sessions/session-1/runs/run-1/cancel",
    }


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        (
            "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_FUNCTION_BASE_URL",
            "http://deployed-aca.azurewebsites.net",
            "HTTPS Function base URL",
        ),
        (
            "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_FUNCTION_BASE_URL",
            "https://user:password@deployed-aca.azurewebsites.net",
            "without a path, credentials, query, or fragment",
        ),
        (
            "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_FUNCTION_BASE_URL",
            "https://deployed-aca.azurewebsites.net?code=secret",
            "without a path, credentials, query, or fragment",
        ),
        (
            "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EASY_AUTH_TOKEN_SCOPE",
            "api://deployed-aca",
            "must end",
        ),
        (
            "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EASY_AUTH_TOKEN_SCOPE",
            "api://different-audience/.default",
            "must match",
        ),
        (
            "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_TIMEOUT_SECONDS",
            "231",
            "between 1 and 230",
        ),
    ],
)
def test_deployed_config_rejects_unsafe_environment(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
    message: str,
) -> None:
    _set_deployed_environment(monkeypatch)
    monkeypatch.setenv(name, value)

    with pytest.raises(AcaSmokeEnvironmentError, match=message):
        support.deployed_aca_smoke_config_from_environment()


@pytest.mark.asyncio
async def test_authorization_header_uses_credential_token_only_for_client_wiring() -> None:
    class CredentialStub:
        async def get_token(self, *scopes: str) -> object:
            assert scopes == ("api://deployed-aca/.default",)
            return SimpleNamespace(token="test-token")

    # This narrow stub proves HTTP client wiring only; it does not prove Entra or Azure authorization.
    assert (
        await support.acquire_authorization_header(CredentialStub(), "api://deployed-aca/.default")
        == "Bearer test-token"
    )


def test_sse_parser_reads_controller_event_shape_and_rejects_malformed_frames() -> None:
    events = support.parse_sse_frames(
        [
            ": heartbeat",
            'id: 1\ndata: {"type":"session","session_id":"session-1"}',
            'id: 2\ndata: {"type":"done"}',
        ]
    )

    assert [(event.sequence, event.payload["type"]) for event in events] == [
        (1, "session"),
        (2, "done"),
    ]
    with pytest.raises(ValueError, match="include id and data"):
        support.parse_sse_frames(['data: {"type":"done"}'])


def test_submission_and_accepted_payloads_follow_the_public_controller_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_deployed_environment(monkeypatch)
    config = support.deployed_aca_smoke_config_from_environment()

    assert support.submission_payload("hello") == {"prompt": "hello"}
    accepted = support.parse_accepted_run(
        {
            "session_id": "session-1",
            "run_id": "run-1",
            "status_url": "/agents/deployed_turn/sessions/session-1/runs/run-1",
            "events_url": "/agents/deployed_turn/sessions/session-1/runs/run-1/events",
            "result_url": "/agents/deployed_turn/sessions/session-1/runs/run-1/result",
            "cancel_url": "/agents/deployed_turn/sessions/session-1/runs/run-1/cancel",
        },
        config,
    )

    assert accepted.session_id == "session-1"
    assert accepted.run_id == "run-1"


def test_deployed_evidence_redaction_removes_query_and_bearer_token() -> None:
    url_evidence = support.redact_deployed_aca_evidence(
        "https://user:password@example.test/path?code=secret Bearer top-secret token=another-secret"
    )
    token_evidence = support.redact_deployed_aca_evidence(
        "Bearer top-secret token=another-secret"
    )

    assert url_evidence == "https://example.test/path"
    assert "top-secret" not in token_evidence
    assert "another-secret" not in token_evidence
    assert "[redacted]" in token_evidence


@pytest.mark.asyncio
async def test_unauthorized_response_body_is_ignored() -> None:
    class UnauthorizedResponse:
        status = 401

        async def read(self) -> bytes:
            return b'["platform-specific", "error"]'

        async def json(self, *, content_type: object = None) -> object:
            del content_type
            return ["platform-specific", "error"]

    assert await support._json_body(UnauthorizedResponse()) == {}  # type: ignore[arg-type]
