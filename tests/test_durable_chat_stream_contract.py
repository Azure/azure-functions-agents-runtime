from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from agent_framework import (
    Agent,
    BaseChatClient,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    FunctionInvocationLayer,
    FunctionTool,
    Message,
    ResponseStream,
    UsageDetails,
)

from azure_functions_agents.experimental.durable_chat_execution_observer import (
    DurableChatExecutionObserver,
)
from azure_functions_agents.experimental.durable_chat_protocol import (
    DurableChatAssistantTextObservationV1,
    DurableChatEnqueueDisposition,
    DurableChatEnqueueResultV1,
    DurableChatModelAttemptObservationV1,
    DurableChatModelAttemptState,
    DurableChatModelProducerV1,
    DurableChatObservationV1,
)
from azure_functions_agents.experimental.durable_loop_activities import (
    MafOneStepModelProvider,
    OneStepModelRequest,
)
from azure_functions_agents.experimental.durable_loop_protocol import (
    DurableLoopBudgetV1,
    DurableRunIdentityV1,
    FrozenToolCatalogV1,
    FrozenToolDescriptorV1,
    MAFMessageBundleV1,
    ToolBehavior,
    ToolProvenance,
    WorkingContextV1,
    canonical_hash,
)


class _HeldFinalStreamChatClient(FunctionInvocationLayer[Any], BaseChatClient[Any]):
    def __init__(
        self,
        *,
        finalizer_started: asyncio.Event,
        release_finalizer: asyncio.Event,
    ) -> None:
        super().__init__(function_invocation_configuration={"enabled": True})
        self._finalizer_started = finalizer_started
        self._release_finalizer = release_finalizer
        self.call_count = 0

    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> ResponseStream[ChatResponseUpdate, ChatResponse[Any]]:
        assert stream
        assert messages
        assert options["instructions"] == "Use the lookup tool."
        self.call_count += 1

        async def updates() -> asyncio.AsyncIterator[ChatResponseUpdate]:
            yield ChatResponseUpdate(
                contents=[
                    Content.from_text("I will look that up."),
                    Content.from_function_call(
                        "call-1",
                        "lookup",
                        arguments={"region": "westus3"},
                    ),
                    Content.from_usage(
                        UsageDetails(
                            input_token_count=7,
                            output_token_count=3,
                            total_token_count=10,
                        )
                    ),
                ],
                role="assistant",
                finish_reason="tool_calls",
            )

        async def finalize(
            updates: Sequence[ChatResponseUpdate],
        ) -> ChatResponse[Any]:
            self._finalizer_started.set()
            await self._release_finalizer.wait()
            return ChatResponse.from_updates(updates)

        return ResponseStream(updates(), finalizer=finalize)


class _ClientManager:
    def __init__(self, client: BaseChatClient[Any]) -> None:
        self._client = client
        self.name = "openai"

    def build_chat_client_with_target(self, _model: str):
        return self._client, SimpleNamespace(
            api_version=None,
            endpoint=None,
            model=None,
            provider=None,
        )


class _ObservationSink:
    def __init__(self) -> None:
        self.observations: list[DurableChatObservationV1] = []

    def try_enqueue(
        self,
        *,
        observation: DurableChatObservationV1,
    ) -> DurableChatEnqueueResultV1:
        self.observations.append(observation)
        return DurableChatEnqueueResultV1(
            disposition=DurableChatEnqueueDisposition.ENQUEUED,
            pending_observations=len(self.observations),
        )


def _request() -> OneStepModelRequest:
    now = datetime(2026, 9, 14, tzinfo=UTC)
    messages = ({"role": "user", "contents": [{"type": "text", "text": "Find West US"}]},)
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
        active_deadline=now + timedelta(hours=1),
        absolute_deadline=now + timedelta(days=1),
        budget=DurableLoopBudgetV1(
            max_model_steps=48,
            max_tool_calls=128,
            max_elapsed_seconds=14_400,
            max_human_waits=8,
            human_wait_seconds=86_400,
            max_argument_bytes=262_144,
            max_result_bytes=1_048_576,
            context_max_bytes=4_194_304,
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
    return OneStepModelRequest(
        identity=identity,
        step_index=0,
        instructions="Use the lookup tool.",
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
            tools=(descriptor,),
            policy_hash=identity.policy_hash,
            package_hash=identity.tool_package_hash,
        ),
        model_settings={},
    )


@pytest.mark.asyncio
async def test_real_maf_stream_delivers_text_before_held_final_and_preserves_tool_result() -> None:
    finalizer_started = asyncio.Event()
    release_finalizer = asyncio.Event()
    invoked_tools: list[str] = []

    def lookup(region: str) -> str:
        invoked_tools.append(region)
        return "West US"

    client = _HeldFinalStreamChatClient(
        finalizer_started=finalizer_started,
        release_finalizer=release_finalizer,
    )
    client.function_invocation_configuration["enabled"] = False
    agent = Agent(
        client=client,
        instructions="Use the lookup tool.",
        tools=[FunctionTool(name="lookup", func=lookup)],
        context_providers=[],
    )
    stream = agent.run(
        [Message("user", [Content.from_text("Find West US")])],
        session=None,
        stream=True,
    )

    first = await asyncio.wait_for(stream.__anext__(), timeout=1)
    assert [content.text for content in first.contents if content.type == "text"] == [
        "I will look that up."
    ]
    assert not finalizer_started.is_set()

    finalizer_held = asyncio.Event()

    async def release_when_finalizer_is_held() -> None:
        await asyncio.wait_for(finalizer_started.wait(), timeout=1)
        assert not release_finalizer.is_set()
        finalizer_held.set()
        release_finalizer.set()

    release = asyncio.create_task(release_when_finalizer_is_held())
    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()
    await release
    assert finalizer_held.is_set()

    response = await stream.get_final_response()
    decision = MafOneStepModelProvider(object()).parse_agent_response(
        _request(),
        response,
    )

    assert client.call_count == 1
    assert client.function_invocation_configuration["enabled"] is False
    assert invoked_tools == []
    assert response.usage_details == {
        "input_token_count": 7,
        "output_token_count": 3,
        "total_token_count": 10,
    }
    assert decision.final_text is None
    assert decision.usage.input_tokens == 7
    assert decision.usage.output_tokens == 3
    assert [(call.call_id, call.name, call.arguments) for call in decision.tool_calls] == [
        ("call-1", "lookup", {"region": "westus3"})
    ]


@pytest.mark.asyncio
async def test_maf_provider_streams_to_chat_observer_before_held_final_response() -> None:
    finalizer_started = asyncio.Event()
    release_finalizer = asyncio.Event()
    client = _HeldFinalStreamChatClient(
        finalizer_started=finalizer_started,
        release_finalizer=release_finalizer,
    )
    sink = _ObservationSink()
    producer = DurableChatModelProducerV1(step_index=0, observation_epoch=1)
    observer = DurableChatExecutionObserver(
        sink=sink,
        run_id="run-1",
        session_id="session-1",
    )

    task = asyncio.create_task(
        MafOneStepModelProvider(_ClientManager(client)).run_one_step_with_observer(
            _request(),
            observer=observer,
            producers=(producer,),
        )
    )
    await asyncio.wait_for(finalizer_started.wait(), timeout=1)

    assert not task.done()
    assert [
        observation.delta
        for observation in sink.observations
        if isinstance(observation, DurableChatAssistantTextObservationV1)
    ] == ["I will look that up."]

    release_finalizer.set()
    decision = await task

    assert client.function_invocation_configuration["enabled"] is False
    assert decision.final_text is None
    assert [(call.call_id, call.name) for call in decision.tool_calls] == [
        ("call-1", "lookup")
    ]


@pytest.mark.asyncio
async def test_maf_stream_timeout_marks_partial_attempt_failed() -> None:
    finalizer_started = asyncio.Event()
    client = _HeldFinalStreamChatClient(
        finalizer_started=finalizer_started,
        release_finalizer=asyncio.Event(),
    )
    sink = _ObservationSink()
    producer = DurableChatModelProducerV1(step_index=0, observation_epoch=1)
    observer = DurableChatExecutionObserver(
        sink=sink,
        run_id="run-1",
        session_id="session-1",
    )

    async def invoke_with_timeout() -> None:
        async with asyncio.timeout(0.1):
            await MafOneStepModelProvider(_ClientManager(client)).run_one_step_with_observer(
                _request(),
                observer=observer,
                producers=(producer,),
            )

    task = asyncio.create_task(invoke_with_timeout())
    await asyncio.wait_for(finalizer_started.wait(), timeout=1)

    with pytest.raises(TimeoutError):
        await task

    assert [
        observation.delta
        for observation in sink.observations
        if isinstance(observation, DurableChatAssistantTextObservationV1)
    ] == ["I will look that up."]
    assert [
        (observation.producer, observation.state)
        for observation in sink.observations
        if isinstance(observation, DurableChatModelAttemptObservationV1)
    ] == [
        (producer, DurableChatModelAttemptState.STARTED),
        (producer, DurableChatModelAttemptState.STREAMING),
        (producer, DurableChatModelAttemptState.FAILED),
    ]
