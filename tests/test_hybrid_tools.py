import asyncio

import pytest
from agent_framework import FunctionInvocationContext, FunctionTool

from azure_functions_agents.experimental.hybrid_protocol import (
    HybridInvocationStatus,
    HybridToolInternalTimings,
    HybridToolInvocationResult,
)
from azure_functions_agents.experimental.hybrid_tools import HybridToolMiddleware


class _Backend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def invoke(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments: dict[str, object],
        deadline: float,
    ) -> HybridToolInvocationResult:
        assert deadline > asyncio.get_running_loop().time()
        self.calls.append((tool_name, arguments))
        return HybridToolInvocationResult(
            protocol_version="1",
            call_id=call_id,
            tool_name=tool_name,
            status=HybridInvocationStatus.SUCCESS,
            value={"ok": True},
            stdout="out",
            stderr="",
            exit_code=0,
            error=None,
            timings=HybridToolInternalTimings(
                queue_wait_ms=1,
                execution_ms=2,
                serialization_ms=3,
            ),
        )


def _tool(name: str) -> FunctionTool:
    async def handler(value: str) -> str:
        return value

    return FunctionTool(
        name=name,
        description=name,
        func=handler,
    )


@pytest.mark.asyncio
async def test_hybrid_middleware_routes_only_exact_local_stub() -> None:
    backend = _Backend()
    local = _tool("local")
    remote = _tool("remote")
    middleware = HybridToolMiddleware(
        backend,
        [local],
        deadline=asyncio.get_running_loop().time() + 30,
    )
    local_context = FunctionInvocationContext(local, {"value": "x"})
    remote_context = FunctionInvocationContext(remote, {"value": "y"})
    next_calls = 0

    async def call_next() -> None:
        nonlocal next_calls
        next_calls += 1
        remote_context.result = "remote"

    await middleware.process(local_context, call_next)
    await middleware.process(remote_context, call_next)

    assert backend.calls == [("local", {"value": "x"})]
    assert local_context.result == {
        "value": {"ok": True},
        "stdout": "out",
        "stderr": "",
        "exit_code": 0,
    }
    assert remote_context.result == "remote"
    assert next_calls == 1
