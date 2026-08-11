"""Send sample workflow-start messages to an already running Functions host."""

from __future__ import annotations

import argparse
import json
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from verify import INCIDENT_PROMPT, RELEASE_PROMPT, extract_workflow_id

type Pipeline = Literal["incident", "release"]

PIPELINES: dict[Pipeline, tuple[str, str]] = {
    "incident": ("incident_commander", INCIDENT_PROMPT),
    "release": ("release_manager", RELEASE_PROMPT),
}
DEFAULT_SESSION_ID = "engineering-ops-manual-session"


def build_chat_request(
    pipeline: Pipeline,
    *,
    base_url: str,
    session_id: str,
) -> Request:
    """Build one owner-specific chat request."""
    owner, prompt = PIPELINES[pipeline]
    return Request(
        f"{base_url.rstrip('/')}/agents/{owner}/chat",
        data=json.dumps({"prompt": prompt}).encode(),
        headers={
            "Content-Type": "application/json",
            "x-ms-session-id": session_id,
        },
        method="POST",
    )


def send_pipeline(
    pipeline: Pipeline,
    *,
    base_url: str,
    session_id: str,
    timeout: float,
) -> str:
    """Send one workflow-start message and return its workflow ID."""
    request = build_chat_request(
        pipeline,
        base_url=base_url,
        session_id=session_id,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
    except HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"{pipeline} chat returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(
            f"could not reach {request.full_url}; start `func` and try again: {exc.reason}"
        ) from exc

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{pipeline} chat returned invalid JSON") from exc
    return extract_workflow_id(payload)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send workflow-start messages to a manually started sample host."
    )
    parser.add_argument(
        "pipeline",
        choices=("incident", "release", "both"),
        help="Pipeline to start.",
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:7071",
        help="Functions host URL (default: http://localhost:7071).",
    )
    parser.add_argument(
        "--session-id",
        default=DEFAULT_SESSION_ID,
        help=f"Shared chat session ID (default: {DEFAULT_SESSION_ID}).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180,
        help="Chat request timeout in seconds (default: 180).",
    )
    args = parser.parse_args()

    selected: tuple[Pipeline, ...] = (
        ("incident", "release") if args.pipeline == "both" else (args.pipeline,)
    )
    for pipeline in selected:
        owner, _ = PIPELINES[pipeline]
        print(f"Sending {pipeline} workflow request to {owner}...")
        workflow_id = send_pipeline(
            pipeline,
            base_url=args.base_url,
            session_id=args.session_id,
            timeout=args.timeout,
        )
        query = urlencode({"workflow_id": workflow_id})
        print(f"{pipeline} workflow ID: {workflow_id}")
        print(
            f"status: {args.base_url.rstrip('/')}/agents/{owner}/workflow-status?{query}"
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        raise SystemExit(f"FAIL: {exc}") from exc
