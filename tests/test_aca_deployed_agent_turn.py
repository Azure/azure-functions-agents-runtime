from __future__ import annotations

import base64
import inspect
import json
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from azure_functions_agents.config.loader import load_agent_specs, load_global_config
from azure_functions_agents.config.merge import compose
from azure_functions_agents.discovery.tools import clear_tool_discovery_cache, discover_user_tools
from azure_functions_agents.session_state import (
    AppIdentity,
    DurableOwnerIdempotencyRecord,
    DurableSessionRecord,
    EntraUserOwnerContext,
    FunctionAppOwnerContext,
    hash_idempotency_key,
    owner_partition,
)
from azure_functions_agents.transport.transport_models import SandboxSummary
from tests.aca_smoke_diagnostics import AcaSmokeEnvironmentError
from tests.live import aca_deployed_agent_support as support
from tests.live import aca_deployed_lifecycle_support as lifecycle_support

_DEPLOYABLE_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "live_aca_deployed_agent_turn"
_NOW = datetime(2026, 8, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Retry-After": "120"}, 120.0),
        ({"retry-after": "1"}, 1.0),
        ({}, 120.0),
        ({"Retry-After": "0"}, 120.0),
        ({"Retry-After": "121"}, 120.0),
        ({"Retry-After": "invalid"}, 120.0),
    ],
)
def test_setup_retry_after_uses_only_a_bounded_lease_delay(
    headers: dict[str, str], expected: float
) -> None:
    assert support.setup_retry_after_seconds(headers) == expected


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


def _set_deployed_lifecycle_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_deployed_environment(monkeypatch)
    monkeypatch.setenv(
        "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_TABLE_SERVICE_URI",
        "https://deployedacatable.table.core.windows.net",
    )
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_TABLE_NAME", "AzureFunctionsAgentsSessions")
    monkeypatch.setenv(
        "AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID",
        "/subscriptions/00000000-0000-0000-0000-000000000000/"
        "resourceGroups/rg/providers/Microsoft.App/sandboxGroups/group",
    )
    monkeypatch.setenv(
        "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_APP_SUBSCRIPTION_ID",
        "00000000-0000-0000-0000-000000000000",
    )
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_APP_SITE_NAME", "deployed-aca")


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
    specs = {
        Path(spec.source_file).stem.removesuffix(".agent"): spec
        for spec in load_agent_specs(_DEPLOYABLE_FIXTURE, strict=True)
    }
    regular = compose(specs["deployed_turn"], global_config)
    load = compose(specs["deployed_load"], global_config)

    assert (_DEPLOYABLE_FIXTURE / "function_app.py").is_file()
    assert (_DEPLOYABLE_FIXTURE / "host.json").is_file()
    assert (_DEPLOYABLE_FIXTURE / ".funcignore").is_file()
    assert global_config.session_runtime is not None
    assert global_config.session_runtime.aca_sandbox is not None
    assert global_config.session_runtime.aca_sandbox.retention is not None
    assert global_config.session_runtime.aca_sandbox.retention.auto_suspend_idle == 60
    assert global_config.session_runtime.aca_sandbox.retention.reclaim_idle == 120
    assert regular.model == "deployed-model"
    assert regular.timeout == 120
    assert regular.tools_disabled
    assert load.timeout == 900
    assert not load.tools_disabled
    assert regular.builtin_endpoints.http_auth.mode == "entra"
    clear_tool_discovery_cache()
    discovered = discover_user_tools(_DEPLOYABLE_FIXTURE)
    assert [tool.name for tool in discovered.tools] == ["qualification_hold"]
    assert regular.mcp_disabled is True
    assert regular.web_request_config is None


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


@pytest.mark.asyncio
async def test_authorization_owner_claims_are_validated_and_redacted() -> None:
    tenant_id = "11111111-2222-3333-4444-555555555555"
    object_id = "66666666-7777-8888-9999-aaaaaaaaaaaa"
    payload = base64.urlsafe_b64encode(
        json.dumps({"tid": tenant_id, "oid": object_id}).encode()
    ).decode().rstrip("=")
    token = f"header.{payload}.signature"

    class Credential:
        async def get_token(self, scope: str) -> object:
            assert scope == "api://deployed-aca/.default"
            return SimpleNamespace(token=token)

    evidence = await support.acquire_authorization_evidence(
        Credential(),
        "api://deployed-aca/.default",
    )
    app = AppIdentity.create(
        subscription_id="11111111-2222-3333-4444-555555555555",
        site_name="deployed-aca",
    )
    selected = owner_partition(
        EntraUserOwnerContext.create(app, "deployed_load", evidence.tenant_id, evidence.object_id)
    )
    durable = replace(_lifecycle_session(), owner_partition=selected)

    assert (evidence.tenant_id, evidence.object_id) == (tenant_id, object_id)
    assert durable.owner_partition == selected
    assert token not in repr(evidence)
    assert tenant_id not in repr(evidence)


def test_authorization_owner_claims_reject_invalid_jwt_identity() -> None:
    with pytest.raises(AcaSmokeEnvironmentError, match="valid tid and oid"):
        support._validated_token_owner("header.eyJ0aWQiOiJub3QtYS1ndWlkIn0.signature")


def test_deployed_lifecycle_config_reads_real_resource_observation_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_deployed_lifecycle_environment(monkeypatch)

    config = lifecycle_support.deployed_aca_lifecycle_config_from_environment()

    assert config.table_service_uri == "https://deployedacatable.table.core.windows.net"
    assert config.table_name == "AzureFunctionsAgentsSessions"
    assert config.app_identity.site_name == "deployed-aca"
    assert config.app_hash.startswith("a1-")
    assert lifecycle_support.LIFECYCLE_AUTO_SUSPEND_SECONDS == 60
    assert lifecycle_support.LIFECYCLE_RECLAIM_IDLE_SECONDS == 120
    assert lifecycle_support.LIFECYCLE_RECONCILER_CADENCE_SECONDS == 60
    assert lifecycle_support.LIFECYCLE_RECLAIM_CONTROLLER_WINDOWS >= 3


def test_lifecycle_support_has_no_ci_reconciler_or_table_writer_api() -> None:
    source = inspect.getsource(lifecycle_support)

    assert "SessionReconciler" not in source
    assert "ReconcileReport" not in source
    assert "AzureTableSessionStateStore" not in source
    assert "query_entities" in source
    for write_api in (
        "create_entity",
        "upsert_entity",
        "update_entity",
        "delete_entity",
        "submit_transaction",
    ):
        assert write_api not in source


@pytest.mark.asyncio
async def test_reclaim_observation_polls_authoritative_table_until_controller_tombstone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = _lifecycle_session()
    tombstoned = replace(
        ready,
        status="tombstoned",
        sandbox_id=None,
        idle_policy_armed=False,
        tombstone_reason="reclaimed_idle_session",
    )
    table = _ReadOnlyTable([ready.to_table_entity(), tombstoned.to_table_entity()])
    resources = _lifecycle_resources(table_client=table, adapter=SimpleNamespace())
    monkeypatch.setattr(lifecycle_support, "_POLL_SECONDS", 0)

    observed = await lifecycle_support.wait_for_reclaimed_session(
        resources,
        session_id=ready.session_id,
        timeout_seconds=1,
    )

    assert observed == tombstoned
    assert len(table.calls) == 2
    assert all(
        call == (
            "RowKey eq 'session:session-lifecycle'",
            2,
        )
        for call in table.calls
    )


@pytest.mark.asyncio
async def test_authoritative_reads_can_require_an_exact_owner_partition() -> None:
    session = _lifecycle_session()
    table = _ReadOnlyTable([session.to_table_entity()])
    resources = _lifecycle_resources(table_client=table, adapter=SimpleNamespace())

    observed = await lifecycle_support.read_authoritative_session(
        resources,
        session_id=session.session_id,
        partition_key=session.owner_partition.partition_key,
    )

    assert observed == session
    assert table.calls == [
        (
            f"PartitionKey eq '{session.owner_partition.partition_key}' and "
            "RowKey eq 'session:session-lifecycle'",
            2,
        )
    ]


@pytest.mark.asyncio
async def test_owner_idempotency_read_hashes_the_raw_key_inside_exact_partition() -> None:
    session = _lifecycle_session()
    raw_key = "raw-idempotency-key"
    record = DurableOwnerIdempotencyRecord.create(
        owner_partition=session.owner_partition,
        idempotency_hash=hash_idempotency_key(raw_key),
        request_hash="a" * 64,
        session_id=session.session_id,
        run_id="run-lifecycle",
        expires_at=_NOW + timedelta(seconds=120),
        created_at=_NOW,
    )
    table = _ReadOnlyTable([record.to_table_entity()])
    resources = _lifecycle_resources(table_client=table, adapter=SimpleNamespace())

    observed = await lifecycle_support.read_owner_idempotency(
        resources,
        partition_key=session.owner_partition.partition_key,
        idempotency_key=raw_key,
    )

    assert observed == record
    assert raw_key not in table.calls[0][0]
    assert "RowKey eq 'owner-idem:" in table.calls[0][0]
    assert f"PartitionKey eq '{session.owner_partition.partition_key}'" in table.calls[0][0]


@pytest.mark.asyncio
async def test_lifecycle_cleanup_deletes_only_exact_label_sandbox_then_observes_tombstone() -> None:
    session = _lifecycle_session()
    tombstoned = replace(
        session,
        status="tombstoned",
        sandbox_id=None,
        idle_policy_armed=False,
        tombstone_reason="reclaimed_idle_session",
    )
    table = _ReadOnlyTable([tombstoned.to_table_entity()])
    adapter = _ExactLabelCleanupAdapter(session)
    resources = _lifecycle_resources(table_client=table, adapter=adapter)
    config = SimpleNamespace(app_hash=session.owner_partition.app_hash)

    await lifecycle_support.cleanup_owned_lifecycle_session(
        resources,
        session=session,
        config=config,  # type: ignore[arg-type]
    )

    assert adapter.list_labels == [lifecycle_support.session_labels(session)]
    assert adapter.deleted_sandbox_ids == [session.sandbox_id]
    assert len(table.calls) == 1


@pytest.mark.asyncio
async def test_lifecycle_cleanup_fails_as_environment_error_when_controller_tombstone_is_unconfirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _lifecycle_session()
    table = _ReadOnlyTable([session.to_table_entity()])
    adapter = _ExactLabelCleanupAdapter(session)
    resources = _lifecycle_resources(table_client=table, adapter=adapter)
    config = SimpleNamespace(app_hash=session.owner_partition.app_hash)
    monkeypatch.setattr(lifecycle_support, "_POLL_SECONDS", 0)

    with pytest.raises(
        AcaSmokeEnvironmentError,
        match=r"ACA-SMOKE-ENV cleanup.*session_id=session-lifecycle",
    ):
        await lifecycle_support.cleanup_owned_lifecycle_session(
            resources,
            session=session,
            config=config,  # type: ignore[arg-type]
        )

    assert adapter.list_labels == [lifecycle_support.session_labels(session)]
    assert adapter.deleted_sandbox_ids == [session.sandbox_id]


class _ReadOnlyTable:
    def __init__(self, entities: list[dict[str, Any]]) -> None:
        self._entities = entities
        self.calls: list[tuple[str, int]] = []

    async def query_entities(
        self,
        *,
        query_filter: str,
        results_per_page: int,
    ) -> Any:
        self.calls.append((query_filter, results_per_page))
        if self._entities:
            yield self._entities.pop(0)


class _ExactLabelCleanupAdapter:
    def __init__(self, session: DurableSessionRecord) -> None:
        self._session = session
        self.list_labels: list[dict[str, str]] = []
        self.deleted_sandbox_ids: list[str | None] = []

    async def list_sandboxes(self, *, labels: dict[str, str]) -> list[SandboxSummary]:
        self.list_labels.append(labels)
        return [
            SandboxSummary.create(
                sandbox_id=self._session.sandbox_id or "missing",
                labels=labels,
            )
        ]

    async def delete_sandbox(self, sandbox_id: str) -> None:
        self.deleted_sandbox_ids.append(sandbox_id)


def _lifecycle_resources(
    *,
    table_client: _ReadOnlyTable,
    adapter: object,
) -> lifecycle_support.DeployedAcaLifecycleResources:
    return lifecycle_support.DeployedAcaLifecycleResources(
        adapter=adapter,  # type: ignore[arg-type]
        table_client=table_client,
        _service_client=SimpleNamespace(),
        _credential=SimpleNamespace(),
    )


def _lifecycle_session() -> DurableSessionRecord:
    app = AppIdentity.create(
        subscription_id="11111111-2222-3333-4444-555555555555",
        site_name="deployed-aca",
    )
    return DurableSessionRecord.create(
        owner_partition=owner_partition(FunctionAppOwnerContext.create(app, "deployed_turn")),
        session_id="session-lifecycle",
        sandbox_id="sandbox-lifecycle",
        generation=1,
        digest_kind="funcs_zip",
        digest="sha256:" + ("a" * 64),
        protocol="1",
        status="ready",
        last_activity_at=_NOW,
        expires_at=_NOW + timedelta(seconds=120),
        idle_policy_armed=True,
        active_run_id=None,
        snapshot_ids=(),
        region="westus2",
        state_store_fingerprint="s1-" + ("b" * 52),
        quarantine_reason=None,
        tombstone_reason=None,
        created_at=_NOW,
        updated_at=_NOW,
        active_operation_id=None,
        operation_sequence=0,
    )


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


@pytest.mark.asyncio
async def test_public_sse_reconnects_after_a_lease_ended_partial_stream() -> None:
    class Content:
        def __init__(self, chunks: list[bytes]) -> None:
            self._chunks = iter(chunks)

        def __aiter__(self) -> Content:
            return self

        async def __anext__(self) -> bytes:
            try:
                return next(self._chunks)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

    class Response:
        status = 200

        def __init__(self, chunks: list[bytes]) -> None:
            self.headers: dict[str, str] = {}
            self.content = Content(chunks)

        async def __aenter__(self) -> Response:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    class Session:
        def __init__(self) -> None:
            self.requests: list[dict[str, str]] = []
            self.responses = [
                Response([b'id: 1\ndata: {"type":"session"}\n\n']),
                Response([b'id: 2\ndata: {"type":"done"}\n\n']),
            ]

        def get(self, _: str, *, headers: dict[str, str]) -> Response:
            self.requests.append(headers)
            return self.responses.pop(0)

    session = Session()
    status, events, _, first_event_at = await support.read_sse_events_with_first_event_time(
        session,  # type: ignore[arg-type]
        "https://example.test/events",
        headers={"Authorization": "******"},
        overall_timeout_seconds=1,
    )

    assert status == 200
    assert [event.sequence for event in events] == [1, 2]
    assert events[-1].payload["type"] == "done"
    assert first_event_at is not None
    assert session.requests[1]["Last-Event-ID"] == "1"


@pytest.mark.asyncio
async def test_public_sse_overall_deadline_cancels_a_blocking_response() -> None:
    class BlockingContent:
        def __aiter__(self) -> BlockingContent:
            return self

        async def __anext__(self) -> bytes:
            await __import__("asyncio").sleep(10)
            raise StopAsyncIteration

    class Response:
        status = 200

        def __init__(self) -> None:
            self.headers: dict[str, str] = {}
            self.content = BlockingContent()

        async def __aenter__(self) -> Response:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    class Session:
        def get(self, *_: object, **__: object) -> Response:
            return Response()

    started = time.perf_counter()
    with pytest.raises(AcaSmokeEnvironmentError, match="overall deadline"):
        await support.read_sse_events_with_first_event_time(
            Session(),  # type: ignore[arg-type]
            "https://example.test/events",
            headers={"Authorization": "******"},
            overall_timeout_seconds=0.01,
        )

    assert time.perf_counter() - started < 1


@pytest.mark.asyncio
async def test_public_sse_non_success_is_typed_redacted_and_never_parsed() -> None:
    class UnexpectedContent:
        def __aiter__(self) -> UnexpectedContent:
            return self

        async def __anext__(self) -> bytes:
            raise AssertionError("A non-success SSE body must not be parsed.")

    class Response:
        status = 500

        def __init__(self) -> None:
            self.headers = {"x-test": "value"}
            self.content = UnexpectedContent()

        async def __aenter__(self) -> Response:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    class Session:
        def get(self, *_: object, **__: object) -> Response:
            return Response()

    with pytest.raises(support.SseResponseStatusError) as failure:
        await support.read_sse_events_with_first_event_time(
            Session(),  # type: ignore[arg-type]
            "https://example.test/events?token=secret",
            headers={"Authorization": "Bearer top-secret"},
            overall_timeout_seconds=1,
        )

    assert failure.value.status == 500
    assert failure.value.status_classification == "server_error"
    assert "HTTP 500" in str(failure.value)
    assert "secret" not in str(failure.value)
    assert "top-secret" not in str(failure.value)
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


@pytest.mark.asyncio
async def test_typed_setup_timeout_body_is_preserved() -> None:
    class SetupTimeoutResponse:
        status = 504

        async def json(self, *, content_type: object = None) -> object:
            del content_type
            return {
                "error": "setup_deadline_exceeded",
                "retry_with": "respond-async",
            }

    assert await support._json_body(SetupTimeoutResponse()) == {  # type: ignore[arg-type]
        "error": "setup_deadline_exceeded",
        "retry_with": "respond-async",
    }
