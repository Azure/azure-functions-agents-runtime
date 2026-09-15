from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from azurefunctions.extensions.http.fastapi import Request

from azure_functions_agents.config.schema import EndpointAuthConfig, EntraAuthConfig
from azure_functions_agents.experimental import durable_chat_http, durable_loop_http
from azure_functions_agents.experimental.durable_chat_config import (
    _HOST_HTTP_ROUTE_PREFIX_ENV,
    DurableChatSettings,
)
from azure_functions_agents.experimental.durable_chat_http import (
    _after_sequence,
    _initialization_matches_durable_input,
    _render_frame,
    _static_response,
    register_durable_chat_http_routes,
)
from azure_functions_agents.experimental.durable_chat_journal import (
    reset_durable_chat_journal_factory,
    set_durable_chat_journal_factory,
)
from azure_functions_agents.experimental.durable_chat_protocol import (
    DurableChatDiagnosticsV1,
    DurableChatEventFrameV1,
    DurableChatFrozenDiagnosticsV1,
    DurableChatObservationHealthV1,
    DurableChatProgressObservationV1,
    DurableChatRunInitializationV1,
)
from azure_functions_agents.experimental.durable_loop_config import (
    DURABLE_LOOP_ENABLED_ENV,
    DurableLoopSettings,
)
from azure_functions_agents.experimental.durable_loop_http import (
    _same_origin_mutation_failure,
    _start_payload,
)
from azure_functions_agents.experimental.durable_loop_protocol import (
    ContentRefV1,
    DurableChatModelMode,
    DurableChatRunOptionsV1,
    DurableLoopRunStatus,
    canonical_hash,
)
from azure_functions_agents.experimental.hybrid_config import HYBRID_SANDBOX_GROUP_ENV


class _App:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}
        self.routes: dict[str, dict[str, object]] = {}

    def durable_client_input(self, *, client_name: str):
        assert client_name == "client"
        return lambda handler: handler

    def route(self, **kwargs: object):
        def decorator(handler: Any) -> Any:
            self.handlers[handler.__name__] = handler
            self.routes[handler.__name__] = kwargs
            return handler

        return decorator


class _Request:
    def __init__(
        self,
        *,
        body: object | None = None,
        headers: dict[str, str] | None = None,
        path_params: dict[str, str] | None = None,
        query_params: dict[str, str] | None = None,
        path: str | None = None,
    ) -> None:
        self._body = body
        self.headers = headers or {}
        self.path_params = path_params or {}
        self.query_params = query_params or {}
        self.url = SimpleNamespace(path=path) if path is not None else None

    async def json(self) -> object:
        return self._body


class _Client:
    def __init__(self) -> None:
        self.status_calls = 0

    async def get_status(self, *_args: object, **_kwargs: object) -> None:
        self.status_calls += 1
        return None


_SANDBOX_GROUP_RESOURCE_ID = (
    "/subscriptions/00000000-0000-0000-0000-000000000000/"
    "resourceGroups/demo/providers/Microsoft.App/sandboxGroups/demo-group"
)
_CHANGED_SANDBOX_GROUP_RESOURCE_ID = (
    "/subscriptions/00000000-0000-0000-0000-000000000000/"
    "resourceGroups/demo/providers/Microsoft.App/sandboxGroups/changed-group"
)


def _resolved(*, auth: EndpointAuthConfig) -> SimpleNamespace:
    return SimpleNamespace(
        builtin_endpoints=SimpleNamespace(http_auth=auth),
        name="Durable assistant",
        slug="main",
    )


def _settings(
    *,
    sandbox_group_resource_id: str | None = None,
) -> DurableChatSettings:
    environment = {DURABLE_LOOP_ENABLED_ENV: "true"}
    if sandbox_group_resource_id is not None:
        environment[HYBRID_SANDBOX_GROUP_ENV] = sandbox_group_resource_id
    return DurableChatSettings.from_environment(
        environment,
        observability_enabled=False,
    )


def _reference(name: str) -> ContentRefV1:
    return ContentRefV1(
        object_id=name,
        sha256="a" * 64,
        byte_length=0,
        media_type="application/json",
        encryption_version="v1",
        retention_class="run",
    )


def _initialization() -> DurableChatRunInitializationV1:
    now = datetime.now(UTC).replace(microsecond=0)
    return DurableChatRunInitializationV1(
        run_id="run-1",
        session_id="session-1",
        owner_hash="a" * 64,
        request_id_hash=canonical_hash({"request_id": "request-1"}),
        request_hash=canonical_hash(
            {
                "prompt": "hello",
                "request_id": "request-1",
                "ui": {"schema_version": "1", "stream_response": True},
            }
        ),
        plan_ref=_reference("plan"),
        input_ref=_reference("input"),
        expires_at=now + timedelta(hours=1),
        committed_generation=2,
        ui=DurableChatRunOptionsV1(
            stream_response=True,
            model_mode=DurableChatModelMode.FOREGROUND,
        ),
        created_at=now,
    )


@pytest.mark.asyncio
async def test_bootstrap_uses_custom_prefix_and_does_not_disclose_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_HOST_HTTP_ROUTE_PREFIX_ENV, "private-api")
    app = _App()
    register_durable_chat_http_routes(
        app,  # type: ignore[arg-type]
        resolved=_resolved(auth=EndpointAuthConfig()),
        settings=DurableLoopSettings(),
        chat_settings=_settings(),
    )

    assert app.routes["durable_chat_shell_v1"]["route"] == "experimental/durable-chat"
    assert app.routes["durable_chat_asset_styles_css_v1"]["route"] == (
        "experimental/durable-chat/styles.css"
    )
    assert {
        route["route"]
        for name, route in app.routes.items()
        if name.startswith("durable_chat_asset_")
    } == {
        "experimental/durable-chat/styles.css",
        "experimental/durable-chat/rendering.js",
        "experimental/durable-chat/history.js",
        "experimental/durable-chat/app.js",
    }
    assert all("{asset_name}" not in route["route"] for route in app.routes.values())
    response = await app.handlers["durable_chat_bootstrap_v1"](_Request())
    body = json.loads(response.body)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert all(route["path_template"].startswith("/private-api/") for route in body["routes"])
    assert "code" not in json.dumps(body).casefold()
    assert len(body["history_namespace"]) == 64
    assert body["sandbox_group_resource_id"] is None


@pytest.mark.asyncio
async def test_anonymous_bootstrap_has_a_distinct_shared_history_namespace() -> None:
    namespaces = {}
    for mode in ("anonymous", "function", "admin"):
        app = _App()
        register_durable_chat_http_routes(
            app,  # type: ignore[arg-type]
            resolved=_resolved(auth=EndpointAuthConfig(mode=mode)),
            settings=DurableLoopSettings(),
            chat_settings=_settings(),
        )
        response = await app.handlers["durable_chat_bootstrap_v1"](_Request())

        assert response.status_code == 200
        assert str(app.routes["durable_chat_bootstrap_v1"]["auth_level"]).lower() == mode
        assert response.headers["cache-control"] == "no-store"
        namespaces[mode] = json.loads(response.body)["history_namespace"]
    assert namespaces["anonymous"] != namespaces["function"]
    assert namespaces["function"] == namespaces["admin"]


@pytest.mark.asyncio
async def test_bootstrap_exposes_the_validated_configured_sandbox_group() -> None:
    app = _App()
    register_durable_chat_http_routes(
        app,  # type: ignore[arg-type]
        resolved=_resolved(auth=EndpointAuthConfig()),
        settings=DurableLoopSettings(),
        chat_settings=_settings(
            sandbox_group_resource_id=_SANDBOX_GROUP_RESOURCE_ID,
        ),
    )

    response = await app.handlers["durable_chat_bootstrap_v1"](_Request())

    assert response.status_code == 200
    assert (
        json.loads(response.body)["sandbox_group_resource_id"]
        == _SANDBOX_GROUP_RESOURCE_ID
    )


@pytest.mark.asyncio
async def test_diagnostics_return_the_frozen_configured_sandbox_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen_group = _SANDBOX_GROUP_RESOURCE_ID
    initialization = _initialization()
    initialization = initialization.model_copy(
        update={
            "diagnostics": DurableChatFrozenDiagnosticsV1(
                sandbox_group_resource_id=frozen_group,
                durable_task_unavailable_reason="DTS is unavailable.",
                application_insights_unavailable_reason=(
                    "Application Insights is unavailable."
                ),
                request_started_at=initialization.created_at,
                request_ends_at=initialization.expires_at,
            )
        }
    )
    diagnostics = DurableChatDiagnosticsV1(
        run_id=initialization.run_id,
        session_id=initialization.session_id,
        model_mode=initialization.ui.model_mode,
        status=DurableLoopRunStatus.PENDING,
        created_at=initialization.created_at,
        updated_at=initialization.created_at,
        expires_at=initialization.expires_at,
        committed_generation=initialization.committed_generation,
        configured_sandbox_group_resource_id=frozen_group,
        observation_health=DurableChatObservationHealthV1(),
    )
    app = _App()
    register_durable_chat_http_routes(
        app,  # type: ignore[arg-type]
        resolved=_resolved(auth=EndpointAuthConfig()),
        settings=DurableLoopSettings(),
        chat_settings=_settings(
            sandbox_group_resource_id=_CHANGED_SANDBOX_GROUP_RESOURCE_ID,
        ),
    )

    async def authorized(*_args: object) -> tuple[object, object]:
        return (
            SimpleNamespace(
                instance_id=initialization.run_id,
                custom_status={},
                output={},
                runtime_status="Pending",
            ),
            SimpleNamespace(
                identity=SimpleNamespace(
                    run_id=initialization.run_id,
                    session_id=initialization.session_id,
                    owner_hash=initialization.owner_hash,
                    request_id_hash=initialization.request_id_hash,
                    request_hash=initialization.request_hash,
                )
            ),
        )

    class Journal:
        async def load_run_initialization(
            self,
            *,
            run_id: str,
        ) -> DurableChatRunInitializationV1:
            assert run_id == initialization.run_id
            return initialization

        async def read_diagnostics(
            self,
            *,
            run_id: str,
        ) -> DurableChatDiagnosticsV1:
            assert run_id == initialization.run_id
            return diagnostics

    monkeypatch.setattr(durable_chat_http, "_authorized_status", authorized)
    monkeypatch.setattr(durable_chat_http, "get_durable_chat_journal", Journal)

    response = await app.handlers["durable_chat_diagnostics_v1"](
        _Request(path_params={"run_id": initialization.run_id}),
        _Client(),
    )

    assert response.status_code == 200
    assert (
        json.loads(response.body)["configured_sandbox_group_resource_id"]
        == frozen_group
    )


@pytest.mark.asyncio
async def test_shell_redirects_to_a_trailing_slash_for_relative_assets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_HOST_HTTP_ROUTE_PREFIX_ENV, "private-api")
    app = _App()
    register_durable_chat_http_routes(
        app,  # type: ignore[arg-type]
        resolved=_resolved(auth=EndpointAuthConfig()),
        settings=DurableLoopSettings(),
        chat_settings=_settings(),
    )

    response = await app.handlers["durable_chat_shell_v1"](
        _Request(path="/private-api/experimental/durable-chat")
    )

    assert response.status_code == 308
    assert response.headers["location"] == "/private-api/experimental/durable-chat/"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_events_authorize_before_accessing_the_journal() -> None:
    app = _App()
    register_durable_chat_http_routes(
        app,  # type: ignore[arg-type]
        resolved=_resolved(
            auth=EndpointAuthConfig(mode="entra", entra=EntraAuthConfig())
        ),
        settings=DurableLoopSettings(),
        chat_settings=_settings(),
    )
    factory_called = False

    def fail_if_accessed() -> Any:
        nonlocal factory_called
        factory_called = True
        raise AssertionError("journal must not be accessed for an unauthorized request")

    set_durable_chat_journal_factory(fail_if_accessed)
    try:
        response = await app.handlers["durable_chat_events_v1"](
            _Request(path_params={"run_id": "run-1"}),
            _Client(),
        )
    finally:
        reset_durable_chat_journal_factory()

    assert response.status_code == 401
    assert not factory_called


@pytest.mark.asyncio
async def test_events_access_the_journal_only_after_status_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _App()
    register_durable_chat_http_routes(
        app,  # type: ignore[arg-type]
        resolved=_resolved(auth=EndpointAuthConfig()),
        settings=DurableLoopSettings(),
        chat_settings=_settings(),
    )
    calls: list[str] = []

    async def authorized(*_args: object) -> tuple[object, object]:
        calls.append("authorized")
        return SimpleNamespace(instance_id="run-1"), SimpleNamespace()

    class EmptyJournal:
        async def load_run_initialization(
            self,
            *,
            run_id: str,
        ) -> None:
            assert run_id == "run-1"
            calls.append("journal")
            return None

    monkeypatch.setattr(durable_chat_http, "_authorized_status", authorized)
    monkeypatch.setattr(durable_chat_http, "get_durable_chat_journal", EmptyJournal)

    response = await app.handlers["durable_chat_events_v1"](
        _Request(path_params={"run_id": "run-1"}),
        _Client(),
    )

    assert calls == ["authorized", "journal"]
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_data_routes_reject_an_initialization_for_another_authorized_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _App()
    register_durable_chat_http_routes(
        app,  # type: ignore[arg-type]
        resolved=_resolved(auth=EndpointAuthConfig()),
        settings=DurableLoopSettings(),
        chat_settings=_settings(),
    )
    initialization = _initialization()
    calls: list[str] = []

    async def authorized(*_args: object) -> tuple[object, object]:
        calls.append("authorized")
        return (
            SimpleNamespace(instance_id="run-1"),
            SimpleNamespace(
                identity=SimpleNamespace(
                    run_id="run-1",
                    session_id="other-session",
                    owner_hash=initialization.owner_hash,
                    request_id_hash=initialization.request_id_hash,
                    request_hash=initialization.request_hash,
                )
            ),
        )

    class MismatchedJournal:
        async def load_run_initialization(
            self,
            *,
            run_id: str,
        ) -> DurableChatRunInitializationV1:
            assert run_id == "run-1"
            calls.append("initialization")
            return initialization

        async def replay(self, **_kwargs: object) -> object:
            raise AssertionError("mismatched initialization must not be replayed")

        async def read_diagnostics(self, **_kwargs: object) -> object:
            raise AssertionError("mismatched initialization must not be read")

    monkeypatch.setattr(durable_chat_http, "_authorized_status", authorized)
    monkeypatch.setattr(
        durable_chat_http,
        "get_durable_chat_journal",
        MismatchedJournal,
    )

    response = await app.handlers["durable_chat_events_v1"](
        _Request(path_params={"run_id": "run-1"}),
        _Client(),
    )

    diagnostics = await app.handlers["durable_chat_diagnostics_v1"](
        _Request(path_params={"run_id": "run-1"}),
        _Client(),
    )

    assert calls == ["authorized", "initialization", "authorized", "initialization"]
    assert response.status_code == 503
    assert diagnostics.status_code == 503


def test_event_cursor_requires_a_nonnegative_integer_and_matching_sources() -> None:
    assert _after_sequence(_Request(headers={"Last-Event-ID": "12"})) == 12
    assert _after_sequence(_Request(query_params={"after_sequence": "0"})) == 0

    with pytest.raises(ValueError):
        _after_sequence(_Request(headers={"Last-Event-ID": "-1"}))
    with pytest.raises(ValueError):
        _after_sequence(
            _Request(
                headers={"Last-Event-ID": "1"},
                query_params={"after_sequence": "2"},
            )
        )


def test_chat_initialization_must_match_the_authorized_run_identity() -> None:
    initialization = _initialization()
    matching_input = SimpleNamespace(
        identity=SimpleNamespace(
            run_id="run-1",
            session_id="session-1",
            owner_hash="a" * 64,
            request_id_hash=initialization.request_id_hash,
            request_hash=initialization.request_hash,
        )
    )
    mismatched_input = SimpleNamespace(
        identity=SimpleNamespace(
            **{
                **matching_input.identity.__dict__,
                "session_id": "other-session",
            }
        )
    )

    assert _initialization_matches_durable_input(initialization, matching_input)
    assert not _initialization_matches_durable_input(initialization, mismatched_input)


def test_sse_frame_uses_sequence_id_and_protocol_event_name() -> None:
    event = DurableChatProgressObservationV1(
        run_id="run-1",
        session_id="session-1",
        observed_at=datetime(2026, 9, 14, tzinfo=UTC),
        progress={
            "status": DurableLoopRunStatus.RUNNING,
            "phase": "model",
            "model_steps": 1,
            "tool_calls": 0,
            "human_waits": 0,
            "step_index": 1,
            "updated_at": datetime(2026, 9, 14, tzinfo=UTC),
        },
    )

    rendered = _render_frame(DurableChatEventFrameV1(sequence=7, event=event))

    assert rendered.startswith("id: 7\nevent: progress\ndata: ")
    assert rendered.endswith("\n\n")


def test_static_assets_are_an_explicit_allowlist() -> None:
    response = _static_response("not-an-asset.js")
    shell = _static_response("index.html")

    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert shell.status_code == 200
    assert "default-src 'self'" in shell.headers["content-security-policy"]
    assert shell.headers["referrer-policy"] == "no-referrer"


def test_chat_routes_are_not_registered_when_the_durable_loop_gate_is_off() -> None:
    app = _App()
    disabled = DurableChatSettings.from_environment({}, observability_enabled=False)

    register_durable_chat_http_routes(
        app,  # type: ignore[arg-type]
        resolved=_resolved(auth=EndpointAuthConfig()),
        settings=DurableLoopSettings(),
        chat_settings=disabled,
    )

    assert app.handlers == {}


def test_chat_routes_register_with_only_the_existing_durable_loop_gate() -> None:
    app = _App()
    settings = DurableChatSettings.from_environment(
        {DURABLE_LOOP_ENABLED_ENV: "true"},
        observability_enabled=False,
    )

    register_durable_chat_http_routes(
        app,  # type: ignore[arg-type]
        resolved=_resolved(auth=EndpointAuthConfig()),
        settings=DurableLoopSettings(),
        chat_settings=settings,
    )

    assert "durable_chat_shell_v1" in app.handlers
    assert "durable_chat_bootstrap_v1" in app.handlers


def test_ui_start_payload_is_gated_and_legacy_payload_is_unmodified() -> None:
    legacy = {"prompt": "hello", "request_id": "request-1"}
    requested = {
        **legacy,
        "ui": {"schema_version": "1", "stream_response": True},
    }

    assert _start_payload(legacy) is legacy
    with pytest.raises(ValueError, match="not enabled"):
        _start_payload(requested)
    assert _start_payload(requested, chat_enabled=True) == requested


def test_static_shell_references_the_literal_sibling_asset_routes() -> None:
    response = _static_response("index.html")
    body = bytes(response.body).decode("utf-8")

    assert 'href="./styles.css"' in body
    assert 'src="./app.js"' in body
    assert "./assets/" not in body


def test_mutating_browser_requests_require_the_same_origin() -> None:
    same_origin = _same_origin_mutation_failure(
        _Request(
            headers={
                "Host": "agent.example.test",
                "Origin": "https://agent.example.test",
                "X-Forwarded-Proto": "https",
            }
        )
    )
    cross_origin = _same_origin_mutation_failure(
        _Request(
            headers={
                "Host": "agent.example.test",
                "Origin": "https://attacker.example.test",
                "X-Forwarded-Proto": "https",
            }
        )
    )
    default_https_port = _same_origin_mutation_failure(
        _Request(
            headers={
                "Host": "agent.example.test:443",
                "Origin": "https://agent.example.test",
                "X-Forwarded-Proto": "https",
            }
        )
    )

    assert same_origin is None
    assert default_https_port is None
    assert cross_origin is not None
    assert cross_origin.status_code == 403
    assert _same_origin_mutation_failure(_Request()) is None


@pytest.mark.parametrize(
    ("forwarded_host", "origin", "accepted"),
    [
        ("localhost:7071", "http://localhost:7071", True),
        ("localhost:80", "http://localhost", True),
        ("localhost:7071", "http://127.0.0.1:9091", False),
        ("localhost:7071", "http://attacker.example", False),
        ("localhost:7071", "https://localhost:7071", False),
        ("localhost:7071", "http://localhost:7072", False),
        ("localhost:7071", "null", False),
        ("localhost:7071,attacker.example", "http://localhost:7071", False),
        ("localhost:7071/path", "http://localhost:7071", False),
        ("localhost:7071?query", "http://localhost:7071", False),
        ("localhost:7071#fragment", "http://localhost:7071", False),
        ("user@localhost:7071", "http://localhost:7071", False),
        ("", "http://127.0.0.1:9091", False),
    ],
)
def test_origin_guard_uses_the_functions_proxy_origin(
    forwarded_host: str, origin: str, accepted: bool
) -> None:
    headers = {
        "host": "127.0.0.1:9091",
        "origin": origin,
        "x-forwarded-host": forwarded_host,
        "x-forwarded-proto": "http",
    }
    request = Request(
        {
            "type": "http",
            "scheme": "http",
            "path": "/",
            "headers": [
                (name.encode("ascii"), value.encode("ascii")) for name, value in headers.items()
            ],
        }
    )

    failure = _same_origin_mutation_failure(request)

    assert (failure is None) is accepted
    if failure is not None:
        assert failure.status_code == 403


@pytest.mark.asyncio
async def test_lost_start_recovery_uses_a_known_initialization_without_revalidating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialization = _initialization()
    sentinel = object()

    class WinnerJournal:
        async def load_run_initialization(
            self,
            *,
            run_id: str,
        ) -> DurableChatRunInitializationV1 | None:
            assert run_id == initialization.run_id
            return initialization

        async def create_run_initialization_once(
            self,
            *,
            initialization: DurableChatRunInitializationV1,
        ) -> DurableChatRunInitializationV1:
            raise AssertionError(f"unexpected new initialization: {initialization.run_id}")

    async def reject_live_metadata(**_kwargs: object) -> object:
        raise AssertionError("known winner must not be revalidated against live settings")

    async def load_winner(
        received: DurableChatRunInitializationV1,
    ) -> object:
        assert received is initialization
        return sentinel

    monkeypatch.setattr(durable_loop_http, "get_durable_chat_journal", WinnerJournal)
    monkeypatch.setattr(durable_loop_http, "_run_metadata", reject_live_metadata)
    monkeypatch.setattr(durable_loop_http, "_load_initialized_chat_input", load_winner)

    result = await durable_loop_http._recover_or_initialize_chat_run(
        admission={},
        client=object(),
        resolved=SimpleNamespace(),
        settings=DurableLoopSettings(),
        chat_settings=_settings(),
        payload={
            "prompt": "hello",
            "request_id": "request-1",
            "ui": {"schema_version": "1", "stream_response": True},
        },
        owner_hash="a" * 64,
        session_id="session-1",
        request_id="request-1",
        run_id="run-1",
    )

    assert result is sentinel


@pytest.mark.asyncio
async def test_expired_chat_recovery_reclaims_only_the_matching_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    payload = {
        "prompt": "hello",
        "request_id": "request-1",
        "ui": {"schema_version": "1", "stream_response": True},
    }
    calls: list[tuple[object, str, dict[str, object]]] = []

    class EmptyJournal:
        async def load_run_initialization(
            self,
            *,
            run_id: str,
        ) -> None:
            assert run_id == "run-1"
            return None

    async def reclaim(
        client: object,
        name: str,
        request: dict[str, object],
    ) -> dict[str, str]:
        calls.append((client, name, request))
        return {"disposition": "reclaimed"}

    monkeypatch.setattr(durable_loop_http, "get_durable_chat_journal", EmptyJournal)
    monkeypatch.setattr(durable_loop_http, "_run_short_orchestration", reclaim)
    client = object()
    result = await durable_loop_http._recover_or_initialize_chat_run(
        admission={
            "admitted_at": (now - timedelta(seconds=2)).isoformat(),
            "expires_at": (now - timedelta(seconds=1)).isoformat(),
        },
        client=client,
        resolved=SimpleNamespace(),
        settings=DurableLoopSettings(),
        chat_settings=_settings(),
        payload=payload,
        owner_hash="a" * 64,
        session_id="session-1",
        request_id="request-1",
        run_id="run-1",
    )

    assert result.status_code == 410
    assert len(calls) == 1
    received_client, name, request = calls[0]
    assert received_client is client
    assert name == "durable_agent_control_v1"
    assert isinstance(request["now"], str)
    assert datetime.fromisoformat(request["now"]).tzinfo is not None
    assert {key: value for key, value in request.items() if key != "now"} == {
        "operation": "reclaim_expired_chat_admission",
        "request_hash": canonical_hash(payload),
        "request_id_hash": canonical_hash({"request_id": "request-1"}),
        "run_id": "run-1",
        "session_entity_key": canonical_hash(
            {"owner_hash": "a" * 64, "session_id": "session-1"}
        ),
    }


@pytest.mark.asyncio
async def test_ui_start_sends_receipt_metadata_and_recovers_a_missing_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _App()
    admission_payloads: list[dict[str, object]] = []
    starts: list[tuple[str, str, object]] = []

    class Client:
        async def get_status(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def start_new(
            self,
            name: str,
            *,
            instance_id: str,
            client_input: object,
        ) -> None:
            starts.append((name, instance_id, client_input))

    class Input:
        identity = SimpleNamespace(
            orchestration_version="durable_agent_turn_orchestrator_v2"
        )

        def model_dump(self, *, mode: str) -> dict[str, object]:
            assert mode == "json"
            return {"run": "input"}

    async def admit(
        _client: object,
        _name: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        admission_payloads.append(payload)
        return {
            "admitted_at": datetime.now(UTC).isoformat(),
            "committed_context_ref": None,
            "committed_generation": 2,
            "disposition": "replayed",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "lifecycle": "running",
            "run_id": "run-1",
        }

    async def recover(**kwargs: object) -> Input:
        assert kwargs["run_id"] == "run-1"
        return Input()

    monkeypatch.setattr(
        durable_loop_http,
        "configure_durable_loop_execution_binding",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(durable_loop_http, "_run_short_orchestration", admit)
    monkeypatch.setattr(durable_loop_http, "_recover_or_initialize_chat_run", recover)
    durable_loop_http.register_durable_loop_http_routes(
        app,  # type: ignore[arg-type]
        resolved=SimpleNamespace(
            builtin_endpoints=SimpleNamespace(http_auth=EndpointAuthConfig()),
            enabled_mcp_names=(),
            tools_disabled=False,
        ),
        settings=DurableLoopSettings(),
        chat_settings=_settings(),
    )

    response = await app.handlers["durable_agent_run_start_v1"](
        _Request(
            body={
                "prompt": "hello",
                "request_id": "request-1",
                "ui": {"schema_version": "1", "stream_response": True},
            },
            headers={"x-ms-session-id": "session-1"},
        ),
        Client(),
    )

    assert admission_payloads[0]["chat_ui"] is True
    assert admission_payloads[0]["request_hash"] == canonical_hash(
        {
            "prompt": "hello",
            "request_id": "request-1",
            "ui": {"schema_version": "1", "stream_response": True},
        }
    )
    assert starts == [
        (
            "durable_agent_turn_orchestrator_v2",
            "run-1",
            {"run": "input"},
        )
    ]
    assert json.loads(response.body)["run_id"] == "run-1"
