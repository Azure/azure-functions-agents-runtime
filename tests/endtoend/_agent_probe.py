"""Minimal stdlib HTTP helpers for driving a booted agent app in E2E tests.

Kept dependency-free (``urllib``) so the agentic tests don't pull in an extra
HTTP client. Targets the builtin chat endpoint that samples expose when
``builtin_endpoints`` is enabled (``POST /agents/{slug}/chat``).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ChatReply:
    """Result of a single chat invocation."""

    status: int
    body: dict[str, Any]
    session_id: str | None

    @property
    def response_text(self) -> str:
        """The agent's textual reply, or ``""`` when absent."""
        val = self.body.get("response")
        return val if isinstance(val, str) else ""


def wait_until_responsive(base_url: str, *, timeout: float = 60.0, poll: float = 0.5) -> None:
    """Block until the host answers admin requests (any non-5xx) or time out."""
    deadline = time.monotonic() + timeout
    url = f"{base_url}/admin/functions"
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status < 500:
                    return
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                return
            last_err = exc
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_err = exc
        time.sleep(poll)
    raise TimeoutError(f"host at {base_url} not responsive after {timeout:.0f}s: {last_err}")


def chat(
    base_url: str,
    slug: str,
    prompt: str,
    *,
    session_id: str | None = None,
    timeout: float = 120.0,
) -> ChatReply:
    """POST a prompt to ``/agents/{slug}/chat`` and return the parsed reply."""
    url = f"{base_url}/agents/{slug}/chat"
    payload = json.dumps({"prompt": prompt}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if session_id:
        headers["x-ms-session-id"] = session_id
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

    resp_sid: str | None = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            status = resp.status
            resp_sid = resp.headers.get("x-ms-session-id")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        status = exc.code
        resp_sid = exc.headers.get("x-ms-session-id") if exc.headers else None

    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = {"raw": raw}
    body: dict[str, Any] = parsed if isinstance(parsed, dict) else {"raw": parsed}

    return ChatReply(
        status=status,
        body=body,
        session_id=resp_sid or (body.get("session_id") if isinstance(body.get("session_id"), str) else None),
    )
