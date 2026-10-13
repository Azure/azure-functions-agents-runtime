from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from azure_functions_agents._local_tool_descriptor import (
    LocalToolParameter,
    LocalToolSpec,
    PortableToolType,
)
from azure_functions_agents.harness.agent_framework import _maf_local_tools
from azure_functions_agents.local_tools.csharp import prepare_csharp_tools


def _spec(
    tmp_path: Path,
    source: str,
    *,
    parameters: tuple[LocalToolParameter, ...] | None = None,
) -> LocalToolSpec:
    source_path = tmp_path / "LookupOrder.cs"
    source_path.write_text(source, encoding="utf-8")
    metadata_path = tmp_path / "tool.md"
    metadata_path.write_text("", encoding="utf-8")
    return LocalToolSpec.create(
        name="lookup_order",
        description="Look up an order.",
        entrypoint="OrderTools.LookupOrder",
        source_path=source_path.resolve(),
        metadata_path=metadata_path.resolve(),
        parameters=parameters
        or (
            LocalToolParameter(
                name="order_id",
                type=PortableToolType.STRING,
                description="Order identifier.",
            ),
        ),
    )


def _has_dotnet_10() -> bool:
    dotnet = shutil.which("dotnet")
    if dotnet is None:
        return False
    import subprocess

    result = subprocess.run(
        [dotnet, "--list-sdks"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return any(line.strip().startswith("10.") for line in result.stdout.splitlines())


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


@pytest.mark.skipif(not _has_dotnet_10(), reason=".NET 10 SDK is not installed")
def test_prepares_csharp_without_executing_customer_code(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        """
public static class OrderTools
{
    static OrderTools() => throw new System.Exception("must not execute during preparation");
    public static object LookupOrder(string order_id) => new { order_id };
}
""",
    )

    result = prepare_csharp_tools((spec,))

    assert result.failed_loads == ()
    [prepared] = result.tools
    assert prepared.launch.command.is_file()
    assert Path(prepared.launch.arguments[0]).name == "LocalToolAdapter.dll"


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_dotnet_10(), reason=".NET 10 SDK is not installed")
async def test_real_maf_round_trip_executes_csharp_tool(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        """
public static class OrderTools
{
    public static object LookupOrder(string order_id)
    {
        System.Console.WriteLine("customer diagnostic");
        return new { status = "shipped", order_id };
    }
}
""",
    )
    result = prepare_csharp_tools((spec,))
    assert result.failed_loads == ()
    [prepared] = result.tools
    [wrapper] = _maf_local_tools.build_maf_local_tools([prepared])

    await wrapper.connect()
    try:
        response = await wrapper.call_tool("lookup_order", order_id="A-42")
    finally:
        await wrapper.close()

    assert response[0].text == '{"status":"shipped","order_id":"A-42"}'


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_dotnet_10(), reason=".NET 10 SDK is not installed")
async def test_csharp_static_state_is_reused(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        """
public static class OrderTools
{
    private static long _count;
    public static string LookupOrder(string order_id) => $"{++_count}:{order_id}";
}
""",
    )
    [prepared] = prepare_csharp_tools((spec,)).tools
    [wrapper] = _maf_local_tools.build_maf_local_tools([prepared])

    await wrapper.connect()
    try:
        first = await wrapper.call_tool("lookup_order", order_id="A")
        second = await wrapper.call_tool("lookup_order", order_id="B")
    finally:
        await wrapper.close()

    assert first[0].text == "1:A"
    assert second[0].text == "2:B"


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_dotnet_10(), reason=".NET 10 SDK is not installed")
async def test_csharp_cancellation_restarts_process(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        """
public static class OrderTools
{
    private static string? _marker;
    public static string LookupOrder(string order_id)
    {
        if (order_id == "slow")
        {
            _marker = "dirty";
            System.Threading.Thread.Sleep(30_000);
        }
        return _marker ?? order_id;
    }
}
""",
    )
    [prepared] = prepare_csharp_tools((spec,)).tools
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


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_dotnet_10(), reason=".NET 10 SDK is not installed")
async def test_csharp_module_initializer_cannot_corrupt_protocol_stdout(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        """
using System.Runtime.CompilerServices;

public static class Startup
{
    [ModuleInitializer]
    public static void Initialize() => System.Console.WriteLine("module initialized");
}

public static class OrderTools
{
    public static string LookupOrder(string order_id) => order_id;
}
""",
    )
    [prepared] = prepare_csharp_tools((spec,)).tools
    [wrapper] = _maf_local_tools.build_maf_local_tools([prepared])

    await wrapper.connect()
    try:
        result = await wrapper.call_tool("lookup_order", order_id="A-42")
    finally:
        await wrapper.close()

    assert result[0].text == "A-42"


@pytest.mark.skipif(not _has_dotnet_10(), reason=".NET 10 SDK is not installed")
def test_csharp_protocol_cancellation_terminates_blocked_process(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        """
public static class OrderTools
{
    public static string LookupOrder(string order_id)
    {
        System.Threading.Thread.Sleep(30_000);
        return order_id;
    }
}
""",
    )
    [prepared] = prepare_csharp_tools((spec,)).tools
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
        process.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-11-25"},
                }
            )
            + "\n"
        )
        process.stdin.flush()
        assert json.loads(process.stdout.readline())["id"] == 1
        process.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "lookup_order",
                        "arguments": {"order_id": "slow"},
                    },
                }
            )
            + "\n"
        )
        process.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": 2, "reason": "test"},
                }
            )
            + "\n"
        )
        process.stdin.flush()
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_dotnet_10(), reason=".NET 10 SDK is not installed")
@pytest.mark.parametrize(
    "method",
    [
        "public static void LookupOrder(string order_id) { }",
        "public static async System.Threading.Tasks.Task LookupOrder(string order_id) { await System.Threading.Tasks.Task.Yield(); }",
        "public static async System.Threading.Tasks.ValueTask LookupOrder(string order_id) { await System.Threading.Tasks.Task.Yield(); }",
        "public static object? LookupOrder(string order_id) => null;",
    ],
)
def test_csharp_empty_results_have_no_content(tmp_path: Path, method: str) -> None:
    spec = _spec(tmp_path, f"public static class OrderTools {{ {method} }}")
    [prepared] = prepare_csharp_tools((spec,)).tools

    response = _raw_call(prepared, {"order_id": "A-42"})

    assert response["result"]["content"] == []


@pytest.mark.skipif(not _has_dotnet_10(), reason=".NET 10 SDK is not installed")
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
def test_csharp_integer_arguments_use_portable_json_semantics(
    tmp_path: Path, value: object, is_error: bool
) -> None:
    spec = _spec(
        tmp_path,
        """
public static class OrderTools
{
    public static long LookupOrder(long value) => value;
}
""",
        parameters=(
            LocalToolParameter(
                name="value",
                type=PortableToolType.INTEGER,
                description="Integer value.",
            ),
        ),
    )
    [prepared] = prepare_csharp_tools((spec,)).tools

    response = _raw_call(prepared, {"value": value})

    assert response["result"]["isError"] is is_error


@pytest.mark.skipif(not _has_dotnet_10(), reason=".NET 10 SDK is not installed")
def test_csharp_rejects_fractional_integer_before_rounding(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        """
public static class OrderTools
{
    public static long LookupOrder(long value) => value;
}
""",
        parameters=(
            LocalToolParameter(
                name="value",
                type=PortableToolType.INTEGER,
                description="Integer value.",
            ),
        ),
    )
    [prepared] = prepare_csharp_tools((spec,)).tools

    response = _raw_call(
        prepared,
        {},
        arguments_json='{"value":9007199254740990.6}',
    )

    assert response["result"]["isError"] is True


@pytest.mark.skipif(not _has_dotnet_10(), reason=".NET 10 SDK is not installed")
def test_csharp_rejects_non_finite_result(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        """
public static class OrderTools
{
    public static double LookupOrder(string order_id) => double.NaN;
}
""",
    )
    [prepared] = prepare_csharp_tools((spec,)).tools

    response = _raw_call(prepared, {"order_id": "A-42"})

    assert response["result"]["isError"] is True
    assert response["result"]["content"][0]["text"] == "Result must be finite."
