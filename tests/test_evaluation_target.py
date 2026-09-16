from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import aiohttp
import pytest
from agent_framework import (
    ExpectedToolCall,
    LocalEvaluator,
    Message,
    SupportsAgentRun,
    evaluate_agent,
    tool_call_args_match,
    tool_calls_present,
)

from azure_functions_agents.evaluation import (
    AnonymousAuth,
    EntraTokenAuth,
    FunctionAgentAuthenticationError,
    FunctionAgentHTTPError,
    FunctionAgentResponseError,
    FunctionAgentTarget,
    FunctionAgentTimeoutError,
    FunctionAgentTransportError,
    FunctionKeyAuth,
)
from azure_functions_agents.evaluation import _target as target_module

type PayloadFactory = Callable[[Mapping[str, str]], Any]


@dataclass
class _ResponsePlan:
    status: int = 200
    payload: Any | PayloadFactory = None
    raw_body: str | None = None
    block: bool = False


class _FakeResponse:
    def __init__(self, plan: _ResponsePlan, headers: Mapping[str, str]) -> None:
        self.status = plan.status
        self._plan = plan
        self._headers = headers

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def text(self) -> str:
        if self._plan.block:
            await asyncio.Event().wait()
        if self._plan.raw_body is not None:
            return self._plan.raw_body
        payload = self._plan.payload
        if callable(payload):
            payload = payload(self._headers)
        if payload is None:
            payload = _valid_payload(self._headers)
        return json.dumps(payload)


class _FakeSession:
    def __init__(
        self,
        plans: list[_ResponsePlan | Exception],
        requests: list[dict[str, Any]],
    ) -> None:
        self._plans = plans
        self._requests = requests

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self._requests.append({"url": url, **kwargs})
        plan = self._plans.pop(0)
        if isinstance(plan, Exception):
            raise plan
        headers = kwargs["headers"]
        assert isinstance(headers, Mapping)
        return _FakeResponse(plan, headers)


def _valid_payload(headers: Mapping[str, str]) -> dict[str, Any]:
    return {
        "session_id": headers["x-ms-session-id"],
        "response": "The total is 42.18.",
        "tool_calls": [],
    }


def _install_http(
    monkeypatch: pytest.MonkeyPatch,
    *plans: _ResponsePlan | Exception,
) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    queued = list(plans)

    def create_session() -> _FakeSession:
        return _FakeSession(queued, requests)

    monkeypatch.setattr(target_module.aiohttp, "ClientSession", create_session)
    return requests


def _run(target: FunctionAgentTarget, prompt: Any = "Read the receipt", **kwargs: Any) -> Any:
    return asyncio.run(target.run(prompt, **kwargs))


def test_target_structurally_satisfies_maf_protocol() -> None:
    target = FunctionAgentTarget("http://localhost:7071/api/agents/receipt/chat", agent_id="receipt")

    assert isinstance(target, SupportsAgentRun)


def test_target_validates_configuration() -> None:
    with pytest.raises(ValueError, match="absolute HTTP"):
        FunctionAgentTarget("localhost:7071/chat", agent_id="receipt")
    with pytest.raises(ValueError, match="user information"):
        FunctionAgentTarget("https://user:secret@example.com/chat", agent_id="receipt")
    with pytest.raises(ValueError, match="fragment"):
        FunctionAgentTarget("https://example.com/chat#result", agent_id="receipt")
    with pytest.raises(ValueError, match="Agent ID"):
        FunctionAgentTarget("https://example.com/chat", agent_id=" ")
    with pytest.raises(ValueError, match="greater than zero"):
        FunctionAgentTarget("https://example.com/chat", agent_id="receipt", timeout=0)


def test_auth_configuration_validates_and_hides_secrets() -> None:
    with pytest.raises(ValueError, match="Function key"):
        FunctionKeyAuth(" ")
    with pytest.raises(ValueError, match="scope"):
        EntraTokenAuth(credential=object(), scope=" ")  # type: ignore[arg-type]

    auth = FunctionKeyAuth("top-secret")
    assert "top-secret" not in repr(auth)


def test_anonymous_request_and_response_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _install_http(
        monkeypatch,
        _ResponsePlan(
            payload=lambda headers: {
                "session_id": headers["x-ms-session-id"],
                "response": "The total is 42.18.",
                "tool_calls": [
                    {
                        "tool_call_id": "call-1",
                        "tool_name": "read_receipt",
                        "arguments": {"currency": "USD", "pages": 1},
                        "result": {"total": 42.18},
                    }
                ],
            }
        ),
    )
    target = FunctionAgentTarget(
        "http://localhost:7071/api/agents/receipt/chat",
        agent_id="receipt",
        name="Receipt reader",
        auth=AnonymousAuth(),
    )

    response = _run(target)

    assert requests[0]["url"].endswith("/api/agents/receipt/chat")
    assert requests[0]["json"] == {"prompt": "Read the receipt"}
    headers = requests[0]["headers"]
    assert "Authorization" not in headers
    assert "x-functions-key" not in headers
    assert response.text == "The total is 42.18."
    contents = [content for message in response.messages for content in message.contents]
    assert [content.type for content in contents] == ["function_call", "function_result", "text"]
    assert contents[0].name == "read_receipt"
    assert contents[0].arguments == {"currency": "USD", "pages": 1}
    assert json.loads(contents[1].result) == {"total": 42.18}
    metadata = response.additional_properties["azure_functions_agents"]
    assert metadata["session_id"] == headers["x-ms-session-id"]
    assert metadata["elapsed_seconds"] >= 0


def test_function_key_header_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _install_http(monkeypatch, _ResponsePlan())
    target = FunctionAgentTarget(
        "https://example.com/chat",
        agent_id="receipt",
        auth=FunctionKeyAuth("function-key"),
    )

    _run(target)

    assert requests[0]["headers"]["x-functions-key"] == "function-key"


def test_entra_token_is_acquired_and_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _install_http(monkeypatch, _ResponsePlan())

    class Credential:
        def __init__(self) -> None:
            self.scopes: list[str] = []

        def get_token(self, *scopes: str, **kwargs: Any) -> Any:
            self.scopes.extend(scopes)
            return type("Token", (), {"token": "entra-token"})()

    credential = Credential()
    target = FunctionAgentTarget(
        "https://example.com/chat",
        agent_id="receipt",
        auth=EntraTokenAuth(
            credential=credential,  # type: ignore[arg-type]
            scope="api://receipt/.default",
        ),
    )

    _run(target)

    assert credential.scopes == ["api://receipt/.default"]
    assert requests[0]["headers"]["Authorization"] == "Bearer entra-token"


def test_entra_token_failure_is_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_http(monkeypatch, _ResponsePlan())

    class Credential:
        def get_token(self, *scopes: str, **kwargs: Any) -> Any:
            raise RuntimeError("credential details")

    target = FunctionAgentTarget(
        "https://example.com/chat",
        agent_id="receipt",
        auth=EntraTokenAuth(
            credential=Credential(),  # type: ignore[arg-type]
            scope="api://receipt/.default",
        ),
    )

    with pytest.raises(FunctionAgentAuthenticationError, match="acquire an Entra token"):
        _run(target)


def test_sessions_are_fresh_by_default_and_reused_when_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = _install_http(
        monkeypatch,
        _ResponsePlan(),
        _ResponsePlan(),
        _ResponsePlan(),
        _ResponsePlan(),
    )
    target = FunctionAgentTarget("https://example.com/chat", agent_id="receipt")

    _run(target)
    _run(target)
    explicit = target.create_session(session_id="ordered-conversation")
    _run(target, session=explicit)
    restored = target.get_session("existing-conversation")
    _run(target, session=restored)

    session_ids = [request["headers"]["x-ms-session-id"] for request in requests]
    assert session_ids[0] != session_ids[1]
    assert session_ids[2] == "ordered-conversation"
    assert session_ids[3] == "existing-conversation"
    assert restored.service_session_id == "existing-conversation"


def test_get_session_requires_local_id_for_mapping_service_id() -> None:
    target = FunctionAgentTarget("https://example.com/chat", agent_id="receipt")

    with pytest.raises(ValueError, match="session_id is required"):
        target.get_session({"conversation": "remote"})

    session = target.get_session(
        {"conversation": "remote"},
        session_id="local-conversation",
    )
    assert session.session_id == "local-conversation"


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        (Message("user", ["Hello"]), "Hello"),
        ([Message("user", ["Hello"])], "Hello"),
    ],
)
def test_maf_message_inputs_are_normalized(
    monkeypatch: pytest.MonkeyPatch,
    prompt: Any,
    expected: str,
) -> None:
    requests = _install_http(monkeypatch, _ResponsePlan())
    target = FunctionAgentTarget("https://example.com/chat", agent_id="receipt")

    _run(target, prompt)

    assert requests[0]["json"] == {"prompt": expected}


def test_unsupported_inputs_and_streaming_fail_before_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = _install_http(monkeypatch)
    target = FunctionAgentTarget("https://example.com/chat", agent_id="receipt")

    with pytest.raises(ValueError, match="prompt is required"):
        _run(target, None)
    with pytest.raises(ValueError, match="exactly one prompt"):
        _run(target, ["one", "two"])
    with pytest.raises(ValueError, match="does not support streaming"):
        _run(target, stream=True)
    assert requests == []


@pytest.mark.parametrize("status", [401, 403])
def test_authentication_http_failures_are_classified(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    _install_http(monkeypatch, _ResponsePlan(status=status, raw_body="denied"))
    target = FunctionAgentTarget("https://example.com/chat", agent_id="receipt")

    with pytest.raises(FunctionAgentAuthenticationError, match=str(status)):
        _run(target)


def test_non_authentication_http_failure_retains_bounded_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_http(monkeypatch, _ResponsePlan(status=500, raw_body="x" * 2000))
    target = FunctionAgentTarget("https://example.com/chat", agent_id="receipt")

    with pytest.raises(FunctionAgentHTTPError) as exc_info:
        _run(target)

    assert exc_info.value.status == 500
    assert len(exc_info.value.response_body) < 1100


@pytest.mark.parametrize(
    ("plan", "message"),
    [
        (_ResponsePlan(raw_body="not-json"), "invalid JSON"),
        (_ResponsePlan(payload=[]), "JSON object"),
        (
            _ResponsePlan(
                payload=lambda headers: {
                    "session_id": headers["x-ms-session-id"],
                    "response": "ok",
                }
            ),
            "tool_calls",
        ),
        (
            _ResponsePlan(
                payload=lambda headers: {
                    **_valid_payload(headers),
                    "session_id": "other-session",
                }
            ),
            "does not match",
        ),
        (
            _ResponsePlan(
                payload=lambda headers: {
                    **_valid_payload(headers),
                    "tool_calls": [{"tool_name": "read_receipt", "arguments": []}],
                }
            ),
            "invalid arguments",
        ),
    ],
)
def test_malformed_responses_are_classified(
    monkeypatch: pytest.MonkeyPatch,
    plan: _ResponsePlan,
    message: str,
) -> None:
    _install_http(monkeypatch, plan)
    target = FunctionAgentTarget("https://example.com/chat", agent_id="receipt")

    with pytest.raises(FunctionAgentResponseError, match=message):
        _run(target)


def test_transport_failure_is_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_http(monkeypatch, aiohttp.ClientConnectionError("offline"))
    target = FunctionAgentTarget("https://example.com/chat", agent_id="receipt")

    with pytest.raises(FunctionAgentTransportError, match="Failed to reach"):
        _run(target)


def test_timeout_is_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_http(monkeypatch, _ResponsePlan(block=True))
    target = FunctionAgentTarget(
        "https://example.com/chat",
        agent_id="receipt",
        timeout=0.01,
    )

    with pytest.raises(FunctionAgentTimeoutError, match="timed out"):
        _run(target)


def test_cancellation_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_http(monkeypatch, _ResponsePlan(block=True))
    target = FunctionAgentTarget("https://example.com/chat", agent_id="receipt")

    async def cancel_run() -> None:
        task = asyncio.create_task(target.run("Read the receipt"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_run())


def test_maf_deterministic_checks_and_repetitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plans = [
        _ResponsePlan(
            payload=lambda headers: {
                **_valid_payload(headers),
                "tool_calls": [
                    {
                        "tool_call_id": "call-1",
                        "tool_name": "read_receipt",
                        "arguments": {"currency": "USD", "pages": 1},
                        "result": {"total": 42.18},
                    }
                ],
            }
        )
        for _ in range(2)
    ]
    requests = _install_http(monkeypatch, *plans)
    target = FunctionAgentTarget("https://example.com/chat", agent_id="receipt")
    evaluator = LocalEvaluator(tool_calls_present, tool_call_args_match)

    results = asyncio.run(
        evaluate_agent(
            agent=target,
            queries=["Read the receipt"],
            expected_tool_calls=[
                [ExpectedToolCall("read_receipt", {"currency": "USD"})]
            ],
            evaluators=evaluator,
            num_repetitions=2,
        )
    )

    assert len(results) == 1
    assert results[0].all_passed
    assert len(results[0].items) == 2
    session_ids = [request["headers"]["x-ms-session-id"] for request in requests]
    assert session_ids[0] != session_ids[1]


@pytest.mark.parametrize(
    ("actual_name", "actual_arguments", "expected_name", "expected_arguments"),
    [
        ("read_receipt", {"pages": "1"}, "read_receipt", {"pages": 1}),
        ("read_receipt", {"currency": "EUR"}, "read_receipt", {"currency": "USD"}),
        ("read_document", {"currency": "USD"}, "read_receipt", {"currency": "USD"}),
        ("read_receipt", {}, "read_receipt", {"currency": "USD"}),
        ("read_receipt", "not-json", "read_receipt", {"currency": "USD"}),
    ],
)
def test_maf_deterministic_checks_reject_false_passes(
    monkeypatch: pytest.MonkeyPatch,
    actual_name: str,
    actual_arguments: dict[str, Any] | str,
    expected_name: str,
    expected_arguments: dict[str, Any],
) -> None:
    _install_http(
        monkeypatch,
        _ResponsePlan(
            payload=lambda headers: {
                **_valid_payload(headers),
                "tool_calls": [
                    {
                        "tool_name": actual_name,
                        "arguments": actual_arguments,
                    }
                ],
            }
        ),
    )
    target = FunctionAgentTarget("https://example.com/chat", agent_id="receipt")

    results = asyncio.run(
        evaluate_agent(
            agent=target,
            queries=["Read the receipt"],
            expected_tool_calls=[[ExpectedToolCall(expected_name, expected_arguments)]],
            evaluators=LocalEvaluator(tool_calls_present, tool_call_args_match),
        )
    )

    assert len(results) == 1
    assert not results[0].all_passed
