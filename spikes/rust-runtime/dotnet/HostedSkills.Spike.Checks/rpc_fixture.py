#!/usr/bin/env python3
"""Fault-injection stdio peer for C# lifecycle checks, NOT the Rust runtime."""

import json
import sys
import time


mode = ""
for line in sys.stdin:
    frame = json.loads(line)
    method, params = frame["method"], frame["params"]
    if method == "initialize":
        mode = params["env"]["fixture_mode"]
        if mode == "initialize_eof":
            break
        if mode == "stderr_flood":
            sys.stderr.write("x" * 262144 + "\n")
            sys.stderr.flush()
        result = {
            "runtime_version": "fixture", "agents": [], "skills": [],
            "provider": {"kind": "fixture", "base_url": "", "auth_mode": "none",
                         "default_model": "mock"},
            "storage_mode": "fixture",
        }
    elif method == "agent.run":
        if mode == "run_eof":
            break
        event = {"session_id": params["session_id"], "agent_name": params["agent_name"],
                 "kind": "delta", "content": "fixture"}
        if params["stream"]:
            print(json.dumps({"jsonrpc": "2.0", "method": "agent.stream", "params": event}), flush=True)
        if mode == "stall":
            time.sleep(120)
        if params["stream"]:
            event["kind"] = "done"
            print(json.dumps({"jsonrpc": "2.0", "method": "agent.stream", "params": event}), flush=True)
        result = {
            "content": "fixture", "session_id": params["session_id"],
            "agent_name": params["agent_name"], "model": "fixture",
            "storage_mode": "fixture", "resumed": False, "tool_calls_made": [],
        }
    elif method == "shutdown":
        if mode == "shutdown_stall":
            time.sleep(120)
        result = {}
    else:
        raise AssertionError(method)
    print(json.dumps({"jsonrpc": "2.0", "id": frame["id"], "result": result}), flush=True)
