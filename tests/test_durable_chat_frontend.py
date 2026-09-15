from __future__ import annotations

import asyncio
import contextlib
import http.server
import json
import shutil
import threading
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from tests.doubles.durable_chat_cdp import chromium_page

_WORKSPACE = Path(__file__).resolve().parents[1]
_ASSET_SOURCE = _WORKSPACE / "src" / "azure_functions_agents" / "public" / "durable-chat"
_ARTIFACT_ROOT = _WORKSPACE / ".dch-frontend"
_PREFIX = "/chat-test"
_SHELL_PATH = f"{_PREFIX}/experimental/durable-chat/"
_ASSET_PATH = _SHELL_PATH
_RUN_BASE = f"{_PREFIX}/experimental/durable-agent-runs"
_NAMESPACE = "a" * 64


class _ChatState:
    def __init__(self) -> None:
        self.bootstrap_sandbox_group: str | None = (
            "/subscriptions/test/resourceGroups/bootstrap-group"
        )
        self.cancel_sets_status = False
        self.config_headers: dict[str, str] = {}
        self.config_redirect_url = ""
        self.completed = False
        self.configured_sandbox_group: str | None = (
            "/subscriptions/test/resourceGroups/admitted-group"
        )
        self.diagnostics_expired = False
        self.diagnostics_gates: dict[int, tuple[threading.Event, threading.Event]] = {}
        self.diagnostics_observation_states: dict[int, str] = {}
        self.diagnostics_requests = 0
        self.human_answer: object | None = None
        self.human_wait = False
        self.include_sandbox_observations = True
        self.request_body: dict[str, Any] | None = None
        self.request_headers: dict[str, str] = {}
        self.result_attempts = 0
        self.result_headers: dict[str, str] = {}
        self.result_mode = "automatic"
        self.result_redirect_url = ""
        self.run_id = "run-frontend-test"
        self.session_id = ""
        self.started = threading.Event()
        self.status_override: str | None = None


class _ChatHandler(http.server.BaseHTTPRequestHandler):
    state: _ChatState
    directory: Path

    def log_message(self, _: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == _SHELL_PATH:
            self._asset("index.html", "text/html; charset=utf-8")
            return
        if path.startswith(_ASSET_PATH):
            asset_name = path.removeprefix(_ASSET_PATH)
            media_type = {
                "app.js": "text/javascript; charset=utf-8",
                "history.js": "text/javascript; charset=utf-8",
                "rendering.js": "text/javascript; charset=utf-8",
                "styles.css": "text/css; charset=utf-8",
            }.get(asset_name)
            if media_type:
                self._asset(asset_name, media_type)
                return
        if path == f"{_SHELL_PATH}config":
            self.state.config_headers = dict(self.headers)
            if self.state.config_redirect_url:
                self._redirect(self.state.config_redirect_url)
                return
            self._json(200, _bootstrap(self.state))
            return
        if path == f"{_RUN_BASE}/{self.state.run_id}/events":
            self._events()
            return
        if path == f"{_RUN_BASE}/{self.state.run_id}/diagnostics":
            self._diagnostics()
            return
        if path == f"{_RUN_BASE}/{self.state.run_id}/input/input-1":
            self._json(
                200,
                {
                    "allow_free_text": False,
                    "choices": [],
                    "expires_at": "2026-01-01T01:00:00+00:00",
                    "question": "Provide a structured response.",
                    "request_id": "input-1",
                    "response_schema": {
                        "type": "object",
                        "required": ["token"],
                        "properties": {
                            "token": {
                                "type": "string",
                                "description": "A schema field, not an auth credential.",
                            }
                        },
                    },
                    "run_id": self.state.run_id,
                },
            )
            return
        if path == f"{_RUN_BASE}/{self.state.run_id}/result":
            self._result()
            return
        if path == f"{_RUN_BASE}/{self.state.run_id}":
            self._json(200, _status(self.state))
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == _RUN_BASE:
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.state.request_body = json.loads(raw)
            self.state.request_headers = dict(self.headers)
            self.state.session_id = self.state.request_body["session_id"]
            self.state.started.set()
            self._json(
                202,
                {
                    "run_id": self.state.run_id,
                    "session_id": self.state.session_id,
                    "status": "Pending",
                },
            )
            return
        if path == f"{_RUN_BASE}/{self.state.run_id}/cancel":
            if self.state.cancel_sets_status:
                self.state.status_override = "Cancelled"
            self._json(
                202,
                {
                    "delivery": "delivered",
                    "run_id": self.state.run_id,
                    "status": "Cancelled",
                },
            )
            return
        if path == f"{_RUN_BASE}/{self.state.run_id}/input/input-1":
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.state.human_answer = json.loads(raw)["answer"]
            self._json(
                202,
                {
                    "delivery": "delivered",
                    "request_id": "input-1",
                    "run_id": self.state.run_id,
                    "status": "accepted",
                },
            )
            return
        self._json(404, {"error": "not_found"})

    def _asset(self, name: str, media_type: str) -> None:
        content = (self.directory / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _diagnostics(self) -> None:
        self.state.diagnostics_requests += 1
        request_number = self.state.diagnostics_requests
        gate = self.state.diagnostics_gates.get(request_number)
        if gate is not None:
            started, release = gate
            started.set()
            if not release.wait(timeout=10):
                self._json(503, {"error": "diagnostics_test_gate_timeout"})
                return
        if self.state.diagnostics_expired:
            self._json(410, {"error": "chat_observations_expired"})
            return
        self._json(
            200,
            _diagnostics(
                self.state,
                sandbox_observation_state=self.state.diagnostics_observation_states.get(
                    request_number,
                    "executing",
                ),
            ),
        )

    def _events(self) -> None:
        snapshot = {
            "schema_version": "1",
            "event_type": "snapshot",
            "captured_at": "2026-01-01T00:00:00+00:00",
            "projection": {
                "schema_version": "1",
                "run_id": self.state.run_id,
                "session_id": self.state.session_id,
                "published_revision": 1,
                "through_sequence": 0,
                "draft": None,
                "progress": {
                    "schema_version": "1",
                    "status": "Running",
                    "phase": "model",
                    "model_steps": 1,
                    "tool_calls": 0,
                    "human_waits": 0,
                    "step_index": 0,
                    "result_available": False,
                    "updated_at": "2026-01-01T00:00:00+00:00",
                },
                "producer_epochs": [
                    {
                        "schema_version": "1",
                        "step_index": 0,
                        "observation_epoch": 1,
                    }
                ],
                "tool_progress": [],
                "sandbox_observations": [],
                "observation_health": {
                    "schema_version": "1",
                    "degraded": False,
                    "reasons": [],
                    "dropped_observations": 0,
                    "reserved_observation_bytes": 0,
                    "last_error_code": None,
                },
            },
        }
        event = {
            "schema_version": "1",
            "sequence": 1,
            "published_revision": 2,
            "event": {
                "schema_version": "1",
                "event_type": "assistant_text",
                "run_id": self.state.run_id,
                "session_id": self.state.session_id,
                "observed_at": "2026-01-01T00:00:01+00:00",
                "producer": {
                    "schema_version": "1",
                    "producer_type": "model",
                    "step_index": 0,
                    "observation_epoch": 1,
                },
                "delta": "Visible <b>model</b> fragment λ.",
            },
        }
        unversioned_event = {
            "schema_version": "1",
            "sequence": 2,
            "published_revision": None,
            "event": {
                "schema_version": "1",
                "event_type": "assistant_text",
                "run_id": self.state.run_id,
                "session_id": self.state.session_id,
                "observed_at": "2026-01-01T00:00:02+00:00",
                "producer": {
                    "schema_version": "1",
                    "producer_type": "model",
                    "step_index": 0,
                    "observation_epoch": 1,
                },
                "delta": " Unversioned event.",
            },
        }
        terminal_event = {
            "schema_version": "1",
            "sequence": 3,
            "published_revision": 3,
            "event": {
                "schema_version": "1",
                "event_type": "terminal",
                "run_id": self.state.run_id,
                "session_id": self.state.session_id,
                "observed_at": "2026-01-01T00:00:03+00:00",
                "status": "Completed",
                "result_available": True,
            },
        }
        body = (
            f"id: 0\nevent: snapshot\ndata: {json.dumps(snapshot, ensure_ascii=False)}\n\n"
            f": heartbeat\n\n"
            f"id: 1\nevent: assistant_text\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            f"id: 2\nevent: assistant_text\ndata: {json.dumps(unversioned_event, ensure_ascii=False)}\n\n"
            f"id: 3\nevent: terminal\ndata: {json.dumps(terminal_event, ensure_ascii=False)}\n\n"
        ).encode()
        split_at = body.index("λ".encode()) + 1
        self.send_response(200)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            self.wfile.write(body[:split_at])
            self.wfile.flush()
            self.wfile.write(body[split_at:])
            self.wfile.flush()
        except BrokenPipeError:
            return

    def _result(self) -> None:
        self.state.result_attempts += 1
        self.state.result_headers = dict(self.headers)
        if self.state.result_redirect_url:
            self._redirect(self.state.result_redirect_url)
            return
        if self.state.result_mode == "not_ready" or not self.state.completed:
            self._json(202, {"error": "result_not_ready"})
            return
        if self.state.result_mode == "malformed":
            self._json(200, {"status": "Completed"})
            return
        if self.state.result_mode == "server_error":
            self._json(500, {"error": "result_service_unavailable"})
            return
        if self.state.result_mode == "unauthorized":
            self._json(401, {"error": "authentication_required"})
            return
        self._json(
            200,
            {
                "response": "Authoritative final answer.",
                "run_id": self.state.run_id,
                "session_id": self.state.session_id,
                "status": "Completed",
            },
        )

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Content-Length", "0")
        self.send_header("Location", location)
        self.end_headers()

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _bootstrap(state: _ChatState | None = None) -> dict[str, Any]:
    route = lambda name, method, path: {  # noqa: E731
        "schema_version": "1",
        "name": name,
        "method": method,
        "path_template": path,
    }
    return {
        "schema_version": "1",
        "agent": {
            "schema_version": "1",
            "slug": "durable-chat-test",
            "display_name": "Durable chat test agent",
        },
        "routes": [
            route("start_run", "POST", _RUN_BASE),
            route("status", "GET", f"{_RUN_BASE}/{{run_id}}"),
            route("result", "GET", f"{_RUN_BASE}/{{run_id}}/result"),
            route("cancel", "POST", f"{_RUN_BASE}/{{run_id}}/cancel"),
            route(
                "human_input_detail",
                "GET",
                f"{_RUN_BASE}/{{run_id}}/input/{{request_id}}",
            ),
            route(
                "human_input_submit",
                "POST",
                f"{_RUN_BASE}/{{run_id}}/input/{{request_id}}",
            ),
            route("events", "GET", f"{_RUN_BASE}/{{run_id}}/events"),
            route("diagnostics", "GET", f"{_RUN_BASE}/{{run_id}}/diagnostics"),
        ],
        "supported_sandbox_profiles": ["per_call", "retained_session"],
        "default_sandbox_profile": "per_call",
        "foreground_streaming_available": True,
        "sandbox_group_resource_id": (
            state.bootstrap_sandbox_group
            if state is not None
            else "/subscriptions/test/resourceGroups/bootstrap-group"
        ),
        "integrations": None,
        "history_namespace": _NAMESPACE,
    }


def _status(state: _ChatState) -> dict[str, Any]:
    status_value = state.status_override or (
        "Completed" if state.completed else "Waiting" if state.human_wait else "Running"
    )
    status: dict[str, Any] = {
        "run_id": state.run_id,
        "session_id": state.session_id,
        "status": status_value,
        "phase": (
            "completed"
            if status_value == "Completed"
            else "human_wait"
            if status_value == "Waiting"
            else status_value.lower()
            if status_value in {"Cancelled", "Failed"}
            else "model"
        ),
        "model_steps": 1,
        "tool_calls": 0,
        "human_waits": 0,
        "step_index": 0,
    }
    if status_value == "Waiting" and state.human_wait and not state.completed:
        status["human_input"] = {
            "allow_free_text": False,
            "choice_count": 0,
            "expires_at": "2026-01-01T01:00:00+00:00",
            "request_id": "input-1",
            "schema_present": True,
        }
    return status


def _diagnostics(
    state: _ChatState, *, sandbox_observation_state: str = "executing"
) -> dict[str, Any]:
    sandbox_observations = (
        [
            {
                "schema_version": "1",
                "producer": {
                    "schema_version": "1",
                    "producer_type": "tool",
                    "call_key": "b" * 64,
                },
                "step_index": 0,
                "tool_name": "local_tool",
                "provenance": "local",
                "sandbox_profile": "per_call",
                "sandbox_group_resource_id": "/subscriptions/test/resourceGroups/test",
                "sandbox_id": "sandbox-test",
                "sandbox_generation": 1,
                "state": sandbox_observation_state,
                "replaced_sandbox_id": None,
                "observed_at": "2026-01-01T00:00:01+00:00",
            }
        ]
        if state.include_sandbox_observations
        else []
    )
    return {
        "schema_version": "1",
        "run_id": state.run_id,
        "session_id": state.session_id,
        "model_mode": "foreground",
        "status": "Running",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:01+00:00",
        "expires_at": "2026-01-01T01:00:00+00:00",
        "committed_generation": 0,
        "configured_sandbox_group_resource_id": state.configured_sandbox_group,
        "sandbox_observations": sandbox_observations,
        "observation_health": {
            "schema_version": "1",
            "degraded": False,
            "reasons": [],
            "dropped_observations": 0,
            "reserved_observation_bytes": 0,
            "last_error_code": None,
        },
        "links": [
            {
                "schema_version": "1",
                "kind": "durable_task_scheduler",
                "available": True,
                "href": "https://example.test/durable-task",
                "unavailable_reason": None,
            },
            {
                "schema_version": "1",
                "kind": "application_insights",
                "available": False,
                "href": None,
                "unavailable_reason": "Application Insights is not configured.",
            },
        ],
    }


@contextlib.contextmanager
def _chat_page(state: _ChatState) -> Iterator[str]:
    directory = _ARTIFACT_ROOT / uuid4().hex
    directory.mkdir(parents=True)
    for source in ("app.js", "history.js", "index.html", "rendering.js", "styles.css"):
        shutil.copyfile(_ASSET_SOURCE / source, directory / source)
    handler = type(
        "FrontendHandler",
        (_ChatHandler,),
        {"directory": directory, "state": state},
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}{_SHELL_PATH}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        shutil.rmtree(directory, ignore_errors=True)
        with contextlib.suppress(OSError):
            _ARTIFACT_ROOT.rmdir()


class _RedirectCaptureState:
    def __init__(self) -> None:
        self.headers: list[dict[str, str]] = []
        self.received = threading.Event()


class _RedirectCaptureHandler(http.server.BaseHTTPRequestHandler):
    state: _RedirectCaptureState

    def log_message(self, _: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        self._record()

    def do_OPTIONS(self) -> None:
        self._record()

    def do_POST(self) -> None:
        self._record()

    def _record(self) -> None:
        self.state.headers.append(dict(self.headers))
        self.state.received.set()
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


@contextlib.contextmanager
def _redirect_target() -> Iterator[tuple[str, _RedirectCaptureState]]:
    state = _RedirectCaptureState()
    handler = type("RedirectCaptureHandler", (_RedirectCaptureHandler,), {"state": state})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}/redirected-result", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextlib.asynccontextmanager
async def _frontend_page(directory: Path) -> AsyncIterator[Any]:
    page_context = None
    page = None
    for attempt in range(2):
        page_context = chromium_page(directory / f"attempt-{attempt}")
        try:
            page = await page_context.__aenter__()
        except PermissionError:
            if attempt == 1:
                raise
            await asyncio.sleep(0.2)
        else:
            break
    if page_context is None or page is None:
        raise AssertionError("Chromium did not create a page.")
    try:
        yield page
    except BaseException as error:
        await page_context.__aexit__(type(error), error, error.__traceback__)
        raise
    else:
        await page_context.__aexit__(None, None, None)


async def _assert_multibyte_prompt_is_rejected(page: Any, state: _ChatState) -> None:
    await page.evaluate(
        "document.querySelector('#durable-chat-prompt').value = 'λ'.repeat(131073);"
        "document.querySelector('#durable-chat-composer').requestSubmit()",
    )
    await page.wait_for(
        "document.querySelector('#durable-chat-announcements').textContent"
        ".includes('256 KiB')",
    )
    assert not state.started.wait(timeout=0.2)


async def _create_empty_session(page: Any) -> str:
    await page.wait_for(
        "document.querySelector('#durable-chat-connection-summary').textContent"
        ".toLowerCase().includes('ready')",
    )
    await page.evaluate("document.querySelector('#durable-chat-new-session').click()")
    await page.wait_for(
        "document.querySelector('#durable-chat-session-id').textContent.trim()"
        " !== '—'",
    )
    await page.wait_for(
        "document.querySelector('#durable-chat-session-list').textContent"
        ".includes('No requests yet')",
    )
    return await page.evaluate(
        "document.querySelector('#durable-chat-session-id').textContent.trim()"
    )


async def _submit_function_key(page: Any, value: str) -> None:
    await page.evaluate(
        "document.querySelector('#durable-chat-connection-toggle').click();"
        "document.querySelector('#durable-chat-functions-key').value = "
        f"{json.dumps(value)};"
        "document.querySelector('#durable-chat-auth-form').requestSubmit()",
    )


async def _provide_function_key(page: Any, value: str) -> None:
    await _submit_function_key(page, value)
    await page.wait_for(
        "document.querySelector('#durable-chat-connection-summary').textContent"
        ".toLowerCase().includes('ready')",
    )


async def _assert_authoritative_completion(page: Any, *, reload: bool = False) -> None:
    if reload:
        await page.call("Page.reload", {"ignoreCache": True})
    await page.wait_for(
        "document.querySelector('#durable-chat-request-list').textContent"
        ".includes('Authoritative final answer.')",
    )
    await page.wait_for(
        "document.querySelector('#durable-chat-history-status').textContent"
        ".includes('Session history is saved in this browser.')",
    )
    assert await page.evaluate(
        "!document.querySelector('#durable-chat-lifecycle-progress').textContent"
        ".includes('Terminal event received')"
    )


@pytest.mark.asyncio
async def test_durable_chat_frontend_uses_bootstrap_history_and_safe_sse_rendering() -> None:
    state = _ChatState()
    browser_directory = _ARTIFACT_ROOT / f"browser-{uuid4().hex}"
    try:
        with _chat_page(state) as page_url:
            async with _frontend_page(browser_directory) as page:
                await page.navigate(page_url)
                await page.wait_for(
                    "document.querySelector('#durable-chat-connection-summary').textContent"
                    ".toLowerCase().includes('ready')",
                )
                assert await page.evaluate(
                    "document.querySelector('#durable-chat-prompt').maxLength"
                ) == 262144
                assert await page.evaluate(
                    "new URL(document.querySelector('link[rel=\"stylesheet\"]').href).pathname"
                ) == f"{_ASSET_PATH}styles.css"
                parser_result = await page.evaluate(
                    """(async () => {
                      const { DurableChatSseParser } = await import("./app.js");
                      const parser = new DurableChatSseParser();
                      const bytes = new TextEncoder().encode(
                        "id: 1\\nevent: message\\ndata: {\\\"text\\\":\\\"λ\\\"}\\n\\n",
                      );
                      const split = new TextDecoder().decode(bytes).indexOf("λ");
                      const byteSplit = new TextEncoder().encode(
                        new TextDecoder().decode(bytes).slice(0, split),
                      ).length + 1;
                      const first = parser.push(bytes.slice(0, byteSplit));
                      const second = parser.push(bytes.slice(byteSplit));
                      return [...first, ...second, ...parser.finish()];
                    })()""",
                )
                assert parser_result == [
                    {"data": '{"text":"λ"}', "event": "message", "id": "1"}
                ]

                session_id = await _create_empty_session(page)
                await _assert_multibyte_prompt_is_rejected(page, state)
                prompt = "Preserve this exact prompt.  \n"
                await page.evaluate(
                    "document.querySelector('#durable-chat-prompt').value = "
                    f"{json.dumps(prompt)};"
                    "document.querySelector('#durable-chat-composer').requestSubmit()",
                )
                assert state.started.wait(timeout=5)
                await page.wait_for(
                    "document.querySelector('#durable-chat-request-list').textContent"
                    ".includes('Visible <b>model</b> fragment λ. Unversioned event.')",
                )
                await page.wait_for(
                    "document.querySelector('#durable-chat-lifecycle-progress').textContent"
                    ".includes('Waiting for the authoritative status and result routes.')",
                )
                assert await page.evaluate(
                    "document.querySelector('#durable-chat-request-list b') === null"
                )
                assert await page.wait_for(
                    "document.querySelector('#durable-chat-sandbox-group').textContent"
                    ".includes('/subscriptions/test/resourceGroups/test')"
                    " && document.querySelector('#durable-chat-sandbox-group-label').textContent"
                    " === 'Sandbox Group'",
                )
                await page.call(
                    "Emulation.setDeviceMetricsOverride",
                    {
                        "width": 1440,
                        "height": 1000,
                        "deviceScaleFactor": 1,
                        "mobile": False,
                    },
                )
                conversation_width = await page.evaluate(
                    "document.querySelector('.durable-chat-conversation').getBoundingClientRect().width"
                )
                await page.evaluate(
                    "document.querySelector('#durable-chat-details-toggle').click()"
                )
                assert await page.evaluate(
                    "document.querySelector('#durable-chat-shell').dataset.detailsVisible"
                ) == "false"
                assert await page.evaluate(
                    "document.querySelector('.durable-chat-conversation').getBoundingClientRect().width"
                ) > conversation_width
                await page.evaluate(
                    "document.querySelector('[data-action=\"view-details\"]').click()"
                )
                assert await page.evaluate(
                    "document.querySelector('#durable-chat-shell').dataset.detailsVisible"
                ) == "true"

                state.completed = True
                await _assert_authoritative_completion(page)
                persisted = await page.evaluate(
                    """(async () => {
                      const { openDurableChatHistory } = await import("./history.js");
                      const history = await openDurableChatHistory({
                        namespace: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                      });
                      const sessions = await history.listSessions();
                      const requests = await history.listRequests(sessions.sessions[0].sessionId);
                      const request = requests.requests[0];
                      history.close();
                      return {
                        body: request.normalizedSubmission,
                        cursor: request.cursor,
                        finalResponse: request.projection.finalResponse,
                        serverPublishedRevision: request.projection.serverPublishedRevision,
                        terminal: request.terminal,
                      };
                    })()""",
                )
                await _assert_authoritative_completion(page, reload=True)
                await page.call(
                    "Emulation.setDeviceMetricsOverride",
                    {
                        "width": 390,
                        "height": 844,
                        "deviceScaleFactor": 1,
                        "mobile": False,
                    },
                )
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth"
                )
    finally:
        shutil.rmtree(browser_directory, ignore_errors=True)
        with contextlib.suppress(OSError):
            _ARTIFACT_ROOT.rmdir()

    assert session_id == state.session_id
    assert state.request_body == persisted["body"]
    assert state.request_body["prompt"] == prompt
    assert state.request_body["ui"] == {
        "schema_version": "1",
        "stream_response": True,
    }
    headers = {key.lower(): value for key, value in state.request_headers.items()}
    assert headers["idempotency-key"]
    assert "x-functions-key" not in headers
    assert persisted["finalResponse"] == "Authoritative final answer."
    assert persisted["cursor"] == {
        "position": 3,
        "revision": 3,
        "value": {
            "after_sequence": 3,
            "published_revision": 3,
            "run_id": state.run_id,
        },
    }
    assert persisted["serverPublishedRevision"] == 3
    assert persisted["terminal"] is True


@pytest.mark.asyncio
async def test_durable_chat_frontend_keeps_configured_sandbox_groups_per_request() -> None:
    state = _ChatState()
    state.bootstrap_sandbox_group = "/subscriptions/test/resourceGroups/bootstrap-current"
    state.configured_sandbox_group = "/subscriptions/test/resourceGroups/frozen-at-admission"
    state.include_sandbox_observations = False
    browser_directory = _ARTIFACT_ROOT / f"configured-group-{uuid4().hex}"
    try:
        with _chat_page(state) as page_url:
            async with _frontend_page(browser_directory) as page:
                await page.navigate(page_url)
                await page.wait_for(
                    "document.querySelector('#durable-chat-connection-summary').textContent"
                    ".toLowerCase().includes('ready')",
                )
                await page.wait_for(
                    "document.querySelector('#durable-chat-configured-sandbox-group').textContent"
                    f" === {json.dumps(state.bootstrap_sandbox_group)}",
                )
                await _create_empty_session(page)
                await page.evaluate(
                    "document.querySelector('#durable-chat-prompt').value = 'Show sandbox group';"
                    "document.querySelector('#durable-chat-composer').requestSubmit()",
                )
                assert state.started.wait(timeout=5)
                await page.wait_for(
                    "document.querySelector('#durable-chat-sandbox-group').textContent"
                    f" === {json.dumps(state.configured_sandbox_group)}",
                )
                assert await page.evaluate(
                    "document.querySelector('#durable-chat-sandbox-group-label').textContent"
                ) == "Configured Sandbox Group"

                state.bootstrap_sandbox_group = None
                await page.call("Page.reload", {"ignoreCache": True})
                await page.wait_for(
                    "document.querySelector('#durable-chat-connection-summary').textContent"
                    ".toLowerCase().includes('ready')",
                )
                assert await page.evaluate(
                    "document.querySelector('#durable-chat-configured-sandbox-group-context').hidden"
                )
                await page.wait_for(
                    "document.querySelector('#durable-chat-sandbox-group').textContent"
                    f" === {json.dumps(state.configured_sandbox_group)}",
                )
                assert await page.evaluate(
                    "document.querySelector('#durable-chat-sandbox-group-label').textContent"
                ) == "Configured Sandbox Group"
    finally:
        shutil.rmtree(browser_directory, ignore_errors=True)
        with contextlib.suppress(OSError):
            _ARTIFACT_ROOT.rmdir()


@pytest.mark.asyncio
async def test_durable_chat_frontend_fences_retry_drafts_and_explicit_cursor_rebases() -> None:
    state = _ChatState()
    browser_directory = _ARTIFACT_ROOT / f"reducer-{uuid4().hex}"
    try:
        with _chat_page(state) as page_url:
            async with _frontend_page(browser_directory) as page:
                await page.navigate(page_url)
                await page.wait_for(
                    "document.querySelector('#durable-chat-connection-summary').textContent"
                    ".toLowerCase().includes('ready')",
                )
                result = await page.evaluate(
                    """(async () => {
                      const {
                        DurableChatProtocolError,
                        deriveDurableChatShellBase,
                        normalizeDurableChatBootstrap,
                        reduceDurableChatProjection,
                      } = await import("./app.js");
                      const { getDurableChatShell } = await import("./rendering.js");
                      const { openDurableChatHistory } = await import("./history.js");
                      const producer = (epoch) => ({
                        schema_version: "1",
                        producer_type: "model",
                        step_index: 1,
                        observation_epoch: epoch,
                      });
                      const event = (event_type, extra = {}) => ({
                        schema_version: "1",
                        event_type,
                        run_id: "run-fenced",
                        session_id: "session-fenced",
                        observed_at: "2026-01-01T00:00:00+00:00",
                        ...extra,
                      });
                      let projection = {
                        producerEpochs: {},
                        status: "running",
                      };
                      projection = reduceDurableChatProjection(
                        projection,
                        event("assistant_text", { producer: producer(1), delta: "old" }),
                        { foregroundStreaming: true },
                      );
                      projection = reduceDurableChatProjection(
                        projection,
                        event("assistant_draft_replaced", {
                          previous_producer: producer(1),
                          producer: producer(2),
                        }),
                        { foregroundStreaming: true },
                      );
                      const afterReplacement = reduceDurableChatProjection(
                        projection,
                        event("assistant_text", { producer: producer(1), delta: " stale" }),
                        { foregroundStreaming: true },
                      );
                      const currentAttempt = reduceDurableChatProjection(
                        afterReplacement,
                        event("assistant_text", { producer: producer(2), delta: "new" }),
                        { foregroundStreaming: true },
                      );
                      const backgroundAttempt = reduceDurableChatProjection(
                        { producerEpochs: {}, status: "running" },
                        event("assistant_text", { producer: producer(1), delta: "hidden" }),
                        { foregroundStreaming: false },
                      );

                      const bootstrap = %BOOTSTRAP%;
                      const shellBaseWithoutSlash = deriveDurableChatShellBase(
                        "https://example.test/custom-prefix/experimental/durable-chat",
                      );
                      const shellBaseWithSlash = deriveDurableChatShellBase(
                        "https://example.test/custom-prefix/experimental/durable-chat/",
                      );
                      bootstrap.routes[0].path_template = "https://outside.example/start";
                      let unsafeRouteRejected = false;
                      try {
                        normalizeDurableChatBootstrap(bootstrap, location);
                      } catch (error) {
                        unsafeRouteRejected = error instanceof DurableChatProtocolError;
                      }
                      const invalidNamespace = %BOOTSTRAP%;
                      invalidNamespace.history_namespace = "not-an-owner-namespace";
                      let invalidNamespaceRejected = false;
                      try {
                        normalizeDurableChatBootstrap(invalidNamespace, location);
                      } catch (error) {
                        invalidNamespaceRejected = error instanceof DurableChatProtocolError;
                      }

                      const history = await openDurableChatHistory({
                        namespace: `fence-${crypto.randomUUID()}`,
                      });
                      const recorded = await history.recordSubmission({
                        sessionId: "session-fenced",
                        requestId: "request-fenced",
                        idempotencyKey: "request-key-fenced",
                        normalizedSubmission: { prompt: "Preserve me." },
                        projection: { state: "submitted" },
                      });
                      const high = await history.applyProjection({
                        sessionId: "session-fenced",
                        requestId: "request-fenced",
                        cursor: { position: 9, revision: 2 },
                        projection: { state: "high" },
                        expectedVersion: recorded.request.version,
                      });
                      let resetWithoutVersion = "";
                      try {
                        await history.applyProjection({
                          sessionId: "session-fenced",
                          requestId: "request-fenced",
                          cursor: { position: 1, revision: 1 },
                          projection: { state: "reset" },
                          transcript: [],
                          draft: null,
                          mode: "snapshot",
                          authoritativeReset: true,
                        });
                      } catch (error) {
                        resetWithoutVersion = error.code;
                      }
                      const reset = await history.applyProjection({
                        sessionId: "session-fenced",
                        requestId: "request-fenced",
                        cursor: { position: 1, revision: 1 },
                        projection: { state: "rebuilt" },
                        transcript: [],
                        draft: null,
                        mode: "snapshot",
                        authoritativeReset: true,
                        expectedVersion: high.request.version,
                      });
                      const staleAfterReset = await history.applyProjection({
                        sessionId: "session-fenced",
                        requestId: "request-fenced",
                        cursor: { position: 0, revision: 1 },
                        projection: { state: "stale" },
                      });
                      history.close();
                      const shell = getDurableChatShell();
                      shell.renderSessions({ historyMode: "volatile", state: "ready" });
                      return {
                        afterReplacementDraft: afterReplacement.draft ?? null,
                        backgroundDraft: backgroundAttempt.draft ?? null,
                        currentDraft: currentAttempt.draft?.text ?? "",
                        resetCursor: reset.request.cursor,
                        resetWithoutVersion,
                        staleAfterReset: staleAfterReset.disposition,
                        invalidNamespaceRejected,
                        shellBaseWithSlash,
                        shellBaseWithoutSlash,
                        unsafeRouteRejected,
                        volatileHistoryStatus: {
                          hidden: document.querySelector("#durable-chat-history-status").hidden,
                          text: document.querySelector("#durable-chat-history-status").textContent.trim(),
                        },
                      };
                    })()""".replace("%BOOTSTRAP%", json.dumps(_bootstrap())),
                )
    finally:
        shutil.rmtree(browser_directory, ignore_errors=True)
        with contextlib.suppress(OSError):
            _ARTIFACT_ROOT.rmdir()

    assert result["afterReplacementDraft"] is None
    assert result["currentDraft"] == "new"
    assert result["backgroundDraft"] is None
    assert result["unsafeRouteRejected"] is True
    assert result["invalidNamespaceRejected"] is True
    assert result["shellBaseWithoutSlash"] == (
        "https://example.test/custom-prefix/experimental/durable-chat/"
    )
    assert result["shellBaseWithSlash"] == (
        "https://example.test/custom-prefix/experimental/durable-chat/"
    )
    assert result["resetWithoutVersion"] == "invalid_record"
    assert result["resetCursor"] == {"position": 1, "revision": 1}
    assert result["staleAfterReset"] == "stale_cursor"
    assert result["volatileHistoryStatus"] == {
        "hidden": False,
        "text": (
            "Browser storage is unavailable. This session history will be lost "
            "when this page closes."
        ),
    }


@pytest.mark.asyncio
async def test_durable_chat_frontend_fences_late_producers_and_terminal_drafts() -> None:
    state = _ChatState()
    browser_directory = _ARTIFACT_ROOT / f"late-producers-{uuid4().hex}"
    try:
        with _chat_page(state) as page_url:
            async with _frontend_page(browser_directory) as page:
                await page.navigate(page_url)
                await page.wait_for(
                    "document.querySelector('#durable-chat-connection-summary').textContent"
                    ".toLowerCase().includes('ready')",
                )
                result = await page.evaluate(
                    """(async () => {
                      const { reduceDurableChatProjection } = await import(
                        document.querySelector('script[type="module"]').src,
                      );
                      const model = (step, epoch) => ({
                        producer_type: "model",
                        step_index: step,
                        observation_epoch: epoch,
                      });
                      const event = (event_type, extra = {}) => ({
                        event_type,
                        observed_at: "2026-01-01T00:00:00+00:00",
                        ...extra,
                      });
                      const tool = (state, updated_at) => event("tool", {
                        progress: {
                          producer: { producer_type: "tool", call_key: "c".repeat(64) },
                          state,
                          step_index: 2,
                          tool_name: "local_tool",
                          updated_at,
                        },
                      });
                      let projection = { producerEpochs: {}, status: "running" };
                      projection = reduceDurableChatProjection(
                        projection,
                        event("assistant_text", { producer: model(2, 1), delta: "new" }),
                        { foregroundStreaming: true },
                      );
                      projection = reduceDurableChatProjection(
                        projection,
                        event("assistant_text", { producer: model(1, 99), delta: " old step" }),
                        { foregroundStreaming: true },
                      );
                      projection = reduceDurableChatProjection(
                        projection,
                        event("assistant_text", { producer: model(2, 2), delta: "replacement" }),
                        { foregroundStreaming: true },
                      );
                      const beforeTerminal = reduceDurableChatProjection(
                        projection,
                        event("assistant_text", { producer: model(2, 1), delta: " old epoch" }),
                        { foregroundStreaming: true },
                      );
                      const terminal = reduceDurableChatProjection(
                        beforeTerminal,
                        event("terminal", { status: "Cancelled", result_available: false }),
                        { foregroundStreaming: true },
                      );
                      const afterTerminal = reduceDurableChatProjection(
                        terminal,
                        event("assistant_text", { producer: model(3, 1), delta: " late" }),
                        { foregroundStreaming: true },
                      );
                      const failedStatus = reduceDurableChatProjection(
                        {
                          draft: { producer: model(1, 1), text: "stale", updatedAt: "" },
                          producerEpochs: { 1: 1 },
                          status: "running",
                        },
                        event("run_status", { status: "Failed" }),
                        { foregroundStreaming: true },
                      );
                      const afterFailedStatus = reduceDurableChatProjection(
                        failedStatus,
                        event("assistant_text", { producer: model(2, 1), delta: " late failed" }),
                        { foregroundStreaming: true },
                      );
                      let toolProjection = reduceDurableChatProjection(
                        { producerEpochs: {}, status: "running" },
                        tool("started", "2026-01-01T00:00:00+00:00"),
                      );
                      toolProjection = reduceDurableChatProjection(
                        toolProjection,
                        tool("succeeded", "2026-01-01T00:00:01+00:00"),
                      );
                      const afterLateTool = reduceDurableChatProjection(
                        toolProjection,
                        tool("started", "2026-01-01T00:00:02+00:00"),
                      );
                      const afterTerminalTool = reduceDurableChatProjection(
                        terminal,
                        tool("started", "2026-01-01T00:00:03+00:00"),
                      );
                      return {
                        afterLateTool: afterLateTool.toolProgress[0].state,
                        afterFailedStatusDraft: afterFailedStatus.draft,
                        afterTerminalDraft: afterTerminal.draft,
                        afterTerminalToolCount: afterTerminalTool.toolProgress?.length ?? 0,
                        beforeTerminalDraft: beforeTerminal.draft?.text ?? "",
                      };
                    })()""",
                )
    finally:
        shutil.rmtree(browser_directory, ignore_errors=True)
        with contextlib.suppress(OSError):
            _ARTIFACT_ROOT.rmdir()

    assert result == {
        "afterLateTool": "succeeded",
        "afterFailedStatusDraft": None,
        "afterTerminalDraft": None,
        "afterTerminalToolCount": 0,
        "beforeTerminalDraft": "replacement",
    }


@pytest.mark.asyncio
async def test_durable_chat_frontend_submits_structured_human_input() -> None:
    state = _ChatState()
    state.human_wait = True
    browser_directory = _ARTIFACT_ROOT / f"human-{uuid4().hex}"
    try:
        with _chat_page(state) as page_url:
            async with _frontend_page(browser_directory) as page:
                await page.navigate(page_url)
                await page.wait_for(
                    "document.querySelector('#durable-chat-connection-summary').textContent"
                    ".toLowerCase().includes('ready')",
                )
                await page.evaluate(
                    "document.querySelector('#durable-chat-new-session').click()"
                )
                await page.wait_for(
                    "!document.querySelector('#durable-chat-prompt').disabled"
                )
                await page.evaluate(
                    "document.querySelector('#durable-chat-prompt').value = 'Need input';"
                    "document.querySelector('#durable-chat-composer').requestSubmit()",
                )
                assert state.started.wait(timeout=5)
                await page.wait_for(
                    "document.querySelector('#durable-chat-request-list').textContent"
                    ".includes('Provide a structured response.')",
                )
                await page.evaluate(
                    "document.querySelector('[data-human-input-form] textarea').value = "
                    "JSON.stringify({ token: 'schema-value' });"
                    "document.querySelector('[data-human-input-form]').requestSubmit()",
                )
                await page.wait_for(
                    "document.querySelector('[data-human-input-form]') === null",
                )
                lifecycle_text = await page.evaluate(
                    "document.querySelector('#durable-chat-lifecycle-progress').textContent"
                )
    finally:
        shutil.rmtree(browser_directory, ignore_errors=True)
        with contextlib.suppress(OSError):
            _ARTIFACT_ROOT.rmdir()

    assert state.human_answer == {"token": "schema-value"}
    assert "A response was submitted." in lifecycle_text


@pytest.mark.asyncio
async def test_durable_chat_frontend_retries_unready_invalid_and_failed_results() -> None:
    state = _ChatState()
    state.completed = True
    state.result_mode = "not_ready"
    browser_directory = _ARTIFACT_ROOT / f"result-retry-{uuid4().hex}"
    try:
        with _chat_page(state) as page_url:
            async with _frontend_page(browser_directory) as page:
                await page.navigate(page_url)
                await _create_empty_session(page)
                await page.evaluate(
                    "document.querySelector('#durable-chat-prompt').value = 'Retrieve final result';"
                    "document.querySelector('#durable-chat-composer').requestSubmit()",
                )
                assert state.started.wait(timeout=5)
                await page.wait_for(
                    "document.querySelector('#durable-chat-request-list').textContent"
                    ".includes('The final response is being retrieved.')",
                )

                state.result_mode = "malformed"
                await page.call("Page.reload", {"ignoreCache": True})
                await page.wait_for(
                    "document.querySelector('#durable-chat-request-list').textContent"
                    ".includes('The completed response did not match the Durable Chat protocol.')",
                )

                state.result_mode = "server_error"
                await page.call("Page.reload", {"ignoreCache": True})
                await page.wait_for(
                    "document.querySelector('#durable-chat-request-list').textContent"
                    ".includes('The completed response could not be retrieved.')",
                )

                state.result_mode = "automatic"
                await _assert_authoritative_completion(page, reload=True)
    finally:
        shutil.rmtree(browser_directory, ignore_errors=True)
        with contextlib.suppress(OSError):
            _ARTIFACT_ROOT.rmdir()

    assert state.result_attempts >= 4


@pytest.mark.asyncio
async def test_durable_chat_frontend_surfaces_result_authentication_failure() -> None:
    state = _ChatState()
    state.completed = True
    state.result_mode = "unauthorized"
    browser_directory = _ARTIFACT_ROOT / f"result-auth-{uuid4().hex}"
    try:
        with _chat_page(state) as page_url:
            async with _frontend_page(browser_directory) as page:
                await page.navigate(page_url)
                await _provide_function_key(page, "synthetic-auth-key")
                await _create_empty_session(page)
                await page.evaluate(
                    "document.querySelector('#durable-chat-prompt').value = 'Retrieve protected result';"
                    "document.querySelector('#durable-chat-composer').requestSubmit()",
                )
                assert state.started.wait(timeout=5)
                await page.wait_for(
                    "document.querySelector('#durable-chat-request-list').textContent"
                    ".includes('Authentication is required to retrieve the completed response.')",
                )
                connection_message = await page.evaluate(
                    "document.querySelector('#durable-chat-connection-message').textContent"
                )
                request_text = await page.evaluate(
                    "document.querySelector('#durable-chat-request-list').textContent"
                )
    finally:
        shutil.rmtree(browser_directory, ignore_errors=True)
        with contextlib.suppress(OSError):
            _ARTIFACT_ROOT.rmdir()

    result_headers = {key.lower(): value for key, value in state.result_headers.items()}
    assert result_headers["x-functions-key"] == "synthetic-auth-key"
    assert "Authentication is required." in connection_message
    assert "Authoritative final answer." not in request_text


@pytest.mark.asyncio
async def test_durable_chat_frontend_rejects_credential_bearing_redirects() -> None:
    state = _ChatState()
    state.completed = True
    browser_directory = _ARTIFACT_ROOT / f"redirect-{uuid4().hex}"
    try:
        with _redirect_target() as (redirect_url, target):
            state.result_redirect_url = redirect_url
            with _chat_page(state) as page_url:
                async with _frontend_page(browser_directory) as page:
                    await page.navigate(page_url)
                    await _provide_function_key(page, "synthetic-redirect-key")
                    await _create_empty_session(page)
                    await page.evaluate(
                        "document.querySelector('#durable-chat-prompt').value = 'Do not forward this key';"
                        "document.querySelector('#durable-chat-composer').requestSubmit()",
                    )
                    assert state.started.wait(timeout=5)
                    await page.wait_for(
                        "document.querySelector('#durable-chat-request-list').textContent"
                        ".includes('The completed response could not be retrieved.')",
                    )
                    await asyncio.sleep(0.2)
    finally:
        shutil.rmtree(browser_directory, ignore_errors=True)
        with contextlib.suppress(OSError):
            _ARTIFACT_ROOT.rmdir()

    result_headers = {key.lower(): value for key, value in state.result_headers.items()}
    assert result_headers["x-functions-key"] == "synthetic-redirect-key"
    assert not target.received.is_set()
    assert target.headers == []


@pytest.mark.asyncio
async def test_durable_chat_frontend_rejects_bootstrap_key_redirects() -> None:
    state = _ChatState()
    browser_directory = _ARTIFACT_ROOT / f"bootstrap-redirect-{uuid4().hex}"
    try:
        with _redirect_target() as (redirect_url, target), _chat_page(state) as page_url:
            async with _frontend_page(browser_directory) as page:
                await page.navigate(page_url)
                await page.wait_for(
                    "document.querySelector('#durable-chat-connection-summary').textContent"
                    ".toLowerCase().includes('ready')",
                )
                state.config_redirect_url = redirect_url
                await _submit_function_key(page, "synthetic-bootstrap-key")
                await page.wait_for(
                    "document.querySelector('#durable-chat-connection-message').textContent"
                    ".includes('The Function App connection could not be established.')",
                )
                await asyncio.sleep(0.2)
    finally:
        shutil.rmtree(browser_directory, ignore_errors=True)
        with contextlib.suppress(OSError):
            _ARTIFACT_ROOT.rmdir()

    config_headers = {key.lower(): value for key, value in state.config_headers.items()}
    assert config_headers["x-functions-key"] == "synthetic-bootstrap-key"
    assert not target.received.is_set()
    assert target.headers == []


@pytest.mark.asyncio
async def test_durable_chat_frontend_preserves_expired_request_diagnostics() -> None:
    state = _ChatState()
    browser_directory = _ARTIFACT_ROOT / f"diagnostics-expiry-{uuid4().hex}"
    try:
        with _chat_page(state) as page_url:
            async with _frontend_page(browser_directory) as page:
                await page.navigate(page_url)
                await _create_empty_session(page)
                await page.evaluate(
                    "document.querySelector('#durable-chat-prompt').value = 'Keep diagnostics';"
                    "document.querySelector('#durable-chat-composer').requestSubmit()",
                )
                assert state.started.wait(timeout=5)
                await page.wait_for(
                    "document.querySelector('#durable-chat-sandbox-history').textContent"
                    ".includes('sandbox-test')",
                )
                state.diagnostics_expired = True
                await page.evaluate(
                    """const request = document.querySelector("[data-request-card]");
                    document.querySelector("#durable-chat-shell").dispatchEvent(
                      new CustomEvent("durable-chat:request-selected", {
                        bubbles: true,
                        detail: { requestId: request.dataset.requestId },
                      }),
                    );""",
                )
                await page.wait_for(
                    "document.querySelector('#durable-chat-details-panel').textContent"
                    ".includes('Previously recorded request diagnostics remain available.')",
                )
                history_text = await page.evaluate(
                    "document.querySelector('#durable-chat-sandbox-history').textContent"
                )
                dts_href = await page.evaluate(
                    "document.querySelector('#durable-chat-open-dts').href"
                )
    finally:
        shutil.rmtree(browser_directory, ignore_errors=True)
        with contextlib.suppress(OSError):
            _ARTIFACT_ROOT.rmdir()

    assert state.diagnostics_requests >= 2
    assert "sandbox-test" in history_text
    assert dts_href == "https://example.test/durable-task"


@pytest.mark.asyncio
async def test_durable_chat_frontend_coalesces_terminal_diagnostics_after_inflight_request() -> None:
    state = _ChatState()
    initial_started = threading.Event()
    initial_release = threading.Event()
    final_started = threading.Event()
    final_release = threading.Event()
    state.diagnostics_gates = {
        1: (initial_started, initial_release),
        2: (final_started, final_release),
    }
    state.diagnostics_observation_states = {
        1: "retained_idle",
        2: "confirmed_deleted",
    }
    state.cancel_sets_status = True
    browser_directory = _ARTIFACT_ROOT / f"terminal-diagnostics-{uuid4().hex}"
    terminal_status = ""
    sandbox_history = ""
    try:
        with _chat_page(state) as page_url:
            async with _frontend_page(browser_directory) as page:
                await page.navigate(page_url)
                await _create_empty_session(page)
                await page.evaluate(
                    "document.querySelector('#durable-chat-prompt').value = "
                    "'Refresh terminal diagnostics';"
                    "document.querySelector('#durable-chat-composer').requestSubmit()",
                )
                assert state.started.wait(timeout=5)
                assert await asyncio.to_thread(initial_started.wait, 5)

                await page.wait_for(
                    "!document.querySelector('#durable-chat-cancel-request').disabled",
                )
                await page.evaluate(
                    "document.querySelector('#durable-chat-cancel-request').click()",
                )
                await page.wait_for(
                    "document.querySelector('#durable-chat-details-status').textContent"
                    ".trim() === 'Cancelled'",
                )

                initial_release.set()
                assert await asyncio.to_thread(final_started.wait, 5)
                assert await page.evaluate(
                    "!document.querySelector('#durable-chat-sandbox-history').textContent"
                    ".toLowerCase().includes('retained idle')",
                )
                assert await page.evaluate(
                    "document.querySelector('#durable-chat-details-status').textContent"
                    ".trim() === 'Cancelled'",
                )

                final_release.set()
                await page.wait_for(
                    "document.querySelector('#durable-chat-sandbox-history').textContent"
                    ".toLowerCase().includes('confirmed deleted')",
                )
                await asyncio.sleep(0.5)
                terminal_status = await page.evaluate(
                    "document.querySelector('#durable-chat-details-status').textContent.trim()",
                )
                sandbox_history = await page.evaluate(
                    "document.querySelector('#durable-chat-sandbox-history').textContent",
                )
    finally:
        initial_release.set()
        final_release.set()
        shutil.rmtree(browser_directory, ignore_errors=True)
        with contextlib.suppress(OSError):
            _ARTIFACT_ROOT.rmdir()

    assert state.diagnostics_requests == 2
    assert terminal_status == "Cancelled"
    assert "retained idle" not in sandbox_history.lower()
    assert "confirmed deleted" in sandbox_history.lower()


@pytest.mark.asyncio
async def test_durable_chat_frontend_ignores_diagnostics_for_cleared_context() -> None:
    state = _ChatState()
    browser_directory = _ARTIFACT_ROOT / f"cleared-diagnostics-{uuid4().hex}"
    try:
        with _chat_page(state) as page_url:
            async with _frontend_page(browser_directory) as page:
                await page.navigate(page_url)
                await page.wait_for(
                    "document.querySelector('#durable-chat-connection-summary').textContent"
                    ".toLowerCase().includes('ready')",
                )
                result = await page.evaluate(
                    """(async () => {
                      const { DurableChatApplication } = await import("./app.js");
                      let resolveDiagnostics;
                      const diagnostics = new Promise((resolve) => {
                        resolveDiagnostics = resolve;
                      });
                      let renderCalls = 0;
                      const shell = {
                        renderRequests() { renderCalls += 1; },
                        renderSession() { renderCalls += 1; },
                        renderSessions() { renderCalls += 1; },
                        setSandboxProfiles() { renderCalls += 1; },
                      };
                      const application = new DurableChatApplication(shell, {
                        fetch: async () => { throw new Error("unexpected fetch"); },
                        location,
                      });
                      const record = {
                        requestId: "request-stale",
                        sessionId: "session-stale",
                        terminal: false,
                        version: 1,
                      };
                      const context = {
                        cursor: null,
                        diagnosticsAttempts: 0,
                        diagnosticsInFlight: false,
                        diagnosticsRetryTimer: null,
                        finalDiagnosticsRefreshRequested: false,
                        finalDiagnosticsRefreshStarted: false,
                        mutations: Promise.resolve(),
                        persisted: true,
                        projection: {
                          diagnostics: null,
                          observationHealth: {},
                          sandboxObservations: [],
                        },
                        record,
                        runId: "run-stale",
                        sessionId: "session-stale",
                        stopped: false,
                      };
                      application.contexts.set(
                        `session-stale${String.fromCharCode(0)}request-stale`,
                        context,
                      );
                      application.requests.set(
                        "session-stale",
                        new Map([["request-stale", record]]),
                      );
                      application.history = { close() {} };
                      application.transport = {
                        diagnostics: () => diagnostics,
                      };

                      const pending = application._loadDiagnostics(context);
                      await Promise.resolve();
                      const inFlightBeforeClear = context.diagnosticsInFlight;
                      application._clearLoadedState();
                      resolveDiagnostics({
                        payload: {
                          links: [],
                          observation_health: {},
                          run_id: "run-stale",
                          sandbox_observations: [],
                          session_id: "session-stale",
                          status: "Running",
                        },
                      });
                      await pending;
                      return {
                        diagnostics: context.projection.diagnostics ?? null,
                        inFlightBeforeClear,
                        renderCalls,
                      };
                    })()""",
                )
    finally:
        shutil.rmtree(browser_directory, ignore_errors=True)
        with contextlib.suppress(OSError):
            _ARTIFACT_ROOT.rmdir()

    assert result == {
        "diagnostics": None,
        "inFlightBeforeClear": True,
        "renderCalls": 0,
    }


@pytest.mark.asyncio
async def test_durable_chat_frontend_keeps_older_request_selected_during_updates_and_reload() -> None:
    state = _ChatState()
    browser_directory = _ARTIFACT_ROOT / f"request-selection-{uuid4().hex}"
    selection: dict[str, str] = {}
    try:
        with _chat_page(state) as page_url:
            async with _frontend_page(browser_directory) as page:
                await page.navigate(page_url)
                await page.wait_for(
                    "document.querySelector('#durable-chat-connection-summary').textContent"
                    ".toLowerCase().includes('ready')",
                )
                selection = await page.evaluate(
                    """(async () => {
                      const { DurableChatApplication } = await import("./app.js");
                      const { getDurableChatShell } = await import("./rendering.js");
                      const namespace = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
                      const sessionId = "session-request-selection";
                      const firstRequestId = "request-first";
                      const secondRequestId = "request-second";
                      const projection = (runId, status, sandboxId) => ({
                        diagnostics: null,
                        foregroundStreaming: true,
                        observationHealth: { degraded: false, reasons: [] },
                        runId,
                        sandboxObservations: [{
                          observed_at: "2026-01-01T00:00:00+00:00",
                          sandbox_id: sandboxId,
                          state: "confirmed_deleted",
                        }],
                        sessionId,
                        status,
                        terminalConfirmed: status === "completed",
                      });
                      let session = {
                        createdAt: "2026-01-01T00:00:00.000Z",
                        sessionId,
                        title: "Selection test",
                        updatedAt: "2026-01-01T00:00:00.000Z",
                        version: 3,
                      };
                      const records = new Map([
                        [firstRequestId, {
                          createdAt: "2026-01-01T00:00:01.000Z",
                          cursor: { position: 1, revision: 1 },
                          draft: null,
                          idempotencyKey: "key-first",
                          normalizedSubmission: { prompt: "First request" },
                          projection: projection("run-first", "completed", "sandbox-first"),
                          requestId: firstRequestId,
                          sessionId,
                          terminal: true,
                          transcript: [],
                          updatedAt: "2026-01-01T00:00:01.000Z",
                          version: 2,
                        }],
                        [secondRequestId, {
                          createdAt: "2026-01-01T00:00:02.000Z",
                          cursor: null,
                          draft: null,
                          idempotencyKey: "key-second",
                          normalizedSubmission: { prompt: "Second request" },
                          projection: projection("run-second", "running", "sandbox-second"),
                          requestId: secondRequestId,
                          sessionId,
                          terminal: false,
                          transcript: [],
                          updatedAt: "2026-01-01T00:00:02.000Z",
                          version: 1,
                        }],
                      ]);
                      const copy = (value) => structuredClone(value);
                      const makeHistory = () => ({
                        mode: "persistent",
                        namespace,
                        close() {},
                        async applyProjection(input) {
                          const record = records.get(input.requestId);
                          const next = {
                            ...record,
                            cursor: copy(input.cursor),
                            projection: copy(input.projection),
                            terminal: record.terminal || input.terminal === true,
                            updatedAt: "2026-01-01T00:00:03.000Z",
                            version: record.version + 1,
                          };
                          if (Object.hasOwn(input, "draft")) {
                            next.draft = copy(input.draft);
                          }
                          if (Object.hasOwn(input, "transcript")) {
                            next.transcript = copy(input.transcript);
                          }
                          records.set(input.requestId, next);
                          session = {
                            ...session,
                            updatedAt: next.updatedAt,
                            version: session.version + 1,
                          };
                          return {
                            disposition: "applied",
                            request: copy(next),
                            session: copy(session),
                          };
                        },
                        async listRequests(requestedSessionId) {
                          return {
                            mode: "persistent",
                            requests: Array.from(records.values())
                              .filter((record) => record.sessionId === requestedSessionId)
                              .sort((left, right) =>
                                left.createdAt.localeCompare(right.createdAt)
                                || left.requestId.localeCompare(right.requestId),
                              )
                              .map(copy),
                          };
                        },
                        async listSessions() {
                          return { mode: "persistent", sessions: [copy(session)] };
                        },
                      });
                      const bootstrap = {
                        defaultSandboxProfile: "per_call",
                        foregroundStreamingAvailable: true,
                        historyNamespace: namespace,
                        supportedSandboxProfiles: ["per_call"],
                      };
                      const application = new DurableChatApplication(getDurableChatShell(), {
                        fetch: async () => { throw new Error("unexpected fetch"); },
                        location,
                      });
                      application.bootstrap = bootstrap;
                      application.history = makeHistory();
                      application._connected = true;
                      application.sessions.set(sessionId, copy(session));
                      const firstContext = application._createContext(copy(records.get(firstRequestId)));
                      const secondContext = application._createContext(copy(records.get(secondRequestId)));
                      application._registerContext(firstContext);
                      application._registerContext(secondContext);
                      application.selectedSessionId = sessionId;
                      application._renderAll();
                      application._bindShellEvents();

                      const firstDetails = Array.from(
                        document.querySelectorAll("#durable-chat-request-list button"),
                      ).find((button) => /details/i.test(button.textContent));
                      if (!firstDetails) {
                        throw new Error("The first request has no View details button.");
                      }
                      const firstButtonRequestId = firstDetails.dataset.requestId;
                      firstDetails.click();
                      await Promise.resolve();
                      const selectedAfterClick = document.querySelector(
                        "#durable-chat-run-id",
                      ).textContent.trim();

                      await application._mutateContext(secondContext, (next) => {
                        next.phase = "tool";
                      });
                      const selectedAfterActiveUpdate = document.querySelector(
                        "#durable-chat-run-id",
                      ).textContent.trim();
                      await application._mutateContext(secondContext, (next) => {
                        next.status = "completed";
                        next.terminalConfirmed = true;
                      }, { terminal: true });
                      const selectedAfterTerminalUpdate = document.querySelector(
                        "#durable-chat-run-id",
                      ).textContent.trim();
                      application.destroy();

                      const reloaded = new DurableChatApplication(getDurableChatShell(), {
                        fetch: async () => { throw new Error("unexpected fetch"); },
                        location,
                      });
                      reloaded.bootstrap = bootstrap;
                      reloaded.history = makeHistory();
                      reloaded._connected = true;
                      await reloaded._loadHistory();
                      reloaded._renderAll();
                      const selectedAfterReload = document.querySelector(
                        "#durable-chat-run-id",
                      ).textContent.trim();
                      reloaded.destroy();
                      return {
                        firstButtonRequestId,
                        selectedAfterActiveUpdate,
                        selectedAfterClick,
                        selectedAfterReload,
                        selectedAfterTerminalUpdate,
                      };
                    })()""",
                )
    finally:
        shutil.rmtree(browser_directory, ignore_errors=True)
        with contextlib.suppress(OSError):
            _ARTIFACT_ROOT.rmdir()

    assert selection == {
        "firstButtonRequestId": "request-first",
        "selectedAfterActiveUpdate": "run-first",
        "selectedAfterClick": "run-first",
        "selectedAfterReload": "run-first",
        "selectedAfterTerminalUpdate": "run-first",
    }
