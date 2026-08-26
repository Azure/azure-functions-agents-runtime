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


def _usage_detail_payload(record: logging.LogRecord) -> dict[str, Any]:
    prefix = "Agent token usage detail: "
    message = record.getMessage()
    assert message.startswith(prefix)
    return json.loads(message.removeprefix(prefix))


def _usage_detail_payloads(caplog: Any) -> list[dict[str, Any]]:
    return [
        _usage_detail_payload(record)
        for record in caplog.records
        if record.getMessage().startswith("Agent token usage detail: ")
    ]


_USAGE_FIELDS = {
    "agent_name",
    "event_name",
    "execution_role",
    "provider",
    "model",
    "model_publisher",
    "input_tokens",
    "output_tokens",
}


def _assert_exact_usage_fields(payload: dict[str, Any]) -> None:
    assert payload.keys() == _USAGE_FIELDS


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
            "openai.cached_input_tokens": 40,
            "prompt/cached_tokens": 41,
            "openai.reasoning_tokens": 50,
            "completion/reasoning_tokens": 51,
            "ignored_provider_field": 99,
        }
    ) == {
        "input_tokens": 0,
        "output_tokens": 12,
    }

    assert runner._normalize_usage_details(
        {
            "input_token_count": True,
            "output_token_count": -1,
            "total_token_count": "12",
        }
    ) == {}
    assert runner._normalize_usage_details(None) == {}


def test_normalize_detailed_usage_details_preserves_valid_maf_dimensions() -> None:
    assert runner._normalize_detailed_usage_details(
        {
            "input_token_count": 10,
            "output_token_count": 8,
            "total_token_count": 18,
            "openai.cached_input_tokens": 4,
            "openai.reasoning_tokens": 5,
            "zero": 0,
            "boolean": True,
            "negative": -1,
            "string": "7",
            1: 2,
        }
    ) == {
        "input_token_count": 10,
        "openai.cached_input_tokens": 4,
        "openai.reasoning_tokens": 5,
        "output_token_count": 8,
        "total_token_count": 18,
        "zero": 0,
    }
    assert runner._normalize_detailed_usage_details(None) == {}


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "Y"])
def test_resolve_detailed_token_usage_accepts_true_values(
    monkeypatch: Any, value: str
) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_DETAILED_TOKEN_USAGE", value)

    assert runner._resolve_detailed_token_usage() is True


@pytest.mark.parametrize("value", ["", "false", "FALSE", "0", "no", "N"])
def test_resolve_detailed_token_usage_defaults_to_false(
    monkeypatch: Any, value: str
) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_DETAILED_TOKEN_USAGE", value)

    assert runner._resolve_detailed_token_usage() is False


def test_resolve_detailed_token_usage_warns_and_disables_invalid_value(
    monkeypatch: Any, caplog: Any
) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_DETAILED_TOKEN_USAGE", "sometimes")

    with caplog.at_level(logging.WARNING, logger="azure.functions.AgentRuntime"):
        assert runner._resolve_detailed_token_usage() is False

    assert "Ignoring invalid AZURE_FUNCTIONS_AGENTS_DETAILED_TOKEN_USAGE value" in caplog.text


@pytest.mark.parametrize(
    "provider_usage",
    [
        {
            "openai.cached_input_tokens": 4,
            "openai.reasoning_tokens": 5,
        },
        {
            "prompt/cached_tokens": 6,
            "completion/reasoning_tokens": 7,
        },
    ],
)
def test_normalize_usage_details_ignores_additional_maf_13_usage_details(
    provider_usage: dict[str, int],
) -> None:
    usage_details = UsageDetails(
        input_token_count=10,
        output_token_count=8,
        total_token_count=18,
        **provider_usage,
    )

    assert runner._normalize_usage_details(usage_details) == {
        "input_tokens": 10,
        "output_tokens": 8,
    }


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("openai", "openai"),
        ("azure_openai", "openai"),
        ("foundry", None),
        (None, None),
    ],
)
def test_model_publisher_is_derived_only_for_known_openai_transports(
    provider: str | None,
    expected: str | None,
) -> None:
    assert runner._model_publisher(provider) == expected


def test_usage_recorder_emits_deterministic_json_once_through_shared_logger(caplog: Any) -> None:
    recorder = runner._AgentUsageRecorder(
        agent_name="billing",
        execution_role="workflow_subagent",
        inference_target=InferenceTarget("azure_openai", "gpt-4o"),
    )

    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
        recorder.emit(
            {
                "input_token_count": 10,
                "output_token_count": 20,
                "total_token_count": 30,
                "openai.cached_input_tokens": 4,
                "openai.reasoning_tokens": 5,
            }
        )
        recorder.emit()

    records = [record for record in caplog.records if record.message.startswith("Agent token usage")]
    assert len(records) == 1
    assert records[0].name == "azure.functions.AgentRuntime"
    payload = _usage_payload(records[0])
    assert payload == {
        "agent_name": "billing",
        "event_name": "agent_token_usage",
        "execution_role": "workflow_subagent",
        "input_tokens": 10,
        "model": "gpt-4o",
        "model_publisher": "openai",
        "output_tokens": 20,
        "provider": "azure_openai",
    }


def test_usage_recorder_logs_null_counts_when_usage_is_unavailable(caplog: Any) -> None:
    recorder = runner._AgentUsageRecorder(
        agent_name="main",
        execution_role="primary",
    )

    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
        recorder.emit()

    payload = _usage_payload(caplog.records[-1])
    _assert_exact_usage_fields(payload)
    assert payload["input_tokens"] is None
    assert payload["output_tokens"] is None


def test_usage_recorder_logs_available_token_counts_independently(caplog: Any) -> None:
    recorder = runner._AgentUsageRecorder(
        agent_name="main",
        execution_role="primary",
    )

    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
        recorder.emit(
            {
                "input_token_count": 10,
            },
        )

    payload = _usage_payload(caplog.records[-1])
    _assert_exact_usage_fields(payload)
    assert payload["input_tokens"] == 10
    assert payload["output_tokens"] is None


def test_usage_recorder_detailed_event_is_disabled_by_default(
    monkeypatch: Any, caplog: Any
) -> None:
    monkeypatch.setattr(runner, "_DETAILED_TOKEN_USAGE_ENABLED", False)
    recorder = runner._AgentUsageRecorder(agent_name="main", execution_role="primary")

    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
        recorder.emit({"input_token_count": 10, "output_token_count": 3})

    assert len(_usage_payloads(caplog)) == 1
    assert _usage_detail_payloads(caplog) == []


def test_usage_recorder_emits_versioned_detailed_event_when_enabled(
    monkeypatch: Any, caplog: Any
) -> None:
    monkeypatch.setattr(runner, "_DETAILED_TOKEN_USAGE_ENABLED", True)
    recorder = runner._AgentUsageRecorder(
        agent_name="billing",
        execution_role="workflow_subagent",
        inference_target=InferenceTarget("foundry", "gpt-test"),
    )

    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
        recorder.emit(
            {
                "input_token_count": 10,
                "output_token_count": 3,
                "total_token_count": 13,
                "openai.cached_input_tokens": 4,
                "openai.reasoning_tokens": 2,
                "ignored": "5",
            }
        )
        recorder.emit()

    canonical = _usage_payloads(caplog)
    assert len(canonical) == 1
    _assert_exact_usage_fields(canonical[0])
    details = _usage_detail_payloads(caplog)
    assert details == [
        {
            "agent_name": "billing",
            "event_name": "agent_token_usage_detail",
            "execution_role": "workflow_subagent",
            "model": "gpt-test",
            "model_publisher": None,
            "provider": "foundry",
            "schema_version": 1,
            "usage_details": {
                "input_token_count": 10,
                "openai.cached_input_tokens": 4,
                "openai.reasoning_tokens": 2,
                "output_token_count": 3,
                "total_token_count": 13,
            },
        }
    ]


def test_usage_recorder_detailed_event_reports_unavailable_usage(
    monkeypatch: Any, caplog: Any
) -> None:
    monkeypatch.setattr(runner, "_DETAILED_TOKEN_USAGE_ENABLED", True)
    recorder = runner._AgentUsageRecorder(agent_name="main", execution_role="primary")

    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
        recorder.emit()

    assert _usage_detail_payloads(caplog)[0]["usage_details"] == {}


def test_usage_recorder_detail_failure_does_not_suppress_canonical_event(
    monkeypatch: Any, caplog: Any
) -> None:
    real_dumps = json.dumps

    def fail_detail(payload: Any, *args: Any, **kwargs: Any) -> str:
        if payload.get("event_name") == "agent_token_usage_detail":
            raise TypeError("detail serialization failed")
        return real_dumps(payload, *args, **kwargs)

    monkeypatch.setattr(runner, "_DETAILED_TOKEN_USAGE_ENABLED", True)
    monkeypatch.setattr(runner.json, "dumps", fail_detail)
    recorder = runner._AgentUsageRecorder(agent_name="main", execution_role="primary")

    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
        recorder.emit({"input_token_count": 4, "output_token_count": 2})

    payloads = _usage_payloads(caplog)
    assert len(payloads) == 1
    assert payloads[0]["input_tokens"] == 4
    assert _usage_detail_payloads(caplog) == []


def test_usage_recorder_never_changes_agent_behavior_when_logging_fails(monkeypatch: Any) -> None:
    logging_attempts = 0

    def fail_logging(*args: Any, **kwargs: Any) -> None:
        nonlocal logging_attempts
        logging_attempts += 1
        raise RuntimeError("logging unavailable")

    monkeypatch.setattr(runner.logger, "info", fail_logging)
    recorder = runner._AgentUsageRecorder(agent_name="main", execution_role="primary")

    recorder.emit({"input_token_count": 4})
    recorder.emit()

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
    _assert_exact_usage_fields(payload)
    assert payload["execution_role"] == "primary"
    assert "session_id" not in payload
    assert payload["input_tokens"] == 11
    assert payload["output_tokens"] == 7


@pytest.mark.asyncio
async def test_run_agent_success_with_maf_optional_usage_absent_logs_unavailable(
    monkeypatch: Any, caplog: Any
) -> None:
    response = AgentResponse(messages=[])

    class Agent:
        async def run(self, *args: Any, **kwargs: Any) -> AgentResponse[Any]:
            return response

    _install_primary_agent(monkeypatch, Agent())
    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
        result = await runner.run_agent("prompt")

    assert response.usage_details is None
    assert result.content == ""
    payload = _usage_payloads(caplog)[0]
    _assert_exact_usage_fields(payload)
    assert payload["input_tokens"] is None
    assert payload["output_tokens"] is None


@pytest.mark.parametrize(
    ("failure", "expected_exception"),
    [("error", ValueError), ("timeout", RuntimeError)],
)
@pytest.mark.asyncio
async def test_run_agent_failure_logs_once(
    monkeypatch: Any,
    caplog: Any,
    failure: str,
    expected_exception: type[BaseException],
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
    _assert_exact_usage_fields(payloads[0])
    assert payloads[0]["input_tokens"] is None
    assert payloads[0]["output_tokens"] is None


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
    _assert_exact_usage_fields(payloads[0])
    assert payloads[0]["input_tokens"] is None
    assert payloads[0]["output_tokens"] is None


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
    _assert_exact_usage_fields(payload)
    assert "session_id" not in payload
    assert payload["input_tokens"] == 5
    assert payload["output_tokens"] == 3
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


@pytest.mark.parametrize("failure", ["error", "timeout"])
@pytest.mark.asyncio
async def test_run_agent_stream_failure_logs_once(
    monkeypatch: Any, caplog: Any, failure: str
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
    _assert_exact_usage_fields(payloads[0])
    assert payloads[0]["input_tokens"] is None
    assert payloads[0]["output_tokens"] is None


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
    _assert_exact_usage_fields(payloads[0])
    assert payloads[0]["input_tokens"] is None
    assert payloads[0]["output_tokens"] is None


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
    _assert_exact_usage_fields(payloads[0])
    assert payloads[0]["input_tokens"] is None
    assert payloads[0]["output_tokens"] is None


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
    _assert_exact_usage_fields(payloads[0])
    assert payloads[0]["input_tokens"] is None
    assert payloads[0]["output_tokens"] is None


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
    payload = _usage_payloads(caplog)[0]
    _assert_exact_usage_fields(payload)
    assert payload["input_tokens"] is None
    assert payload["output_tokens"] is None


@pytest.mark.asyncio
async def test_run_agent_stream_yields_done_before_collecting_usage_on_close(
    monkeypatch: Any, caplog: Any
) -> None:
    usage_collection_started = asyncio.Event()
    release_usage = asyncio.Event()
    response = AgentResponse(
        messages=[],
        usage_details=UsageDetails(
            input_token_count=3,
            output_token_count=2,
            total_token_count=5,
        ),
    )

    class Stream:
        def __aiter__(self) -> Stream:
            return self

        async def __anext__(self) -> Any:
            raise StopAsyncIteration

        async def get_final_response(self) -> AgentResponse[Any]:
            usage_collection_started.set()
            await release_usage.wait()
            return response

    class Agent:
        def run(self, *args: Any, **kwargs: Any) -> Stream:
            return Stream()

    _install_primary_agent(monkeypatch, Agent())
    stream = runner.run_agent_stream("prompt")

    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
        session = await stream.__anext__()
        done = await asyncio.wait_for(stream.__anext__(), timeout=0.5)

        assert json.loads(session.removeprefix("data: "))["type"] == "session"
        assert json.loads(done.removeprefix("data: "))["type"] == "done"
        assert not usage_collection_started.is_set()

        close = asyncio.create_task(stream.aclose())
        await usage_collection_started.wait()
        release_usage.set()
        await close

    payload = _usage_payloads(caplog)[0]
    _assert_exact_usage_fields(payload)
    assert payload["input_tokens"] == 3
    assert payload["output_tokens"] == 2


@pytest.mark.asyncio
async def test_leaf_agent_logs_distinct_attempts_and_execution_roles(
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
    assert payloads[0]["execution_role"] == payloads[1]["execution_role"] == "workflow_subagent"
    assert payloads[2]["agent_name"] == "analyst"
    assert payloads[2]["execution_role"] == "delegate"
    for payload in payloads:
        _assert_exact_usage_fields(payload)
        assert payload["provider"] == "azure_openai"
        assert payload["model"] == "gpt-deployment"
        assert payload["model_publisher"] == "openai"
        assert payload["input_tokens"] == 2
        assert payload["output_tokens"] == 1


@pytest.mark.parametrize(
    ("failure", "execution_role", "expected_exception"),
    [
        ("error", "delegate", ValueError),
        ("timeout", "workflow_subagent", TimeoutError),
    ],
)
@pytest.mark.asyncio
async def test_leaf_agent_failure_logs_once(
    monkeypatch: Any,
    caplog: Any,
    failure: str,
    execution_role: str,
    expected_exception: type[BaseException],
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
        )

    payloads = _usage_payloads(caplog)
    assert len(payloads) == 1
    _assert_exact_usage_fields(payloads[0])
    assert payloads[0]["execution_role"] == execution_role
    assert payloads[0]["input_tokens"] is None
    assert payloads[0]["output_tokens"] is None


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
    _assert_exact_usage_fields(payloads[0])
    assert payloads[0]["input_tokens"] is None
    assert payloads[0]["output_tokens"] is None


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
