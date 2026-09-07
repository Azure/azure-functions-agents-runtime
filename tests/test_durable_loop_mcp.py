from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from agent_framework import FunctionTool

from azure_functions_agents.experimental import durable_loop_execution
from azure_functions_agents.experimental.durable_loop_activities import (
    InMemoryDurableContentStore,
)
from azure_functions_agents.experimental.durable_loop_execution import (
    DurableExecutionPlaneRouter,
)
from azure_functions_agents.experimental.durable_loop_mcp import DurableRemoteMcpLane
from azure_functions_agents.experimental.durable_loop_protocol import (
    DurableFaultProfile,
    FrozenToolDescriptorV1,
    SandboxExecutionProfile,
    ToolBehavior,
    ToolProvenance,
    ToolRequestV1,
    ToolResultStatus,
    canonical_hash,
    tool_request_hash,
)
from azure_functions_agents.experimental.durable_loop_receipts import (
    ActivityReceiptStatus,
    ActivityReceiptV1,
    InMemoryDurableKeyedDocumentStore,
    create_activity_receipt,
)
from azure_functions_agents.experimental.hybrid_apim import HybridApimClientManager


class _Configured:
    name = "learn"
    allowed_tools = ("microsoft_docs_search",)


class _Session:
    name = "learn"

    def __init__(self) -> None:
        self.connects = 0
        self.calls = 0
        self.closed = 0
        self.functions = [
            FunctionTool(
                name="microsoft_docs_search",
                description="Search Microsoft Learn.",
                func=None,
                input_model={
                    "additionalProperties": False,
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "type": "object",
                },
                additional_properties={
                    "_mcp_remote_name": "microsoft_docs_search"
                },
            )
        ]

    async def connect(self, *, reset: bool = False) -> None:
        assert reset is False
        self.connects += 1

    async def call_tool(self, tool_name: str, **kwargs: object):
        assert tool_name == "microsoft_docs_search"
        self.calls += 1
        return f"result:{kwargs['query']}"

    async def close(self) -> None:
        self.closed += 1


def _manager() -> HybridApimClientManager:
    return HybridApimClientManager(
        base_url="https://gateway.test/model/openai/v1",
        audience=None,
        subscription_key="private",
    )


def _request(behavior: ToolBehavior = ToolBehavior.READ_ONLY) -> ToolRequestV1:
    arguments = {"query": "durable functions"}
    request_hash = tool_request_hash(
        tool_name="microsoft_docs_search",
        arguments=arguments,
        behavior=behavior,
        provenance=ToolProvenance.REMOTE,
        policy_hash="a" * 64,
        catalog_hash="b" * 64,
        package_hash="c" * 64,
    )
    return ToolRequestV1(
        run_id="run-1",
        session_id="session-1",
        step_index=0,
        call_ordinal=0,
        provider_call_id="provider-call-1",
        call_key=canonical_hash({"call": 1}),
        tool_name="microsoft_docs_search",
        provenance=ToolProvenance.REMOTE,
        behavior=behavior,
        arguments=arguments,
        request_hash=request_hash,
        policy_hash="a" * 64,
        catalog_hash="b" * 64,
        package_hash="c" * 64,
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )


@pytest.mark.asyncio
async def test_remote_mcp_initializes_once_and_deduplicates_result() -> None:
    session = _Session()
    lane = DurableRemoteMcpLane(
        manager=_manager(),
        base_url="https://gateway.test/mcp",
        configured=(_Configured(),),
        content=InMemoryDurableContentStore(),
        receipts=InMemoryDurableKeyedDocumentStore(),
        session_factory=lambda _configured, _http_client: session,
    )

    descriptors = await lane.discover()
    first = await lane.dispatch(_request())
    replay = await lane.dispatch(_request())

    assert [item.name for item in descriptors] == ["microsoft_docs_search"]
    assert descriptors[0].provenance is ToolProvenance.REMOTE
    assert first.status is ToolResultStatus.SUCCEEDED
    assert first.value == "result:durable functions"
    assert replay.deduplicated is True
    assert session.connects == 1
    assert session.calls == 1
    assert "private" not in replay.model_dump_json()


@pytest.mark.asyncio
async def test_remote_mutating_started_receipt_terminalizes_ambiguous() -> None:
    receipts = InMemoryDurableKeyedDocumentStore()
    request = _request(ToolBehavior.MUTATING)
    await create_activity_receipt(
        receipts,
        f"activities/remote-mcp/{request.call_key}",
        ActivityReceiptV1(
            operation_key=request.call_key,
            request_hash=request.request_hash,
            kind="remote_mcp",
            status=ActivityReceiptStatus.STARTED,
            attempt=1,
            updated_at=datetime.now(UTC),
        ),
    )
    session = _Session()
    lane = DurableRemoteMcpLane(
        manager=_manager(),
        base_url="https://gateway.test/mcp",
        configured=(_Configured(),),
        content=InMemoryDurableContentStore(),
        receipts=receipts,
        session_factory=lambda _configured, _http_client: session,
    )

    result = await lane.dispatch(request)

    assert result.status is ToolResultStatus.AMBIGUOUS
    assert result.error is not None
    assert result.error.possibly_committed is True
    assert session.calls == 0


@pytest.mark.asyncio
async def test_remote_mutation_failure_after_effect_is_ambiguous_and_deduped() -> None:
    class SideEffectSession(_Session):
        async def call_tool(self, tool_name: str, **kwargs: object):
            await super().call_tool(tool_name, **kwargs)
            raise RuntimeError("acknowledgement lost")

    receipts = InMemoryDurableKeyedDocumentStore()
    session = SideEffectSession()
    lane = DurableRemoteMcpLane(
        manager=_manager(),
        base_url="https://gateway.test/mcp",
        configured=(_Configured(),),
        content=InMemoryDurableContentStore(),
        receipts=receipts,
        session_factory=lambda _configured, _http_client: session,
    )
    request = _request(ToolBehavior.MUTATING)

    first = await lane.dispatch(request)
    replay = await lane.dispatch(request)

    assert first.status is ToolResultStatus.AMBIGUOUS
    assert replay.status is ToolResultStatus.AMBIGUOUS
    assert replay.deduplicated is True
    assert session.calls == 1


@pytest.mark.asyncio
async def test_router_rejects_forged_tool_behavior_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = FrozenToolDescriptorV1(
        name="dangerous",
        description="Dangerous write.",
        parameters={"additionalProperties": True, "type": "object"},
        provenance=ToolProvenance.REMOTE,
        behavior=ToolBehavior.MUTATING,
    )

    class Remote:
        calls = 0

        async def discover(self):
            return (descriptor,)

        async def dispatch(self, request):
            self.calls += 1
            raise AssertionError(request)

    class Local:
        async def discover(self):
            return (), "c" * 64

        async def dispatch(self, request):
            raise AssertionError(request)

        async def cleanup(self, **_kwargs):
            return None

    (tmp_path / "durable-loop-tools.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "tools": {
                    "dangerous": {
                        "provenance": "remote",
                        "behavior": "mutating",
                        "parallel_safe": False,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        durable_loop_execution,
        "get_app_root",
        lambda: tmp_path,
    )
    remote = Remote()
    router = DurableExecutionPlaneRouter(remote=remote, local=Local())  # type: ignore[arg-type]
    snapshot = await router.freeze_catalog(
        policy_hash="a" * 64,
        sandbox_profile=SandboxExecutionProfile.PER_CALL,
    )
    arguments: dict[str, object] = {}
    request_hash = tool_request_hash(
        tool_name="dangerous",
        arguments=arguments,
        behavior=ToolBehavior.READ_ONLY,
        provenance=ToolProvenance.REMOTE,
        policy_hash=snapshot.catalog.policy_hash,
        catalog_hash=snapshot.catalog.catalog_hash,
        package_hash=snapshot.package_hash,
        sandbox_profile=SandboxExecutionProfile.PER_CALL,
        fault_profile=DurableFaultProfile.NONE,
    )
    forged = ToolRequestV1(
        run_id="run-1",
        session_id="session-1",
        step_index=0,
        call_ordinal=0,
        provider_call_id="call-1",
        call_key="d" * 64,
        tool_name="dangerous",
        provenance=ToolProvenance.REMOTE,
        behavior=ToolBehavior.READ_ONLY,
        arguments=arguments,
        request_hash=request_hash,
        policy_hash=snapshot.catalog.policy_hash,
        catalog_hash=snapshot.catalog.catalog_hash,
        package_hash=snapshot.package_hash,
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )

    with pytest.raises(RuntimeError, match="classification"):
        await router.dispatch(forged)
    assert remote.calls == 0
