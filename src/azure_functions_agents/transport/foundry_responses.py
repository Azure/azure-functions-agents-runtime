"""Narrow async firewall for Foundry Hosted Agent Responses."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from importlib import import_module
from inspect import signature
from typing import TYPE_CHECKING, Literal, Protocol, cast, overload, runtime_checkable

from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import HttpResponseError

from azure_functions_agents._credential import build_async_credential

if TYPE_CHECKING:
    from azure.ai.projects.models import VersionRefIndicator
    from openai import APIConnectionError, APIStatusError, APITimeoutError

FOUNDRY_RESPONSES_API_VERSION = "v1"
_TRACEPARENT_HEADER = "traceparent"
_TRACESTATE_HEADER = "tracestate"
_TRACEPARENT_PATTERN = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")
_TRACESTATE_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_*/-]{0,255}(?:@[a-z0-9_*/-]{1,241})?$")
_TRACESTATE_VALUE_PATTERN = re.compile(r"^[\x20-\x2b\x2d-\x3c\x3e-\x7e]{0,256}$")
_TRACEPARENT_MAX_LENGTH = 55
_TRACESTATE_MAX_LENGTH = 512
_TRACESTATE_MAX_MEMBERS = 32


class FoundryResponsesError(Exception):
    """Base error for the Foundry Responses transport boundary."""


class FoundryResponsesDependencyError(FoundryResponsesError):
    """Raised when the agent-bound Foundry SDK surface is unavailable."""


class FoundryResponsesClosedError(FoundryResponsesError):
    """Raised when a closed Responses transport is used."""


class FoundryResponsesProtocolError(FoundryResponsesError):
    """Raised when a provider result cannot be safely normalized."""


class FoundryResponsesOperation(StrEnum):
    """A provider operation exposed by this narrow transport."""

    CREATE_SESSION = "create_session"
    GET_SESSION = "get_session"
    CREATE = "create"
    RETRIEVE = "retrieve"
    REPLAY = "replay"
    CANCEL = "cancel"


class FoundryResponsesOperationErrorKind(StrEnum):
    """Sanitized categories for provider operation failures."""

    UNAUTHORIZED = "unauthorized"
    NOT_FOUND = "not_found"
    INVALID_REQUEST = "invalid_request"
    RATE_LIMITED = "rate_limited"
    TRANSIENT = "transient"
    UNKNOWN = "unknown"


class FoundryResponsesOperationError(FoundryResponsesError):
    """A sanitized operation failure that exposes no provider payload."""

    def __init__(
        self,
        operation: FoundryResponsesOperation,
        kind: FoundryResponsesOperationErrorKind,
        *,
        retryable: bool,
    ) -> None:
        self.operation = operation
        self.kind = kind
        self.retryable = retryable
        super().__init__(f"Foundry Responses {operation.value} failed: {kind.value}.")


class FoundryResponseStatus(StrEnum):
    """Provider lifecycle state projected into the runtime boundary."""

    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INCOMPLETE = "incomplete"
    UNKNOWN = "unknown"


class FoundryResponseFailureKind(StrEnum):
    """Sanitized categories for a failed Response payload."""

    RATE_LIMITED = "rate_limited"
    TRANSIENT = "transient"
    INVALID_INPUT = "invalid_input"
    CONTENT_FILTERED = "content_filtered"
    UNKNOWN = "unknown"


class FoundryResponseIncompleteReason(StrEnum):
    """Sanitized reasons for an incomplete Response payload."""

    MAX_OUTPUT_TOKENS = "max_output_tokens"
    CONTENT_FILTER = "content_filter"
    UNKNOWN = "unknown"


class FoundryResponseEventKind(StrEnum):
    """Response event kinds retained by the transport."""

    CREATED = "created"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INCOMPLETE = "incomplete"
    TEXT_DELTA = "text_delta"
    TEXT_DONE = "text_done"
    ERROR = "error"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class FoundryResponseFailure:
    """A structured provider failure without its raw code or message."""

    kind: FoundryResponseFailureKind
    retryable: bool

    @classmethod
    def create(cls, provider_code: str | None) -> FoundryResponseFailure:
        kind = (
            FoundryResponseFailureKind.UNKNOWN
            if provider_code is None
            else _FAILURE_KIND_BY_PROVIDER_CODE.get(
                provider_code,
                FoundryResponseFailureKind.UNKNOWN,
            )
        )
        return cls(
            kind=kind,
            retryable=kind
            in {FoundryResponseFailureKind.RATE_LIMITED, FoundryResponseFailureKind.TRANSIENT},
        )


@dataclass(frozen=True, slots=True)
class FoundryResponseIncompleteDetails:
    """A structured incomplete result without provider-specific values."""

    reason: FoundryResponseIncompleteReason

    @classmethod
    def create(cls, provider_reason: str | None) -> FoundryResponseIncompleteDetails:
        return cls(
            reason=(
                FoundryResponseIncompleteReason.UNKNOWN
                if provider_reason is None
                else _INCOMPLETE_REASON_BY_PROVIDER_VALUE.get(
                    provider_reason,
                    FoundryResponseIncompleteReason.UNKNOWN,
                )
            )
        )


@dataclass(frozen=True, slots=True)
class FoundrySessionCreateRequest:
    """Input for one explicitly version-bound hosted-agent session."""

    agent_session_id: str
    agent_version: str

    @classmethod
    def create(
        cls,
        *,
        agent_session_id: str,
        agent_version: str,
    ) -> FoundrySessionCreateRequest:
        return cls(
            agent_session_id=_require_nonempty_text(agent_session_id, "agent_session_id"),
            agent_version=_require_nonempty_text(agent_version, "agent_version"),
        )


@dataclass(frozen=True, slots=True)
class FoundrySession:
    """A version-bound provider session projected without an SDK object."""

    agent_session_id: str
    agent_version: str

    @classmethod
    def create(
        cls,
        *,
        agent_session_id: str,
        agent_version: str,
    ) -> FoundrySession:
        return cls(
            agent_session_id=_require_nonempty_text(agent_session_id, "agent_session_id"),
            agent_version=_require_nonempty_text(agent_version, "agent_version"),
        )


@dataclass(frozen=True, slots=True)
class FoundryResponseCreateRequest:
    """Input for one stored background Response submission."""

    input_text: str
    agent_session_id: str | None = None
    trace_headers: tuple[tuple[str, str], ...] = field(default=(), repr=False)

    @classmethod
    def create(
        cls,
        *,
        input_text: str,
        agent_session_id: str | None = None,
        trace_headers: Mapping[str, str] | None = None,
    ) -> FoundryResponseCreateRequest:
        return cls(
            input_text=_require_nonempty_text(input_text, "input_text"),
            agent_session_id=_optional_identifier(agent_session_id, "agent_session_id"),
            trace_headers=_canonical_trace_headers(trace_headers),
        )


@dataclass(frozen=True, slots=True)
class FoundryResponse:
    """A provider Response projected without an SDK object or raw error values."""

    response_id: str
    status: FoundryResponseStatus
    output_text: str
    agent_session_id: str | None
    error: FoundryResponseFailure | None
    incomplete_details: FoundryResponseIncompleteDetails | None

    @classmethod
    def create(
        cls,
        *,
        response_id: str,
        status: FoundryResponseStatus,
        output_text: str,
        agent_session_id: str | None,
        error: FoundryResponseFailure | None,
        incomplete_details: FoundryResponseIncompleteDetails | None,
    ) -> FoundryResponse:
        if not isinstance(status, FoundryResponseStatus):
            raise FoundryResponsesProtocolError("Foundry Responses returned an invalid status.")
        if not isinstance(output_text, str):
            raise FoundryResponsesProtocolError("Foundry Responses returned invalid output text.")
        return cls(
            response_id=_require_nonempty_text(response_id, "response_id"),
            status=status,
            output_text=output_text,
            agent_session_id=_optional_identifier(agent_session_id, "agent_session_id"),
            error=error,
            incomplete_details=incomplete_details,
        )


@dataclass(frozen=True, slots=True)
class FoundryResponseText:
    """Text emitted by a retained output-text event."""

    text: str

    @classmethod
    def create(cls, text: str) -> FoundryResponseText:
        if not isinstance(text, str):
            raise FoundryResponsesProtocolError("Foundry Responses returned invalid event text.")
        return cls(text=text)


type FoundryResponseEventData = FoundryResponse | FoundryResponseText | FoundryResponseFailure | None


@dataclass(frozen=True, slots=True)
class FoundryResponseEvent:
    """A typed Response event retaining its provider 0-based sequence."""

    provider_sequence: int
    kind: FoundryResponseEventKind
    data: FoundryResponseEventData

    @classmethod
    def create(
        cls,
        *,
        provider_sequence: int,
        kind: FoundryResponseEventKind,
        data: FoundryResponseEventData,
    ) -> FoundryResponseEvent:
        if isinstance(provider_sequence, bool) or not isinstance(provider_sequence, int):
            raise FoundryResponsesProtocolError("Foundry Responses returned an invalid event sequence.")
        if provider_sequence < 0:
            raise FoundryResponsesProtocolError("Foundry Responses returned an invalid event sequence.")
        if not isinstance(kind, FoundryResponseEventKind):
            raise FoundryResponsesProtocolError("Foundry Responses returned an invalid event kind.")
        return cls(provider_sequence=provider_sequence, kind=kind, data=data)


@runtime_checkable
class FoundryResponseEventStream(Protocol):
    """A replayable Response plus its current live event reader."""

    response: FoundryResponse

    def __aiter__(self) -> AsyncIterator[FoundryResponseEvent]: ...

    async def close(self) -> None:
        """Close this reader without cancelling the background Response."""


@runtime_checkable
class FoundryResponsesTransport(Protocol):
    """The private mapping boundary for stored Hosted Agent Responses."""

    async def create_session(self, request: FoundrySessionCreateRequest) -> FoundrySession:
        """Create one caller-selected session pinned to a concrete agent version."""

    async def get_session(self, agent_session_id: str) -> FoundrySession:
        """Get one existing session bound to this hosted agent."""

    async def create(self, request: FoundryResponseCreateRequest) -> FoundryResponse:
        """Create exactly one stored, background, non-streaming Response."""

    async def create_stream(
        self,
        request: FoundryResponseCreateRequest,
    ) -> FoundryResponseEventStream:
        """Create one stored background Response and retain its live reader."""

    async def retrieve(self, response_id: str) -> FoundryResponse:
        """Retrieve one Response by its private provider identifier."""

    def replay(
        self,
        response_id: str,
        *,
        starting_after: int | None = None,
    ) -> AsyncIterator[FoundryResponseEvent]:
        """Replay or tail Response events after an optional provider sequence."""

    async def cancel(self, response_id: str) -> FoundryResponse:
        """Request cancellation of one stored background Response."""

    async def close(self) -> None:
        """Release the client and credential resources owned by this transport."""


class _AsyncCloseable(Protocol):
    async def close(self) -> None: ...


class _ProviderError(Protocol):
    code: str | None


class _ProviderIncompleteDetails(Protocol):
    reason: str | None


class _ProviderResponse(Protocol):
    id: str
    status: str | None
    output_text: str
    error: _ProviderError | None
    incomplete_details: _ProviderIncompleteDetails | None
    model_extra: Mapping[str, object] | None


class _ProviderEvent(Protocol):
    type: str
    sequence_number: int
    response: _ProviderResponse
    delta: str
    text: str
    code: str | None


class _ProviderEventStream(_AsyncCloseable, Protocol):
    def __aiter__(self) -> AsyncIterator[_ProviderEvent]: ...


class _ProjectedResponseEventStream(FoundryResponseEventStream):
    def __init__(
        self,
        *,
        created_event: FoundryResponseEvent,
        stream: _ProviderEventStream,
        iterator: AsyncIterator[_ProviderEvent],
        errors: _SdkErrorTypes,
    ) -> None:
        assert isinstance(created_event.data, FoundryResponse)
        self.response = created_event.data
        self._created_event = created_event
        self._stream = stream
        self._iterator = iterator
        self._errors = errors
        self._closed = False
        self._iterated = False

    def __aiter__(self) -> AsyncIterator[FoundryResponseEvent]:
        if self._iterated:
            raise FoundryResponsesProtocolError(
                "Foundry Responses live stream can only be consumed once."
            )
        self._iterated = True
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[FoundryResponseEvent]:
        try:
            yield self._created_event
            async for event in self._iterator:
                yield _project_event(event)
        except self._errors.status_error as exc:
            raise _operation_error_for_status(
                FoundryResponsesOperation.REPLAY,
                exc.status_code,
            ) from None
        except (self._errors.connection_error, self._errors.timeout_error):
            raise _transient_operation_error(FoundryResponsesOperation.REPLAY) from None
        finally:
            await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await _close_resource(self._stream)


class _ResponsesResource(Protocol):
    @overload
    async def create(
        self,
        *,
        input: str,
        background: bool,
        store: bool,
        stream: Literal[False],
        extra_body: Mapping[str, str] | None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> _ProviderResponse: ...

    @overload
    async def create(
        self,
        *,
        input: str,
        background: bool,
        store: bool,
        stream: Literal[True],
        extra_body: Mapping[str, str] | None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> _ProviderEventStream: ...

    @overload
    async def retrieve(
        self,
        response_id: str,
        *,
        stream: Literal[False],
    ) -> _ProviderResponse: ...

    @overload
    async def retrieve(
        self,
        response_id: str,
        *,
        stream: Literal[True],
    ) -> _ProviderEventStream: ...

    @overload
    async def retrieve(
        self,
        response_id: str,
        *,
        stream: Literal[True],
        starting_after: int,
    ) -> _ProviderEventStream: ...

    async def cancel(self, response_id: str) -> _ProviderResponse: ...


class _ProviderSession(Protocol):
    agent_session_id: str
    version_indicator: _ProviderVersionIndicator


class _ProviderVersionIndicator(Protocol):
    agent_version: str


class _AgentsResource(Protocol):
    async def create_session(
        self,
        agent_name: str,
        *,
        version_indicator: VersionRefIndicator,
        agent_session_id: str,
    ) -> _ProviderSession: ...

    async def get_session(
        self,
        *,
        agent_name: str,
        session_id: str,
    ) -> _ProviderSession: ...


class _OpenAIClient(_AsyncCloseable, Protocol):
    responses: _ResponsesResource


class _AgentBoundProjectClient(_AsyncCloseable, Protocol):
    agents: _AgentsResource

    def get_openai_client(
        self,
        *,
        agent_name: str,
    ) -> _OpenAIClient: ...


@dataclass(frozen=True, slots=True)
class _SdkErrorTypes:
    azure_status_error: type[HttpResponseError]
    status_error: type[APIStatusError]
    connection_error: type[APIConnectionError]
    timeout_error: type[APITimeoutError]


@dataclass(frozen=True, slots=True)
class _SdkFactories:
    project_client: Callable[..., _AgentBoundProjectClient]
    version_ref_indicator: Callable[..., VersionRefIndicator]
    errors: _SdkErrorTypes


def _load_sdk_factories() -> _SdkFactories:
    """Load the agent-bound SDK surface only when this transport opens."""

    try:
        projects_module = import_module("azure.ai.projects.aio")
        projects_models_module = import_module("azure.ai.projects.models")
        openai_module = import_module("openai")
    except ImportError:
        raise FoundryResponsesDependencyError(
            "Foundry Responses support requires azure-ai-projects and openai."
        ) from None

    project_client_type = projects_module.AIProjectClient
    get_openai_client = project_client_type.get_openai_client
    if (
        "allow_preview" not in signature(project_client_type).parameters
        or "agent_name" not in signature(get_openai_client).parameters
    ):
        raise FoundryResponsesDependencyError(
            "Foundry Responses support requires the agent-bound azure-ai-projects preview API."
        )

    return _SdkFactories(
        project_client=cast(Callable[..., _AgentBoundProjectClient], project_client_type),
        version_ref_indicator=projects_models_module.VersionRefIndicator,
        errors=_SdkErrorTypes(
            azure_status_error=HttpResponseError,
            status_error=openai_module.APIStatusError,
            connection_error=openai_module.APIConnectionError,
            timeout_error=openai_module.APITimeoutError,
        ),
    )


_SDK_FACTORIES: Callable[[], _SdkFactories] = _load_sdk_factories
_CREDENTIAL_FACTORY: Callable[[], AsyncTokenCredential] = build_async_credential


class FoundryResponsesAdapter(FoundryResponsesTransport):
    """A closeable, agent-bound adapter for the Foundry Responses SDK."""

    def __init__(
        self,
        *,
        credential: AsyncTokenCredential,
        project_client: _AgentBoundProjectClient,
        openai_client: _OpenAIClient,
        agent_name: str,
        version_ref_indicator: Callable[..., VersionRefIndicator],
        errors: _SdkErrorTypes,
    ) -> None:
        self._credential = credential
        self._project_client = project_client
        self._openai_client = openai_client
        self._agent_name = agent_name
        self._version_ref_indicator = version_ref_indicator
        self._errors = errors
        self._closed = False

    @classmethod
    async def open(
        cls,
        *,
        project_endpoint: str,
        agent_name: str,
    ) -> FoundryResponsesAdapter:
        """Build a current agent-bound client with the pinned Responses API version."""

        endpoint = _require_nonempty_text(project_endpoint, "project_endpoint")
        resolved_agent_name = _require_nonempty_text(agent_name, "agent_name")
        factories = _SDK_FACTORIES()
        credential = _CREDENTIAL_FACTORY()
        try:
            project_client = factories.project_client(
                endpoint=endpoint,
                credential=credential,
                api_version=FOUNDRY_RESPONSES_API_VERSION,
                allow_preview=True,
            )
        except BaseException:
            await _close_resource(credential)
            raise
        try:
            openai_client = project_client.get_openai_client(agent_name=resolved_agent_name)
        except BaseException:
            try:
                await _close_resource(project_client)
            finally:
                await _close_resource(credential)
            raise
        return cls(
            credential=credential,
            project_client=project_client,
            openai_client=openai_client,
            agent_name=resolved_agent_name,
            version_ref_indicator=factories.version_ref_indicator,
            errors=factories.errors,
        )

    async def create_session(self, request: FoundrySessionCreateRequest) -> FoundrySession:
        self._ensure_open()
        try:
            session = await self._project_client.agents.create_session(
                self._agent_name,
                version_indicator=self._version_ref_indicator(
                    agent_version=request.agent_version
                ),
                agent_session_id=request.agent_session_id,
            )
        except self._errors.azure_status_error as exc:
            if exc.status_code == 409:
                try:
                    session = await self._project_client.agents.get_session(
                        agent_name=self._agent_name,
                        session_id=request.agent_session_id,
                    )
                except self._errors.azure_status_error as get_error:
                    raise _operation_error_for_status(
                        FoundryResponsesOperation.CREATE_SESSION,
                        get_error.status_code or 0,
                    ) from None
                return _project_session(session)
            raise _operation_error_for_status(
                FoundryResponsesOperation.CREATE_SESSION,
                exc.status_code or 0,
            ) from None
        except self._errors.status_error as exc:
            raise _operation_error_for_status(
                FoundryResponsesOperation.CREATE_SESSION,
                exc.status_code,
            ) from None
        except (self._errors.connection_error, self._errors.timeout_error):
            raise _transient_operation_error(FoundryResponsesOperation.CREATE_SESSION) from None
        return _project_session(session)

    async def get_session(self, agent_session_id: str) -> FoundrySession:
        self._ensure_open()
        resolved_session_id = _require_nonempty_text(
            agent_session_id,
            "agent_session_id",
        )
        try:
            session = await self._project_client.agents.get_session(
                agent_name=self._agent_name,
                session_id=resolved_session_id,
            )
        except self._errors.azure_status_error as exc:
            raise _operation_error_for_status(
                FoundryResponsesOperation.GET_SESSION,
                exc.status_code or 0,
            ) from None
        except self._errors.status_error as exc:
            raise _operation_error_for_status(
                FoundryResponsesOperation.GET_SESSION,
                exc.status_code,
            ) from None
        except (self._errors.connection_error, self._errors.timeout_error):
            raise _transient_operation_error(FoundryResponsesOperation.GET_SESSION) from None
        return _project_session(session)

    async def create(self, request: FoundryResponseCreateRequest) -> FoundryResponse:
        self._ensure_open()
        extra_body = (
            None
            if request.agent_session_id is None
            else {"agent_session_id": request.agent_session_id}
        )
        try:
            if request.trace_headers:
                response = await self._openai_client.responses.create(
                    input=request.input_text,
                    background=True,
                    store=True,
                    stream=False,
                    extra_body=extra_body,
                    extra_headers=dict(request.trace_headers),
                )
            else:
                response = await self._openai_client.responses.create(
                    input=request.input_text,
                    background=True,
                    store=True,
                    stream=False,
                    extra_body=extra_body,
                )
        except self._errors.status_error as exc:
            raise _operation_error_for_status(FoundryResponsesOperation.CREATE, exc.status_code) from None
        except (self._errors.connection_error, self._errors.timeout_error):
            raise _transient_operation_error(FoundryResponsesOperation.CREATE) from None
        return _project_response(response)

    async def create_stream(
        self,
        request: FoundryResponseCreateRequest,
    ) -> FoundryResponseEventStream:
        self._ensure_open()
        extra_body = (
            None
            if request.agent_session_id is None
            else {"agent_session_id": request.agent_session_id}
        )
        stream: _ProviderEventStream | None = None
        handed_off = False
        try:
            if request.trace_headers:
                stream = await self._openai_client.responses.create(
                    input=request.input_text,
                    background=True,
                    store=True,
                    stream=True,
                    extra_body=extra_body,
                    extra_headers=dict(request.trace_headers),
                )
            else:
                stream = await self._openai_client.responses.create(
                    input=request.input_text,
                    background=True,
                    store=True,
                    stream=True,
                    extra_body=extra_body,
                )
            iterator = stream.__aiter__()
            try:
                first_event = await anext(iterator)
            except StopAsyncIteration:
                raise FoundryResponsesProtocolError(
                    "Foundry Responses create returned no created event."
                ) from None
            projected = _project_event(first_event)
            if (
                projected.provider_sequence != 0
                or projected.kind is not FoundryResponseEventKind.CREATED
                or not isinstance(projected.data, FoundryResponse)
            ):
                raise FoundryResponsesProtocolError(
                    "Foundry Responses create did not begin with a created event."
                )
            handed_off = True
            return _ProjectedResponseEventStream(
                created_event=projected,
                stream=stream,
                iterator=iterator,
                errors=self._errors,
            )
        except self._errors.status_error as exc:
            raise _operation_error_for_status(FoundryResponsesOperation.CREATE, exc.status_code) from None
        except (self._errors.connection_error, self._errors.timeout_error):
            raise _transient_operation_error(FoundryResponsesOperation.CREATE) from None
        finally:
            if stream is not None and not handed_off:
                await _close_resource(stream)

    async def retrieve(self, response_id: str) -> FoundryResponse:
        self._ensure_open()
        resolved_response_id = _require_nonempty_text(response_id, "response_id")
        try:
            response = await self._openai_client.responses.retrieve(
                resolved_response_id,
                stream=False,
            )
        except self._errors.status_error as exc:
            raise _operation_error_for_status(
                FoundryResponsesOperation.RETRIEVE,
                exc.status_code,
            ) from None
        except (self._errors.connection_error, self._errors.timeout_error):
            raise _transient_operation_error(FoundryResponsesOperation.RETRIEVE) from None
        return _project_response(response)

    async def replay(
        self,
        response_id: str,
        *,
        starting_after: int | None = None,
    ) -> AsyncIterator[FoundryResponseEvent]:
        self._ensure_open()
        resolved_response_id = _require_nonempty_text(response_id, "response_id")
        resolved_starting_after = _optional_provider_sequence(starting_after)
        stream: _ProviderEventStream | None = None
        try:
            if resolved_starting_after is None:
                stream = await self._openai_client.responses.retrieve(
                    resolved_response_id,
                    stream=True,
                )
            else:
                stream = await self._openai_client.responses.retrieve(
                    resolved_response_id,
                    stream=True,
                    starting_after=resolved_starting_after,
                )
            async for event in stream:
                yield _project_event(event)
        except self._errors.status_error as exc:
            raise _operation_error_for_status(FoundryResponsesOperation.REPLAY, exc.status_code) from None
        except (self._errors.connection_error, self._errors.timeout_error):
            raise _transient_operation_error(FoundryResponsesOperation.REPLAY) from None
        finally:
            if stream is not None:
                await _close_resource(stream)

    async def cancel(self, response_id: str) -> FoundryResponse:
        self._ensure_open()
        resolved_response_id = _require_nonempty_text(response_id, "response_id")
        try:
            response = await self._openai_client.responses.cancel(resolved_response_id)
        except self._errors.status_error as exc:
            raise _operation_error_for_status(FoundryResponsesOperation.CANCEL, exc.status_code) from None
        except (self._errors.connection_error, self._errors.timeout_error):
            raise _transient_operation_error(FoundryResponsesOperation.CANCEL) from None
        return _project_response(response)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await _close_resource(self._openai_client)
        finally:
            try:
                await _close_resource(self._project_client)
            finally:
                await _close_resource(self._credential)

    def _ensure_open(self) -> None:
        if self._closed:
            raise FoundryResponsesClosedError("Foundry Responses transport is closed.")


_STATUS_BY_PROVIDER_VALUE: Mapping[str, FoundryResponseStatus] = {
    "queued": FoundryResponseStatus.QUEUED,
    "in_progress": FoundryResponseStatus.IN_PROGRESS,
    "completed": FoundryResponseStatus.COMPLETED,
    "failed": FoundryResponseStatus.FAILED,
    "cancelled": FoundryResponseStatus.CANCELLED,
    "canceled": FoundryResponseStatus.CANCELLED,
    "incomplete": FoundryResponseStatus.INCOMPLETE,
}
_FAILURE_KIND_BY_PROVIDER_CODE: Mapping[str, FoundryResponseFailureKind] = {
    "rate_limit_exceeded": FoundryResponseFailureKind.RATE_LIMITED,
    "server_error": FoundryResponseFailureKind.TRANSIENT,
    "vector_store_timeout": FoundryResponseFailureKind.TRANSIENT,
    "invalid_prompt": FoundryResponseFailureKind.INVALID_INPUT,
    "image_content_policy_violation": FoundryResponseFailureKind.CONTENT_FILTERED,
}
_INCOMPLETE_REASON_BY_PROVIDER_VALUE: Mapping[str, FoundryResponseIncompleteReason] = {
    "max_output_tokens": FoundryResponseIncompleteReason.MAX_OUTPUT_TOKENS,
    "content_filter": FoundryResponseIncompleteReason.CONTENT_FILTER,
}
_EVENT_KIND_BY_PROVIDER_TYPE: Mapping[str, FoundryResponseEventKind] = {
    "response.created": FoundryResponseEventKind.CREATED,
    "response.in_progress": FoundryResponseEventKind.IN_PROGRESS,
    "response.completed": FoundryResponseEventKind.COMPLETED,
    "response.failed": FoundryResponseEventKind.FAILED,
    "response.cancelled": FoundryResponseEventKind.CANCELLED,
    "response.incomplete": FoundryResponseEventKind.INCOMPLETE,
    "response.output_text.delta": FoundryResponseEventKind.TEXT_DELTA,
    "response.output_text.done": FoundryResponseEventKind.TEXT_DONE,
    "error": FoundryResponseEventKind.ERROR,
}
_RESPONSE_SNAPSHOT_EVENT_KINDS = frozenset(
    {
        FoundryResponseEventKind.CREATED,
        FoundryResponseEventKind.IN_PROGRESS,
        FoundryResponseEventKind.COMPLETED,
        FoundryResponseEventKind.FAILED,
        FoundryResponseEventKind.CANCELLED,
        FoundryResponseEventKind.INCOMPLETE,
    }
)


def _project_response(response: _ProviderResponse) -> FoundryResponse:
    """Normalize a typed SDK Response without retaining provider-specific values."""

    error = None if response.error is None else FoundryResponseFailure.create(response.error.code)
    incomplete_details = (
        None
        if response.incomplete_details is None
        else FoundryResponseIncompleteDetails.create(response.incomplete_details.reason)
    )
    return FoundryResponse.create(
        response_id=response.id,
        status=(
            FoundryResponseStatus.UNKNOWN
            if response.status is None
            else _STATUS_BY_PROVIDER_VALUE.get(response.status, FoundryResponseStatus.UNKNOWN)
        ),
        output_text=response.output_text,
        agent_session_id=_session_id_from_response(response),
        error=error,
        incomplete_details=incomplete_details,
    )


def _project_session(session: _ProviderSession) -> FoundrySession:
    return FoundrySession.create(
        agent_session_id=session.agent_session_id,
        agent_version=session.version_indicator.agent_version,
    )


def _project_event(event: _ProviderEvent) -> FoundryResponseEvent:
    """Normalize one retained SDK event into a stable typed event."""

    kind = _EVENT_KIND_BY_PROVIDER_TYPE.get(event.type, FoundryResponseEventKind.OTHER)
    if kind in _RESPONSE_SNAPSHOT_EVENT_KINDS:
        data: FoundryResponseEventData = _project_response(event.response)
    elif kind is FoundryResponseEventKind.TEXT_DELTA:
        data = FoundryResponseText.create(event.delta)
    elif kind is FoundryResponseEventKind.TEXT_DONE:
        data = FoundryResponseText.create(event.text)
    elif kind is FoundryResponseEventKind.ERROR:
        data = FoundryResponseFailure.create(event.code)
    else:
        data = None
    return FoundryResponseEvent.create(
        provider_sequence=event.sequence_number,
        kind=kind,
        data=data,
    )


def _session_id_from_response(response: _ProviderResponse) -> str | None:
    model_extra = response.model_extra
    if model_extra is None:
        return None
    return _optional_identifier(model_extra.get("agent_session_id"), "agent_session_id")


def _operation_error_for_status(
    operation: FoundryResponsesOperation,
    status_code: int,
) -> FoundryResponsesOperationError:
    if status_code in {401, 403}:
        return FoundryResponsesOperationError(
            operation,
            FoundryResponsesOperationErrorKind.UNAUTHORIZED,
            retryable=False,
        )
    if status_code == 404:
        return FoundryResponsesOperationError(
            operation,
            FoundryResponsesOperationErrorKind.NOT_FOUND,
            retryable=False,
        )
    if status_code == 429:
        return FoundryResponsesOperationError(
            operation,
            FoundryResponsesOperationErrorKind.RATE_LIMITED,
            retryable=True,
        )
    if status_code in {408, 409, 425} or status_code >= 500:
        return _transient_operation_error(operation)
    if 400 <= status_code < 500:
        return FoundryResponsesOperationError(
            operation,
            FoundryResponsesOperationErrorKind.INVALID_REQUEST,
            retryable=False,
        )
    return FoundryResponsesOperationError(
        operation,
        FoundryResponsesOperationErrorKind.UNKNOWN,
        retryable=False,
    )


def _transient_operation_error(
    operation: FoundryResponsesOperation,
) -> FoundryResponsesOperationError:
    return FoundryResponsesOperationError(
        operation,
        FoundryResponsesOperationErrorKind.TRANSIENT,
        retryable=True,
    )


def _require_nonempty_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FoundryResponsesProtocolError(f"{field_name} must be a non-empty string.")
    return value


def _optional_identifier(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_nonempty_text(value, field_name)


def _canonical_trace_headers(
    headers: Mapping[str, str] | None,
) -> tuple[tuple[str, str], ...]:
    if headers is None:
        return ()
    if not isinstance(headers, Mapping):
        raise FoundryResponsesProtocolError("Foundry Responses trace headers are invalid.")
    header_names = set(headers)
    if not header_names:
        return ()
    if header_names - {_TRACEPARENT_HEADER, _TRACESTATE_HEADER}:
        raise FoundryResponsesProtocolError("Foundry Responses trace headers are invalid.")
    traceparent = headers.get(_TRACEPARENT_HEADER)
    if not isinstance(traceparent, str) or not _is_valid_traceparent(traceparent):
        raise FoundryResponsesProtocolError("Foundry Responses trace headers are invalid.")
    canonical_headers: tuple[tuple[str, str], ...] = ((_TRACEPARENT_HEADER, traceparent),)
    tracestate = headers.get(_TRACESTATE_HEADER)
    if tracestate is None:
        return canonical_headers
    if not isinstance(tracestate, str) or not _is_valid_tracestate(tracestate):
        raise FoundryResponsesProtocolError("Foundry Responses trace headers are invalid.")
    return (*canonical_headers, (_TRACESTATE_HEADER, tracestate))


def _is_valid_traceparent(value: str) -> bool:
    if len(value) != _TRACEPARENT_MAX_LENGTH:
        return False
    match = _TRACEPARENT_PATTERN.fullmatch(value)
    return match is not None and match.group(1) != "0" * 32 and match.group(2) != "0" * 16


def _is_valid_tracestate(value: str) -> bool:
    if not value or len(value) > _TRACESTATE_MAX_LENGTH:
        return False
    members = value.split(",")
    if len(members) > _TRACESTATE_MAX_MEMBERS:
        return False
    seen_keys: set[str] = set()
    for member in members:
        if len(member) > 256 or member != member.strip() or member.count("=") != 1:
            return False
        key, member_value = member.split("=")
        if (
            key in seen_keys
            or _TRACESTATE_KEY_PATTERN.fullmatch(key) is None
            or _TRACESTATE_VALUE_PATTERN.fullmatch(member_value) is None
            or member_value != member_value.strip()
        ):
            return False
        seen_keys.add(key)
    return True


def _optional_provider_sequence(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FoundryResponsesProtocolError("starting_after must be a non-negative integer.")
    return value


async def _close_resource(resource: _AsyncCloseable) -> None:
    await resource.close()
