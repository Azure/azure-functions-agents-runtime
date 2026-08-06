from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from types import SimpleNamespace
from typing import Any

import pytest
from agent_framework import (
    Agent,
    AgentResponse,
    BaseChatClient,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    FunctionInvocationLayer,
    Message,
    ResponseStream,
    UsageDetails,
)

from azure_functions_agents import runner
from azure_functions_agents._function_tool import tool
from azure_functions_agents.client_manager import InferenceTarget
from azure_functions_agents.registration.capabilities import AgentCapabilities


def _usage_payload(record: logging.LogRecord) -> dict[str, Any]:
    prefix = "Agent token usage: "
    message = record.getMessage()
    assert message.startswith(prefix)
    return json.loads(message.removeprefix(prefix))


def _usage_payloads(caplog: Any) -> list[dict[str, Any]]:
    return [
        _usage_payload(record)
        for record in caplog.records
        if record.getMessage().startswith("Agent token usage: ")
    ]


def _install_primary_agent(
    monkeypatch: Any,
    agent: Any,
    session_id: str = "session-1",
    inference_target: InferenceTarget | None = None,
) -> None:
    async def build(
        *args: Any, **kwargs: Any
    ) -> tuple[Any, Any, str, None, InferenceTarget]:
        return agent, object(), session_id, None, inference_target or InferenceTarget()

    monkeypatch.setattr(runner, "_build_agent_session_history", build)


async def _collect_stream(stream: AsyncIterator[str]) -> list[str]:
    return [chunk async for chunk in stream]


def test_normalize_usage_details_keeps_only_non_negative_integer_counts() -> None:
    assert runner._normalize_usage_details(
        {
            "input_token_count": 0,
            "output_token_count": 12,
            "total_token_count": 12,
            "cache_creation_input_token_count": 3,
            "cache_read_input_token_count": 4,
            "reasoning_output_token_count": 5,
            "ignored_provider_field": 99,
        }
    ) == {
        "input_tokens": 0,
        "output_tokens": 12,
        "total_tokens": 12,
        "cache_creation_input_tokens": 3,
        "cache_read_input_tokens": 4,
        "reasoning_output_tokens": 5,
    }

    assert runner._normalize_usage_details(
        {
            "input_token_count": True,
            "output_token_count": -1,
            "total_token_count": "12",
        }
    ) == {}
    assert runner._normalize_usage_details(None) == {}


def test_usage_recorder_emits_deterministic_json_once_through_shared_logger(caplog: Any) -> None:
    recorder = runner._AgentUsageRecorder(
        agent_name="billing",
        execution_role="workflow_subagent",
        workflow_id="workflow-1",
        workflow_node_id="node-2",
    )

    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
        recorder.emit(
            "success",
            {
                "input_token_count": 10,
                "output_token_count": 20,
                "total_token_count": 30,
            },
        )
        recorder.emit("error")

    records = [record for record in caplog.records if record.message.startswith("Agent token usage")]
    assert len(records) == 1
    assert records[0].name == "azure.functions.AgentRuntime"
    payload = _usage_payload(records[0])
    assert payload == {
        "agent_name": "billing",
        "event_name": "agent_token_usage",
        "execution_role": "workflow_subagent",
        "input_tokens": 10,
        "model": None,
        "outcome": "success",
        "output_tokens": 20,
        "provider": None,
        "total_tokens": 30,
        "usage_available": True,
        "usage_complete": True,
        "usage_scope": "agent_run_local",
        "usage_source": "final_response",
        "workflow_id": "workflow-1",
        "workflow_node_id": "node-2",
    }


def test_usage_recorder_marks_missing_usage_unavailable(caplog: Any) -> None:
    recorder = runner._AgentUsageRecorder(
        agent_name="main",
        execution_role="primary",
    )

    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
        recorder.emit("timeout")

    payload = _usage_payload(caplog.records[-1])
    assert payload["usage_available"] is False
    assert payload["usage_complete"] is False
    assert payload["usage_source"] == "unavailable"
    assert "input_tokens" not in payload
    assert "output_tokens" not in payload
    assert "total_tokens" not in payload


def test_usage_recorder_marks_partial_base_counts_incomplete(caplog: Any) -> None:
    recorder = runner._AgentUsageRecorder(
        agent_name="main",
        execution_role="primary",
    )

    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
        recorder.emit(
            "success",
            {
                "input_token_count": 10,
                "output_token_count": 4,
            },
        )

    payload = _usage_payload(caplog.records[-1])
    assert payload["usage_available"] is True
    assert payload["usage_complete"] is False
    assert payload["usage_source"] == "final_response"


def test_usage_recorder_never_changes_agent_behavior_when_logging_fails(monkeypatch: Any) -> None:
    logging_attempts = 0

    def fail_logging(*args: Any, **kwargs: Any) -> None:
        nonlocal logging_attempts
        logging_attempts += 1
        raise RuntimeError("logging unavailable")

    monkeypatch.setattr(runner.logger, "info", fail_logging)
    recorder = runner._AgentUsageRecorder(agent_name="main", execution_role="primary")

    recorder.emit("success", {"total_token_count": 4})
    recorder.emit("error")

    assert logging_attempts == 1


@pytest.mark.asyncio
async def test_run_agent_logs_usage_from_real_maf_final_response(
    monkeypatch: Any, caplog: Any
) -> None:
    response = AgentResponse(
        messages=[],
        usage_details=UsageDetails(
            input_token_count=11,
            output_token_count=7,
            total_token_count=18,
        ),
    )

    class Agent:
        async def run(self, *args: Any, **kwargs: Any) -> AgentResponse[Any]:
            return response

    _install_primary_agent(monkeypatch, Agent())
    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
        result = await runner.run_agent("prompt", agent_name="main")

    assert result.session_id == "session-1"
    payload = _usage_payload(caplog.records[-1])
    assert payload["execution_role"] == "primary"
    assert "session_id" not in payload
    assert payload["input_tokens"] == 11
    assert payload["output_tokens"] == 7
    assert payload["total_tokens"] == 18


@pytest.mark.asyncio
async def test_run_agent_success_without_usage_logs_unavailable(
    monkeypatch: Any, caplog: Any
) -> None:
    class Agent:
        async def run(self, *args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(text="done", messages=[], usage_details=None)

    target = InferenceTarget(
        provider="foundry",
        model="claude-deployment",
    )
    _install_primary_agent(monkeypatch, Agent(), inference_target=target)
    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
        result = await runner.run_agent("prompt")

    assert result.content == "done"
    assert _usage_payloads(caplog)[0]["outcome"] == "success"
    assert _usage_payloads(caplog)[0]["usage_available"] is False
    assert _usage_payloads(caplog)[0]["provider"] == "foundry"
    assert "inference_host" not in _usage_payloads(caplog)[0]
    assert _usage_payloads(caplog)[0]["model"] == "claude-deployment"


@pytest.mark.parametrize(
    ("failure", "expected_exception", "expected_outcome"),
    [("error", ValueError, "error"), ("timeout", RuntimeError, "timeout")],
)
@pytest.mark.asyncio
async def test_run_agent_failure_logs_once(
    monkeypatch: Any,
    caplog: Any,
    failure: str,
    expected_exception: type[BaseException],
    expected_outcome: str,
) -> None:
    class Agent:
        async def run(self, *args: Any, **kwargs: Any) -> Any:
            if failure == "error":
                raise ValueError("model failed")
            await asyncio.sleep(10)

    _install_primary_agent(monkeypatch, Agent())
    with (
        caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"),
        pytest.raises(expected_exception),
    ):
        await runner.run_agent("prompt", timeout=0.01)

    payloads = _usage_payloads(caplog)
    assert len(payloads) == 1
    assert payloads[0]["outcome"] == expected_outcome
    assert payloads[0]["usage_available"] is False


@pytest.mark.asyncio
async def test_run_agent_cancellation_logs_once(monkeypatch: Any, caplog: Any) -> None:
    started = asyncio.Event()

    class Agent:
        async def run(self, *args: Any, **kwargs: Any) -> Any:
            started.set()
            await asyncio.Event().wait()

    _install_primary_agent(monkeypatch, Agent())
    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
        task = asyncio.create_task(runner.run_agent("prompt"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    payloads = _usage_payloads(caplog)
    assert len(payloads) == 1
    assert payloads[0]["outcome"] == "cancelled"


@pytest.mark.asyncio
async def test_run_agent_build_failure_emits_no_usage_record(
    monkeypatch: Any, caplog: Any
) -> None:
    async def fail_build(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("configuration failed")

    monkeypatch.setattr(runner, "_build_agent_session_history", fail_build)
    with (
        caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"),
        pytest.raises(RuntimeError, match="configuration failed"),
    ):
        await runner.run_agent("prompt")

    assert _usage_payloads(caplog) == []


@pytest.mark.asyncio
async def test_run_agent_stream_logs_usage_from_real_maf_final_response(
    monkeypatch: Any, caplog: Any
) -> None:
    response = AgentResponse(
        messages=[],
        usage_details=UsageDetails(
            input_token_count=5,
            output_token_count=3,
            total_token_count=8,
        ),
    )

    async def updates() -> AsyncIterator[Any]:
        if False:
            yield None

    class Agent:
        def run(self, *args: Any, **kwargs: Any) -> ResponseStream[Any, AgentResponse[Any]]:
            return ResponseStream(updates(), finalizer=lambda _: response)

    target = InferenceTarget("openai", "gpt-4o-mini")
    _install_primary_agent(monkeypatch, Agent(), "session-2", target)
    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
        events = [chunk async for chunk in runner.run_agent_stream("prompt", agent_name="main")]

    assert [json.loads(event.removeprefix("data: "))["type"] for event in events] == [
        "session",
        "done",
    ]
    usage_records = [
        record for record in caplog.records if record.getMessage().startswith("Agent token usage: ")
    ]
    assert len(usage_records) == 1
    payload = _usage_payload(usage_records[0])
    assert "session_id" not in payload
    assert payload["usage_source"] == "final_response"
    assert payload["total_tokens"] == 8
    assert payload["provider"] == "openai"
    assert "inference_host" not in payload
    assert payload["model"] == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_real_maf_final_response_aggregates_two_tool_call_turns() -> None:
    class TwoTurnChatClient(FunctionInvocationLayer[Any], BaseChatClient[Any]):
        def __init__(self) -> None:
            super().__init__()
            self.call_count = 0

        def _inner_get_response(
            self,
            *,
            messages: Sequence[Message],
            stream: bool,
            options: Mapping[str, Any],
            **kwargs: Any,
        ) -> Any:
            self.call_count += 1
            is_tool_turn = self.call_count == 1
            usage = UsageDetails(
                input_token_count=2 if is_tool_turn else 3,
                output_token_count=1 if is_tool_turn else 4,
                total_token_count=3 if is_tool_turn else 7,
            )
            content = (
                Content.from_function_call("call-1", "lookup", arguments={})
                if is_tool_turn
                else Content.from_text("complete")
            )
            if stream:

                async def updates() -> AsyncIterator[ChatResponseUpdate]:
                    yield ChatResponseUpdate(
                        contents=[content, Content.from_usage(usage)],
                        role="assistant",
                        finish_reason="tool_calls" if is_tool_turn else "stop",
                    )

                return ResponseStream(updates(), finalizer=ChatResponse.from_updates)

            async def response() -> ChatResponse[Any]:
                return ChatResponse(
                    messages=[Message("assistant", [content])],
                    usage_details=usage,
                    finish_reason="tool_calls" if is_tool_turn else "stop",
                )

            return response()

    lookup = tool(lambda: "found", name="lookup")
    non_streaming_response = await Agent(TwoTurnChatClient(), tools=[lookup]).run("prompt")

    stream = Agent(TwoTurnChatClient(), tools=[lookup]).run("prompt", stream=True)
    async for _ in stream:
        pass
    streaming_response = await stream.get_final_response()

    expected_usage = {
        "input_token_count": 5,
        "output_token_count": 5,
        "total_token_count": 10,
    }
    assert non_streaming_response.usage_details == expected_usage
    assert streaming_response.usage_details == expected_usage


@pytest.mark.parametrize(("failure", "expected_outcome"), [("error", "error"), ("timeout", "timeout")])
@pytest.mark.asyncio
async def test_run_agent_stream_failure_logs_once(
    monkeypatch: Any, caplog: Any, failure: str, expected_outcome: str
) -> None:
    class Stream:
        def __aiter__(self) -> Stream:
            return self

        async def __anext__(self) -> Any:
            if failure == "error":
                raise ValueError("stream failed")
            await asyncio.sleep(10)

    class Agent:
        def run(self, *args: Any, **kwargs: Any) -> Stream:
            return Stream()

    _install_primary_agent(monkeypatch, Agent())
    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
        events = [
            chunk
            async for chunk in runner.run_agent_stream(
                "prompt", timeout=0.01 if failure == "timeout" else 1.0
            )
        ]

    assert json.loads(events[-1].removeprefix("data: "))["type"] == "error"
    payloads = _usage_payloads(caplog)
    assert len(payloads) == 1
    assert payloads[0]["outcome"] == expected_outcome
    assert payloads[0]["usage_available"] is False


@pytest.mark.asyncio
async def test_run_agent_stream_cancellation_logs_once(monkeypatch: Any, caplog: Any) -> None:
    started = asyncio.Event()

    class Stream:
        def __aiter__(self) -> Stream:
            return self

        async def __anext__(self) -> Any:
            started.set()
            await asyncio.Event().wait()

    class Agent:
        def run(self, *args: Any, **kwargs: Any) -> Stream:
            return Stream()

    _install_primary_agent(monkeypatch, Agent())

    async def collect() -> list[str]:
        return [chunk async for chunk in runner.run_agent_stream("prompt")]

    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
        task = asyncio.create_task(collect())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    payloads = _usage_payloads(caplog)
    assert len(payloads) == 1
    assert payloads[0]["outcome"] == "cancelled"


@pytest.mark.asyncio
async def test_run_agent_stream_aclose_logs_cancelled_once(monkeypatch: Any, caplog: Any) -> None:
    async def updates() -> AsyncIterator[Any]:
        yield SimpleNamespace(contents=[SimpleNamespace(type="text", text="hello")])

    class Agent:
        def run(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
            return updates()

    _install_primary_agent(monkeypatch, Agent())
    stream = runner.run_agent_stream("prompt")
    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
        await stream.__anext__()
        await stream.__anext__()
        await stream.aclose()

    payloads = _usage_payloads(caplog)
    assert len(payloads) == 1
    assert payloads[0]["outcome"] == "cancelled"


@pytest.mark.asyncio
async def test_run_agent_stream_without_final_response_logs_success_unavailable(
    monkeypatch: Any, caplog: Any
) -> None:
    async def updates() -> AsyncIterator[Any]:
        if False:
            yield None

    class Agent:
        def run(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
            return updates()

    _install_primary_agent(monkeypatch, Agent())
    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
        events = [chunk async for chunk in runner.run_agent_stream("prompt")]

    assert json.loads(events[-1].removeprefix("data: "))["type"] == "done"
    payloads = _usage_payloads(caplog)
    assert len(payloads) == 1
    assert payloads[0]["outcome"] == "success"
    assert payloads[0]["usage_available"] is False


@pytest.mark.asyncio
async def test_run_agent_stream_bounds_hanging_final_response(
    monkeypatch: Any, caplog: Any
) -> None:
    class Stream:
        def __aiter__(self) -> Stream:
            return self

        async def __anext__(self) -> Any:
            raise StopAsyncIteration

        async def get_final_response(self) -> Any:
            await asyncio.sleep(10)

    class Agent:
        def run(self, *args: Any, **kwargs: Any) -> Stream:
            return Stream()

    monkeypatch.setattr(runner, "_FINAL_USAGE_TIMEOUT_SECONDS", 0.01)
    _install_primary_agent(monkeypatch, Agent())
    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
        events = await asyncio.wait_for(
            _collect_stream(runner.run_agent_stream("prompt")), timeout=0.2
        )

    assert json.loads(events[-1].removeprefix("data: "))["type"] == "done"
    assert _usage_payloads(caplog)[0]["usage_available"] is False


@pytest.mark.asyncio
async def test_leaf_agent_logs_distinct_workflow_attempts_and_delegate_role(
    monkeypatch: Any, caplog: Any
) -> None:
    class Agent:
        async def run(self, *args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(
                text="done",
                usage_details={"input_token_count": 2, "output_token_count": 1},
            )

    target = InferenceTarget(
        "azure_openai",
        "gpt-deployment",
    )
    monkeypatch.setattr(runner, "_build_delegated_agent", lambda *args: (Agent(), target))
    resolved = SimpleNamespace(slug="analyst")
    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
        for _ in range(2):
            result = await runner.run_leaf_agent_task(
                resolved,
                AgentCapabilities(),
                "task",
                timeout=1.0,
                execution_role="workflow_subagent",
                workflow_id="workflow-1",
                workflow_node_id="node-1",
            )
        await runner.run_leaf_agent_task(
            resolved,
            AgentCapabilities(),
            "delegated task",
            timeout=1.0,
            execution_role="delegate",
        )

    assert result == "done"
    payloads = _usage_payloads(caplog)
    assert len(payloads) == 3
    assert payloads[0]["workflow_id"] == payloads[1]["workflow_id"] == "workflow-1"
    assert payloads[0]["workflow_node_id"] == payloads[1]["workflow_node_id"] == "node-1"
    assert payloads[2]["agent_name"] == "analyst"
    assert payloads[2]["execution_role"] == "delegate"
    assert payloads[2]["workflow_id"] is None
    for payload in payloads:
        assert payload["provider"] == "azure_openai"
        assert "inference_host" not in payload
        assert payload["model"] == "gpt-deployment"


@pytest.mark.parametrize(
    ("failure", "execution_role", "expected_exception", "expected_outcome"),
    [
        ("error", "delegate", ValueError, "error"),
        ("timeout", "workflow_subagent", TimeoutError, "timeout"),
    ],
)
@pytest.mark.asyncio
async def test_leaf_agent_failure_logs_once(
    monkeypatch: Any,
    caplog: Any,
    failure: str,
    execution_role: str,
    expected_exception: type[BaseException],
    expected_outcome: str,
) -> None:
    class Agent:
        async def run(self, *args: Any, **kwargs: Any) -> Any:
            if failure == "error":
                raise ValueError("model failed")
            await asyncio.Event().wait()

    monkeypatch.setattr(
        runner,
        "_build_delegated_agent",
        lambda *args: (Agent(), InferenceTarget("foundry", "model-one")),
    )
    with (
        caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"),
        pytest.raises(expected_exception),
    ):
        await runner.run_leaf_agent_task(
            SimpleNamespace(slug="analyst"),
            AgentCapabilities(),
            "task",
            timeout=0.01 if failure == "timeout" else 1.0,
            execution_role=execution_role,
            workflow_id="workflow-1" if execution_role == "workflow_subagent" else None,
            workflow_node_id="node-1" if execution_role == "workflow_subagent" else None,
        )

    payloads = _usage_payloads(caplog)
    assert len(payloads) == 1
    assert payloads[0]["execution_role"] == execution_role
    assert payloads[0]["outcome"] == expected_outcome
    assert payloads[0]["usage_available"] is False


@pytest.mark.asyncio
async def test_leaf_agent_cancellation_logs_once(monkeypatch: Any, caplog: Any) -> None:
    started = asyncio.Event()

    class Agent:
        async def run(self, *args: Any, **kwargs: Any) -> Any:
            started.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(
        runner,
        "_build_delegated_agent",
        lambda *args: (Agent(), InferenceTarget()),
    )
    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
        task = asyncio.create_task(
            runner.run_leaf_agent_task(
                SimpleNamespace(slug="analyst"),
                AgentCapabilities(),
                "task",
                timeout=1.0,
                execution_role="delegate",
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    payloads = _usage_payloads(caplog)
    assert len(payloads) == 1
    assert payloads[0]["outcome"] == "cancelled"


@pytest.mark.asyncio
async def test_leaf_agent_construction_failure_emits_no_usage_record(
    monkeypatch: Any, caplog: Any
) -> None:
    def fail_build(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("configuration failed")

    monkeypatch.setattr(runner, "_build_delegated_agent", fail_build)
    with (
        caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"),
        pytest.raises(RuntimeError, match="configuration failed"),
    ):
        await runner.run_leaf_agent_task(
            SimpleNamespace(slug="analyst"),
            AgentCapabilities(),
            "task",
            timeout=1.0,
            execution_role="delegate",
        )

    assert _usage_payloads(caplog) == []
