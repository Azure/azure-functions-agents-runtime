"""Exercise an already-running local sample. Successful phases make real model calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def first(client: httpx.Client, path: Path) -> None:
    if path.exists():
        raise RuntimeError("Evidence file already exists; choose a new file for a new conversation.")
    tag = "preview-" + uuid.uuid4().hex
    expected = "receipt-" + hashlib.sha256(tag.encode("utf-8")).hexdigest()[:24]
    response = client.post(
        "/agents/main/chat",
        json={"prompt": f"Call make_receipt exactly once with tag '{tag}'. Reply only with its result."},
    )
    response.raise_for_status()
    result = response.json()
    assert set(result) == {"session_id", "response", "tool_calls"}, "Unexpected public result shape"
    assert result["session_id"] == response.headers["x-ms-session-id"], "Session identity mismatch"
    assert expected in result["response"], "Real model reply did not contain the tool's receipt"
    assert len(result["tool_calls"]) == 1, "Expected exactly one custom tool call"
    call = result["tool_calls"][0]
    assert call["tool_name"] == "make_receipt", "Unexpected tool"
    arguments = call["arguments"]
    if isinstance(arguments, str):
        arguments = json.loads(arguments)
    assert arguments == {"tag": tag}, "Custom tool received unexpected arguments"
    assert expected in str(call["result"]), "Expected custom tool result is missing"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump({"session_id": result["session_id"], "expected": expected}, stream)
    print("PASS first: real model reply, exactly one make_receipt call, public session id saved")


def followup(client: httpx.Client, path: Path) -> None:
    saved = json.loads(path.read_text(encoding="utf-8"))
    response = client.post(
        "/agents/main/chat",
        headers={"x-ms-session-id": saved["session_id"]},
        json={"prompt": "What receipt did the tool return previously? Reply only with it. Do not call tools."},
    )
    response.raise_for_status()
    result = response.json()
    assert result["session_id"] == saved["session_id"], "Native continuity changed public identity"
    assert saved["expected"] in result["response"], "Value-free follow-up lost the previous tool result"
    assert result["tool_calls"] == [], "Follow-up unexpectedly executed a tool"
    print("PASS followup: same session recalled prior tool result without a new tool call")


def negative(client: httpx.Client) -> None:
    response = client.post(
        "/agents/main/chat",
        headers={"x-ms-session-id": "unknown-" + uuid.uuid4().hex},
        json={"prompt": "This must fail before any model call."},
    )
    assert response.status_code == 409, "Unknown preview session did not return HTTP 409"
    error = response.json()["error"].lower()
    assert "native session has no completed turn to resume" in error, (
        "Missing unknown native session diagnostic"
    )
    stream = client.post("/agents/main/chatstream", json={"prompt": "Do not call a model."})
    assert stream.status_code == 501, "Unsupported streaming did not fail explicitly"
    history = client.get("/agents/main/history")
    assert history.status_code == 501, "Preview returned a success-shaped MAF transcript"
    print("PASS negative: unknown session, streaming and history fail without model calls")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:7071")
    parser.add_argument("--phase", choices=("first", "followup", "negative", "all"), default="all")
    parser.add_argument("--evidence", type=Path, default=Path(".preview-evidence.json"))
    parser.add_argument(
        "--restart-host", action="store_true",
        help="Run all phases and restart a real local Functions host between the two turns.",
    )
    args = parser.parse_args()
    if args.restart_host:
        if args.phase != "all":
            parser.error("--restart-host runs all phases.")
        if os.environ.get("AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT", "").strip().lower() not in {"true", "1"}:
            parser.error("--restart-host requires the explicit Copilot preview flag.")
        required = ["AZURE_FUNCTIONS_AGENTS_PROVIDER", "AZURE_FUNCTIONS_AGENTS_SESSION_DIR"]
        if os.environ.get("AZURE_FUNCTIONS_AGENTS_PROVIDER") == "foundry":
            required.extend(["FOUNDRY_PROJECT_ENDPOINT", "FOUNDRY_MODEL"])
        else:
            required.extend(["AZURE_FUNCTIONS_AGENTS_MODEL", "OPENAI_API_KEY"])
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            parser.error("Run the README environment setup in this terminal; missing: " + ", ".join(missing))
        repository = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(repository / "tests" / "endtoend"))
        from _func_host import running_host

        app = Path(__file__).resolve().parent / "src"
        with running_host(app, timeout=120) as host:
            with httpx.Client(base_url=host.base_url, timeout=75, trust_env=False) as client:
                first(client, args.evidence)
        with running_host(app, timeout=120) as host:
            with httpx.Client(base_url=host.base_url, timeout=75, trust_env=False) as client:
                followup(client, args.evidence)
                negative(client)
        print("PASS local Functions host restart between completed turns")
        return
    url = urlsplit(args.base_url)
    if url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        parser.error("This sample driver targets an isolated local Functions host only.")
    with httpx.Client(base_url=args.base_url, timeout=75, trust_env=False) as client:
        if args.phase in {"first", "all"}:
            first(client, args.evidence)
        if args.phase in {"followup", "all"}:
            followup(client, args.evidence)
        if args.phase in {"negative", "all"}:
            negative(client)


if __name__ == "__main__":
    main()
