"""Unit tests for the Foundry Hosted Agent Responses transport firewall."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest

from azure_functions_agents.transport import foundry_responses
from azure_functions_agents.transport.foundry_responses import (
    FOUNDRY_RESPONSES_API_VERSION,
    FoundryResponse,
    FoundryResponseCreateRequest,
    FoundryResponseEventKind,
    FoundryResponseFailure,
    FoundryResponseFailureKind,
    FoundryResponseIncompleteReason,
    FoundryResponsesAdapter,
    FoundryResponsesClosedError,
    FoundryResponsesOperation,
    FoundryResponsesOperationError,
    FoundryResponsesOperationErrorKind,
    FoundryResponsesProtocolError,
    FoundryResponseStatus,
    FoundryResponseText,
    FoundrySessionCreateRequest,
)

_TRACEPARENT = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
_TRACESTATE = "vendor=state"


@dataclass
class _FakeProviderError:
    code: str | None
    message: str = "provider-message-that-must-not-leak"


@dataclass
class _FakeIncompleteDetails:
    reason: str | None


@dataclass
class _FakeProviderResponse:
    id: str = "response-private-id"
    status: str | None = "completed"
    output_text: str = "answer"
    error: _FakeProviderError | None = None
    incomplete_details: _FakeIncompleteDetails | None = None
    model_extra: dict[str, object] | None = None


@dataclass
class _FakeProviderEvent:
    type: str
    sequence_number: int
    response: _FakeProviderResponse | None = None
    delta: str = ""
    text: str = ""
    code: str | None = None


class _FakeStream:
    def __init__(self, events: list[_FakeProviderEvent]) -> None:
        self.events = events
        self.close_calls = 0

    def __aiter__(self) -> AsyncIterator[_FakeProviderEvent]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[_FakeProviderEvent]:
        for event in self.events:
            yield event

    async def close(self) -> None:
        self.close_calls += 1


class _FakeResponses:
    def __init__(self) -> None:
        self.created_response = _FakeProviderResponse()
        self.retrieved_response = _FakeProviderResponse()
        self.cancelled_response = _FakeProviderResponse(status="cancelled")
        self.stream = _FakeStream([])
        self.created_streams: list[_FakeStream] = []
        self.create_calls: list[dict[str, object]] = []
        self.retrieve_calls: list[tuple[str, dict[str, object]]] = []
        self.cancel_calls: list[str] = []
        self.create_error: Exception | None = None

    async def create(self, **kwargs: object) -> object:
        self.create_calls.append(kwargs)
        if self.create_error is not None:
            raise self.create_error
        if kwargs["stream"]:
            stream = _FakeStream(
                [
                    _FakeProviderEvent(
                        "response.created",
                        0,
                        response=self.created_response,
                    )
                ]
            )
            self.created_streams.append(stream)
            return stream
        return self.created_response

    async def retrieve(self, response_id: str, **kwargs: object) -> object:
        self.retrieve_calls.append((response_id, kwargs))
        if kwargs["stream"]:
            return self.stream
        return self.retrieved_response

    async def cancel(self, response_id: str) -> _FakeProviderResponse:
        self.cancel_calls.append(response_id)
        return self.cancelled_response


class _FakeOpenAIClient:
    def __init__(self, responses: _FakeResponses, closed: list[str]) -> None:
        self.responses = responses
        self._closed = closed

    async def close(self) -> None:
        self._closed.append("openai")


class _FakeProjectClient:
    def __init__(self, openai_client: _FakeOpenAIClient, closed: list[str]) -> None:
        self._openai_client = openai_client
        self._closed = closed
        self.agent_name: str | None = None
        self.api_version: str | None = None
        self.agents = _FakeAgents()

    def get_openai_client(
        self,
        *,
        agent_name: str,
    ) -> _FakeOpenAIClient:
        self.agent_name = agent_name
        return self._openai_client

    async def close(self) -> None:
        self._closed.append("project")


class _FakeCredential:
    def __init__(self, closed: list[str]) -> None:
        self._closed = closed

    async def close(self) -> None:
        self._closed.append("credential")


class _FakeStatusError(Exception):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeConnectionError(Exception):
    pass


class _FakeTimeoutError(Exception):
    pass


class _FakeAzureStatusError(Exception):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


@dataclass
class _FakeSession:
    agent_session_id: str
    version_indicator: _FakeVersionRef


class _FakeAgents:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, str]] = []
        self.get_calls: list[tuple[str, str]] = []
        self.error: Exception | None = None
        self.get_error: Exception | None = None

    async def create_session(
        self,
        agent_name: str,
        *,
        version_indicator: object,
        agent_session_id: str,
    ) -> _FakeSession:
        self.calls.append((agent_name, version_indicator, agent_session_id))
        if self.error is not None:
            raise self.error
        return _FakeSession(
            agent_session_id=agent_session_id,
            version_indicator=version_indicator,  # type: ignore[arg-type]
        )

    async def get_session(
        self,
        *,
        agent_name: str,
        session_id: str,
    ) -> _FakeSession:
        self.get_calls.append((agent_name, session_id))
        if self.get_error is not None:
            raise self.get_error
        version = next(
            version_indicator
            for called_name, version_indicator, called_id in self.calls
            if called_name == agent_name and called_id == session_id
        )
        return _FakeSession(
            agent_session_id=session_id,
            version_indicator=version,  # type: ignore[arg-type]
        )


@dataclass
class _FakeVersionRef:
    agent_version: str


class _FakeEnvironment:
    def __init__(self) -> None:
        self.closed: list[str] = []
        self.responses = _FakeResponses()
        self.openai_client = _FakeOpenAIClient(self.responses, self.closed)
        self.project_client = _FakeProjectClient(self.openai_client, self.closed)
        self.credential = _FakeCredential(self.closed)
        self.endpoint: str | None = None
        self.credential_passed: object | None = None
        self.api_version: str | None = None
        self.allow_preview: bool | None = None

    def make_project_client(
        self,
        *,
        endpoint: str,
        credential: object,
        api_version: str,
        allow_preview: bool,
    ) -> _FakeProjectClient:
        self.endpoint = endpoint
        self.credential_passed = credential
        self.api_version = api_version
        self.allow_preview = allow_preview
        return self.project_client

    def factories(self) -> foundry_responses._SdkFactories:
        return foundry_responses._SdkFactories(
            project_client=self.make_project_client,
            version_ref_indicator=_FakeVersionRef,
            errors=foundry_responses._SdkErrorTypes(
                azure_status_error=_FakeAzureStatusError,
                status_error=_FakeStatusError,
                connection_error=_FakeConnectionError,
                timeout_error=_FakeTimeoutError,
            ),
        )


async def _open_adapter(
    monkeypatch: pytest.MonkeyPatch,
    environment: _FakeEnvironment,
) -> FoundryResponsesAdapter:
    monkeypatch.setattr(foundry_responses, "_SDK_FACTORIES", environment.factories)
    monkeypatch.setattr(foundry_responses, "_CREDENTIAL_FACTORY", lambda: environment.credential)
    return await FoundryResponsesAdapter.open(
        project_endpoint="https://project.services.ai.azure.com/api/projects/runtime",
        agent_name="runtime-agent",
    )


@pytest.mark.asyncio
async def test_create_session_pins_caller_selected_id_to_concrete_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _FakeEnvironment()
    adapter = await _open_adapter(monkeypatch, environment)

    session = await adapter.create_session(
        FoundrySessionCreateRequest.create(
            agent_session_id="private-session",
            agent_version="7",
        )
    )

    assert session.agent_session_id == "private-session"
    assert session.agent_version == "7"
    assert environment.project_client.agents.calls == [
        (
            "runtime-agent",
            _FakeVersionRef(agent_version="7"),
            "private-session",
        )
    ]


@pytest.mark.asyncio
async def test_create_session_normalizes_azure_http_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _FakeEnvironment()
    environment.project_client.agents.error = _FakeAzureStatusError(403)
    adapter = await _open_adapter(monkeypatch, environment)

    with pytest.raises(FoundryResponsesOperationError) as exc_info:
        await adapter.create_session(
            FoundrySessionCreateRequest.create(
                agent_session_id="private-session",
                agent_version="7",
            )
        )

    assert exc_info.value.operation is FoundryResponsesOperation.CREATE_SESSION
    assert exc_info.value.kind is FoundryResponsesOperationErrorKind.UNAUTHORIZED
    assert exc_info.value.retryable is False


@pytest.mark.asyncio
async def test_create_session_recovers_existing_deterministic_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _FakeEnvironment()
    environment.project_client.agents.error = _FakeAzureStatusError(409)
    adapter = await _open_adapter(monkeypatch, environment)

    session = await adapter.create_session(
        FoundrySessionCreateRequest.create(
            agent_session_id="private-session",
            agent_version="7",
        )
    )

    assert session.agent_session_id == "private-session"
    assert session.agent_version == "7"


@pytest.mark.asyncio
async def test_get_session_returns_provider_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _FakeEnvironment()
    adapter = await _open_adapter(monkeypatch, environment)
    await adapter.create_session(
        FoundrySessionCreateRequest.create(
            agent_session_id="private-session",
            agent_version="7",
        )
    )

    session = await adapter.get_session("private-session")

    assert session.agent_session_id == "private-session"
    assert session.agent_version == "7"
    assert environment.project_client.agents.get_calls == [
        ("runtime-agent", "private-session")
    ]


@pytest.mark.asyncio
async def test_get_session_normalizes_missing_retained_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _FakeEnvironment()
    environment.project_client.agents.get_error = _FakeAzureStatusError(404)
    adapter = await _open_adapter(monkeypatch, environment)

    with pytest.raises(FoundryResponsesOperationError) as exc_info:
        await adapter.get_session("private-session")

    assert exc_info.value.operation is FoundryResponsesOperation.GET_SESSION
    assert exc_info.value.kind is FoundryResponsesOperationErrorKind.NOT_FOUND
    assert exc_info.value.retryable is False


@pytest.mark.asyncio
async def test_create_binds_agent_client_and_forces_stored_background_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _FakeEnvironment()
    environment.responses.created_response = _FakeProviderResponse(
        id="response-1",
        status="queued",
        output_text="",
        model_extra={"agent_session_id": "agent-session-1"},
    )

    adapter = await _open_adapter(monkeypatch, environment)
    response = await adapter.create(
        FoundryResponseCreateRequest.create(
            input_text="Run the configured agent.",
            agent_session_id="agent-session-1",
        )
    )

    assert environment.endpoint == "https://project.services.ai.azure.com/api/projects/runtime"
    assert environment.credential_passed is environment.credential
    assert environment.project_client.agent_name == "runtime-agent"
    assert environment.api_version == FOUNDRY_RESPONSES_API_VERSION
    assert environment.allow_preview is True
    assert environment.responses.create_calls == [
        {
            "input": "Run the configured agent.",
            "background": True,
            "store": True,
            "stream": False,
            "extra_body": {"agent_session_id": "agent-session-1"},
        }
    ]
    assert response.response_id == "response-1"
    assert response.status is FoundryResponseStatus.QUEUED
    assert response.output_text == ""
    assert response.agent_session_id == "agent-session-1"


@pytest.mark.asyncio
async def test_create_passes_exact_w3c_trace_headers_to_the_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _FakeEnvironment()
    adapter = await _open_adapter(monkeypatch, environment)

    await adapter.create(
        FoundryResponseCreateRequest.create(
            input_text="Run the configured agent.",
            trace_headers={"traceparent": _TRACEPARENT, "tracestate": _TRACESTATE},
        )
    )

    assert environment.responses.create_calls == [
        {
            "input": "Run the configured agent.",
            "background": True,
            "store": True,
            "stream": False,
            "extra_body": None,
            "extra_headers": {"traceparent": _TRACEPARENT, "tracestate": _TRACESTATE},
        }
    ]


@pytest.mark.asyncio
async def test_create_stream_retains_and_closes_the_initial_live_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _FakeEnvironment()
    environment.responses.created_response = _FakeProviderResponse(
        id="response-stream",
        status="queued",
        output_text="",
        model_extra={"agent_session_id": "agent-session-1"},
    )
    adapter = await _open_adapter(monkeypatch, environment)

    stream = await adapter.create_stream(
        FoundryResponseCreateRequest.create(
            input_text="Run the configured agent.",
            agent_session_id="agent-session-1",
            trace_headers={"traceparent": _TRACEPARENT, "tracestate": _TRACESTATE},
        )
    )

    assert stream.response.response_id == "response-stream"
    assert environment.responses.create_calls == [
        {
            "input": "Run the configured agent.",
            "background": True,
            "store": True,
            "stream": True,
            "extra_body": {"agent_session_id": "agent-session-1"},
            "extra_headers": {"traceparent": _TRACEPARENT, "tracestate": _TRACESTATE},
        }
    ]
    assert environment.responses.created_streams[0].close_calls == 0

    events = [event async for event in stream]

    assert [(event.provider_sequence, event.kind) for event in events] == [
        (0, FoundryResponseEventKind.CREATED)
    ]
    assert environment.responses.created_streams[0].close_calls == 1


def test_response_create_request_canonicalizes_valid_w3c_trace_headers() -> None:
    request = FoundryResponseCreateRequest.create(
        input_text="prompt",
        trace_headers={"traceparent": _TRACEPARENT, "tracestate": _TRACESTATE},
    )

    assert request.trace_headers == (
        ("traceparent", _TRACEPARENT),
        ("tracestate", _TRACESTATE),
    )


@pytest.mark.parametrize(
    "trace_headers",
    [
        {"Traceparent": _TRACEPARENT},
        {"traceparent": _TRACEPARENT, "baggage": "private=value"},
        {"traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-0A"},
        {"traceparent": "00-00000000000000000000000000000000-0123456789abcdef-01"},
        {"tracestate": _TRACESTATE},
        {"traceparent": _TRACEPARENT, "tracestate": "Vendor=state"},
        {"traceparent": _TRACEPARENT, "tracestate": "vendor=state,vendor=other"},
        {"traceparent": _TRACEPARENT, "tracestate": "vendor=" + ("a" * 513)},
    ],
)
def test_response_create_request_rejects_malformed_or_extra_trace_headers(
    trace_headers: object,
) -> None:
    with pytest.raises(FoundryResponsesProtocolError):
        FoundryResponseCreateRequest.create(
            input_text="prompt",
            trace_headers=trace_headers,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_create_omits_session_extra_body_when_no_mapping_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _FakeEnvironment()
    adapter = await _open_adapter(monkeypatch, environment)

    await adapter.create(FoundryResponseCreateRequest.create(input_text="First turn"))

    assert environment.responses.create_calls[0]["extra_body"] is None


@pytest.mark.asyncio
async def test_adapter_structurally_satisfies_the_responses_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _FakeEnvironment()
    adapter = await _open_adapter(monkeypatch, environment)

    assert isinstance(adapter, foundry_responses.FoundryResponsesTransport)


@pytest.mark.asyncio
async def test_retrieve_and_cancel_normalize_response_status_and_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _FakeEnvironment()
    environment.responses.retrieved_response = _FakeProviderResponse(
        id="response-2",
        status="incomplete",
        output_text="partial answer",
        error=_FakeProviderError("rate_limit_exceeded"),
        incomplete_details=_FakeIncompleteDetails("content_filter"),
    )
    adapter = await _open_adapter(monkeypatch, environment)

    retrieved = await adapter.retrieve("response-2")
    cancelled = await adapter.cancel("response-2")

    assert retrieved.status is FoundryResponseStatus.INCOMPLETE
    assert retrieved.error == FoundryResponseFailure(
        kind=FoundryResponseFailureKind.RATE_LIMITED,
        retryable=True,
    )
    assert retrieved.incomplete_details is not None
    assert retrieved.incomplete_details.reason is FoundryResponseIncompleteReason.CONTENT_FILTER
    assert "provider-message-that-must-not-leak" not in repr(retrieved)
    assert environment.responses.retrieve_calls == [("response-2", {"stream": False})]
    assert cancelled.status is FoundryResponseStatus.CANCELLED
    assert environment.responses.cancel_calls == ["response-2"]


@pytest.mark.asyncio
async def test_replay_preserves_provider_sequences_and_closes_only_the_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _FakeEnvironment()
    snapshot = _FakeProviderResponse(id="response-3", status="in_progress", output_text="")
    environment.responses.stream = _FakeStream(
        [
            _FakeProviderEvent("response.created", 0, response=snapshot),
            _FakeProviderEvent("response.output_text.delta", 1, delta="hel"),
            _FakeProviderEvent("response.output_text.done", 2, text="hello"),
            _FakeProviderEvent("error", 3, code="server_error"),
            _FakeProviderEvent("response.output_item.added", 4),
        ]
    )
    adapter = await _open_adapter(monkeypatch, environment)

    events = [event async for event in adapter.replay("response-3", starting_after=0)]

    assert [(event.provider_sequence, event.kind) for event in events] == [
        (0, FoundryResponseEventKind.CREATED),
        (1, FoundryResponseEventKind.TEXT_DELTA),
        (2, FoundryResponseEventKind.TEXT_DONE),
        (3, FoundryResponseEventKind.ERROR),
        (4, FoundryResponseEventKind.OTHER),
    ]
    assert events[0].data == FoundryResponse.create(
        response_id="response-3",
        status=FoundryResponseStatus.IN_PROGRESS,
        output_text="",
        agent_session_id=None,
        error=None,
        incomplete_details=None,
    )
    assert events[1].data == FoundryResponseText("hel")
    assert events[2].data == FoundryResponseText("hello")
    assert events[3].data == FoundryResponseFailure(
        kind=FoundryResponseFailureKind.TRANSIENT,
        retryable=True,
    )
    assert events[4].data is None
    assert environment.responses.retrieve_calls == [
        ("response-3", {"stream": True, "starting_after": 0})
    ]
    assert environment.responses.stream.close_calls == 1
    assert environment.responses.cancel_calls == []
    assert environment.closed == []


@pytest.mark.asyncio
async def test_replay_omits_starting_after_and_closes_an_early_disconnected_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _FakeEnvironment()
    environment.responses.stream = _FakeStream(
        [
            _FakeProviderEvent(
                "response.created",
                0,
                response=_FakeProviderResponse(id="response-4", output_text=""),
            ),
            _FakeProviderEvent("response.output_text.delta", 1, delta="later"),
        ]
    )
    adapter = await _open_adapter(monkeypatch, environment)
    reader = adapter.replay("response-4")

    first = await anext(reader)
    await reader.aclose()

    assert first.provider_sequence == 0
    assert environment.responses.retrieve_calls == [("response-4", {"stream": True})]
    assert environment.responses.stream.close_calls == 1
    assert environment.responses.cancel_calls == []
    assert environment.closed == []


@pytest.mark.asyncio
async def test_provider_operation_errors_are_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _FakeEnvironment()
    environment.responses.create_error = _FakeStatusError(429)
    adapter = await _open_adapter(monkeypatch, environment)

    with pytest.raises(FoundryResponsesOperationError) as exc_info:
        await adapter.create(FoundryResponseCreateRequest.create(input_text="retryable failure"))

    assert exc_info.value.operation is FoundryResponsesOperation.CREATE
    assert exc_info.value.kind is FoundryResponsesOperationErrorKind.RATE_LIMITED
    assert exc_info.value.retryable is True
    assert "response-private-id" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_close_releases_owned_resources_once_and_prevents_future_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _FakeEnvironment()
    adapter = await _open_adapter(monkeypatch, environment)

    await adapter.close()
    await adapter.close()

    assert environment.closed == ["openai", "project", "credential"]
    with pytest.raises(FoundryResponsesClosedError):
        await adapter.retrieve("response-5")


@pytest.mark.parametrize(
    "factory",
    [
        lambda: FoundryResponseCreateRequest.create(input_text=""),
        lambda: FoundryResponseCreateRequest.create(input_text="prompt", agent_session_id=" "),
        lambda: foundry_responses.FoundryResponseEvent.create(
            provider_sequence=-1,
            kind=FoundryResponseEventKind.OTHER,
            data=None,
        ),
        lambda: foundry_responses.FoundryResponseEvent.create(
            provider_sequence=True,
            kind=FoundryResponseEventKind.OTHER,
            data=None,
        ),
        lambda: foundry_responses._optional_provider_sequence("not-a-sequence"),
    ],
)
def test_factories_reject_invalid_boundary_values(factory: Any) -> None:
    with pytest.raises(FoundryResponsesProtocolError):
        factory()
