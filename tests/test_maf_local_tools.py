from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from azure_functions_agents._local_tool_descriptor import (
    LocalToolParameter,
    LocalToolSpec,
    PortableToolType,
)
from azure_functions_agents.harness.agent_framework import _maf_local_tools
from azure_functions_agents.local_tools.javascript import prepare_javascript_tools


@pytest.fixture(autouse=True)
def clear_adapter_cache() -> Iterator[None]:
    _maf_local_tools.clear_maf_local_tools_cache()
    yield
    _maf_local_tools.clear_maf_local_tools_cache()


def _spec(tmp_path: Path, source: str) -> LocalToolSpec:
    source_path = tmp_path / "greet.js"
    source_path.write_text(source, encoding="utf-8")
    metadata_path = tmp_path / "tool.md"
    metadata_path.write_text("", encoding="utf-8")
    return LocalToolSpec.create(
        name="greet",
        description="Create a friendly greeting.",
        entrypoint="greet",
        source_path=source_path.resolve(),
        metadata_path=metadata_path.resolve(),
        parameters=(
            LocalToolParameter(
                name="name",
                type=PortableToolType.STRING,
                description="Person to greet.",
            ),
        ),
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_builds_cached_maf_stdio_wrapper(tmp_path: Path) -> None:
    [prepared] = prepare_javascript_tools(
        (_spec(tmp_path, "export function greet(name) { return `Hello, ${name}!`; }\n"),)
    ).tools

    [first] = _maf_local_tools.build_maf_local_tools([prepared])
    [second] = _maf_local_tools.build_maf_local_tools([prepared])

    assert first is second
    assert first.name == "local_greet"
    assert first.allowed_tools == ["greet"]
    assert first.load_tools_flag is True
    assert first.load_prompts_flag is False
    assert first.approval_mode == "never_require"


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
async def test_real_maf_round_trip_executes_javascript_tool(tmp_path: Path) -> None:
    source = """
export function greet(name) {
  console.log("customer diagnostic");
  process.stdout.write("customer stdout\\n");
  return { greeting: `Hello, ${name}!` };
}
"""
    [prepared] = prepare_javascript_tools((_spec(tmp_path, source),)).tools
    [wrapper] = _maf_local_tools.build_maf_local_tools([prepared])

    await wrapper.connect()
    try:
        result = await wrapper.call_tool("greet", name="Ada")
    finally:
        await wrapper.close()

    assert len(result) == 1
    assert result[0].text == '{"greeting":"Hello, Ada!"}'


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
async def test_wrapper_reuses_worker_local_javascript_module_state(tmp_path: Path) -> None:
    source = """
let count = 0;
export async function greet(name) {
  await Promise.resolve();
  count += 1;
  return `${count}:${name}`;
}
"""
    [prepared] = prepare_javascript_tools((_spec(tmp_path, source),)).tools
    [wrapper] = _maf_local_tools.build_maf_local_tools([prepared])

    await wrapper.connect()
    try:
        first = await wrapper.call_tool("greet", name="Ada")
        second = await wrapper.call_tool("greet", name="Lin")
    finally:
        await wrapper.close()

    assert first[0].text == "1:Ada"
    assert second[0].text == "2:Lin"


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
async def test_adapter_rejects_invalid_arguments(tmp_path: Path) -> None:
    [prepared] = prepare_javascript_tools(
        (_spec(tmp_path, "export function greet(name) { return name; }\n"),)
    ).tools
    [wrapper] = _maf_local_tools.build_maf_local_tools([prepared])

    await wrapper.connect()
    try:
        with pytest.raises(Exception, match="Missing required argument"):
            await wrapper.call_tool("greet")
        with pytest.raises(Exception, match="must be string"):
            await wrapper.call_tool("greet", name=42)
    finally:
        await wrapper.close()


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
async def test_adapter_cancellation_restarts_javascript_worker(tmp_path: Path) -> None:
    source = """
export async function greet(name) {
  if (name === "slow") {
    globalThis.cancelMarker = "dirty";
    await new Promise((resolve) => setTimeout(resolve, 30_000));
  }
  return globalThis.cancelMarker ?? name;
}
"""
    [prepared] = prepare_javascript_tools((_spec(tmp_path, source),)).tools
    [wrapper] = _maf_local_tools.build_maf_local_tools([prepared])

    await wrapper.connect()
    try:
        call = asyncio.create_task(wrapper.call_tool("greet", name="slow"))
        await asyncio.sleep(0.1)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        await asyncio.sleep(0.1)
        result = await wrapper.call_tool("greet", name="fast")
    finally:
        await wrapper.close()

    assert result[0].text == "fast"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_adapter_diagnostics_do_not_expose_customer_source(tmp_path: Path) -> None:
    source = 'export function greet(name) { throw new Error(`failed: ${name}`); }\n'
    [prepared] = prepare_javascript_tools((_spec(tmp_path, source),)).tools
    [wrapper] = _maf_local_tools.build_maf_local_tools([prepared])

    process = subprocess.Popen(
        [wrapper.command, *wrapper.args],
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
                    "params": {"name": "greet", "arguments": {"name": "Ada"}},
                }
            )
            + "\n"
        )
        process.stdin.flush()
        assert json.loads(process.stdout.readline())["result"]["isError"] is True
    finally:
        process.terminate()
        _, diagnostics = process.communicate(timeout=5)

    assert "data:text/javascript;base64" not in diagnostics
    assert source not in diagnostics
    assert "<javascript-tool:greet>" in diagnostics


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
async def test_adapter_rejects_source_changed_after_preparation(tmp_path: Path) -> None:
    spec = _spec(tmp_path, "export function greet(name) { return name; }\n")
    [prepared] = prepare_javascript_tools((spec,)).tools
    spec.source_path.write_text(
        "export function greet(name) { return `changed ${name}`; }\n",
        encoding="utf-8",
    )
    [wrapper] = _maf_local_tools.build_maf_local_tools([prepared])

    await wrapper.connect()
    try:
        with pytest.raises(Exception, match="source changed"):
            await wrapper.call_tool("greet", name="Ada")
    finally:
        await wrapper.close()
