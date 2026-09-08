from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from azure.containerapps.sandbox import SandboxGroupClient
from azure.identity import AzureCliCredential

_BASE_URL = "https://func-durable-loop-0904.azurewebsites.net"
_SANDBOX_ENDPOINT = "https://management.eastus2.azuredevcompute.io"
_SUBSCRIPTION_ID = "2ac40cf6-193e-4a44-a55b-d7a17bdd5aee"
_RESOURCE_GROUP = "larohra-durable-agent-loop"
_SANDBOX_GROUP = "sbg-durable-loop-0904"
_RUN_ID = re.compile(r"run-[0-9a-f]{32}")
_SESSION_ID = re.compile(r"[0-9a-f]{32}")
_HUMAN_ID = re.compile(r"human-[0-9]+-[0-9a-f]{16}")
_MAX_BODY_BYTES = 32 * 1024
_MAX_RESPONSE_BYTES = 256 * 1024

_SCENARIOS = {
    "retained_create": {
        "prompt": (
            "Exercise retained workspace continuity. Use write_file to write the exact "
            "text LIVE-DEMO-MARKER-0908 to leadership-demo-marker.txt, then use "
            "read_file to read the same file. Return only the marker and the number "
            "of successful tool calls."
        ),
        "sandbox_profile": "retained_session",
    },
    "retained_reuse": {
        "prompt": (
            "Use read_file once to read leadership-demo-marker.txt from the retained "
            "workspace. Return only the exact file text."
        ),
        "sandbox_profile": "retained_session",
    },
    "fault_recovery": {
        "prompt": (
            "Run the adaptive_probe qualification from step 0, follow every returned "
            "next_action exactly, and stop at adaptive-probe-terminal-5. Return only "
            "the terminal marker and logical tool-call count."
        ),
        "sandbox_profile": "per_call",
        "fault_profile": "tool_activity_ack_loss_once",
    },
    "hitl": {
        "prompt": (
            "Exercise durable human clarification. Ask which bounded demo label to "
            "use, with exactly two choices Alpha and Beta, by calling "
            "request_human_input as the only tool call. After the answer, return only "
            "HITL-RESUMED followed by the selected label."
        ),
        "sandbox_profile": "per_call",
    },
}


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, object]:
    raw_length = handler.headers.get("Content-Length", "0")
    try:
        length = int(raw_length)
    except ValueError as exc:
        raise ValueError("invalid_content_length") from exc
    if not 0 <= length <= _MAX_BODY_BYTES:
        raise ValueError("request_too_large")
    raw = handler.rfile.read(length)
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_json") from exc
    if not isinstance(payload, dict):
        raise ValueError("body_must_be_object")
    return payload


def _function_request(
    method: str,
    path: str,
    *,
    payload: dict[str, object] | None = None,
    idempotency_key: str | None = None,
) -> tuple[int, dict[str, Any]]:
    key = os.environ.get("DURABLE_LOOP_FUNCTION_KEY")
    if not key:
        raise RuntimeError("DURABLE_LOOP_FUNCTION_KEY is required")
    headers = {
        "Accept": "application/json",
        "Cache-Control": "no-store",
        "x-functions-key": key,
    }
    body = None
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    request = Request(f"{_BASE_URL}{path}", data=body, headers=headers, method=method)
    try:
        opened = urlopen(request, timeout=120)
    except HTTPError as error:
        opened = error
    with opened as response:
        status = response.status
        content_type = response.headers.get_content_type()
        content = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(content) > _MAX_RESPONSE_BYTES:
        raise RuntimeError("function_response_too_large")
    if content_type != "application/json":
        raise RuntimeError(f"function_response_not_json:{status}")
    document = json.loads(content)
    if not isinstance(document, dict):
        raise RuntimeError("function_response_not_object")
    return status, document


def _safe_run_status(document: dict[str, Any]) -> dict[str, object]:
    human = document.get("human_input")
    safe_human: dict[str, object] | None = None
    if isinstance(human, dict):
        safe_human = {
            key: human[key]
            for key in (
                "request_id",
                "expires_at",
                "allow_free_text",
                "choice_count",
                "schema_present",
            )
            if key in human
        }
    safe = {
        key: document[key]
        for key in (
            "run_id",
            "session_id",
            "status",
            "phase",
            "model_steps",
            "tool_calls",
            "human_waits",
            "step_index",
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "external_content_bytes",
            "parked_seconds",
            "error",
            "disposition",
            "possibly_committed",
        )
        if key in document
    }
    if safe_human:
        safe["human_input"] = safe_human
    return safe


def _sandbox_inventory() -> dict[str, object]:
    client = SandboxGroupClient(
        _SANDBOX_ENDPOINT,
        AzureCliCredential(),
        subscription_id=_SUBSCRIPTION_ID,
        resource_group=_RESOURCE_GROUP,
        sandbox_group=_SANDBOX_GROUP,
    )
    try:
        sandboxes = list(client.list_sandboxes())
    finally:
        client.close()
    return {
        "count": len(sandboxes),
        "items": [
            {
                "alias": f"sandbox-{hashlib.sha256(item.id.encode()).hexdigest()[:8]}",
                "state": item.state,
                "created_at": item.created_at,
                "auto_suspend_seconds": (
                    item.lifecycle.auto_suspend.interval
                    if item.lifecycle and item.lifecycle.auto_suspend
                    else None
                ),
                "auto_delete_seconds": (
                    item.lifecycle.auto_delete.delete_interval_seconds
                    if item.lifecycle and item.lifecycle.auto_delete
                    else None
                ),
            }
            for item in sandboxes
        ],
    }


class ControlRoomHandler(BaseHTTPRequestHandler):
    server_version = "DurableLoopControlRoom/1"

    def do_GET(self) -> None:
        if self.path == "/":
            self._send_file(Path(__file__).with_name("control-room.html"))
            return
        if self.path == "/api/sandboxes":
            self._send_json(HTTPStatus.OK, _sandbox_inventory())
            return
        if self.path.startswith("/api/run/"):
            run_id = self.path.removeprefix("/api/run/")
            if _RUN_ID.fullmatch(run_id) is None:
                self._send_error(HTTPStatus.BAD_REQUEST, "invalid_run_id")
                return
            status, document = _function_request(
                "GET",
                f"/api/experimental/durable-agent-runs/{run_id}",
            )
            self._send_json(status, _safe_run_status(document))
            return
        self._send_error(HTTPStatus.NOT_FOUND, "not_found")

    def do_POST(self) -> None:
        try:
            payload = _read_json(self)
            if self.path == "/api/start":
                self._start(payload)
                return
            if self.path == "/api/human-answer":
                self._answer(payload)
                return
            self._send_error(HTTPStatus.NOT_FOUND, "not_found")
        except ValueError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        except RuntimeError as exc:
            self._send_error(HTTPStatus.BAD_GATEWAY, str(exc))

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _start(self, payload: dict[str, object]) -> None:
        scenario_name = payload.get("scenario")
        if not isinstance(scenario_name, str) or scenario_name not in _SCENARIOS:
            raise ValueError("invalid_scenario")
        scenario = _SCENARIOS[scenario_name]
        request_body: dict[str, object] = {
            **scenario,
            "request_id": f"demo-{scenario_name}-{uuid.uuid4().hex[:12]}",
        }
        if scenario_name == "retained_reuse":
            session_id = payload.get("session_id")
            if not isinstance(session_id, str) or _SESSION_ID.fullmatch(session_id) is None:
                raise ValueError("invalid_session_id")
            request_body["session_id"] = session_id
        status, document = _function_request(
            "POST",
            "/api/experimental/durable-agent-runs",
            payload=request_body,
        )
        safe = {
            key: document[key]
            for key in ("run_id", "session_id", "status")
            if key in document
        }
        safe["scenario"] = scenario_name
        self._send_json(status, safe)

    def _answer(self, payload: dict[str, object]) -> None:
        run_id = payload.get("run_id")
        request_id = payload.get("request_id")
        answer = payload.get("answer")
        if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
            raise ValueError("invalid_run_id")
        if not isinstance(request_id, str) or _HUMAN_ID.fullmatch(request_id) is None:
            raise ValueError("invalid_human_request_id")
        if answer not in {"Alpha", "Beta"}:
            raise ValueError("invalid_answer")
        status, document = _function_request(
            "POST",
            f"/api/experimental/durable-agent-runs/{run_id}/input/{request_id}",
            payload={"answer": answer},
            idempotency_key=f"demo-answer-{uuid.uuid4().hex}",
        )
        self._send_json(status, _safe_run_status(document))

    def _send_file(self, path: Path) -> None:
        content = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(content)

    def _send_json(self, status: int | HTTPStatus, payload: dict[str, object]) -> None:
        content = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def _send_error(self, status: HTTPStatus, code: str) -> None:
        self._send_json(status, {"error": code})


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the live Durable Agent Loop demo control room.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not os.environ.get("DURABLE_LOOP_FUNCTION_KEY"):
        raise RuntimeError("DURABLE_LOOP_FUNCTION_KEY is required")
    server = ThreadingHTTPServer((args.host, args.port), ControlRoomHandler)
    print(f"http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
