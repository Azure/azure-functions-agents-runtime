from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from azure_functions_agents.experimental.durable_loop_protocol import (
    FrozenToolDescriptorV1,
    ToolBehavior,
    ToolProvenance,
    ToolRequestV1,
    ToolResultStatus,
    deterministic_call_key,
    tool_request_hash,
)
from azure_functions_agents.experimental.durable_loop_tools import (
    DurableToolRegistry,
)

_HASH = "a" * 64


def _descriptor(
    name: str,
    *,
    behavior: ToolBehavior,
    parallel_safe: bool = False,
) -> FrozenToolDescriptorV1:
    return FrozenToolDescriptorV1(
        name=name,
        description=name,
        parameters={"additionalProperties": False, "type": "object"},
        provenance=ToolProvenance.REMOTE,
        behavior=behavior,
        parallel_safe=parallel_safe,
    )


def _request(
    descriptor: FrozenToolDescriptorV1,
    *,
    argument_byte_limit: int = 1024 * 1024,
    result_byte_limit: int = 8 * 1024 * 1024,
) -> ToolRequestV1:
    arguments: dict[str, object] = {}
    request_hash = tool_request_hash(
        tool_name=descriptor.name,
        arguments=arguments,
        behavior=descriptor.behavior,
        provenance=descriptor.provenance,
        argument_byte_limit=argument_byte_limit,
        result_byte_limit=result_byte_limit,
        policy_hash=_HASH,
        catalog_hash="b" * 64,
        package_hash="c" * 64,
    )
    return ToolRequestV1(
        run_id="run-1",
        step_index=0,
        call_ordinal=0,
        provider_call_id="call-1",
        call_key=deterministic_call_key(
            run_id="run-1",
            step_index=0,
            call_ordinal=0,
            decision_hash="d" * 64,
            tool_name=descriptor.name,
        ),
        tool_name=descriptor.name,
        provenance=descriptor.provenance,
        behavior=descriptor.behavior,
        arguments=arguments,
        argument_byte_limit=argument_byte_limit,
        result_byte_limit=result_byte_limit,
        request_hash=request_hash,
        policy_hash=_HASH,
        catalog_hash="b" * 64,
        package_hash="c" * 64,
        deadline=datetime(2026, 9, 4, tzinfo=UTC),
    )


def _request_at(
    descriptor: FrozenToolDescriptorV1,
    *,
    ordinal: int,
) -> ToolRequestV1:
    request = _request(descriptor)
    return request.model_copy(
        update={
            "call_ordinal": ordinal,
            "provider_call_id": f"call-{ordinal}",
            "call_key": deterministic_call_key(
                run_id="run-1",
                step_index=0,
                call_ordinal=ordinal,
                decision_hash="d" * 64,
                tool_name=descriptor.name,
            ),
        }
    )


def test_tool_request_accepts_configured_max_payload_limits() -> None:
    request = _request(
        _descriptor("max_bounded_read", behavior=ToolBehavior.READ_ONLY),
        argument_byte_limit=1024 * 1024,
        result_byte_limit=8 * 1024 * 1024,
    )

    assert request.argument_byte_limit == 1024 * 1024
    assert request.result_byte_limit == 8 * 1024 * 1024


def test_tool_request_hash_binds_payload_limits() -> None:
    request = _request(
        _descriptor("bounded_read", behavior=ToolBehavior.READ_ONLY)
    )
    tampered = request.model_dump()
    tampered["result_byte_limit"] = 1024

    with pytest.raises(ValueError, match="tool request hash mismatch"):
        ToolRequestV1.model_validate(tampered)


@pytest.mark.asyncio
async def test_dispatch_registry_deduplicates_idempotent_write() -> None:
    registry = DurableToolRegistry()
    descriptor = _descriptor("write_counter", behavior=ToolBehavior.IDEMPOTENT_WRITE)
    effects: list[str] = []

    def handler(_arguments, call_key):
        effects.append(call_key)
        return {"count": len(effects)}

    registry.register(descriptor, handler)
    dispatcher = registry.build_dispatcher()
    request = _request(descriptor)

    first = await dispatcher.dispatch(request)
    second = await dispatcher.dispatch(request)

    assert first.status is ToolResultStatus.SUCCEEDED
    assert second.deduplicated
    assert effects == [request.call_key]


@pytest.mark.asyncio
async def test_distinct_read_calls_can_execute_concurrently() -> None:
    registry = DurableToolRegistry()
    descriptor = _descriptor(
        "parallel_read",
        behavior=ToolBehavior.READ_ONLY,
        parallel_safe=True,
    )
    both_started = asyncio.Event()
    release = asyncio.Event()
    active = 0

    async def handler(_arguments, _call_key):
        nonlocal active
        active += 1
        if active == 2:
            both_started.set()
        await release.wait()
        active -= 1
        return {"ok": True}

    registry.register(descriptor, handler)
    dispatcher = registry.build_dispatcher()
    first = asyncio.create_task(dispatcher.dispatch(_request_at(descriptor, ordinal=0)))
    second = asyncio.create_task(dispatcher.dispatch(_request_at(descriptor, ordinal=1)))

    await asyncio.wait_for(both_started.wait(), timeout=1)
    release.set()
    results = await asyncio.gather(first, second)

    assert all(result.status is ToolResultStatus.SUCCEEDED for result in results)


@pytest.mark.asyncio
async def test_dispatch_registry_returns_typed_timeout_and_failure() -> None:
    registry = DurableToolRegistry()
    timeout = _descriptor("timeout", behavior=ToolBehavior.READ_ONLY)
    failure = _descriptor("failure", behavior=ToolBehavior.MUTATING)

    def time_out(_arguments, _call_key):
        raise TimeoutError

    def fail(_arguments, _call_key):
        raise RuntimeError("secret provider detail")

    registry.register(timeout, time_out)
    registry.register(failure, fail)
    dispatcher = registry.build_dispatcher()

    timeout_result = await dispatcher.dispatch(_request(timeout))
    failure_result = await dispatcher.dispatch(_request(failure))

    assert timeout_result.status is ToolResultStatus.TIMED_OUT
    assert timeout_result.error is not None
    assert timeout_result.error.code == "tool_timeout"
    assert failure_result.status is ToolResultStatus.FAILED
    assert failure_result.error is not None
    assert failure_result.error.code == "tool_execution_failed"
    assert "secret" not in failure_result.model_dump_json()
