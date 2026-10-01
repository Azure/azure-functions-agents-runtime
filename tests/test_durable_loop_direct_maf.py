from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from agent_framework import (
    BaseChatClient,
    ChatResponse,
    Content,
    FunctionInvocationLayer,
    Message,
)

from azure_functions_agents.client_manager import InferenceTarget, MAFClientManager
from azure_functions_agents.experimental.durable_loop_activities import (
    BlobDurableContentStore,
    DirectMafOneStepModelProvider,
    InMemoryDurableContentStore,
    OneStepModelRequest,
)
from azure_functions_agents.experimental.durable_loop_config import (
    DURABLE_LOOP_BACKGROUND_MODEL_ENABLED_ENV,
    DURABLE_LOOP_ENABLED_ENV,
    DurableLoopConfigurationError,
    DurableLoopSettings,
)
from azure_functions_agents.experimental.durable_loop_execution import (
    DurableLoopExecutionBinding,
    DurableModelOnlyExecutionPlane,
    build_durable_execution_plane,
)
from azure_functions_agents.experimental.durable_loop_protocol import (
    DurableLoopBudgetV1,
    DurableRunIdentityV1,
    FrozenToolCatalogV1,
    MAFMessageBundleV1,
    SandboxExecutionProfile,
    WorkingContextV1,
    canonical_hash,
)
from azure_functions_agents.experimental.durable_loop_receipts import (
    BlobDurableKeyedDocumentStore,
    InMemoryDurableKeyedDocumentStore,
)
from azure_functions_agents.experimental.durable_loop_registration import (
    configure_durable_loop_execution_binding,
    get_durable_loop_activity_runtime,
    reset_durable_loop_activity_runtime_factory,
)
from azure_functions_agents.experimental.durable_loop_tools import (
    DurableToolDispatchError,
)

_TARGET = InferenceTarget(
    provider="foundry",
    model="foundry-model",
    endpoint="https://foundry.test/projects/project",
    api_version="responses-v1",
)


class _OneStepFoundryClient(FunctionInvocationLayer[Any], BaseChatClient[Any]):
    def __init__(self) -> None:
        super().__init__(function_invocation_configuration={"enabled": True})
        self.call_count = 0
        self.received_messages: list[list[Message]] = []
        self.received_options: list[dict[str, Any]] = []

    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Mapping[str, Any],
        **_kwargs: Any,
    ) -> Any:
        assert not stream
        self.call_count += 1
        self.received_messages.append(list(messages))
        self.received_options.append(dict(options))

        async def response() -> ChatResponse[Any]:
            return ChatResponse(
                messages=[Message("assistant", [Content.from_text("direct answer")])],
                response_id="foundry-response-1",
                finish_reason="stop",
                usage_details={
                    "input_token_count": 7,
                    "output_token_count": 3,
                },
            )

        return response()


class _FoundryManager(MAFClientManager):
    def __init__(self, client: _OneStepFoundryClient) -> None:
        self.client = client

    def resolve_model(self, requested: str | None) -> str:
        return requested or "foundry-model"

    def resolve_inference_target(self, model: str | None) -> InferenceTarget:
        assert self.resolve_model(model) == _TARGET.model
        return _TARGET

    def build_chat_client(self, model: str | None) -> Any:
        assert self.resolve_model(model) == _TARGET.model
        return self.client

    def build_chat_client_with_target(
        self,
        model: str | None,
    ) -> tuple[Any, InferenceTarget]:
        return self.build_chat_client(model), _TARGET


def _request(catalog: FrozenToolCatalogV1) -> OneStepModelRequest:
    now = datetime(2026, 10, 1, tzinfo=UTC)
    deployment_hash = canonical_hash(
        {
            "api_version": _TARGET.api_version,
            "endpoint": _TARGET.endpoint,
            "model": _TARGET.model,
            "provider": _TARGET.provider,
        }
    )
    identity = DurableRunIdentityV1(
        run_id="run-1",
        session_id="session-1",
        request_id_hash="a" * 64,
        request_hash="b" * 64,
        owner_hash="c" * 64,
        agent_slug="main",
        agent_hash="d" * 64,
        catalog_hash=catalog.catalog_hash,
        deployment_hash=deployment_hash,
        tool_package_hash=catalog.package_hash,
        policy_hash=catalog.policy_hash,
        orchestration_version="durable_agent_turn_orchestrator_v1",
        created_at=now,
        active_deadline=now + timedelta(hours=1),
        absolute_deadline=now + timedelta(days=1),
        budget=DurableLoopBudgetV1(
            max_model_steps=48,
            max_tool_calls=0,
            max_elapsed_seconds=3600,
            max_human_waits=0,
            human_wait_seconds=3600,
            max_argument_bytes=262144,
            max_result_bytes=1048576,
            context_max_bytes=4194304,
            context_compaction_percent=75,
            max_parallel_reads=4,
            continue_as_new_checkpoints=20,
        ),
    )
    bundle = MAFMessageBundleV1.create(
        messages=(
            {
                "role": "user",
                "contents": [{"type": "text", "text": "answer directly"}],
            },
        ),
        maf_core_version="1.17.0",
        provider="foundry",
        model="foundry-model",
        api_version="responses-v1",
    )
    return OneStepModelRequest(
        identity=identity,
        step_index=0,
        instructions="Answer without tools.",
        working_context=WorkingContextV1(
            bundle=bundle,
            compaction_generation=0,
            source_audit_hash=bundle.bundle_hash,
            source_start=0,
            source_end=1,
            estimated_tokens=3,
        ),
        catalog=catalog,
        model_settings={"background": False},
    )


@pytest.mark.asyncio
async def test_default_runtime_constructs_and_executes_direct_foundry_model_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = InMemoryDurableContentStore()
    receipts = InMemoryDurableKeyedDocumentStore()
    client = _OneStepFoundryClient()
    manager = _FoundryManager(client)
    monkeypatch.setenv(DURABLE_LOOP_ENABLED_ENV, "true")
    monkeypatch.setenv(DURABLE_LOOP_BACKGROUND_MODEL_ENABLED_ENV, "false")
    monkeypatch.setattr(
        BlobDurableContentStore,
        "from_environment",
        classmethod(lambda _cls: content),
    )
    monkeypatch.setattr(
        BlobDurableKeyedDocumentStore,
        "from_environment",
        classmethod(lambda _cls: receipts),
    )
    monkeypatch.setattr(
        "azure_functions_agents.experimental.durable_loop_registration.get_client_manager",
        lambda: manager,
    )
    configure_durable_loop_execution_binding(
        enabled_mcp_names=(),
        local_tools_enabled=False,
    )
    reset_durable_loop_activity_runtime_factory()

    try:
        runtime = get_durable_loop_activity_runtime()
        assert isinstance(runtime.model, DirectMafOneStepModelProvider)
        assert isinstance(runtime.tools, DurableModelOnlyExecutionPlane)
        assert runtime.background_model is None

        snapshot = await runtime.tools.freeze_catalog(
            policy_hash="1" * 64,
            sandbox_profile=SandboxExecutionProfile.PER_CALL,
        )
        assert snapshot.catalog.tools == ()
        decision = await runtime.model.run_one_step(_request(snapshot.catalog))

        assert client.call_count == 1
        assert client.function_invocation_configuration["enabled"] is False
        assert "background" not in client.received_options[0]
        assert decision.final_text == "direct answer"
        assert decision.usage.input_tokens == 7
        assert decision.usage.output_tokens == 3
        assert decision.assistant_message["role"] == "assistant"
    finally:
        configure_durable_loop_execution_binding(
            enabled_mcp_names=(),
            local_tools_enabled=True,
        )
        reset_durable_loop_activity_runtime_factory()


@pytest.mark.asyncio
async def test_direct_model_only_execution_plane_rejects_tool_dispatch() -> None:
    plane = DurableModelOnlyExecutionPlane()

    with pytest.raises(DurableToolDispatchError, match="cannot dispatch tools"):
        await plane.dispatch(object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("settings", "binding", "message"),
    [
        (
            DurableLoopSettings(background_model_enabled=True),
            DurableLoopExecutionBinding(
                enabled_mcp_names=(),
                local_tools_enabled=False,
            ),
            "background model execution requires HybridApimClientManager",
        ),
        (
            DurableLoopSettings(),
            DurableLoopExecutionBinding(
                enabled_mcp_names=("remote",),
                local_tools_enabled=False,
            ),
            "remote MCP tools require HybridApimClientManager",
        ),
        (
            DurableLoopSettings(),
            DurableLoopExecutionBinding(
                enabled_mcp_names=(),
                local_tools_enabled=True,
            ),
            "local tools require HybridApimClientManager",
        ),
    ],
)
def test_direct_maf_runtime_fails_closed_for_unsupported_combinations(
    settings: DurableLoopSettings,
    binding: DurableLoopExecutionBinding,
    message: str,
) -> None:
    with pytest.raises(DurableLoopConfigurationError, match=message):
        build_durable_execution_plane(
            client_manager=MAFClientManager(),
            settings=settings,
            content=InMemoryDurableContentStore(),
            receipts=InMemoryDurableKeyedDocumentStore(),
            binding=binding,
        )
