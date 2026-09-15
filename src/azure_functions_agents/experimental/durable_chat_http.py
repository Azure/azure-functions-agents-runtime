"""Hosted shell, bootstrap, SSE, and diagnostics routes for durable chat."""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import azure.durable_functions as df
import azure.functions as func
from azurefunctions.extensions.http.fastapi import Request, Response, StreamingResponse

from .._logger import logger
from ..config import ResolvedAgent
from ..registration._auth import resolve_endpoint_auth_level
from .durable_chat_config import (
    DurableChatSettings,
    build_durable_chat_diagnostic_links,
    durable_chat_route,
)
from .durable_chat_journal import (
    DurableChatInitializationError,
    DurableChatJournalError,
    get_durable_chat_journal,
)
from .durable_chat_protocol import (
    MAX_DURABLE_CHAT_REPLAY_EVENTS,
    DurableChatAgentIdentityV1,
    DurableChatBootstrapV1,
    DurableChatDiagnosticsV1,
    DurableChatEventFrameV1,
    DurableChatEventType,
    DurableChatHttpMethod,
    DurableChatObservationBatchV1,
    DurableChatReplayDisposition,
    DurableChatReplayPageV1,
    DurableChatRouteDescriptorV1,
    DurableChatRouteName,
    DurableChatRunInitializationV1,
    DurableChatStreamFrameV1,
    DurableChatTerminalObservationV1,
)
from .durable_loop_config import DurableLoopSettings
from .durable_loop_http import (
    _ROUTE_BASE,
    _authorized_owner,
    _authorized_status,
    _json_response,
    _owner_hash,
    _status_projection,
)
from .durable_loop_protocol import DurableLoopRunStatus, SandboxExecutionProfile
from .durable_loop_registration import _normalize_durable_client_binding_annotation

_SHELL_ROUTE = "experimental/durable-chat"
_CONFIG_ROUTE = f"{_SHELL_ROUTE}/config"
_EVENTS_ROUTE = f"{_ROUTE_BASE}/{{run_id}}/events"
_DIAGNOSTICS_ROUTE = f"{_ROUTE_BASE}/{{run_id}}/diagnostics"
_EVENT_LEASE_SECONDS = 210.0
_EVENT_HEARTBEAT_SECONDS = 15.0
_EVENT_POLL_SECONDS = 1.0
_CURSOR_PATTERN = re.compile(r"^(?:0|[1-9][0-9]{0,18})$")
_ASSET_ROOT = Path(__file__).resolve().parents[1] / "public" / "durable-chat"
_ASSET_MEDIA_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
    "rendering.js": "text/javascript; charset=utf-8",
    "history.js": "text/javascript; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
}
_STATIC_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; connect-src 'self'; "
        "font-src 'self'; form-action 'self'; frame-ancestors 'none'; "
        "img-src 'self' data:; object-src 'none'; script-src 'self'; style-src 'self'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
_DATA_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def register_durable_chat_http_routes(  # noqa: PLR0915
    app: func.FunctionApp,
    *,
    resolved: ResolvedAgent,
    settings: DurableLoopSettings,
    chat_settings: DurableChatSettings,
) -> None:
    """Register hosted durable-chat surfaces under the durable-loop gate."""
    if not chat_settings.enabled:
        return
    auth = resolved.builtin_endpoints.http_auth
    auth_level = resolve_endpoint_auth_level(auth)

    async def get_shell(req: Request) -> Response:
        if _needs_shell_slash_redirect(req):
            return Response(
                status_code=308,
                headers={
                    **_STATIC_HEADERS,
                    "Location": durable_chat_route(f"/{_SHELL_ROUTE}/"),
                },
            )
        return _static_response("index.html")

    async def get_bootstrap(req: Request) -> Response:
        owner = _authorized_owner(req, auth)
        if isinstance(owner, Response):
            return _with_data_headers(owner)
        bootstrap = DurableChatBootstrapV1(
            agent=DurableChatAgentIdentityV1(
                slug=resolved.slug,
                display_name=resolved.name,
            ),
            routes=_bootstrap_routes(),
            supported_sandbox_profiles=_supported_sandbox_profiles(settings),
            default_sandbox_profile=_default_sandbox_profile(),
            foreground_streaming_available=not settings.background_model_enabled,
            sandbox_group_resource_id=chat_settings.sandbox_group_resource_id,
            integrations=chat_settings.integration_metadata(),
            history_namespace=_history_namespace(
                owner_hash=_owner_hash(owner),
                agent_slug=resolved.slug,
            ),
        )
        return _chat_model_response(bootstrap)

    async def get_events(
        req: Request,
        client: df.DurableOrchestrationClient,
    ) -> Response:
        authorized = await _authorized_status(req, client, auth)
        if isinstance(authorized, Response):
            return _with_data_headers(authorized)
        status, durable_input = authorized
        run_id = status.instance_id
        journal = get_durable_chat_journal()
        initialization = await _load_initialization(journal, run_id)
        if isinstance(initialization, Response):
            return initialization
        if not _initialization_matches_durable_input(initialization, durable_input):
            logger.warning("durable-chat initialization did not match authorized run")
            return _chat_json_response(
                {"error": "chat_initialization_unavailable"},
                status_code=503,
            )
        if _expired(initialization):
            return _chat_json_response(
                {"error": "chat_observations_expired"},
                status_code=410,
            )
        try:
            after_sequence = _after_sequence(req)
            await _reconcile_terminal_status(
                journal=journal,
                status=status,
                durable_input=durable_input,
                initialization=initialization,
            )
            initial_terminal = _is_authoritative_terminal_status(status)
            initial_page = await journal.replay(
                run_id=run_id,
                after_sequence=after_sequence,
                limit=MAX_DURABLE_CHAT_REPLAY_EVENTS,
            )
        except ValueError:
            return _chat_json_response({"error": "invalid_event_cursor"}, status_code=400)
        except DurableChatJournalError:
            return _chat_json_response(
                {"error": "chat_observations_unavailable"},
                status_code=503,
            )
        if initial_page.disposition is DurableChatReplayDisposition.CURSOR_AHEAD:
            return _chat_json_response(
                {
                    "error": "event_cursor_ahead",
                    "through_sequence": initial_page.through_sequence,
                },
                status_code=409,
            )
        return StreamingResponse(
            _stream_events(
                client=client,
                durable_input=durable_input,
                initialization=initialization,
                journal=journal,
                initial_page=initial_page,
                initial_terminal=initial_terminal,
            ),
            media_type="text/event-stream",
            headers={
                **_DATA_HEADERS,
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    async def get_diagnostics(
        req: Request,
        client: df.DurableOrchestrationClient,
    ) -> Response:
        authorized = await _authorized_status(req, client, auth)
        if isinstance(authorized, Response):
            return _with_data_headers(authorized)
        status, durable_input = authorized
        run_id = status.instance_id
        journal = get_durable_chat_journal()
        initialization = await _load_initialization(journal, run_id)
        if isinstance(initialization, Response):
            return initialization
        if not _initialization_matches_durable_input(initialization, durable_input):
            logger.warning("durable-chat initialization did not match authorized run")
            return _chat_json_response(
                {"error": "chat_initialization_unavailable"},
                status_code=503,
            )
        if _expired(initialization):
            return _chat_json_response(
                {"error": "chat_observations_expired"},
                status_code=410,
            )
        try:
            diagnostics = await journal.read_diagnostics(run_id=run_id)
        except DurableChatJournalError:
            return _chat_json_response(
                {"error": "chat_observations_unavailable"},
                status_code=503,
            )
        if diagnostics is None:
            return _chat_json_response(
                {"error": "chat_observations_not_available"},
                status_code=404,
            )
        return _chat_model_response(
            _authoritative_diagnostics(
                diagnostics=diagnostics,
                initialization=initialization,
                status=status,
            )
        )

    _register_static_route(app, "durable_chat_shell_v1", _SHELL_ROUTE, get_shell)
    for asset_name in _ASSET_MEDIA_TYPES:
        if asset_name == "index.html":
            continue
        _register_static_route(
            app,
            f"durable_chat_asset_{asset_name.replace('.', '_')}_v1",
            f"{_SHELL_ROUTE}/{asset_name}",
            _asset_handler(asset_name),
        )
    _register_route(
        app,
        "durable_chat_bootstrap_v1",
        _CONFIG_ROUTE,
        ["GET"],
        auth_level,
        get_bootstrap,
        durable_client=False,
    )
    _register_route(
        app,
        "durable_chat_events_v1",
        _EVENTS_ROUTE,
        ["GET"],
        auth_level,
        get_events,
        durable_client=True,
    )
    _register_route(
        app,
        "durable_chat_diagnostics_v1",
        _DIAGNOSTICS_ROUTE,
        ["GET"],
        auth_level,
        get_diagnostics,
        durable_client=True,
    )


def _asset_handler(asset_name: str) -> Callable[[Request], Awaitable[Response]]:
    async def serve_asset(req: Request) -> Response:
        return _static_response(asset_name)

    return serve_asset


def _register_static_route(
    app: func.FunctionApp,
    name: str,
    route: str,
    handler: Any,
) -> None:
    handler.__name__ = name
    app.route(
        route=route,
        methods=["GET"],
        auth_level=func.AuthLevel.ANONYMOUS,
    )(handler)


def _register_route(
    app: func.FunctionApp,
    name: str,
    route: str,
    methods: list[str],
    auth_level: func.AuthLevel,
    handler: Any,
    *,
    durable_client: bool,
) -> None:
    handler.__name__ = name
    decorated = (
        app.durable_client_input(client_name="client")(handler)
        if durable_client
        else handler
    )
    if durable_client:
        _normalize_durable_client_binding_annotation(
            decorated,
            client_name="client",
        )
    app.route(route=route, methods=methods, auth_level=auth_level)(decorated)


def _static_response(asset_name: str) -> Response:
    media_type = _ASSET_MEDIA_TYPES.get(asset_name)
    if media_type is None:
        return _chat_json_response({"error": "asset_not_found"}, status_code=404)
    path = _ASSET_ROOT / asset_name
    try:
        content = path.read_bytes()
    except OSError:
        logger.warning("durable-chat static asset is unavailable: asset=%s", asset_name)
        return _chat_json_response({"error": "asset_not_found"}, status_code=404)
    return Response(content=content, media_type=media_type, headers=_STATIC_HEADERS)


def _needs_shell_slash_redirect(req: Request) -> bool:
    url = getattr(req, "url", None)
    path = getattr(url, "path", None)
    return isinstance(path, str) and bool(path) and not path.endswith("/")


def _bootstrap_routes() -> tuple[DurableChatRouteDescriptorV1, ...]:
    base = durable_chat_route(f"/{_ROUTE_BASE}")
    return (
        DurableChatRouteDescriptorV1(
            name=DurableChatRouteName.START_RUN,
            method=DurableChatHttpMethod.POST,
            path_template=base,
        ),
        DurableChatRouteDescriptorV1(
            name=DurableChatRouteName.STATUS,
            method=DurableChatHttpMethod.GET,
            path_template=f"{base}/{{run_id}}",
        ),
        DurableChatRouteDescriptorV1(
            name=DurableChatRouteName.RESULT,
            method=DurableChatHttpMethod.GET,
            path_template=f"{base}/{{run_id}}/result",
        ),
        DurableChatRouteDescriptorV1(
            name=DurableChatRouteName.CANCEL,
            method=DurableChatHttpMethod.POST,
            path_template=f"{base}/{{run_id}}/cancel",
        ),
        DurableChatRouteDescriptorV1(
            name=DurableChatRouteName.HUMAN_INPUT_DETAIL,
            method=DurableChatHttpMethod.GET,
            path_template=f"{base}/{{run_id}}/input/{{request_id}}",
        ),
        DurableChatRouteDescriptorV1(
            name=DurableChatRouteName.HUMAN_INPUT_SUBMIT,
            method=DurableChatHttpMethod.POST,
            path_template=f"{base}/{{run_id}}/input/{{request_id}}",
        ),
        DurableChatRouteDescriptorV1(
            name=DurableChatRouteName.EVENTS,
            method=DurableChatHttpMethod.GET,
            path_template=durable_chat_route(f"/{_EVENTS_ROUTE}"),
        ),
        DurableChatRouteDescriptorV1(
            name=DurableChatRouteName.DIAGNOSTICS,
            method=DurableChatHttpMethod.GET,
            path_template=durable_chat_route(f"/{_DIAGNOSTICS_ROUTE}"),
        ),
    )


def _supported_sandbox_profiles(
    settings: DurableLoopSettings,
) -> tuple[SandboxExecutionProfile, ...]:
    profiles = [SandboxExecutionProfile.PER_CALL]
    if settings.retained_sandbox_enabled:
        profiles.append(SandboxExecutionProfile.RETAINED_SESSION)
    return tuple(profiles)


def _default_sandbox_profile() -> SandboxExecutionProfile:
    return SandboxExecutionProfile.PER_CALL


def _history_namespace(*, owner_hash: str, agent_slug: str) -> str:
    deployment = (
        os.environ.get("WEBSITE_SITE_NAME", "").strip()
        or os.environ.get("WEBSITE_HOSTNAME", "").strip()
        or "local"
    )
    from .durable_loop_protocol import canonical_hash

    return canonical_hash(
        {
            "agent": agent_slug,
            "deployment": deployment,
            "owner": owner_hash,
            "route": durable_chat_route(f"/{_SHELL_ROUTE}"),
            "schema_version": "1",
        }
    )


async def _load_initialization(
    journal: Any,
    run_id: str,
) -> DurableChatRunInitializationV1 | Response:
    try:
        initialization = await journal.load_run_initialization(run_id=run_id)
    except DurableChatInitializationError:
        return _chat_json_response(
            {"error": "chat_initialization_unavailable"},
            status_code=503,
        )
    if initialization is None:
        return _chat_json_response(
            {"error": "chat_observations_not_available"},
            status_code=404,
        )
    return initialization


def _initialization_matches_durable_input(
    initialization: DurableChatRunInitializationV1,
    durable_input: Any,
) -> bool:
    identity = getattr(durable_input, "identity", None)
    return (
        getattr(identity, "run_id", None) == initialization.run_id
        and getattr(identity, "session_id", None) == initialization.session_id
        and getattr(identity, "owner_hash", None) == initialization.owner_hash
        and getattr(identity, "request_id_hash", None) == initialization.request_id_hash
        and getattr(identity, "request_hash", None) == initialization.request_hash
    )


def _expired(initialization: DurableChatRunInitializationV1) -> bool:
    return datetime.now(UTC) >= initialization.expires_at.astimezone(UTC)


def _after_sequence(req: Request) -> int:
    header = req.headers.get("Last-Event-ID")
    query = _query_value(req, "after_sequence")
    if header is not None and query is not None and header != query:
        raise ValueError("event cursor sources conflict")
    raw = header if header is not None else query
    if raw is None:
        return 0
    if _CURSOR_PATTERN.fullmatch(raw) is None:
        raise ValueError("event cursor is invalid")
    return int(raw)


def _query_value(req: Request, name: str) -> str | None:
    values = getattr(req, "query_params", None)
    if values is None:
        return None
    value = values.get(name)
    return value if isinstance(value, str) else None


async def _stream_events(
    *,
    client: Any,
    durable_input: Any,
    initialization: DurableChatRunInitializationV1,
    journal: Any,
    initial_page: DurableChatReplayPageV1,
    initial_terminal: bool,
) -> AsyncIterator[str]:
    page = initial_page
    cursor = page.requested_after_sequence
    deadline = time.monotonic() + _EVENT_LEASE_SECONDS
    next_heartbeat = time.monotonic() + _EVENT_HEARTBEAT_SECONDS
    while True:
        emitted = False
        for frame in _render_replay_page(page):
            emitted = True
            if isinstance(frame, DurableChatEventFrameV1):
                cursor = frame.sequence
            else:
                cursor = frame.projection.through_sequence
            yield _render_frame(frame)
        if initial_terminal or _is_terminal_status_page(page):
            return
        now = time.monotonic()
        if now >= deadline:
            return
        if not emitted and now >= next_heartbeat:
            next_heartbeat = now + _EVENT_HEARTBEAT_SECONDS
            yield ": heartbeat\n\n"
        await asyncio.sleep(min(_EVENT_POLL_SECONDS, max(0.001, deadline - now)))
        try:
            status = await client.get_status(
                durable_input.identity.run_id,
                show_input=False,
            )
            if status is not None:
                await _reconcile_terminal_status(
                    journal=journal,
                    status=status,
                    durable_input=durable_input,
                    initialization=initialization,
                )
                initial_terminal = _is_authoritative_terminal_status(status)
            page = await journal.replay(
                run_id=durable_input.identity.run_id,
                after_sequence=cursor,
                limit=MAX_DURABLE_CHAT_REPLAY_EVENTS,
            )
        except (DurableChatJournalError, DurableChatInitializationError, ValueError):
            return


def _render_replay_page(
    page: DurableChatReplayPageV1,
) -> tuple[DurableChatStreamFrameV1, ...]:
    frames: list[DurableChatStreamFrameV1] = []
    if page.snapshot is not None:
        frames.append(page.snapshot)
    frames.extend(page.events)
    return tuple(frames)


def _render_frame(frame: DurableChatStreamFrameV1) -> str:
    if isinstance(frame, DurableChatEventFrameV1):
        event_name = frame.event.event_type.value
        sequence = frame.sequence
    else:
        event_name = DurableChatEventType.SNAPSHOT.value
        sequence = frame.projection.through_sequence
    return (
        f"id: {sequence}\n"
        f"event: {event_name}\n"
        f"data: {frame.model_dump_json()}\n\n"
    )


def _is_terminal_status_page(page: DurableChatReplayPageV1) -> bool:
    frames = _render_replay_page(page)
    return any(
        isinstance(frame, DurableChatEventFrameV1)
        and isinstance(frame.event, DurableChatTerminalObservationV1)
        for frame in frames
    )


async def _reconcile_terminal_status(
    *,
    journal: Any,
    status: Any,
    durable_input: Any,
    initialization: DurableChatRunInitializationV1,
) -> None:
    projection = _status_projection(status)
    raw_status = projection.get("status")
    if not isinstance(raw_status, str):
        return
    try:
        run_status = DurableLoopRunStatus(raw_status)
    except ValueError:
        return
    if run_status not in {
        DurableLoopRunStatus.COMPLETED,
        DurableLoopRunStatus.FAILED,
        DurableLoopRunStatus.CANCELLED,
    }:
        return
    output = status.output if isinstance(status.output, Mapping) else {}
    result_available = (
        run_status is DurableLoopRunStatus.COMPLETED
        and output.get("status") == DurableLoopRunStatus.COMPLETED.value
    )
    if run_status is DurableLoopRunStatus.COMPLETED and not result_available:
        return
    terminal = DurableChatTerminalObservationV1(
        run_id=durable_input.identity.run_id,
        session_id=durable_input.identity.session_id,
        observed_at=datetime.now(UTC),
        status=run_status,
        result_available=result_available,
        committed_generation=initialization.committed_generation,
        error_code=(
            _safe_error_code(projection.get("error"))
            if run_status is not DurableLoopRunStatus.COMPLETED
            else None
        ),
    )
    try:
        await journal.publish(
            run_id=terminal.run_id,
            expected_published_revision=0,
            batch=terminal_batch(terminal),
            deadline=datetime.now(UTC).replace(microsecond=0)
            + _event_publication_delta(),
        )
    except Exception:
        logger.warning("durable-chat terminal reconciliation was unavailable")


def _is_authoritative_terminal_status(status: Any) -> bool:
    projection = _status_projection(status)
    raw_status = projection.get("status")
    if not isinstance(raw_status, str):
        return False
    try:
        run_status = DurableLoopRunStatus(raw_status)
    except ValueError:
        return False
    if run_status is DurableLoopRunStatus.COMPLETED:
        output = status.output if isinstance(status.output, Mapping) else {}
        return output.get("status") == DurableLoopRunStatus.COMPLETED.value
    return run_status in {
        DurableLoopRunStatus.FAILED,
        DurableLoopRunStatus.CANCELLED,
    }


def terminal_batch(
    terminal: DurableChatTerminalObservationV1,
) -> DurableChatObservationBatchV1:
    return DurableChatObservationBatchV1(
        run_id=terminal.run_id,
        observations=(terminal,),
    )


def _event_publication_delta() -> timedelta:
    return timedelta(seconds=10)


def _safe_error_code(value: object) -> str | None:
    if not isinstance(value, str) or re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", value) is None:
        return None
    return value


def _authoritative_diagnostics(
    *,
    diagnostics: DurableChatDiagnosticsV1,
    initialization: DurableChatRunInitializationV1,
    status: Any,
) -> DurableChatDiagnosticsV1:
    projection = _status_projection(status)
    raw_status = projection.get("status")
    try:
        run_status = (
            DurableLoopRunStatus(raw_status)
            if isinstance(raw_status, str)
            else diagnostics.status
        )
    except ValueError:
        run_status = diagnostics.status
    updated_at = min(datetime.now(UTC), initialization.expires_at.astimezone(UTC))
    return diagnostics.model_copy(
        update={
            "status": run_status,
            "updated_at": max(diagnostics.created_at, updated_at),
            "links": build_durable_chat_diagnostic_links(
                initialization.diagnostics,
                run_id=initialization.run_id,
            ),
        }
    )


def _chat_model_response(model: Any, *, status_code: int = 200) -> Response:
    return _chat_json_response(model.model_dump(mode="json"), status_code=status_code)


def _chat_json_response(
    body: Mapping[str, object],
    *,
    status_code: int = 200,
) -> Response:
    return _json_response(
        body,
        status_code=status_code,
        headers=_DATA_HEADERS,
    )


def _with_data_headers(response: Response) -> Response:
    response.headers.update(_DATA_HEADERS)
    return response
