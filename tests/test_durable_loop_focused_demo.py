from __future__ import annotations

import importlib.util
import json
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http import HTTPStatus
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

ROOT = Path(__file__).resolve().parents[1]
FOCUSED_DIR = ROOT / "samples" / "durable-agent-loop-spike" / "demo" / "focused"


def _load_proxy() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "durable_loop_focused_proxy",
        FOCUSED_DIR / "proxy.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


proxy = _load_proxy()


class _FakeUpstream:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.requests: list[
            tuple[str, str, dict[str, object] | None, str | None]
        ] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> object:
        self.requests.append((method, path, payload, idempotency_key))
        assert self.responses
        return self.responses.pop(0)


@contextmanager
def _running_proxy(
    fake: _FakeUpstream,
) -> Iterator[tuple[object, str]]:
    config = proxy.DemoConfig(
        function_url="https://function.example",
        function_key="test-function-key",
        dts_dashboard_url="https://dashboard.durabletask.io/task-hub",
    )
    server = proxy.create_server(
        config=config,
        host="127.0.0.1",
        port=0,
        upstream=fake,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, cast(str, server.authority)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(
    authority: str,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    host, port_text = authority.rsplit(":", 1)
    connection = HTTPConnection(host, int(port_text), timeout=5)
    request_headers = {"Host": authority, **(headers or {})}
    connection.request(method, path, body=body, headers=request_headers)
    response = connection.getresponse()
    content = response.read()
    response_headers = {name.lower(): value for name, value in response.getheaders()}
    connection.close()
    return response.status, response_headers, content


def _csrf_headers(server: object, authority: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Origin": f"http://{authority}",
        "X-CSRF-Token": cast(str, server.csrf_token),
    }


def test_loopback_binding_validation_is_literal_only() -> None:
    proxy.validate_bind_host("127.0.0.1")
    proxy.validate_bind_host("::1")

    for host in ("localhost", "0.0.0.0", "::", "192.0.2.1"):
        with pytest.raises(ValueError, match="exactly"):
            proxy.validate_bind_host(host)


def test_dashboard_url_is_sanitized_before_browser_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DURABLE_LOOP_FUNCTION_URL", "https://function.example")
    monkeypatch.setenv("DURABLE_LOOP_FUNCTION_KEY", "secret")
    monkeypatch.setenv(
        "DTS_TASK_HUB_DASHBOARD_URL",
        (
            "https://dashboard.durabletask.io/task-hub"
            "?endpoint=https%3A%2F%2Fscheduler.eastus2.durabletask.io"
            "&taskhub=demo"
        ),
    )
    config = proxy.DemoConfig.from_env()
    assert config.dts_dashboard_url.endswith("&taskhub=demo")

    for unsafe in (
        "http://dashboard.example/task-hub",
        "https://user:password@dashboard.example/task-hub",
        "https://dashboard.example/task-hub",
        "https://dashboard.durabletask.io/task-hub?instance=raw",
        "https://dashboard.example/task-hub#raw",
    ):
        monkeypatch.setenv("DTS_TASK_HUB_DASHBOARD_URL", unsafe)
        with pytest.raises(RuntimeError, match="DTS_TASK_HUB_DASHBOARD_URL"):
            proxy.DemoConfig.from_env()


def test_host_origin_and_csrf_fail_closed() -> None:
    with _running_proxy(_FakeUpstream([])) as (server, authority):
        status, headers, _ = _request(authority, "GET", "/")
        assert status == HTTPStatus.OK
        assert "focused_csrf=" in headers["set-cookie"]
        assert headers["cache-control"] == "no-store"
        assert headers["x-content-type-options"] == "nosniff"
        assert "connect-src 'self'" in headers["content-security-policy"]
        assert "frame-ancestors 'none'" in headers["content-security-policy"]

        status, _, body = _request(
            authority,
            "GET",
            "/api/config",
            headers={"Host": "127.0.0.1:1"},
        )
        assert status == HTTPStatus.BAD_REQUEST
        assert json.loads(body) == {"error": "invalid_host"}

        valid = _csrf_headers(server, authority)
        invalid_origin = {**valid, "Origin": "http://127.0.0.1:1"}
        status, _, body = _request(
            authority,
            "POST",
            "/api/runs",
            body=b"{}",
            headers=invalid_origin,
        )
        assert status == HTTPStatus.FORBIDDEN
        assert json.loads(body) == {"error": "invalid_origin"}

        missing_csrf = {key: value for key, value in valid.items() if key != "X-CSRF-Token"}
        status, _, body = _request(
            authority,
            "POST",
            "/api/runs",
            body=b"{}",
            headers=missing_csrf,
        )
        assert status == HTTPStatus.FORBIDDEN
        assert json.loads(body) == {"error": "invalid_csrf"}


def test_fixed_routes_methods_and_request_size_limits() -> None:
    with _running_proxy(_FakeUpstream([])) as (server, authority):
        status, _, body = _request(authority, "GET", "/api/unknown")
        assert status == HTTPStatus.NOT_FOUND
        assert json.loads(body) == {"error": "not_found"}

        status, _, body = _request(authority, "PUT", "/api/runs")
        assert status == HTTPStatus.METHOD_NOT_ALLOWED
        assert json.loads(body) == {"error": "method_not_allowed"}

        oversized = b"{" + b" " * proxy.MAX_REQUEST_BYTES + b"}"
        status, _, body = _request(
            authority,
            "POST",
            "/api/runs",
            body=oversized,
            headers=_csrf_headers(server, authority),
        )
        assert status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
        assert json.loads(body) == {"error": "request_too_large"}


def test_upstream_redirects_and_large_responses_are_rejected() -> None:
    class RedirectHandler(BaseHTTPRequestHandler):
        mode = "redirect"

        def do_GET(self) -> None:
            if self.mode == "redirect":
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", "https://attacker.example/")
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")
                return
            content = b'{"value":"' + b"x" * proxy.MAX_UPSTREAM_RESPONSE_BYTES + b'"}'
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    upstream_server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    thread = threading.Thread(target=upstream_server.serve_forever, daemon=True)
    thread.start()
    client = proxy.UpstreamClient(
        f"http://127.0.0.1:{upstream_server.server_address[1]}",
        "secret",
    )
    try:
        with pytest.raises(proxy.ProxyError, match="upstream_redirect_rejected"):
            client.request("GET", "/redirect")
        RedirectHandler.mode = "large"
        with pytest.raises(proxy.ProxyError, match="upstream_response_too_large"):
            client.request("GET", "/large")
    finally:
        upstream_server.shutdown()
        upstream_server.server_close()
        thread.join(timeout=5)


def test_aliases_and_browser_responses_never_expose_raw_identifiers_or_key() -> None:
    raw_run = "run-low-entropy-7"
    raw_session = "session-3"
    fake = _FakeUpstream(
        [
            proxy.UpstreamResponse(
                HTTPStatus.ACCEPTED,
                {
                    "run_id": raw_run,
                    "session_id": raw_session,
                    "status": "running",
                },
            ),
            proxy.UpstreamResponse(
                HTTPStatus.OK,
                {
                    "run_id": raw_run,
                    "session_id": raw_session,
                    "status": "completed",
                    "phase": "complete",
                    "model_steps": 2,
                    "tool_calls": 1,
                },
            ),
            proxy.UpstreamResponse(
                HTTPStatus.OK,
                {
                    "run_id": raw_run,
                    "session_id": raw_session,
                    "status": "completed",
                    "response": (
                        f"Finished {raw_run} in {raw_session}; "
                        "key test-function-key."
                    ),
                },
            ),
        ]
    )
    with _running_proxy(fake) as (server, authority):
        status, _, app_js = _request(authority, "GET", "/app.js")
        assert status == HTTPStatus.OK
        assert b"localStorage" not in app_js
        assert b"test-function-key" not in app_js

        payload = json.dumps({"prompt": "hello", "scenario": "normal"}).encode()
        status, _, started = _request(
            authority,
            "POST",
            "/api/runs",
            body=payload,
            headers=_csrf_headers(server, authority),
        )
        started_body = json.loads(started)
        assert status == HTTPStatus.ACCEPTED
        assert started_body["run_alias"].startswith("RUN ")
        assert started_body["session_alias"].startswith("SESSION ")
        assert raw_run.encode() not in started
        assert raw_session.encode() not in started
        assert started_body["run_alias"] not in raw_run
        assert started_body["session_alias"] not in raw_session

        run_handle = started_body["run_handle"]
        status, _, projected = _request(authority, "GET", f"/api/runs/{run_handle}")
        assert status == HTTPStatus.OK
        assert raw_run.encode() not in projected
        assert raw_session.encode() not in projected

        status, _, result = _request(
            authority,
            "GET",
            f"/api/runs/{run_handle}/result",
        )
        assert status == HTTPStatus.OK
        assert raw_run.encode() not in result
        assert raw_session.encode() not in result
        assert b"test-function-key" not in result
        assert started_body["run_alias"].encode() in result
        assert started_body["session_alias"].encode() in result
        assert server.store.raw_run_id_for_automation(started_body["run_alias"]) == raw_run

        status, _, sessions = _request(authority, "GET", "/api/sessions")
        assert status == HTTPStatus.OK
        assert raw_session.encode() not in sessions
        assert b"test-function-key" not in sessions


def test_human_choices_use_accessible_radio_cards() -> None:
    index = (FOCUSED_DIR / "index.html").read_text(encoding="utf-8")
    app = (FOCUSED_DIR / "app.js").read_text(encoding="utf-8")
    styles = (FOCUSED_DIR / "styles.css").read_text(encoding="utf-8")

    assert "<fieldset>" in index
    assert 'id="humanChoicesForm"' in index
    assert 'id="humanChoiceSubmit"' in index
    assert 'input.type = "radio"' in app
    assert "label.htmlFor = choiceId" in app
    assert 'input.name = "human-choice"' in app
    assert ".human-choice-card:focus-within" in styles
    assert ".human-choice-card:has(input:checked)" in styles


def test_start_resume_human_and_sandbox_requests_project_only_fixed_fields() -> None:
    raw_run = "run-upstream-123"
    raw_session = "session-upstream-456"
    raw_human = "human-upstream-789"
    fake = _FakeUpstream(
        [
            proxy.UpstreamResponse(
                HTTPStatus.ACCEPTED,
                {"run_id": raw_run, "session_id": raw_session, "status": "running"},
            ),
            proxy.UpstreamResponse(
                HTTPStatus.OK,
                {
                    "run_id": raw_run,
                    "session_id": raw_session,
                    "status": "running",
                    "phase": "human_wait",
                    "model_steps": 1,
                    "tool_calls": 0,
                    "human_input": {
                        "request_id": raw_human,
                        "allow_free_text": True,
                        "choice_count": 2,
                        "expires_at": "2026-09-09T00:00:00Z",
                        "detail_url": f"/raw/{raw_run}/{raw_human}",
                    },
                    "private": "discard-me",
                },
            ),
            proxy.UpstreamResponse(
                HTTPStatus.OK,
                {
                    "run_id": raw_run,
                    "request_id": raw_human,
                    "question": "Choose one",
                    "choices": ["Alpha", "Beta"],
                    "allow_free_text": True,
                    "response_schema": {"secret": "discard-me"},
                },
            ),
            proxy.UpstreamResponse(
                HTTPStatus.OK,
                {
                    "sandbox_instance_alias": "sandbox-amber-4",
                    "generation": 2,
                    "state": "Running",
                    "workspace_checkpoint_present": True,
                    "provider_id": "discard-me",
                },
            ),
            proxy.UpstreamResponse(
                HTTPStatus.ACCEPTED,
                {"run_id": raw_run, "request_id": raw_human, "status": "accepted"},
            ),
            proxy.UpstreamResponse(
                HTTPStatus.ACCEPTED,
                {
                    "run_id": "run-second",
                    "session_id": raw_session,
                    "status": "running",
                },
            ),
        ]
    )
    with _running_proxy(fake) as (server, authority):
        headers = _csrf_headers(server, authority)
        first_payload = json.dumps(
            {"prompt": "first", "scenario": "model_apim_429_once"}
        ).encode()
        _, _, first = _request(
            authority,
            "POST",
            "/api/runs",
            body=first_payload,
            headers=headers,
        )
        first_body = json.loads(first)
        run_handle = first_body["run_handle"]
        session_handle = first_body["session_handle"]
        first_upstream = fake.requests[0]
        assert first_upstream[0:2] == (
            "POST",
            "/api/experimental/durable-agent-runs",
        )
        assert first_upstream[2] is not None
        assert first_upstream[2]["sandbox_profile"] == "retained_session"
        assert first_upstream[2]["fault_profile"] == "model_apim_429_once"
        assert "session_id" not in first_upstream[2]

        _, _, status_content = _request(authority, "GET", f"/api/runs/{run_handle}")
        status_body = json.loads(status_content)
        human_handle = status_body["human_input"]["handle"]
        assert "detail_url" not in status_body["human_input"]
        assert "private" not in status_body
        assert raw_human.encode() not in status_content

        _, _, detail_content = _request(
            authority,
            "GET",
            f"/api/runs/{run_handle}/human/{human_handle}",
        )
        detail = json.loads(detail_content)
        assert detail["choices"] == ["Alpha", "Beta"]
        assert "response_schema" not in detail

        _, _, sandbox_content = _request(
            authority,
            "GET",
            f"/api/runs/{run_handle}/sandbox",
        )
        sandbox = json.loads(sandbox_content)
        assert sandbox == {
            "sandbox_instance_alias": "sandbox-amber-4",
            "generation": 2,
            "state": "Running",
            "workspace_checkpoint_present": True,
        }

        answer_payload = json.dumps({"answer": "Alpha"}).encode()
        status, _, answer_content = _request(
            authority,
            "POST",
            f"/api/runs/{run_handle}/human/{human_handle}",
            body=answer_payload,
            headers=headers,
        )
        assert status == HTTPStatus.ACCEPTED
        assert raw_run.encode() not in answer_content
        answer_upstream = fake.requests[4]
        assert answer_upstream[0] == "POST"
        assert answer_upstream[1].endswith(f"/{raw_run}/input/{raw_human}")
        assert answer_upstream[2] == {"answer": "Alpha"}
        assert cast(str, answer_upstream[3]).startswith("focused-answer-")

        resume_payload = json.dumps(
            {
                "prompt": "resume",
                "scenario": "normal",
                "session_handle": session_handle,
            }
        ).encode()
        _, _, resumed = _request(
            authority,
            "POST",
            "/api/runs",
            body=resume_payload,
            headers=headers,
        )
        resume_upstream = fake.requests[5]
        assert resume_upstream[2] is not None
        assert resume_upstream[2]["session_id"] == raw_session
        assert "fault_profile" not in resume_upstream[2]
        assert raw_session.encode() not in resumed
