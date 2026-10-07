"""Exercise an already-running local sample. Successful phases make real model calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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
        headers={"x-ms-session-id": "../outside"},
        json={"prompt": "This must fail before any model call."},
    )
    assert response.status_code == 400, "Invalid session identity did not return HTTP 400"
    error = response.json()["error"].lower()
    assert "invalid session_id" in error, "Missing invalid session identity diagnostic"
    stream = client.post("/agents/main/chatstream", json={"prompt": "Do not call a model."})
    assert stream.status_code == 501, "Unsupported streaming did not fail explicitly"
    history = client.get("/agents/main/history")
    assert history.status_code == 501, "Preview returned a success-shaped MAF transcript"
    print("PASS negative: invalid session, streaming and history fail without model calls")


def skill(client: httpx.Client) -> None:
    response = client.post("/agents/main/chat", json={
        "prompt": "Run the preview-check skill test. Load the skill, then use view to read "
        "its references/check.txt. Return the skill marker and the exact file marker. "
        "Do not guess, use a network tool, or run a shell command.",
    })
    response.raise_for_status()
    result = response.json()
    assert "SKILL_LOADED_PREVIEW_CHECK" in result["response"], "Skill marker is missing"
    assert "REFERENCE_READ_7C42A9" in result["response"], "Reference marker is missing"
    calls = result["tool_calls"]
    assert {call["tool_name"] for call in calls} == {"skill", "view"}, "Expected skill and view only"
    assert all(call.get("success") is True for call in calls), "A native skill tool failed"
    reference = Path(__file__).resolve().parent / "src" / "skills" / "preview-check" / "references" / "check.txt"
    for call in calls:
        if call["tool_name"] != "view":
            continue
        arguments = call["arguments"]
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        assert Path(arguments["path"]).resolve() == reference.resolve(), "View read an unexpected file"
        assert "REFERENCE_READ_7C42A9" in call["result"], "View did not return the reference marker"
    print("PASS skill: native skill and view read the exact sample reference")


def _learn_links(text: str) -> set[str]:
    return {
        link.rstrip(".,;")
        for link in re.findall(r"""https://learn\.microsoft\.com/[^\s"'<>()\[\]]+""", text)
    }


def mcp(client: httpx.Client) -> None:
    response = client.post("/agents/main/chat", json={
        "prompt": "For an MCP check, use microsoft_docs_search on microsoft-learn to search "
        "for the Azure Functions Python programming model. Include a Microsoft Learn link "
        "from the result. Do not use web_request, make_receipt, a skill, or a shell command.",
    })
    response.raise_for_status()
    result = response.json()
    calls = result["tool_calls"]
    assert calls, "The response has no MCP tool evidence"
    assert all(
        call["tool_name"].endswith("microsoft_docs_search") and call.get("success") is True
        for call in calls
    ), "Expected successful Microsoft Learn search calls only"
    links = {
        link
        for call in calls
        for link in _learn_links(call["result"])
    }
    assert links, "MCP returned no Learn link"
    assert links & _learn_links(result["response"]), "The response has no link from the MCP result"
    print("PASS mcp: Microsoft Learn search returned a documentation link")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:7071")
    parser.add_argument(
        "--phase", choices=("first", "followup", "negative", "all", "skill", "mcp", "capabilities"),
        default="all",
    )
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
        app = Path(__file__).resolve().parent / "src"
        required = ["AZURE_FUNCTIONS_AGENTS_PROVIDER"]
        connection = os.environ.get("AzureWebJobsStorage", "").strip()
        service_uri = os.environ.get("AzureWebJobsStorage__blobServiceUri", "").strip()
        if connection or service_uri:
            required.append("AZURE_FUNCTIONS_AGENTS_SESSION_CONTAINER")
            if os.environ.get("AZURE_FUNCTIONS_AGENTS_TEST_DISPOSABLE_BLOB") != "1":
                parser.error("Blob restart requires AZURE_FUNCTIONS_AGENTS_TEST_DISPOSABLE_BLOB=1.")
            settings = app / "local.settings.json"
            if not settings.exists():
                parser.error(
                    "Create untracked src/local.settings.json from the sample template and remove "
                    "its blank AzureWebJobsStorage entry before Blob restart."
                )
            values = json.loads(settings.read_text(encoding="utf-8"))["Values"]
            if connection:
                value = values.get("AzureWebJobsStorage")
                if value is not None and value != os.environ["AzureWebJobsStorage"]:
                    parser.error(
                        "Remove the blank/mismatched AzureWebJobsStorage entry from local.settings.json "
                        "so the worker inherits the explicit connection from the environment."
                    )
            else:
                # A connection string always wins over blobServiceUri, so none may be inherited.
                uri_value = values.get("AzureWebJobsStorage__blobServiceUri")
                if values.get("AzureWebJobsStorage") or (
                    uri_value is not None and uri_value != os.environ["AzureWebJobsStorage__blobServiceUri"]
                ):
                    parser.error(
                        "Remove AzureWebJobsStorage and any mismatched AzureWebJobsStorage__blobServiceUri "
                        "entry from local.settings.json so the worker uses the explicit Entra ID service URI."
                    )
        else:
            required.append("AZURE_FUNCTIONS_AGENTS_SESSION_DIR")
        provider = os.environ.get("AZURE_FUNCTIONS_AGENTS_PROVIDER", "").strip().lower()
        if provider == "foundry":
            required.extend(["FOUNDRY_PROJECT_ENDPOINT", "FOUNDRY_MODEL"])
        elif provider == "azure_openai":
            required.append("AZURE_OPENAI_ENDPOINT")
            if not os.environ.get("AZURE_FUNCTIONS_AGENTS_MODEL", "").strip():
                required.append("AZURE_OPENAI_DEPLOYMENT")
        else:
            required.extend(["AZURE_FUNCTIONS_AGENTS_MODEL", "OPENAI_API_KEY"])
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            parser.error("Run the README environment setup in this terminal; missing: " + ", ".join(missing))
        repository = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(repository / "tests" / "endtoend"))
        from _func_host import running_host

        with running_host(app, timeout=120) as host:
            with httpx.Client(base_url=host.base_url, timeout=75, trust_env=False) as client:
                first(client, args.evidence)
        with running_host(app, timeout=120) as host:
            with httpx.Client(base_url=host.base_url, timeout=75, trust_env=False) as client:
                followup(client, args.evidence)
                negative(client)
        print("PASS local Functions host restart and SDK follow-up")
        return
    url = urlsplit(args.base_url)
    if url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        parser.error("This sample driver targets an isolated local Functions host only.")
    with httpx.Client(base_url=args.base_url, timeout=180, trust_env=False) as client:
        if args.phase in {"first", "all"}:
            first(client, args.evidence)
        if args.phase in {"followup", "all"}:
            followup(client, args.evidence)
        if args.phase in {"negative", "all"}:
            negative(client)
        if args.phase in {"skill", "capabilities"}:
            skill(client)
        if args.phase in {"mcp", "capabilities"}:
            mcp(client)


if __name__ == "__main__":
    main()
