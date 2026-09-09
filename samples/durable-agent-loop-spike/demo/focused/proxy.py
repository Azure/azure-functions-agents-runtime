from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import socket
import ssl
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

MAX_REQUEST_BYTES = 16 * 1024
MAX_UPSTREAM_RESPONSE_BYTES = 256 * 1024
MAX_LOCAL_RESPONSE_BYTES = 512 * 1024
MAX_PROMPT_BYTES = 12 * 1024
POLLABLE_STATUS_FIELDS = (
    "status",
    "phase",
    "model_steps",
    "tool_calls",
    "human_waits",
    "step_index",
)
SANDBOX_FIELDS = (
    "sandbox_instance_alias",
    "generation",
    "state",
    "workspace_checkpoint_present",
)
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})
SCENARIOS = frozenset({"normal", "model_apim_429_once"})
ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
HANDLE = re.compile(r"[A-Za-z0-9_-]{16,96}")
STATIC_ROOT = Path(__file__).resolve().parent
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
}
ALIAS_WORDS = (
    "AMBER",
    "BIRCH",
    "CEDAR",
    "CORAL",
    "DELTA",
    "EMBER",
    "FROST",
    "HARBOR",
    "IVORY",
    "NORTH",
    "ONYX",
    "PINE",
    "RIVER",
    "SOLAR",
    "TIDAL",
    "WEST",
)


class ProxyError(Exception):
    def __init__(self, status: HTTPStatus, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class UpstreamResponse:
    status: int
    document: dict[str, object]


@dataclass(frozen=True)
class DemoConfig:
    function_url: str
    function_key: str
    dts_dashboard_url: str

    @classmethod
    def from_env(cls) -> DemoConfig:
        function_url = _sanitize_url(
            os.environ.get("DURABLE_LOOP_FUNCTION_URL", ""),
            name="DURABLE_LOOP_FUNCTION_URL",
            https_only=False,
        )
        function_key = os.environ.get("DURABLE_LOOP_FUNCTION_KEY", "")
        if not function_key:
            raise RuntimeError("DURABLE_LOOP_FUNCTION_KEY is required")
        dts_url = _sanitize_dts_url(
            os.environ.get("DTS_TASK_HUB_DASHBOARD_URL", ""),
        )
        return cls(
            function_url=function_url,
            function_key=function_key,
            dts_dashboard_url=dts_url,
        )


@dataclass
class SessionRecord:
    handle: str
    alias: str
    raw_id: str


@dataclass
class RunRecord:
    handle: str
    alias: str
    raw_id: str
    session_handle: str
    human_requests: dict[str, str] = field(default_factory=dict)


class AliasStore:
    def __init__(
        self,
        *,
        token_factory: Callable[[], str] | None = None,
        alias_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(24))
        self._alias_factory = alias_factory or self._random_alias
        self._sessions_by_raw: dict[str, SessionRecord] = {}
        self._sessions_by_handle: OrderedDict[str, SessionRecord] = OrderedDict()
        self._runs_by_handle: dict[str, RunRecord] = {}
        self._aliases: set[str] = set()

    def remember(
        self,
        *,
        raw_run_id: str,
        raw_session_id: str,
    ) -> tuple[RunRecord, SessionRecord]:
        session = self._sessions_by_raw.get(raw_session_id)
        if session is None:
            session = SessionRecord(
                handle=self._new_handle("session"),
                alias=self._new_alias("SESSION"),
                raw_id=raw_session_id,
            )
            self._sessions_by_raw[raw_session_id] = session
            self._sessions_by_handle[session.handle] = session
        else:
            self._sessions_by_handle.move_to_end(session.handle)
        run = RunRecord(
            handle=self._new_handle("run"),
            alias=self._new_alias("RUN"),
            raw_id=raw_run_id,
            session_handle=session.handle,
        )
        self._runs_by_handle[run.handle] = run
        return run, session

    def session(self, handle: str) -> SessionRecord:
        try:
            record = self._sessions_by_handle[handle]
        except KeyError as exc:
            raise ProxyError(HTTPStatus.NOT_FOUND, "session_not_found") from exc
        self._sessions_by_handle.move_to_end(handle)
        return record

    def run(self, handle: str) -> RunRecord:
        try:
            return self._runs_by_handle[handle]
        except KeyError as exc:
            raise ProxyError(HTTPStatus.NOT_FOUND, "run_not_found") from exc

    def remember_human_request(self, run: RunRecord, raw_request_id: str) -> str:
        for handle, raw_id in run.human_requests.items():
            if raw_id == raw_request_id:
                return handle
        handle = self._new_handle("input")
        run.human_requests[handle] = raw_request_id
        return handle

    def human_request(self, run: RunRecord, handle: str) -> str:
        try:
            return run.human_requests[handle]
        except KeyError as exc:
            raise ProxyError(HTTPStatus.NOT_FOUND, "human_input_not_found") from exc

    def recent_sessions(self) -> list[dict[str, str]]:
        return [
            {"handle": item.handle, "alias": item.alias}
            for item in reversed(self._sessions_by_handle.values())
        ]

    def raw_run_id_for_automation(self, alias: str) -> str:
        for run in self._runs_by_handle.values():
            if run.alias == alias:
                return run.raw_id
        raise KeyError(alias)

    def replace_raw_ids(self, value: str) -> str:
        safe = value
        for run in self._runs_by_handle.values():
            safe = safe.replace(run.raw_id, run.alias)
        for session in self._sessions_by_handle.values():
            safe = safe.replace(session.raw_id, session.alias)
        return safe

    def _new_handle(self, prefix: str) -> str:
        while True:
            handle = f"{prefix}_{self._token_factory()}"
            if (
                handle not in self._sessions_by_handle
                and handle not in self._runs_by_handle
            ):
                return handle

    def _new_alias(self, kind: str) -> str:
        while True:
            alias = self._alias_factory(kind)
            if alias not in self._aliases:
                self._aliases.add(alias)
                return alias

    @staticmethod
    def _random_alias(kind: str) -> str:
        return f"{kind} {secrets.choice(ALIAS_WORDS)}-{secrets.randbelow(9) + 1}"


class UpstreamClient:
    def __init__(self, base_url: str, function_key: str) -> None:
        parsed = urlsplit(base_url)
        self._scheme = parsed.scheme
        self._host = parsed.hostname or ""
        self._port = parsed.port
        self._base_path = parsed.path.rstrip("/")
        self._function_key = function_key

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> UpstreamResponse:
        body = None
        headers = {
            "Accept": "application/json",
            "Cache-Control": "no-store",
            "x-functions-key": self._function_key,
        }
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        connection = self._connection()
        try:
            connection.request(method, f"{self._base_path}{path}", body, headers)
            response = connection.getresponse()
            content = response.read(MAX_UPSTREAM_RESPONSE_BYTES + 1)
            status = response.status
            content_type = response.getheader("Content-Type", "")
        except (OSError, ssl.SSLError) as exc:
            raise ProxyError(HTTPStatus.BAD_GATEWAY, "upstream_unavailable") from exc
        finally:
            connection.close()
        if 300 <= status < 400:
            raise ProxyError(HTTPStatus.BAD_GATEWAY, "upstream_redirect_rejected")
        if len(content) > MAX_UPSTREAM_RESPONSE_BYTES:
            raise ProxyError(HTTPStatus.BAD_GATEWAY, "upstream_response_too_large")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise ProxyError(HTTPStatus.BAD_GATEWAY, "upstream_response_not_json")
        try:
            document = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProxyError(
                HTTPStatus.BAD_GATEWAY,
                "upstream_response_invalid",
            ) from exc
        if not isinstance(document, dict):
            raise ProxyError(HTTPStatus.BAD_GATEWAY, "upstream_response_invalid")
        return UpstreamResponse(status=status, document=document)

    def _connection(self) -> HTTPConnection:
        if self._scheme == "https":
            return HTTPSConnection(self._host, self._port, timeout=120)
        return HTTPConnection(self._host, self._port, timeout=120)


class FocusedDemoServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        config: DemoConfig,
        upstream: UpstreamClient,
        store: AliasStore,
    ) -> None:
        host, _ = address
        validate_bind_host(host)
        if host == "::1":
            self.address_family = socket.AF_INET6
        super().__init__(address, FocusedDemoHandler)
        actual_port = int(self.server_address[1])
        self.authority = f"[::1]:{actual_port}" if host == "::1" else f"{host}:{actual_port}"
        self.origin = f"http://{self.authority}"
        self.config = config
        self.csrf_token = secrets.token_urlsafe(32)
        self.store = store
        self.upstream = upstream


class FocusedDemoHandler(BaseHTTPRequestHandler):
    server: FocusedDemoServer
    server_version = "DurableLoopFocusedDemo/1"

    def do_GET(self) -> None:
        try:
            self._validate_host()
            self._get()
        except ProxyError as exc:
            self._send_json(exc.status, {"error": exc.code})

    def do_POST(self) -> None:
        try:
            self._validate_host()
            self._validate_csrf()
            payload = self._read_json()
            self._post(payload)
        except ProxyError as exc:
            self._send_json(exc.status, {"error": exc.code})

    def do_DELETE(self) -> None:
        self._method_not_allowed()

    def do_CONNECT(self) -> None:
        self._method_not_allowed()

    def do_HEAD(self) -> None:
        self._method_not_allowed()

    def do_OPTIONS(self) -> None:
        self._method_not_allowed()

    def do_PATCH(self) -> None:
        self._method_not_allowed()

    def do_PUT(self) -> None:
        self._method_not_allowed()

    def do_TRACE(self) -> None:
        self._method_not_allowed()

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _get(self) -> None:
        path = self._request_path()
        if path == "/":
            self._send_static("index.html", "text/html; charset=utf-8", set_csrf=True)
            return
        if path == "/app.js":
            self._send_static("app.js", "text/javascript; charset=utf-8")
            return
        if path == "/styles.css":
            self._send_static("styles.css", "text/css; charset=utf-8")
            return
        if path == "/api/config":
            self._send_json(
                HTTPStatus.OK,
                {"dts_dashboard_url": self.server.config.dts_dashboard_url},
            )
            return
        if path == "/api/sessions":
            self._send_json(
                HTTPStatus.OK,
                {"sessions": self.server.store.recent_sessions()},
            )
            return
        match = re.fullmatch(r"/api/runs/([^/]+)", path)
        if match:
            self._get_status(self._checked_handle(match.group(1)))
            return
        match = re.fullmatch(r"/api/runs/([^/]+)/result", path)
        if match:
            self._get_result(self._checked_handle(match.group(1)))
            return
        match = re.fullmatch(r"/api/runs/([^/]+)/sandbox", path)
        if match:
            self._get_sandbox(self._checked_handle(match.group(1)))
            return
        match = re.fullmatch(r"/api/runs/([^/]+)/human/([^/]+)", path)
        if match:
            self._get_human(
                self._checked_handle(match.group(1)),
                self._checked_handle(match.group(2)),
            )
            return
        raise ProxyError(HTTPStatus.NOT_FOUND, "not_found")

    def _post(self, payload: dict[str, object]) -> None:
        path = self._request_path()
        if path == "/api/runs":
            self._start(payload)
            return
        match = re.fullmatch(r"/api/runs/([^/]+)/human/([^/]+)", path)
        if match:
            self._answer(
                self._checked_handle(match.group(1)),
                self._checked_handle(match.group(2)),
                payload,
            )
            return
        raise ProxyError(HTTPStatus.NOT_FOUND, "not_found")

    def _start(self, payload: dict[str, object]) -> None:
        _reject_extra_fields(payload, {"prompt", "scenario", "session_handle"})
        prompt = payload.get("prompt")
        if (
            not isinstance(prompt, str)
            or not prompt.strip()
            or len(prompt.encode()) > MAX_PROMPT_BYTES
        ):
            raise ProxyError(HTTPStatus.BAD_REQUEST, "invalid_prompt")
        scenario = payload.get("scenario", "normal")
        if not isinstance(scenario, str) or scenario not in SCENARIOS:
            raise ProxyError(HTTPStatus.BAD_REQUEST, "invalid_scenario")
        request_body: dict[str, object] = {
            "prompt": prompt.strip(),
            "request_id": f"focused-demo-{uuid.uuid4().hex}",
            "sandbox_profile": "retained_session",
        }
        session_handle = payload.get("session_handle")
        if session_handle is not None:
            if not isinstance(session_handle, str):
                raise ProxyError(HTTPStatus.BAD_REQUEST, "invalid_session_handle")
            request_body["session_id"] = self.server.store.session(
                self._checked_handle(session_handle)
            ).raw_id
        if scenario == "model_apim_429_once":
            request_body["fault_profile"] = scenario
        upstream = self.server.upstream.request(
            "POST",
            "/api/experimental/durable-agent-runs",
            payload=request_body,
        )
        if upstream.status not in {HTTPStatus.OK, HTTPStatus.ACCEPTED}:
            self._send_upstream_error(upstream)
            return
        raw_run = upstream.document.get("run_id")
        raw_session = upstream.document.get("session_id")
        if not isinstance(raw_run, str) or not isinstance(raw_session, str):
            raise ProxyError(HTTPStatus.BAD_GATEWAY, "upstream_response_invalid")
        run, session = self.server.store.remember(
            raw_run_id=raw_run,
            raw_session_id=raw_session,
        )
        self._send_json(
            upstream.status,
            {
                "run_handle": run.handle,
                "run_alias": run.alias,
                "session_handle": session.handle,
                "session_alias": session.alias,
                "scenario": scenario,
                "status": self._safe_text(
                    upstream.document.get("status"),
                    "accepted",
                ),
            },
        )

    def _get_status(self, run_handle: str) -> None:
        run = self.server.store.run(run_handle)
        upstream = self.server.upstream.request(
            "GET",
            f"/api/experimental/durable-agent-runs/{quote(run.raw_id, safe='')}",
        )
        if upstream.status != HTTPStatus.OK:
            self._send_upstream_error(upstream)
            return
        session = self.server.store.session(run.session_handle)
        body: dict[str, object] = {
            "run_handle": run.handle,
            "run_alias": run.alias,
            "session_handle": session.handle,
            "session_alias": session.alias,
        }
        for key in POLLABLE_STATUS_FIELDS:
            value = upstream.document.get(key)
            if isinstance(value, str):
                body[key] = self._safe_text(value)
            elif isinstance(value, int) and not isinstance(value, bool):
                body[key] = value
        human = upstream.document.get("human_input")
        if isinstance(human, dict):
            raw_request_id = human.get("request_id")
            if isinstance(raw_request_id, str):
                human_handle = self.server.store.remember_human_request(
                    run,
                    raw_request_id,
                )
                body["human_input"] = {
                    "handle": human_handle,
                    "allow_free_text": human.get("allow_free_text") is True,
                    "choice_count": _safe_nonnegative_int(human.get("choice_count")),
                    "expires_at": self._safe_text(human.get("expires_at")),
                }
        self._send_json(HTTPStatus.OK, body)

    def _get_result(self, run_handle: str) -> None:
        run = self.server.store.run(run_handle)
        upstream = self.server.upstream.request(
            "GET",
            (
                "/api/experimental/durable-agent-runs/"
                f"{quote(run.raw_id, safe='')}/result"
            ),
        )
        if upstream.status not in {HTTPStatus.OK, HTTPStatus.ACCEPTED}:
            self._send_upstream_error(upstream)
            return
        response = upstream.document.get("response")
        body: dict[str, object] = {
            "run_handle": run.handle,
            "run_alias": run.alias,
            "status": self._safe_text(upstream.document.get("status"), "pending"),
        }
        if isinstance(response, str):
            body["response"] = self._redact(response)
        self._send_json(upstream.status, body)

    def _get_sandbox(self, run_handle: str) -> None:
        run = self.server.store.run(run_handle)
        upstream = self.server.upstream.request(
            "GET",
            (
                "/api/experimental/durable-agent-runs/"
                f"{quote(run.raw_id, safe='')}/sandbox"
            ),
        )
        if upstream.status != HTTPStatus.OK:
            self._send_upstream_error(upstream)
            return
        body: dict[str, object] = {}
        for key in SANDBOX_FIELDS:
            value = upstream.document.get(key)
            if not _safe_sandbox_value(key, value):
                continue
            body[key] = self._redact(value) if isinstance(value, str) else value
        self._send_json(HTTPStatus.OK, body)

    def _get_human(self, run_handle: str, human_handle: str) -> None:
        run = self.server.store.run(run_handle)
        raw_request = self.server.store.human_request(run, human_handle)
        upstream = self.server.upstream.request(
            "GET",
            (
                "/api/experimental/durable-agent-runs/"
                f"{quote(run.raw_id, safe='')}/input/"
                f"{quote(raw_request, safe='')}"
            ),
        )
        if upstream.status != HTTPStatus.OK:
            self._send_upstream_error(upstream)
            return
        question = upstream.document.get("question")
        choices = upstream.document.get("choices")
        self._send_json(
            HTTPStatus.OK,
            {
                "handle": human_handle,
                "question": (
                    self._redact(question)
                    if isinstance(question, str)
                    else ""
                ),
                "choices": (
                    [
                        self._redact(choice)
                        for choice in choices
                        if isinstance(choice, str)
                    ][:20]
                    if isinstance(choices, list)
                    else []
                ),
                "allow_free_text": upstream.document.get("allow_free_text") is True,
                "expires_at": self._safe_text(upstream.document.get("expires_at")),
            },
        )

    def _answer(
        self,
        run_handle: str,
        human_handle: str,
        payload: dict[str, object],
    ) -> None:
        _reject_extra_fields(payload, {"answer"})
        answer = payload.get("answer")
        if (
            not isinstance(answer, str)
            or not answer.strip()
            or len(answer.encode()) > MAX_REQUEST_BYTES
        ):
            raise ProxyError(HTTPStatus.BAD_REQUEST, "invalid_answer")
        run = self.server.store.run(run_handle)
        raw_request = self.server.store.human_request(run, human_handle)
        upstream = self.server.upstream.request(
            "POST",
            (
                "/api/experimental/durable-agent-runs/"
                f"{quote(run.raw_id, safe='')}/input/"
                f"{quote(raw_request, safe='')}"
            ),
            payload={"answer": answer.strip()},
            idempotency_key=f"focused-answer-{uuid.uuid4().hex}",
        )
        if upstream.status != HTTPStatus.ACCEPTED:
            self._send_upstream_error(upstream)
            return
        self._send_json(
            HTTPStatus.ACCEPTED,
            {
                "run_handle": run.handle,
                "run_alias": run.alias,
                "status": "accepted",
            },
        )

    def _send_upstream_error(self, upstream: UpstreamResponse) -> None:
        value = upstream.document.get("error")
        code = value if isinstance(value, str) and ERROR_CODE.fullmatch(value) else "upstream_error"
        self._send_json(upstream.status, {"error": code})

    def _safe_text(self, value: object, default: str = "") -> str:
        return self._redact(_safe_string(value, default))

    def _redact(self, value: str) -> str:
        safe = self.server.store.replace_raw_ids(value)
        return safe.replace(self.server.config.function_key, "[redacted]")

    def _validate_host(self) -> None:
        if self.headers.get("Host") != self.server.authority:
            raise ProxyError(HTTPStatus.BAD_REQUEST, "invalid_host")

    def _validate_csrf(self) -> None:
        if self.headers.get("Origin") != self.server.origin:
            raise ProxyError(HTTPStatus.FORBIDDEN, "invalid_origin")
        if self.headers.get("X-CSRF-Token") != self.server.csrf_token:
            raise ProxyError(HTTPStatus.FORBIDDEN, "invalid_csrf")

    def _read_json(self) -> dict[str, object]:
        if self.headers.get("Transfer-Encoding"):
            raise ProxyError(HTTPStatus.BAD_REQUEST, "unsupported_transfer_encoding")
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type.lower() != "application/json":
            raise ProxyError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "json_required")
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else -1
        except ValueError as exc:
            raise ProxyError(HTTPStatus.BAD_REQUEST, "invalid_content_length") from exc
        if length < 0:
            raise ProxyError(HTTPStatus.LENGTH_REQUIRED, "content_length_required")
        if length > MAX_REQUEST_BYTES:
            raise ProxyError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request_too_large")
        try:
            value = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProxyError(HTTPStatus.BAD_REQUEST, "invalid_json") from exc
        if not isinstance(value, dict):
            raise ProxyError(HTTPStatus.BAD_REQUEST, "body_must_be_object")
        return value

    def _request_path(self) -> str:
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            raise ProxyError(HTTPStatus.NOT_FOUND, "not_found")
        return parsed.path

    @staticmethod
    def _checked_handle(value: str) -> str:
        if HANDLE.fullmatch(value) is None:
            raise ProxyError(HTTPStatus.NOT_FOUND, "not_found")
        return value

    def _send_static(
        self,
        filename: str,
        content_type: str,
        *,
        set_csrf: bool = False,
    ) -> None:
        content = (STATIC_ROOT / filename).read_bytes()
        headers = {"Content-Type": content_type}
        if set_csrf:
            headers["Set-Cookie"] = (
                f"focused_csrf={self.server.csrf_token}; Path=/; SameSite=Strict"
            )
        self._send_bytes(HTTPStatus.OK, content, headers)

    def _send_json(
        self,
        status: int | HTTPStatus,
        payload: dict[str, object],
    ) -> None:
        content = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode()
        self._send_bytes(
            status,
            content,
            {"Content-Type": "application/json; charset=utf-8"},
        )

    def _send_bytes(
        self,
        status: int | HTTPStatus,
        content: bytes,
        headers: dict[str, str],
    ) -> None:
        if len(content) > MAX_LOCAL_RESPONSE_BYTES:
            content = b'{"error":"response_too_large"}'
            status = HTTPStatus.BAD_GATEWAY
            headers = {"Content-Type": "application/json; charset=utf-8"}
        self.send_response(int(status))
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _method_not_allowed(self) -> None:
        try:
            self._validate_host()
        except ProxyError as exc:
            self._send_json(exc.status, {"error": exc.code})
            return
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method_not_allowed"})


def validate_bind_host(host: str) -> None:
    if host not in LOOPBACK_HOSTS:
        raise ValueError("bind host must be exactly 127.0.0.1 or ::1")


def create_server(
    *,
    config: DemoConfig,
    host: str = "127.0.0.1",
    port: int = 8765,
    upstream: UpstreamClient | None = None,
    store: AliasStore | None = None,
) -> FocusedDemoServer:
    return FocusedDemoServer(
        (host, port),
        config=config,
        upstream=upstream or UpstreamClient(config.function_url, config.function_key),
        store=store or AliasStore(),
    )


def _sanitize_url(value: str, *, name: str, https_only: bool) -> str:
    if not value:
        raise RuntimeError(f"{name} is required")
    parsed = urlsplit(value)
    allowed_schemes = {"https"} if https_only else {"http", "https"}
    if (
        parsed.scheme not in allowed_schemes
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError(f"{name} must be a credential-free {sorted(allowed_schemes)} URL")
    return value.rstrip("/")


def _sanitize_dts_url(value: str) -> str:
    if not value:
        raise RuntimeError("DTS_TASK_HUB_DASHBOARD_URL is required")
    parsed = urlsplit(value)
    query = parse_qs(parsed.query, keep_blank_values=False)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "dashboard.durabletask.io"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or set(query) - {"endpoint", "taskhub", "tenantId"}
    ):
        raise RuntimeError(
            "DTS_TASK_HUB_DASHBOARD_URL must be a credential-free "
            "https://dashboard.durabletask.io URL"
        )
    return value


def _reject_extra_fields(
    payload: dict[str, object],
    allowed: set[str],
) -> None:
    if set(payload) - allowed:
        raise ProxyError(HTTPStatus.BAD_REQUEST, "unexpected_field")


def _safe_string(value: object, default: str = "") -> str:
    if isinstance(value, str) and len(value) <= 256:
        return value
    return default


def _safe_nonnegative_int(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _safe_sandbox_value(key: str, value: object) -> bool:
    if key == "generation":
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    if key == "workspace_checkpoint_present":
        return isinstance(value, bool)
    return isinstance(value, str) and len(value) <= 128


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the focused Durable Chat demo.")
    parser.add_argument("--host", default="127.0.0.1", choices=sorted(LOOPBACK_HOSTS))
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = create_server(
        config=DemoConfig.from_env(),
        host=args.host,
        port=args.port,
    )
    print(f"Focused Durable Chat: {server.origin}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
