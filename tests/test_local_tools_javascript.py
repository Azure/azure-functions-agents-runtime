from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from azure_functions_agents._local_tool_descriptor import (
    LocalToolParameter,
    LocalToolSpec,
    PortableToolType,
)
from azure_functions_agents.local_tools import javascript


def _spec(
    tmp_path: Path,
    source: str,
    *,
    parameters: tuple[LocalToolParameter, ...] = (),
) -> LocalToolSpec:
    source_path = tmp_path / "tool.js"
    source_path.write_text(source, encoding="utf-8")
    metadata_path = tmp_path / "tool.md"
    metadata_path.write_text("", encoding="utf-8")
    return LocalToolSpec.create(
        name="greet",
        description="Create a greeting.",
        entrypoint="greet",
        source_path=source_path.resolve(),
        metadata_path=metadata_path.resolve(),
        parameters=parameters,
    )


def _raw_call(
    prepared,
    arguments: dict[str, object],
    *,
    arguments_json: str | None = None,
) -> dict[str, object]:
    process = subprocess.Popen(
        [str(prepared.launch.command), *prepared.launch.arguments],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdin is not None
        assert process.stdout is not None
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        }
        process.stdin.write(json.dumps(initialize) + "\n")
        process.stdin.flush()
        json.loads(process.stdout.readline())
        if arguments_json is None:
            call = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": prepared.name, "arguments": arguments},
                }
            )
        else:
            call = (
                '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":'
                f'{{"name":{json.dumps(prepared.name)},"arguments":{arguments_json}}}}}'
            )
        process.stdin.write(call + "\n")
        process.stdin.flush()
        response = json.loads(process.stdout.readline())
        return response
    finally:
        process.terminate()
        process.communicate(timeout=5)


def test_empty_inventory_does_not_resolve_node(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        javascript.shutil,
        "which",
        lambda _command: pytest.fail("Node resolution must stay lazy"),
    )

    result = javascript.prepare_javascript_tools(())

    assert result.tools == ()
    assert result.failed_loads == ()


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_prepares_valid_javascript_without_executing_it(tmp_path: Path) -> None:
    source = "throw new Error('must not execute during preparation');\nexport function greet() {}\n"
    spec = _spec(tmp_path, source)

    result = javascript.prepare_javascript_tools((spec,))

    assert result.failed_loads == ()
    [prepared] = result.tools
    assert prepared.spec is spec
    assert prepared.launch.command.is_file()
    assert prepared.source_sha256 == hashlib.sha256(spec.source_path.read_bytes()).hexdigest()


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_syntax_failure_is_isolated(tmp_path: Path) -> None:
    spec = _spec(tmp_path, "export function greet( {\n")

    result = javascript.prepare_javascript_tools((spec,))

    assert result.tools == ()
    assert len(result.failed_loads) == 1
    assert "JavaScript syntax error" in result.failed_loads[0][1]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
@pytest.mark.parametrize(
    ("value", "is_error"),
    [
        (9007199254740991, False),
        (-9007199254740991, False),
        (9007199254740992, True),
        ("42", True),
        (1.5, True),
        (1e100, True),
    ],
)
def test_javascript_integer_arguments_use_portable_json_semantics(
    tmp_path: Path, value: object, is_error: bool
) -> None:
    spec = _spec(
        tmp_path,
        "export function greet(value) { return value; }\n",
        parameters=(
            LocalToolParameter(
                name="value",
                type=PortableToolType.INTEGER,
                description="Integer value.",
            ),
        ),
    )
    [prepared] = javascript.prepare_javascript_tools((spec,)).tools

    response = _raw_call(prepared, {"value": value})

    assert response["result"]["isError"] is is_error


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_javascript_rejects_fractional_integer_before_rounding(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        "export function greet(value) { return value; }\n",
        parameters=(
            LocalToolParameter(
                name="value",
                type=PortableToolType.INTEGER,
                description="Integer value.",
            ),
        ),
    )
    [prepared] = javascript.prepare_javascript_tools((spec,)).tools

    response = _raw_call(
        prepared,
        {},
        arguments_json='{"value":9007199254740990.6}',
    )

    assert response["result"]["isError"] is True


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_javascript_rejects_non_finite_result(tmp_path: Path) -> None:
    [prepared] = javascript.prepare_javascript_tools(
        (_spec(tmp_path, "export function greet() { return Number.NaN; }\n"),)
    ).tools

    response = _raw_call(prepared, {})

    assert response["result"]["isError"] is True
    assert response["result"]["content"][0]["text"] == "Result must be finite."


@pytest.mark.parametrize(
    "result",
    ("{ value: Number.NaN }", "[Number.POSITIVE_INFINITY]"),
)
@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_javascript_rejects_nested_non_finite_result(tmp_path: Path, result: str) -> None:
    [prepared] = javascript.prepare_javascript_tools(
        (_spec(tmp_path, f"export function greet() {{ return {result}; }}\n"),)
    ).tools

    response = _raw_call(prepared, {})

    assert response["result"]["isError"] is True
    assert response["result"]["content"][0]["text"] == "Result must be finite."


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_javascript_startup_cancellation_prevents_execution(tmp_path: Path) -> None:
    marker = tmp_path / "initialized"
    source = f"""
import {{ writeFileSync }} from "node:fs";
await new Promise((resolve) => setTimeout(resolve, 500));
writeFileSync({json.dumps(str(marker))}, "started");
export function greet(value) {{ return value; }}
"""
    spec = _spec(
        tmp_path,
        source,
        parameters=(
            LocalToolParameter(
                name="value",
                type=PortableToolType.STRING,
                description="Value.",
            ),
        ),
    )
    [prepared] = javascript.prepare_javascript_tools((spec,)).tools
    process = subprocess.Popen(
        [str(prepared.launch.command), *prepared.launch.arguments],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdin is not None
        assert process.stdout is not None
        for request in (
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25"},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "greet", "arguments": {"value": "cancelled"}},
            },
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 2},
            },
        ):
            process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()
        initialized = json.loads(process.stdout.readline())
        cancelled = json.loads(process.stdout.readline())
        time.sleep(0.7)
    finally:
        process.terminate()
        process.communicate(timeout=5)

    assert initialized["id"] == 1
    assert cancelled["result"]["isError"] is True
    assert cancelled["result"]["content"][0]["text"] == "JavaScript tool call was cancelled."
    assert not marker.exists()


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_javascript_startup_cancellation_preserves_other_calls(tmp_path: Path) -> None:
    source = """
await new Promise((resolve) => setTimeout(resolve, 500));
export function greet(value) { return value; }
"""
    spec = _spec(
        tmp_path,
        source,
        parameters=(
            LocalToolParameter(
                name="value",
                type=PortableToolType.STRING,
                description="Value.",
            ),
        ),
    )
    [prepared] = javascript.prepare_javascript_tools((spec,)).tools
    process = subprocess.Popen(
        [str(prepared.launch.command), *prepared.launch.arguments],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdin is not None
        assert process.stdout is not None
        requests = (
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25"},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "greet", "arguments": {"value": "cancelled"}},
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "greet", "arguments": {"value": "survivor"}},
            },
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 2},
            },
        )
        for request in requests:
            process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()
        initialized = json.loads(process.stdout.readline())
        cancelled = json.loads(process.stdout.readline())
        process.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "greet", "arguments": {"value": "replacement"}},
                }
            )
            + "\n"
        )
        process.stdin.flush()
        remaining = [
            json.loads(process.stdout.readline()),
            json.loads(process.stdout.readline()),
        ]
    finally:
        process.terminate()
        process.communicate(timeout=5)

    assert initialized["id"] == 1
    assert cancelled["id"] == 2
    assert cancelled["result"]["isError"] is True
    responses = {response["id"]: response for response in remaining}
    assert responses[3]["result"]["content"][0]["text"] == "survivor"
    assert responses[4]["result"]["content"][0]["text"] == "replacement"
