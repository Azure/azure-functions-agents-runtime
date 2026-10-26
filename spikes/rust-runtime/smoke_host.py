#!/usr/bin/env python3
"""Bounded real Functions-host smoke test; all LLM inference is deterministic MOCK."""

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parent
APP = ROOT / "dotnet" / "HostedSkills.Spike"
# Public Azurite emulator credentials, never a real storage account.
EMULATOR_KEY = (
    "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
)


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def descendants(pid):
    rows = subprocess.check_output(["ps", "-eo", "pid=,ppid="], text=True)
    pairs = [tuple(map(int, row.split())) for row in rows.splitlines()]
    found = {pid}
    while True:
        children = {child for child, parent in pairs if parent in found}
        if children <= found:
            return found - {pid}
        found |= children


def stop(process):
    if process is None:
        return
    children = descendants(process.pid)
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=12)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
    # Only exact descendants captured from this owned process are considered.
    for pid in children:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 2
    while children and time.monotonic() < deadline:
        for pid in children.copy():
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                children.remove(pid)
        if children:
            time.sleep(0.1)
    for pid in children:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def post(base, route, payload, evidence, name, status=200):
    request = urllib.request.Request(
        base + route,
        json.dumps(payload).encode(),
        {"Content-Type": "application/json", "x-fhs-session-id": "direct-http"},
    )
    try:
        response = urllib.request.urlopen(request, timeout=55)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        body = response.read().decode()
        (evidence / f"{name}.json").write_text(body)
        assert response.status == status, f"{name}: HTTP {response.status}: {body}"
        return json.loads(body)


def wait_for_host(process, logfile, base):
    deadline = time.monotonic() + 100
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"func exited with {process.returncode}; see {logfile}")
        text = logfile.read_text(errors="replace")
        if all(marker in text for marker in (
            "HostedSkillSupport", "ShippingQuote", "Worker process started and initialized",
            "Rust stdio ready",
        )):
            try:
                with urllib.request.urlopen(base + "/admin/host/status", timeout=2) as response:
                    if json.load(response).get("state") == "Running":
                        return
            except (OSError, urllib.error.URLError):
                pass
        time.sleep(0.3)
    raise TimeoutError(f"Functions worker did not become ready; see {logfile}")


def check_turn(response, session_id, number, storage):
    result, state = response["result"], response["session"]
    assert response["inference"] == "MOCK"
    assert result["session_id"] == session_id
    assert result["agent_name"] == "order_assistant"
    assert result["resumed"] is (number > 1), result
    assert result["storage_mode"] == storage, result
    assert f"turn={number} " in result["content"], result
    assert state["has_persisted_state"] is True
    assert "turns.jsonl" in state["session_state_files"]
    turns = [json.loads(line) for line in state["transcript"].splitlines() if line]
    assert len(turns) == number, turns
    assert [turn["turn"] for turn in turns] == list(range(1, number + 1))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--func", default=str(ROOT / ".tools" / "core-tools" / "func"))
    parser.add_argument("--storage", choices=("local", "azurite"), default="local")
    parser.add_argument("--azurite", default=shutil.which("azurite-blob", mode=os.F_OK))
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args()
    evidence = (args.evidence_dir or ROOT / ".tools" / "smoke" / uuid.uuid4().hex).resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("AzureWebJobsStorage") or key in (
            "AZURE_STORAGE_BEARER_TOKEN", "AZURE_FUNCTIONS_AGENTS_SESSION_CONTAINER",
        ):
            del env[key]
    env.update({
        "FUNCTIONS_WORKER_RUNTIME": "dotnet-isolated",
        "AzureWebJobsSecretStorageType": "files",
        "AzureWebJobsStorage": "",
        "FHS_MOCK_LLM": "true",
        "FHS_RUNTIME_PATH": str(ROOT / "target" / "debug" / "fhs-runtime"),
        "DEFAULT_AGENT_MODEL": "mock-model",
        "ENVIRONMENT_NAME": "dotnet-isolated",
        "TENANT_NAME": "ContosoRetail",
        "AZURE_FUNCTIONS_AGENTS_PROVIDER": "foundry",
        "FOUNDRY_PROJECT_ENDPOINT": "https://contoso-foundry.services.ai.azure.com/api/projects/retail",
        "AZURE_FUNCTIONS_AGENTS_LOCAL_STORAGE_DIR": str(evidence / "sessions"),
        "AZURE_FUNCTIONS_AGENTS_SESSION_CONTAINER": "fhs-smoke-" + uuid.uuid4().hex,
    })
    # Keep any Cargo/test scratch data in the ignored spike tools directory.
    scratch = ROOT / ".tools" / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    env.update({"TMPDIR": str(scratch), "TMP": str(scratch), "TEMP": str(scratch)})
    with (evidence / "build.log").open("w") as log:
        subprocess.run(["cargo", "build", "--bin", "fhs-runtime"], cwd=ROOT,
                       env=env, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=180)
        subprocess.run(["dotnet", "build", "--nologo", "--verbosity", "minimal"], cwd=APP,
                       env=env, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=180)

    host = azurite = None
    logs = []
    try:
        if args.storage == "azurite":
            if not args.azurite:
                raise RuntimeError("azurite-blob not found; install Azurite or pass --azurite")
            blob_port = unused_port()
            azurite_path = Path(args.azurite).resolve()
            command = [str(azurite_path)] if os.access(azurite_path, os.X_OK) else ["node", str(azurite_path)]
            azurite_log = (evidence / "azurite.log").open("w")
            logs.append(azurite_log)
            azurite = subprocess.Popen(command + [
                "--blobHost", "127.0.0.1", "--blobPort", str(blob_port),
                "--location", str(evidence / "azurite"), "--silent", "--skipApiVersionCheck",
            ], env=env, stdout=azurite_log, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 20
            while True:
                if azurite.poll() is not None:
                    raise RuntimeError("Azurite exited; see azurite.log")
                try:
                    with socket.create_connection(("127.0.0.1", blob_port), timeout=1):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Azurite did not listen within 20 seconds")
                    time.sleep(0.2)
            env["AzureWebJobsStorage"] = (
                "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
                f"AccountKey={EMULATOR_KEY};BlobEndpoint=http://127.0.0.1:{blob_port}/devstoreaccount1;"
            )

        port = unused_port()
        base = f"http://127.0.0.1:{port}"

        def start_host(name):
            logfile = evidence / f"{name}.log"
            log = logfile.open("w")
            logs.append(log)
            process = subprocess.Popen(
                [str(Path(shutil.which(args.func) or args.func).resolve()),
                 "host", "start", "--no-build", "--port", str(port)],
                cwd=APP / "bin" / "Debug" / "net10.0",
                env=env, stdout=log, stderr=subprocess.STDOUT,
            )
            return process, logfile

        host, logfile = start_host("host")
        wait_for_host(host, logfile, base)
        shipping = post(base, "/api/shipping/quote",
                        {"orderId": "ORD-900", "zip": "98052"}, evidence, "shipping")
        assert shipping == {
            "orderId": "ORD-900", "zip": "98052", "sessionId": "direct-http",
            "invocationPath": "http-trigger", "carrier": "PriorityExpress", "rateUsd": 14.25,
        }, shipping
        post(base, "/api/support", {}, evidence, "bad-request", status=400)
        session_id = "host-smoke-" + uuid.uuid4().hex
        prompt = 'Please lookup_order and calculate_shipping_http for {"orderId":"ORD-900","zip":"98052"}'
        first = post(base, "/api/support", {"prompt": prompt, "sessionId": session_id}, evidence, "turn1")
        storage = "blob" if args.storage == "azurite" else "local"
        check_turn(first, session_id, 1, storage)
        result = first["result"]
        assert set(result["tool_calls_made"]) == {"lookup_order", "calculate_shipping_http"}, result
        for value in ("Ada Lovelace", "PAID", "149.99", "PriorityExpress", "14.25",
                      "ORD-900", "98052", "stdio-shared-handler", "ContosoRetail", "order-policy"):
            assert value in result["content"], (value, result)
        second = post(base, "/api/support",
                      {"prompt": "Confirm this is a resumed conversation.", "sessionId": session_id},
                      evidence, "turn2")
        check_turn(second, session_id, 2, storage)
        assert second["result"]["tool_calls_made"] == []
        stop(host)
        host, logfile = start_host("host-restarted")
        wait_for_host(host, logfile, base)
        third = post(base, "/api/support",
                     {"prompt": "Resume after the worker and Rust process restart.", "sessionId": session_id},
                     evidence, "turn3-after-restart")
        check_turn(third, session_id, 3, storage)
        summary = {
            "indexed_functions": ["HostedSkillSupport", "ShippingQuote"],
            "inference": "MOCK", "storage": storage, "http_port": port, "session_id": session_id,
            "assertions": [
                "HTTP ShippingQuote returns PriorityExpress / 14.25",
                "HTTP support rejects missing prompt with 400",
                "HTTP support calls both C# tools over Rust stdio",
                "second HTTP request resumes persisted turns 1 and 2",
                "third HTTP request resumes turn 3 after full host restart",
            ],
        }
        (evidence / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print(json.dumps(summary, indent=2))
        print(f"PASS: real Functions HTTP + Rust stdio ({storage}); evidence: {evidence}")
    finally:
        stop(host)
        stop(azurite)
        for log in logs:
            log.close()


if __name__ == "__main__":
    main()
