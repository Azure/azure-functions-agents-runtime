from __future__ import annotations

import asyncio
import concurrent.futures
import json
import re
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
from azure_functions_agents.harness.agent_framework import _maf_local_tools
from azure_functions_agents.local_tools.go import prepare_go_tools


def _has_supported_go() -> bool:
    executable = shutil.which("go")
    if executable is None:
        return False
    result = subprocess.run(
        [executable, "version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    match = re.search(r"\bgo1\.(\d+)", result.stdout)
    return result.returncode == 0 and match is not None and int(match.group(1)) >= 25


def _spec(
    tmp_path: Path,
    source: str,
    *,
    parameters: tuple[LocalToolParameter, ...] | None = None,
    source_name: str = "lookup_order.go",
) -> LocalToolSpec:
    source_path = tmp_path / source_name
    source_path.write_text(source, encoding="utf-8")
    metadata_path = tmp_path / "tool.md"
    metadata_path.write_text("", encoding="utf-8")
    return LocalToolSpec.create(
        name="lookup_order",
        description="Look up an order.",
        entrypoint="LookupOrder",
        source_path=source_path.resolve(),
        metadata_path=metadata_path.resolve(),
        parameters=parameters
        if parameters is not None
        else (
            LocalToolParameter(
                name="order_id",
                type=PortableToolType.STRING,
                description="Order identifier.",
            ),
        ),
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


def _write_request(process: subprocess.Popen[str], request: dict[str, object]) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(request) + "\n")
    process.stdin.flush()


def _read_response(process: subprocess.Popen[str]) -> dict[str, object]:
    assert process.stdout is not None
    return json.loads(process.stdout.readline())


@pytest.mark.skipif(not _has_supported_go(), reason="Go 1.25 or newer is not installed")
def test_prepares_go_without_executing_customer_code(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        """
package main

import "fmt"

func init() { panic("must not execute during preparation") }
func LookupOrder(orderID string) any { return fmt.Sprintf("%s", orderID) }
""",
    )

    result = prepare_go_tools((spec,))

    assert result.failed_loads == ()
    [prepared] = result.tools
    assert prepared.launch.command.is_file()
    assert Path(prepared.launch.arguments[1]).is_file()


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_supported_go(), reason="Go 1.25 or newer is not installed")
async def test_real_maf_round_trip_executes_go_tool(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        """
package main

import "fmt"

func LookupOrder(orderID string) any {
    fmt.Println("customer diagnostic")
    return map[string]any{"status": "shipped", "order_id": orderID}
}
""",
    )
    [prepared] = prepare_go_tools((spec,)).tools
    [wrapper] = _maf_local_tools.build_maf_local_tools([prepared])

    await wrapper.connect()
    try:
        response = await wrapper.call_tool("lookup_order", order_id="A-42")
    finally:
        await wrapper.close()

    assert response[0].text == '{"order_id":"A-42","status":"shipped"}'


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_supported_go(), reason="Go 1.25 or newer is not installed")
async def test_go_package_state_is_reused(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        """
package main

import "fmt"

var count int64

func LookupOrder(orderID string) string {
    count++
    return fmt.Sprintf("%d:%s", count, orderID)
}
""",
    )
    [prepared] = prepare_go_tools((spec,)).tools
    [wrapper] = _maf_local_tools.build_maf_local_tools([prepared])

    await wrapper.connect()
    try:
        first = await wrapper.call_tool("lookup_order", order_id="A")
        second = await wrapper.call_tool("lookup_order", order_id="B")
    finally:
        await wrapper.close()

    assert first[0].text == "1:A"
    assert second[0].text == "2:B"


@pytest.mark.skipif(not _has_supported_go(), reason="Go 1.25 or newer is not installed")
def test_go_init_output_cannot_corrupt_protocol_stdout(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        """
package main

import "fmt"

func init() { fmt.Println("package initialized") }
func LookupOrder(orderID string) string { return orderID }
""",
    )
    [prepared] = prepare_go_tools((spec,)).tools

    response = _raw_call(prepared, {"order_id": "A-42"})

    assert response["result"]["content"][0]["text"] == "A-42"


@pytest.mark.skipif(not _has_supported_go(), reason="Go 1.25 or newer is not installed")
def test_go_error_result_becomes_tool_error(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        """
package main

import "errors"

func LookupOrder(orderID string) (string, error) {
    return "", errors.New("order was not found")
}
""",
    )
    [prepared] = prepare_go_tools((spec,)).tools

    response = _raw_call(prepared, {"order_id": "missing"})

    assert response["result"]["isError"] is True
    assert response["result"]["content"][0]["text"] == "order was not found"


@pytest.mark.skipif(not _has_supported_go(), reason="Go 1.25 or newer is not installed")
def test_go_signature_failure_is_isolated(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        "package main\nfunc LookupOrder(orderID int64) string { return \"bad\" }\n",
    )

    result = prepare_go_tools((spec,))

    assert result.tools == ()
    assert "parameter 1 must be string" in result.failed_loads[0][1]


@pytest.mark.skipif(not _has_supported_go(), reason="Go 1.25 or newer is not installed")
@pytest.mark.parametrize(
    "source_name",
    ["_lookup.go", "lookup_test.go", "lookup_windows.go"],
)
def test_go_build_ignores_authored_filename_semantics(
    tmp_path: Path, source_name: str
) -> None:
    spec = _spec(
        tmp_path,
        "package main\nfunc LookupOrder(orderID string) string { return orderID }\n",
        source_name=source_name,
    )

    result = prepare_go_tools((spec,))

    assert result.failed_loads == ()
    assert len(result.tools) == 1


@pytest.mark.skipif(not _has_supported_go(), reason="Go 1.25 or newer is not installed")
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
def test_go_integer_arguments_use_portable_json_semantics(
    tmp_path: Path, value: object, is_error: bool
) -> None:
    spec = _spec(
        tmp_path,
        "package main\nfunc LookupOrder(value int64) int64 { return value }\n",
        parameters=(
            LocalToolParameter(
                name="value",
                type=PortableToolType.INTEGER,
                description="Integer value.",
            ),
        ),
    )
    [prepared] = prepare_go_tools((spec,)).tools

    response = _raw_call(prepared, {"value": value})

    assert response["result"]["isError"] is is_error


@pytest.mark.skipif(not _has_supported_go(), reason="Go 1.25 or newer is not installed")
def test_go_rejects_fractional_integer_before_rounding(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        "package main\nfunc LookupOrder(value int64) int64 { return value }\n",
        parameters=(
            LocalToolParameter(
                name="value",
                type=PortableToolType.INTEGER,
                description="Integer value.",
            ),
        ),
    )
    [prepared] = prepare_go_tools((spec,)).tools

    response = _raw_call(
        prepared,
        {},
        arguments_json='{"value":9007199254740990.6}',
    )

    assert response["result"]["isError"] is True


@pytest.mark.skipif(not _has_supported_go(), reason="Go 1.25 or newer is not installed")
def test_go_rejects_non_finite_result(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        """
package main

import "math"

func LookupOrder(orderID string) float64 { return math.NaN() }
""",
    )
    [prepared] = prepare_go_tools((spec,)).tools

    response = _raw_call(prepared, {"order_id": "A-42"})

    assert response["result"]["isError"] is True
    assert response["result"]["content"][0]["text"] == "result must be finite"


@pytest.mark.skipif(not _has_supported_go(), reason="Go 1.25 or newer is not installed")
def test_concurrent_go_preparation_reuses_one_artifact(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        "package main\nfunc LookupOrder(orderID string) string { return orderID + \"-race\" }\n",
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: prepare_go_tools((spec,)), range(2)))

    assert all(result.failed_loads == () for result in results)
    commands = {result.tools[0].launch.command for result in results}
    assert len(commands) == 1
    assert commands.pop().is_file()


@pytest.mark.skipif(not _has_supported_go(), reason="Go 1.25 or newer is not installed")
def test_cancelling_queued_go_call_prevents_execution(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        """
package main

import (
    "strconv"
    "time"
)

var queuedRuns int

func LookupOrder(orderID string) string {
    if orderID == "slow" { time.Sleep(500 * time.Millisecond) }
    if orderID == "queued" { queuedRuns++ }
    if orderID == "check" { return strconv.Itoa(queuedRuns) }
    return orderID
}
""",
    )
    [prepared] = prepare_go_tools((spec,)).tools
    process = subprocess.Popen(
        [str(prepared.launch.command), *prepared.launch.arguments],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _write_request(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25"},
            },
        )
        _read_response(process)
        for request_id, order_id in ((2, "slow"), (3, "queued")):
            _write_request(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {
                        "name": "lookup_order",
                        "arguments": {"order_id": order_id},
                    },
                },
            )
        _write_request(
            process,
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 3},
            },
        )
        _write_request(
            process,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "lookup_order",
                    "arguments": {"order_id": "duplicate"},
                },
            },
        )
        responses = [_read_response(process) for _index in range(3)]
        id_three = [response for response in responses if response["id"] == 3]
        assert len(id_three) == 2
        messages = {response["result"]["content"][0]["text"] for response in id_three}
        assert messages == {"Duplicate Go tool call ID.", "Go tool call was cancelled."}
        _write_request(
            process,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "lookup_order",
                    "arguments": {"order_id": "check"},
                },
            },
        )
        check = _read_response(process)
    finally:
        process.terminate()
        process.communicate(timeout=5)

    assert check["result"]["content"][0]["text"] == "0"


@pytest.mark.skipif(not _has_supported_go(), reason="Go 1.25 or newer is not installed")
def test_unknown_go_cancellation_id_is_ignored(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        "package main\nfunc LookupOrder(orderID string) string { return orderID }\n",
    )
    [prepared] = prepare_go_tools((spec,)).tools
    process = subprocess.Popen(
        [str(prepared.launch.command), *prepared.launch.arguments],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _write_request(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25"},
            },
        )
        _read_response(process)
        _write_request(
            process,
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 2},
            },
        )
        _write_request(
            process,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "lookup_order",
                    "arguments": {"order_id": "still-runs"},
                },
            },
        )
        response = _read_response(process)
    finally:
        process.terminate()
        process.communicate(timeout=5)

    assert response["result"]["content"][0]["text"] == "still-runs"


@pytest.mark.skipif(not _has_supported_go(), reason="Go 1.25 or newer is not installed")
def test_go_startup_cancellation_is_prompt_and_restartable(tmp_path: Path) -> None:
    marker = tmp_path / "initialized"
    source = f"""
package main

import (
    "os"
    "time"
)

func init() {{
    marker := {json.dumps(str(marker))}
    if _, err := os.Stat(marker); os.IsNotExist(err) {{
        _ = os.WriteFile(marker, []byte("started"), 0600)
        time.Sleep(30 * time.Second)
    }}
}}

func LookupOrder(orderID string) string {{ return orderID }}
"""
    spec = _spec(tmp_path, source)
    [prepared] = prepare_go_tools((spec,)).tools
    process = subprocess.Popen(
        [str(prepared.launch.command), *prepared.launch.arguments],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stderr = ""
    try:
        _write_request(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25"},
            },
        )
        _read_response(process)
        _write_request(
            process,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "lookup_order",
                    "arguments": {"order_id": "slow-start"},
                },
            },
        )
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists()
        cancelled_at = time.monotonic()
        _write_request(
            process,
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 2},
            },
        )
        cancelled = _read_response(process)
        cancellation_duration = time.monotonic() - cancelled_at
        _write_request(
            process,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "lookup_order",
                    "arguments": {"order_id": "fast"},
                },
            },
        )
        restarted = _read_response(process)
    finally:
        process.terminate()
        _, stderr = process.communicate(timeout=5)

    assert cancelled["result"]["isError"] is True
    assert cancellation_duration < 5
    assert restarted["result"]["content"][0]["text"] == "fast", stderr


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_supported_go(), reason="Go 1.25 or newer is not installed")
async def test_go_cancellation_restarts_customer_process(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        """
package main

import "time"

var dirty bool

func LookupOrder(orderID string) string {
    if orderID == "slow" {
        dirty = true
        time.Sleep(30 * time.Second)
    }
    if dirty { return "dirty" }
    return orderID
}
""",
    )
    [prepared] = prepare_go_tools((spec,)).tools
    [wrapper] = _maf_local_tools.build_maf_local_tools([prepared])

    await wrapper.connect()
    try:
        call = asyncio.create_task(wrapper.call_tool("lookup_order", order_id="slow"))
        await asyncio.sleep(0.1)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        result = await wrapper.call_tool("lookup_order", order_id="fast")
    finally:
        await wrapper.close()

    assert result[0].text == "fast"
