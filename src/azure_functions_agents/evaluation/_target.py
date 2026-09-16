"""MAF-compatible target for an Azure Functions agent chat endpoint."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from agent_framework import AgentResponse, AgentRunInputs, AgentSession, Content, Message
from azure.core.credentials import TokenCredential

from .._session_id import SESSION_ID_PATTERN


class FunctionAgentTargetError(RuntimeError):
    """Base class for failures while invoking a Function agent target."""


class FunctionAgentAuthenticationError(FunctionAgentTargetError):
    """Authentication failed before or during target invocation."""


class FunctionAgentTimeoutError(FunctionAgentTargetError):
    """Target invocation exceeded its configured timeout."""


class FunctionAgentTransportError(FunctionAgentTargetError):
    """The target could not be reached over HTTP."""


class FunctionAgentHTTPError(FunctionAgentTargetError):
    """The target returned an unsuccessful HTTP response."""

    def __init__(self, status: int, response_body: str) -> None:
        self.status = status
        self.response_body = response_body
        super().__init__(f"Function agent target returned HTTP {status}: {response_body}")


class FunctionAgentResponseError(FunctionAgentTargetError):
    """The target returned a response that does not match the chat contract."""


@dataclass(frozen=True)
class AnonymousAuth:
    """Send no target credential."""

    async def _request_headers(self) -> Mapping[str, str]:
        return {}


@dataclass(frozen=True)
class FunctionKeyAuth:
    """Authenticate with an Azure Functions function or host key."""

    key: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("Function key must not be empty")

    async def _request_headers(self) -> Mapping[str, str]:
        return {"x-functions-key": self.key}


@dataclass(frozen=True)
class EntraTokenAuth:
    """Acquire an Entra bearer token through an Azure ``TokenCredential``."""

    credential: TokenCredential = field(repr=False)
    scope: str

    def __post_init__(self) -> None:
        if not self.scope.strip():
            raise ValueError("Entra token scope must not be empty")

    async def _request_headers(self) -> Mapping[str, str]:
        try:
            token = await asyncio.to_thread(self.credential.get_token, self.scope)
        except Exception as exc:
            raise FunctionAgentAuthenticationError(
                "Failed to acquire an Entra token for the Function agent target"
            ) from exc
        return {"Authorization": f"Bearer {token.token}"}


type FunctionAgentAuth = AnonymousAuth | FunctionKeyAuth | EntraTokenAuth


class FunctionAgentTarget:
    """Preview MAF target backed by an existing built-in Function chat endpoint.

    The endpoint must already be enabled through ``builtin_endpoints.chat_api``.
    This client neither registers nor deploys an endpoint.
    """

    def __init__(
        self,
        endpoint_url: str,
        *,
        agent_id: str,
        name: str | None = None,
        description: str | None = None,
        auth: FunctionAgentAuth | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.endpoint_url = _validate_endpoint_url(endpoint_url)
        if not agent_id.strip():
            raise ValueError("Agent ID must not be empty")
        if timeout <= 0:
            raise ValueError("Timeout must be greater than zero")

        self.id = agent_id
        self.name = name
        self.description = description
        self.auth = auth or AnonymousAuth()
        self.timeout = timeout

    def create_session(self, *, session_id: str | None = None) -> AgentSession:
        """Create local MAF session metadata for the Function conversation."""
        if session_id is not None:
            _validate_session_id(session_id)
        return AgentSession(session_id=session_id)

    def get_session(
        self,
        service_session_id: str | Mapping[str, Any],
        *,
        session_id: str | None = None,
    ) -> AgentSession:
        """Map an existing Function session ID into local MAF session metadata."""
        if session_id is None:
            if not isinstance(service_session_id, str):
                raise ValueError(
                    "session_id is required when service_session_id is not a string"
                )
            session_id = service_session_id
        _validate_session_id(session_id)
        return AgentSession(
            session_id=session_id,
            service_session_id=service_session_id,
        )

    async def run(
        self,
        messages: AgentRunInputs | None = None,
        *,
        stream: bool = False,
        session: AgentSession | None = None,
        function_invocation_kwargs: Mapping[str, Any] | None = None,
        client_kwargs: Mapping[str, Any] | None = None,
    ) -> AgentResponse[Any]:
        """Invoke the synchronous Function chat endpoint and return MAF evidence."""
        del function_invocation_kwargs, client_kwargs
        if stream:
            raise ValueError("FunctionAgentTarget does not support streaming runs")

        prompt = _extract_prompt(messages)
        session_id = session.session_id if session is not None else uuid.uuid4().hex
        _validate_session_id(session_id)
        started = time.perf_counter()

        try:
            async with asyncio.timeout(self.timeout):
                headers = {
                    "Accept": "application/json",
                    "x-ms-session-id": session_id,
                    **await self.auth._request_headers(),
                }
                timeout = aiohttp.ClientTimeout(total=self.timeout)
                async with aiohttp.ClientSession() as http_session, http_session.post(
                    self.endpoint_url,
                    json={"prompt": prompt},
                    headers=headers,
                    timeout=timeout,
                    allow_redirects=False,
                ) as response:
                    response_text = await response.text()
                    if response.status in {401, 403}:
                        raise FunctionAgentAuthenticationError(
                            f"Function agent target rejected authentication with HTTP {response.status}"
                        )
                    if response.status < 200 or response.status >= 300:
                        raise FunctionAgentHTTPError(
                            response.status,
                            _bounded_response_body(response_text),
                        )
        except TimeoutError as exc:
            raise FunctionAgentTimeoutError(
                f"Function agent target timed out after {self.timeout:g}s"
            ) from exc
        except aiohttp.ClientError as exc:
            raise FunctionAgentTransportError(
                "Failed to reach the Function agent target"
            ) from exc

        payload = _parse_response(response_text, expected_session_id=session_id)
        elapsed_seconds = time.perf_counter() - started
        return _to_agent_response(
            payload,
            agent_id=self.id,
            elapsed_seconds=elapsed_seconds,
        )


def _validate_endpoint_url(endpoint_url: str) -> str:
    value = endpoint_url.strip()
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("Endpoint URL must be an absolute HTTP or HTTPS URL")
    if parts.username is not None or parts.password is not None:
        raise ValueError("Endpoint URL must not contain user information")
    if parts.fragment:
        raise ValueError("Endpoint URL must not contain a fragment")
    return value


def _validate_session_id(session_id: str) -> None:
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        raise ValueError(f"Session ID must match {SESSION_ID_PATTERN.pattern}")


def _extract_prompt(messages: AgentRunInputs | None) -> str:
    if messages is None:
        raise ValueError("A prompt is required")

    item: str | Content | Message
    if isinstance(messages, (str, Content, Message)):
        item = messages
    elif isinstance(messages, Sequence):
        if len(messages) != 1:
            raise ValueError("FunctionAgentTarget accepts exactly one prompt per run")
        item = messages[0]
    else:
        raise TypeError("Unsupported MAF run input")

    if isinstance(item, str):
        prompt = item
    elif isinstance(item, Message):
        prompt = item.text
    elif item.type == "text":
        prompt = item.text or ""
    else:
        raise ValueError("FunctionAgentTarget accepts text input only")

    if not prompt.strip():
        raise ValueError("Prompt must not be empty")
    return prompt.strip()


def _bounded_response_body(value: str, limit: int = 1024) -> str:
    text = value.strip()
    if not text:
        return "<empty response>"
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _parse_response(response_text: str, *, expected_session_id: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise FunctionAgentResponseError("Function agent target returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise FunctionAgentResponseError("Function agent target response must be a JSON object")

    session_id = payload.get("session_id")
    response = payload.get("response")
    tool_calls = payload.get("tool_calls")
    if not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(session_id):
        raise FunctionAgentResponseError("Response field 'session_id' is missing or invalid")
    if session_id != expected_session_id:
        raise FunctionAgentResponseError("Response session_id does not match the requested session")
    if not isinstance(response, str):
        raise FunctionAgentResponseError("Response field 'response' must be a string")
    if not isinstance(tool_calls, list):
        raise FunctionAgentResponseError("Response field 'tool_calls' must be a list")

    return payload


def _to_agent_response(
    payload: Mapping[str, Any],
    *,
    agent_id: str,
    elapsed_seconds: float,
) -> AgentResponse[Any]:
    messages: list[Message] = []
    raw_tool_calls = payload["tool_calls"]
    for index, value in enumerate(raw_tool_calls):
        if not isinstance(value, Mapping):
            raise FunctionAgentResponseError(f"Tool call {index} must be an object")
        name = value.get("tool_name")
        if not isinstance(name, str) or not name.strip():
            raise FunctionAgentResponseError(f"Tool call {index} has an invalid tool_name")
        arguments = value.get("arguments")
        if arguments is not None and not isinstance(arguments, (str, Mapping)):
            raise FunctionAgentResponseError(f"Tool call {index} has invalid arguments")
        call_id_value = value.get("tool_call_id")
        if call_id_value is not None and not isinstance(call_id_value, str):
            raise FunctionAgentResponseError(f"Tool call {index} has an invalid tool_call_id")
        call_id = call_id_value or f"function-agent-call-{index}"

        messages.append(
            Message(
                role="assistant",
                contents=[
                    Content.from_function_call(
                        call_id=call_id,
                        name=name,
                        arguments=arguments,
                    )
                ],
            )
        )
        if "result" in value:
            messages.append(
                Message(
                    role="tool",
                    contents=[
                        Content.from_function_result(
                            call_id=call_id,
                            result=value["result"],
                        )
                    ],
                )
            )

    response_text = payload["response"]
    session_id = payload["session_id"]
    messages.append(Message(role="assistant", contents=[Content.from_text(response_text)]))
    return AgentResponse(
        messages=messages,
        agent_id=agent_id,
        raw_representation=dict(payload),
        additional_properties={
            "azure_functions_agents": {
                "elapsed_seconds": elapsed_seconds,
                "session_id": session_id,
            }
        },
    )
