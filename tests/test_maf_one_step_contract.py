from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from typing import Any

import httpx
import pytest
from agent_framework import (
    Agent,
    BaseChatClient,
    ChatResponse,
    Content,
    FunctionInvocationLayer,
    FunctionTool,
    Message,
)
from agent_framework.openai import OpenAIChatClient
from openai import AsyncOpenAI

from azure_functions_agents.client_manager import ClientManager, InferenceTarget
from azure_functions_agents.experimental.durable_loop_activities import (
    DurableLoopModelError,
    DurableLoopProviderTerminalError,
    MafOneStepModelProvider,
    OneStepModelRequest,
    append_model_and_tool_messages,
)
from azure_functions_agents.experimental.durable_loop_protocol import (
    DurableLoopBudgetV1,
    DurableRunIdentityV1,
    FrozenToolCatalogV1,
    FrozenToolDescriptorV1,
    MAFMessageBundleV1,
    ToolBehavior,
    ToolProvenance,
    ToolResultStatus,
    ToolResultV1,
    WorkingContextV1,
    canonical_hash,
)


class _OneStepChatClient(FunctionInvocationLayer[Any], BaseChatClient[Any]):
    def __init__(
        self,
        contents: Sequence[Content],
        *,
        finish_reason: str = "tool_calls",
        invocation_enabled: bool = False,
        usage_details: Mapping[str, int] | None = None,
    ) -> None:
        super().__init__(
            function_invocation_configuration={"enabled": invocation_enabled}
        )
        self.call_count = 0
        self.received_messages: list[list[Message]] = []
        self.received_options: list[dict[str, Any]] = []
        self.received_kwargs: list[dict[str, Any]] = []
        self._contents = list(contents)
        self._finish_reason = finish_reason
        self._usage_details = dict(usage_details or {})

    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> Any:
        assert not stream
        self.call_count += 1
        self.received_messages.append(list(messages))
        self.received_options.append(dict(options))
        self.received_kwargs.append(dict(kwargs))

        async def response() -> ChatResponse[Any]:
            return ChatResponse(
                messages=[Message("assistant", self._contents)],
                response_id="response-1",
                finish_reason=self._finish_reason,
                usage_details=self._usage_details,
            )

        return response()


class _ClientManager(ClientManager):
    def __init__(
        self,
        client: Any,
        target: InferenceTarget | None = None,
    ) -> None:
        self.client = client
        self.target = target

    def resolve_model(self, requested: str | None) -> str:
        return requested or "model"

    def build_chat_client(self, model: str | None) -> Any:
        assert model == "model"
        return self.client

    def build_chat_client_with_target(
        self,
        model: str | None,
    ) -> tuple[Any, InferenceTarget]:
        return self.build_chat_client(model), self.target or InferenceTarget()


def _provider_request() -> OneStepModelRequest:
    now = datetime(2026, 9, 4, tzinfo=UTC)
    identity = DurableRunIdentityV1(
        run_id="run-1",
        session_id="session-1",
        request_id_hash="a" * 64,
        request_hash="b" * 64,
        owner_hash="c" * 64,
        agent_slug="main",
        agent_hash="d" * 64,
        catalog_hash="e" * 64,
        deployment_hash="f" * 64,
        tool_package_hash="1" * 64,
        policy_hash="2" * 64,
        orchestration_version="durable_agent_turn_orchestrator_v1",
        created_at=now,
        active_deadline=now + timedelta(hours=4),
        absolute_deadline=now + timedelta(days=7),
        budget=DurableLoopBudgetV1(
            max_model_steps=48,
            max_tool_calls=128,
            max_cost_microunits=100,
            input_cost_microunits_per_million_tokens=1_000_000,
            output_cost_microunits_per_million_tokens=2_000_000,
            max_elapsed_seconds=14400,
            max_human_waits=8,
            human_wait_seconds=86400,
            max_argument_bytes=262144,
            max_result_bytes=1048576,
            context_max_bytes=4194304,
            context_compaction_percent=75,
            max_parallel_reads=4,
            continue_as_new_checkpoints=20,
        ),
    )
    messages = (
        {"role": "user", "contents": [{"type": "text", "text": "work"}]},
    )
    return OneStepModelRequest(
        identity=identity,
        step_index=0,
        instructions="Respond.",
        working_context=WorkingContextV1(
            bundle=MAFMessageBundleV1.create(
                messages=messages,
                maf_core_version="1.17.0",
                provider="openai",
                model="model",
                api_version="responses-v1",
            ),
            compaction_generation=0,
            source_audit_hash=canonical_hash(messages),
            source_start=0,
            source_end=1,
            estimated_tokens=4,
        ),
        catalog=FrozenToolCatalogV1.create(
            tools=(),
            policy_hash="a" * 64,
            package_hash="b" * 64,
        ),
        model_settings={},
    )


def test_target_maf_versions_are_exact() -> None:
    assert version("agent-framework-core") == "1.17.0"
    assert version("agent-framework-openai") == "1.14.2"
    assert version("agent-framework-foundry") == "1.12.0"


def test_target_maf_lock_uses_portable_official_release_assets() -> None:
    lock = (Path(__file__).parents[1] / "uv.lock").read_text(encoding="utf-8")

    assert "C:/Users" not in lock
    assert "session-state" not in lock
    assert (
        "https://github.com/microsoft/agent-framework/releases/download/"
        "python-1.17.0/agent_framework_core-1.17.0-py3-none-any.whl"
    ) in lock
    assert "agent_framework_openai-1.14.2-py3-none-any.whl" in lock
    assert "agent_framework_foundry-1.12.0-py3-none-any.whl" in lock


@pytest.mark.asyncio
async def test_one_step_agent_returns_ordered_calls_without_invoking_tools() -> None:
    invocations: list[str] = []

    def first_tool() -> str:
        invocations.append("first")
        return "first"

    def second_tool() -> str:
        invocations.append("second")
        return "second"

    client = _OneStepChatClient(
        [
            Content.from_function_call(
                "call-1",
                "first_tool",
                arguments={"value": 1},
            ),
            Content.from_function_call(
                "call-2",
                "second_tool",
                arguments={"value": 2},
            ),
        ],
    )
    agent = Agent(
        client=client,
        instructions="Choose tools.",
        tools=[
            FunctionTool(name="first_tool", func=first_tool),
            FunctionTool(name="second_tool", func=second_tool),
        ],
        context_providers=[],
    )

    response = await agent.run(
        [Message("user", [Content.from_text("work")])],
        session=None,
    )

    assert client.call_count == 1
    assert invocations == []
    assert [
        (content.call_id, content.name, content.arguments)
        for content in response.messages[0].contents
    ] == [
        ("call-1", "first_tool", {"value": 1}),
        ("call-2", "second_tool", {"value": 2}),
    ]
    assert len(client.received_messages) == 1
    assert [message.role for message in client.received_messages[0]] == ["user"]
    assert client.received_options[0]["instructions"] == "Choose tools."


@pytest.mark.asyncio
async def test_one_step_agent_has_no_implicit_session_or_history_state() -> None:
    client = _OneStepChatClient([Content.from_text("done")])
    agent = Agent(
        client=client,
        instructions="Respond.",
        context_providers=[],
    )
    explicit = [
        Message("assistant", [Content.from_text("previous")]),
        Message("user", [Content.from_text("continue")]),
    ]

    response = await agent.run(explicit, session=None)

    assert response.text == "done"
    assert client.call_count == 1
    assert [message.role for message in client.received_messages[0]] == [
        "assistant",
        "user",
    ]
    assert client.received_messages[0] == explicit
    assert client.received_options[0]["instructions"] == "Respond."


def test_maf_message_round_trip_preserves_reasoning_call_and_result_fields() -> None:
    message = Message(
        "assistant",
        [
            Content.from_text_reasoning(
                id="reasoning-1",
                text="summary",
                protected_data="encrypted-reasoning",
                additional_properties={"provider_item_id": "item-1"},
            ),
            Content.from_function_call(
                "call-1",
                "lookup",
                arguments={"region": "westus3"},
                id="function-1",
                additional_properties={"provider_item_id": "item-2"},
            ),
        ],
        message_id="message-1",
        additional_properties={"trace": "retained"},
    )
    tool_message = Message(
        "tool",
        [
            Content.from_function_result(
                "call-1",
                result={"healthy": True},
                additional_properties={"provider_item_id": "item-3"},
            )
        ],
    )

    restored = [
        Message.from_dict(message.to_dict()),
        Message.from_dict(tool_message.to_dict()),
    ]

    assert [item.to_dict() for item in restored] == [
        message.to_dict(),
        tool_message.to_dict(),
    ]
    reasoning = restored[0].contents[0]
    assert reasoning.protected_data == "encrypted-reasoning"
    assert reasoning.additional_properties["provider_item_id"] == "item-1"
    call = restored[0].contents[1]
    assert call.call_id == "call-1"
    assert call.arguments == {"region": "westus3"}
    result = restored[1].contents[0]
    assert result.call_id == "call-1"
    assert result.result == '{"healthy": true}'


@pytest.mark.asyncio
async def test_runtime_maf_provider_builds_fresh_stateless_one_step_agent() -> None:
    client = _OneStepChatClient(
        [
            Content.from_text_reasoning(protected_data="encrypted-reasoning"),
            Content.from_function_call(
                "call-1",
                "lookup",
                arguments={"region": "westus3"},
            ),
        ],
        invocation_enabled=True,
        usage_details={
            "input_token_count": 3,
            "openai.reasoning_tokens": 5,
            "output_token_count": 7,
        },
    )
    now = datetime(2026, 9, 4, tzinfo=UTC)
    identity = DurableRunIdentityV1(
        run_id="run-1",
        session_id="session-1",
        request_id_hash="a" * 64,
        request_hash="b" * 64,
        owner_hash="c" * 64,
        agent_slug="main",
        agent_hash="d" * 64,
        catalog_hash="e" * 64,
        deployment_hash="f" * 64,
        tool_package_hash="1" * 64,
        policy_hash="2" * 64,
        orchestration_version="durable_agent_turn_orchestrator_v1",
        created_at=now,
        active_deadline=now + timedelta(hours=4),
        absolute_deadline=now + timedelta(days=7),
        budget=DurableLoopBudgetV1(
            max_model_steps=48,
            max_tool_calls=128,
            max_cost_microunits=100,
            input_cost_microunits_per_million_tokens=1_000_000,
            output_cost_microunits_per_million_tokens=2_000_000,
            max_elapsed_seconds=14400,
            max_human_waits=8,
            human_wait_seconds=86400,
            max_argument_bytes=262144,
            max_result_bytes=1048576,
            context_max_bytes=4194304,
            context_compaction_percent=75,
            max_parallel_reads=4,
            continue_as_new_checkpoints=20,
        ),
    )
    descriptor = FrozenToolDescriptorV1(
        name="lookup",
        description="Lookup a region.",
        parameters={
            "additionalProperties": False,
            "properties": {"region": {"type": "string"}},
            "required": ["region"],
            "type": "object",
        },
        provenance=ToolProvenance.REMOTE,
        behavior=ToolBehavior.READ_ONLY,
    )
    catalog = FrozenToolCatalogV1.create(
        tools=(descriptor,),
        policy_hash=identity.policy_hash,
        package_hash=identity.tool_package_hash,
    )
    messages = (
        {"role": "user", "contents": [{"type": "text", "text": "work"}]},
    )
    request = OneStepModelRequest(
        identity=identity,
        step_index=0,
        instructions="Choose a tool.",
        working_context=WorkingContextV1(
            bundle=MAFMessageBundleV1.create(
                messages=messages,
                maf_core_version="1.17.0",
                provider="openai",
                model="model",
                api_version="responses-v1",
            ),
            compaction_generation=0,
            source_audit_hash=canonical_hash(messages),
            source_start=0,
            source_end=1,
            estimated_tokens=4,
        ),
        catalog=catalog,
        model_settings={"temperature": 0},
    )

    decision = await MafOneStepModelProvider(_ClientManager(client)).run_one_step(
        request
    )

    assert client.call_count == 1
    assert client.function_invocation_configuration["enabled"] is False
    assert client.received_options[0]["store"] is False
    assert client.received_options[0]["model"] == "model"
    assert client.received_options[0]["include"] == [
        "reasoning.encrypted_content"
    ]
    assert [(call.call_id, call.name) for call in decision.tool_calls] == [
        ("call-1", "lookup")
    ]
    assert decision.usage.reasoning_tokens == 5
    assert decision.usage.cost_microunits == 17
    assert decision.response_id_hash is not None
    assert "response-1" not in decision.model_dump_json()
    reasoning = decision.assistant_message["contents"][0]  # type: ignore[index]
    assert reasoning["protected_data"] == "encrypted-reasoning"  # type: ignore[index]


@pytest.mark.asyncio
async def test_runtime_maf_provider_rejects_blank_terminal_text() -> None:
    client = _OneStepChatClient(
        [Content.from_text("")],
        finish_reason="stop",
    )

    with pytest.raises(
        DurableLoopProviderTerminalError,
        match="model_response_incomplete:empty",
    ):
        await MafOneStepModelProvider(_ClientManager(client)).run_one_step(
            _provider_request()
        )


@pytest.mark.asyncio
async def test_runtime_maf_provider_rejects_partial_length_response() -> None:
    client = _OneStepChatClient(
        [Content.from_text("partial answer")],
        finish_reason="length",
        usage_details={
            "input_token_count": 70,
            "openai.reasoning_tokens": 12000,
            "output_token_count": 12000,
        },
    )

    with pytest.raises(DurableLoopProviderTerminalError) as raised:
        await MafOneStepModelProvider(_ClientManager(client)).run_one_step(
            _provider_request()
        )

    assert raised.value.code == "model_response_incomplete"
    assert raised.value.usage.output_tokens == 12000
    assert raised.value.usage.reasoning_tokens == 12000


@pytest.mark.asyncio
async def test_real_openai_responses_client_sends_one_request_per_invocation() -> None:
    requests: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://example.test/v1/responses"
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "created_at": 1,
                "error": None,
                "id": "resp_1",
                "incomplete_details": None,
                "instructions": "Choose a tool.",
                "max_output_tokens": None,
                "metadata": {},
                "model": "model",
                "object": "response",
                "output": [
                    {
                        "encrypted_content": "cipher",
                        "id": "rs_1",
                        "summary": [
                            {"text": "summary", "type": "summary_text"}
                        ],
                        "type": "reasoning",
                    },
                    {
                        "arguments": '{"region":"westus3"}',
                        "call_id": "call_1",
                        "id": "fc_1",
                        "name": "lookup",
                        "status": "completed",
                        "type": "function_call",
                    },
                ],
                "parallel_tool_calls": True,
                "previous_response_id": None,
                "status": "completed",
                "temperature": 0,
                "tool_choice": "auto",
                "tools": [],
                "top_p": 1,
                "truncation": "disabled",
                "usage": {
                    "input_tokens": 1,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 2,
                    "output_tokens_details": {"reasoning_tokens": 1},
                    "total_tokens": 3,
                },
                "user": None,
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sdk = AsyncOpenAI(
        api_key="test",
        base_url="https://example.test/v1",
        http_client=http_client,
    )
    client = OpenAIChatClient(model="model", async_client=sdk)
    now = datetime(2026, 9, 4, tzinfo=UTC)
    identity = DurableRunIdentityV1(
        run_id="run-1",
        session_id="session-1",
        request_id_hash="a" * 64,
        request_hash="b" * 64,
        owner_hash="c" * 64,
        agent_slug="main",
        agent_hash="d" * 64,
        catalog_hash="e" * 64,
        deployment_hash="f" * 64,
        tool_package_hash="1" * 64,
        policy_hash="2" * 64,
        orchestration_version="durable_agent_turn_orchestrator_v1",
        created_at=now,
        active_deadline=now + timedelta(hours=4),
        absolute_deadline=now + timedelta(days=7),
        budget=DurableLoopBudgetV1(
            max_model_steps=48,
            max_tool_calls=128,
            max_elapsed_seconds=14400,
            max_human_waits=8,
            human_wait_seconds=86400,
            max_argument_bytes=262144,
            max_result_bytes=1048576,
            context_max_bytes=4194304,
            context_compaction_percent=75,
            max_parallel_reads=4,
            continue_as_new_checkpoints=20,
        ),
    )
    descriptor = FrozenToolDescriptorV1(
        name="lookup",
        description="Lookup.",
        parameters={
            "additionalProperties": False,
            "properties": {"region": {"type": "string"}},
            "required": ["region"],
            "type": "object",
        },
        provenance=ToolProvenance.REMOTE,
        behavior=ToolBehavior.READ_ONLY,
    )
    catalog = FrozenToolCatalogV1.create(
        tools=(descriptor,),
        policy_hash=identity.policy_hash,
        package_hash=identity.tool_package_hash,
    )
    messages = (
        {"role": "user", "contents": [{"type": "text", "text": "work"}]},
    )
    request = OneStepModelRequest(
        identity=identity,
        step_index=0,
        instructions="Choose a tool.",
        working_context=WorkingContextV1(
            bundle=MAFMessageBundleV1.create(
                messages=messages,
                maf_core_version="1.17.0",
                provider="openai",
                model="model",
                api_version="responses-v1",
            ),
            compaction_generation=0,
            source_audit_hash=canonical_hash(messages),
            source_start=0,
            source_end=1,
            estimated_tokens=4,
        ),
        catalog=catalog,
        model_settings={},
    )

    try:
        provider = MafOneStepModelProvider(_ClientManager(client))
        decision = await provider.run_one_step(request)
        repeated = await provider.run_one_step(request)
    finally:
        await http_client.aclose()

    assert len(requests) == 2
    assert requests[0] == requests[1]
    assert requests[0]["store"] is False
    assert requests[0]["include"] == ["reasoning.encrypted_content"]
    assert "conversation" not in requests[0]
    assert "previous_response_id" not in requests[0]
    assert requests[0]["instructions"] == "Choose a tool."
    assert requests[0]["tools"][0]["name"] == "lookup"
    assert decision.tool_calls[0].arguments == {"region": "westus3"}
    assert decision.response_id_hash is not None
    assert "resp_1" not in decision.model_dump_json()
    assert repeated.assistant_message == decision.assistant_message
    assert Message.from_dict(decision.assistant_message).to_dict() == (
        decision.assistant_message
    )
    reasoning = decision.assistant_message["contents"][0]  # type: ignore[index]
    assert reasoning["protected_data"] == "cipher"  # type: ignore[index]


@pytest.mark.parametrize(
    "output",
    [
        [
            {
                "encrypted_content": "reasoning-only",
                "id": "rs_1",
                "summary": [],
                "type": "reasoning",
            }
        ],
        [
            {
                "content": [
                    {
                        "annotations": [],
                        "text": "partial answer",
                        "type": "output_text",
                    }
                ],
                "id": "msg_1",
                "role": "assistant",
                "status": "incomplete",
                "type": "message",
            }
        ],
        [
            {
                "arguments": '{"region":"westus3"}',
                "call_id": "call_1",
                "id": "fc_1",
                "name": "lookup",
                "status": "incomplete",
                "type": "function_call",
            }
        ],
    ],
)
@pytest.mark.asyncio
async def test_real_openai_client_rejects_incomplete_response_shapes(
    output: list[dict[str, object]],
) -> None:
    requests: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "created_at": 1,
                "error": None,
                "id": "resp_incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "instructions": "Use the tool.",
                "max_output_tokens": 12000,
                "metadata": {},
                "model": "model",
                "object": "response",
                "output": output,
                "parallel_tool_calls": True,
                "previous_response_id": None,
                "status": "incomplete",
                "temperature": 0,
                "tool_choice": "auto",
                "tools": [],
                "top_p": 1,
                "truncation": "disabled",
                "usage": {
                    "input_tokens": 70,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 12000,
                    "output_tokens_details": {"reasoning_tokens": 12000},
                    "total_tokens": 12070,
                },
                "user": None,
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sdk = AsyncOpenAI(
        api_key="test",
        base_url="https://example.test/v1",
        http_client=http_client,
    )
    client = OpenAIChatClient(model="model", async_client=sdk)
    request = _provider_request()
    try:
        with pytest.raises(DurableLoopProviderTerminalError) as raised:
            await MafOneStepModelProvider(_ClientManager(client)).run_one_step(
                request
            )
    finally:
        await http_client.aclose()

    assert len(requests) == 1
    assert raised.value.code == "model_response_incomplete"
    assert raised.value.usage.output_tokens == 12000
    assert raised.value.usage.reasoning_tokens == 12000


@pytest.mark.asyncio
async def test_real_openai_client_continues_from_externalized_reasoning_and_tool_result() -> None:
    requests: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        output: list[dict[str, object]]
        finish_status: str
        if len(requests) == 1:
            output = [
                {
                    "encrypted_content": "cipher-step-1",
                    "id": "rs_1",
                    "summary": [{"text": "plan", "type": "summary_text"}],
                    "type": "reasoning",
                },
                {
                    "arguments": '{"region":"westus3"}',
                    "call_id": "call_1",
                    "id": "fc_1",
                    "name": "lookup",
                    "status": "completed",
                    "type": "function_call",
                },
            ]
            finish_status = "completed"
        else:
            output = [
                {
                    "content": [
                        {
                            "annotations": [],
                            "text": "continued",
                            "type": "output_text",
                        }
                    ],
                    "id": "msg_2",
                    "role": "assistant",
                    "status": "completed",
                    "type": "message",
                }
            ]
            finish_status = "completed"
        return httpx.Response(
            200,
            json={
                "created_at": len(requests),
                "error": None,
                "id": f"resp_{len(requests)}",
                "incomplete_details": None,
                "instructions": "Use the tool.",
                "max_output_tokens": None,
                "metadata": {},
                "model": "model",
                "object": "response",
                "output": output,
                "parallel_tool_calls": True,
                "previous_response_id": None,
                "status": finish_status,
                "temperature": 0,
                "tool_choice": "auto",
                "tools": [],
                "top_p": 1,
                "truncation": "disabled",
                "usage": {
                    "input_tokens": 1,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 2,
                    "output_tokens_details": {"reasoning_tokens": 1},
                    "total_tokens": 3,
                },
                "user": None,
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sdk = AsyncOpenAI(
        api_key="test",
        base_url="https://example.test/v1",
        http_client=http_client,
    )
    client = OpenAIChatClient(model="model", async_client=sdk)
    descriptor = FrozenToolDescriptorV1(
        name="lookup",
        description="Lookup.",
        parameters={
            "additionalProperties": False,
            "properties": {"region": {"type": "string"}},
            "required": ["region"],
            "type": "object",
        },
        provenance=ToolProvenance.REMOTE,
        behavior=ToolBehavior.READ_ONLY,
    )
    initial = _provider_request()
    initial = replace(
        initial,
        catalog=FrozenToolCatalogV1.create(
            tools=(descriptor,),
            policy_hash=initial.identity.policy_hash,
            package_hash=initial.identity.tool_package_hash,
        ),
        instructions="Use the tool.",
    )
    provider = MafOneStepModelProvider(_ClientManager(client))
    try:
        first = await provider.run_one_step(initial)
        tool_result = ToolResultV1(
            run_id=initial.identity.run_id,
            step_index=0,
            call_ordinal=0,
            provider_call_id=first.tool_calls[0].call_id,
            call_key="a" * 64,
            request_hash="b" * 64,
            tool_name="lookup",
            status=ToolResultStatus.SUCCEEDED,
            value=[
                {
                    "type": "text",
                    "text": '{"healthy":true,"region":"westus3"}',
                }
            ],
            elapsed_ms=1.0,
        )
        externalized = WorkingContextV1.model_validate_json(
            append_model_and_tool_messages(
                initial.working_context,
                first,
                (tool_result,),
            ).model_dump_json()
        )
        second = await provider.run_one_step(
            replace(
                initial,
                step_index=1,
                working_context=externalized,
            )
        )
    finally:
        await http_client.aclose()

    assert second.final_text == "continued"
    assert len(requests) == 2
    serialized_second = json.dumps(requests[1])
    assert "cipher-step-1" in serialized_second
    assert "call_1" in serialized_second
    assert "function_call_output" in serialized_second
    assert serialized_second.index("cipher-step-1") < serialized_second.index("call_1")
    function_call = next(
        item
        for item in requests[1]["input"]
        if item.get("type") == "function_call"
    )
    assert function_call["arguments"] == '{"region":"westus3"}'
    function_result = next(
        item
        for item in requests[1]["input"]
        if item.get("type") == "function_call_output"
    )
    assert function_result["output"] == '{"healthy":true,"region":"westus3"}'
    assert serialized_second.index("call_1") < serialized_second.index(
        "function_call_output"
    )


@pytest.mark.asyncio
async def test_runtime_maf_provider_rejects_reasoning_without_encrypted_data() -> None:
    request = _provider_request()
    reasoning_message = {
        "role": "assistant",
        "contents": [{"type": "reasoning", "text": "summary"}],
    }
    messages = (*request.working_context.bundle.messages, reasoning_message)
    context = request.working_context.model_copy(
        update={
            "bundle": MAFMessageBundleV1.create(
                messages=messages,
                maf_core_version="1.17.0",
                provider="openai",
                model="model",
                api_version="responses-v1",
            ),
            "source_end": len(messages),
        }
    )
    client = _OneStepChatClient(
        [Content.from_text("unused")],
        finish_reason="stop",
    )

    with pytest.raises(DurableLoopModelError, match="missing encrypted data"):
        await MafOneStepModelProvider(_ClientManager(client)).run_one_step(
            replace(request, working_context=context)
        )

    assert client.call_count == 0


@pytest.mark.asyncio
async def test_runtime_maf_provider_rejects_changed_inference_target() -> None:
    request = _provider_request()
    expected_target = InferenceTarget(
        provider="openai",
        model="model",
        endpoint="https://expected.example/v1",
        api_version="responses-v1",
    )
    request = replace(
        request,
        identity=request.identity.model_copy(
            update={
                "deployment_hash": canonical_hash(
                    {
                        "api_version": expected_target.api_version,
                        "endpoint": expected_target.endpoint,
                        "model": expected_target.model,
                        "provider": expected_target.provider,
                    }
                )
            }
        ),
    )
    client = _OneStepChatClient(
        [Content.from_text("unused")],
        finish_reason="stop",
    )
    changed = InferenceTarget(
        provider="openai",
        model="model",
        endpoint="https://changed.example/v1",
        api_version="responses-v1",
    )

    with pytest.raises(DurableLoopModelError, match="frozen deployment binding"):
        await MafOneStepModelProvider(
            _ClientManager(client, changed)
        ).run_one_step(request)

    assert client.call_count == 0


@pytest.mark.parametrize(
    "model_settings",
    [
        {"store": True},
        {"conversation_id": "conversation-1"},
        {"previous_response_id": "response-1"},
        {"conversation": "conversation-1"},
        {"continuation_token": "token"},
        {"model": "different-model"},
    ],
)
@pytest.mark.asyncio
async def test_runtime_maf_provider_rejects_stateful_or_mismatched_options(
    model_settings: dict[str, object],
) -> None:
    client = _OneStepChatClient([Content.from_text("done")])
    now = datetime(2026, 9, 4, tzinfo=UTC)
    identity = DurableRunIdentityV1(
        run_id="run-1",
        session_id="session-1",
        request_id_hash="a" * 64,
        request_hash="b" * 64,
        owner_hash="c" * 64,
        agent_slug="main",
        agent_hash="d" * 64,
        catalog_hash="e" * 64,
        deployment_hash="f" * 64,
        tool_package_hash="1" * 64,
        policy_hash="2" * 64,
        orchestration_version="durable_agent_turn_orchestrator_v1",
        created_at=now,
        active_deadline=now + timedelta(hours=4),
        absolute_deadline=now + timedelta(days=7),
        budget=DurableLoopBudgetV1(
            max_model_steps=48,
            max_tool_calls=128,
            max_elapsed_seconds=14400,
            max_human_waits=8,
            human_wait_seconds=86400,
            max_argument_bytes=262144,
            max_result_bytes=1048576,
            context_max_bytes=4194304,
            context_compaction_percent=75,
            max_parallel_reads=4,
            continue_as_new_checkpoints=20,
        ),
    )
    messages = (
        {"role": "user", "contents": [{"type": "text", "text": "work"}]},
    )
    catalog = FrozenToolCatalogV1.create(
        tools=(),
        policy_hash=identity.policy_hash,
        package_hash=identity.tool_package_hash,
    )
    request = OneStepModelRequest(
        identity=identity,
        step_index=0,
        instructions="Respond.",
        working_context=WorkingContextV1(
            bundle=MAFMessageBundleV1.create(
                messages=messages,
                maf_core_version="1.17.0",
                provider="openai",
                model="model",
                api_version="responses-v1",
            ),
            compaction_generation=0,
            source_audit_hash=canonical_hash(messages),
            source_start=0,
            source_end=1,
            estimated_tokens=4,
        ),
        catalog=catalog,
        model_settings=model_settings,
    )

    with pytest.raises(DurableLoopModelError):
        await MafOneStepModelProvider(_ClientManager(client)).run_one_step(request)

    assert client.call_count == 0
