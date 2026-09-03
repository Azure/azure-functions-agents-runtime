import asyncio
import json

import pytest
from agent_framework import FunctionInvocationContext, FunctionTool

from azure_functions_agents.experimental.hybrid_config import (
    HYBRID_SANDBOX_GROUP_ENV,
    HYBRID_SANDBOX_REGION_ENV,
    HybridSandboxSettings,
)
from azure_functions_agents.experimental.hybrid_observability import HybridMetric
from azure_functions_agents.experimental.hybrid_protocol import (
    HybridInvocationStatus,
    HybridToolDescriptor,
    HybridToolInternalTimings,
    HybridToolInvocationResult,
    HybridToolManifest,
    HybridToolProvenance,
    canonical_hybrid_json_bytes,
)
from azure_functions_agents.experimental.hybrid_tools import (
    HybridToolMiddleware,
    InvocationSandboxLease,
    open_hybrid_invocation,
)
from azure_functions_agents.transport.transport_models import SandboxFileNotFoundError


class _Backend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def invoke(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments: dict[str, object],
        deadline: float,
    ) -> HybridToolInvocationResult:
        assert deadline > asyncio.get_running_loop().time()
        self.calls.append((call_id, tool_name, arguments))
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
    local_context = FunctionInvocationContext(
        local,
        {"value": "x"},
        metadata={"call_id": "maf-call-1"},
    )
    remote_context = FunctionInvocationContext(remote, {"value": "y"})
    next_calls = 0

    async def call_next() -> None:
        nonlocal next_calls
        next_calls += 1
        remote_context.result = "remote"

    await middleware.process(local_context, call_next)
    await middleware.process(remote_context, call_next)

    assert backend.calls == [("maf-call-1", "local", {"value": "x"})]
    assert local_context.result == {
        "value": {"ok": True},
        "stdout": "out",
        "stderr": "",
        "exit_code": 0,
    }
    assert remote_context.result == "remote"
    assert next_calls == 1


class _Handle:
    def __init__(self) -> None:
        self.results: dict[str, bytes] = {}
        self.requests: list[dict[str, object]] = []
        self.deleted = 0
        self.closed = 0

    async def write_file(
        self, path: str, content: bytes, *, create_dirs: bool = False
    ) -> None:
        assert create_dirs
        if "/requests/" not in path:
            return
        request = json.loads(content)
        self.requests.append(request)
        call_id = str(request["call_id"])
        result = HybridToolInvocationResult(
            protocol_version="1",
            call_id=call_id,
            tool_name=str(request["tool_name"]),
            status=HybridInvocationStatus.SUCCESS,
            value={"sequence": len(self.requests)},
            stdout="",
            stderr="",
            exit_code=None,
            error=None,
            timings=HybridToolInternalTimings(
                queue_wait_ms=1,
                execution_ms=2,
                serialization_ms=3,
            ),
        )
        self.results[path.replace("/requests/", "/results/")] = canonical_hybrid_json_bytes(
            result
        )

    async def read_file(self, path: str) -> bytes:
        try:
            return self.results[path]
        except KeyError:
            raise SandboxFileNotFoundError("missing") from None

    async def delete(self) -> None:
        self.deleted += 1

    async def close(self) -> None:
        self.closed += 1


class _Provider:
    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


def _settings() -> HybridSandboxSettings:
    return HybridSandboxSettings(
        group_resource_id="group",
        region="westus2",
        allowed_hosts=(),
        sandbox_disk="python-3.13",
        create_timeout_seconds=5,
        ready_timeout_seconds=5,
        drain_timeout_seconds=1,
        auto_delete_seconds=60,
        orphan_age_seconds=1200,
    )


def _manifest() -> HybridToolManifest:
    return HybridToolManifest(
        protocol_version="1",
        tools=(
            HybridToolDescriptor(
                name="customer_probe",
                description="probe",
                parameters={
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                    "additionalProperties": False,
                },
                provenance=HybridToolProvenance.LOCAL,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_lease_shares_one_handle_for_sequential_calls_and_deletes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = _Handle()
    provider = _Provider()
    recorded_values: list[tuple[HybridMetric, float]] = []
    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools.record_hybrid_value",
        lambda metric, value: recorded_values.append((metric, value)),
    )
    lease = InvocationSandboxLease(
        settings=_settings(),
        operation_id="operation",
        provider=provider,  # type: ignore[arg-type]
        handle=handle,  # type: ignore[arg-type]
        manifest=_manifest(),
    )
    deadline = asyncio.get_running_loop().time() + 5

    first = await lease.invoke(
        call_id="first",
        tool_name="customer_probe",
        arguments={"message": "a"},
        deadline=deadline,
    )
    second = await lease.invoke(
        call_id="second",
        tool_name="customer_probe",
        arguments={"message": "b"},
        deadline=deadline,
    )
    await lease.close()
    await lease.close()

    assert first.value == {"sequence": 1}
    assert second.value == {"sequence": 2}
    assert [request["call_id"] for request in handle.requests] == ["first", "second"]
    assert handle.deleted == 1
    assert handle.closed == 1
    assert provider.closed == 1
    assert (
        len(
            [
                value
                for metric, value in recorded_values
                if metric is HybridMetric.TOOL_QUEUE_DURATION
            ]
        )
        == 2
    )


@pytest.mark.asyncio
async def test_hybrid_context_closes_lease_on_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Lease:
        closed = False

        def build_tools(
            self, *, deadline: float
        ) -> tuple[list[FunctionTool], HybridToolMiddleware]:
            assert deadline > asyncio.get_running_loop().time()
            backend = _Backend()
            tool = _tool("local")
            return [tool], HybridToolMiddleware(backend, [tool], deadline=deadline)

        async def close(self) -> None:
            self.closed = True

    lease = _Lease()

    async def acquire(
        settings: HybridSandboxSettings,
        **_kwargs: object,
    ) -> _Lease:
        assert settings.region == "westus2"
        return lease

    monkeypatch.setenv(HYBRID_SANDBOX_GROUP_ENV, "group")
    monkeypatch.setenv(HYBRID_SANDBOX_REGION_ENV, "westus2")
    monkeypatch.setattr(InvocationSandboxLease, "acquire", acquire)

    with pytest.raises(asyncio.CancelledError):
        async with open_hybrid_invocation(
            timeout_seconds=30,
            tools=[],
            sandbox_tools=None,
            skill_paths=None,
            web_request_tools=None,
            workflow_enabled=False,
            subagents=None,
        ):
            raise asyncio.CancelledError

    assert lease.closed
