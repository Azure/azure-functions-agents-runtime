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
            "inference_mode": "foundry" if mode == "foundry" else "mock",
            "storage_mode": "fixture",
        }
    elif method == "agent.run":
        if mode == "run_eof":
            break
        if mode in {"telemetry", "telemetry_failure", "telemetry_tool_error"}:
            run_id = params.get("run_id")
            assert run_id and params.get("trace_context")
            print(json.dumps({
                "jsonrpc": "2.0", "method": "fhs.telemetry.model_call",
                "params": {
                    "run_id": run_id, "call_id": "1", "phase": "start",
                    "model": "fixture-model",
                },
            }), flush=True)
            print(json.dumps({
                "jsonrpc": "2.0", "method": "fhs.telemetry.model_call",
                "params": {
                    "run_id": run_id, "call_id": "1", "phase": "start",
                    "model": "duplicate-start-must-be-ignored",
                },
            }), flush=True)
            if mode == "telemetry_failure":
                print(json.dumps({
                    "jsonrpc": "2.0", "id": frame["id"],
                    "error": {"code": -32011, "message": "fixture run failed"},
                }), flush=True)
                continue
            print(json.dumps({
                "jsonrpc": "2.0", "method": "fhs.telemetry.model_call",
                "params": {
                    "run_id": run_id, "call_id": "1", "phase": "end",
                    "model": "fixture-model", "outcome": "success",
                },
            }), flush=True)
            print(json.dumps({
                "jsonrpc": "2.0", "id": 9001, "method": "tool.call",
                "params": {
                    "tool_name": "fixture_tool", "call_id": "tool-1",
                    "session_id": params["session_id"], "arguments": {},
                    "trigger_metadata": None, "run_id": run_id,
                },
            }), flush=True)
            tool_result = json.loads(sys.stdin.readline())
            assert tool_result["id"] == 9001
            assert tool_result["result"]["is_error"] == (mode == "telemetry_tool_error")
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
