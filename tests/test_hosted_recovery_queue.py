"""Storage Queue recovery contracts without a queue client or Functions host."""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from contextlib import suppress
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from azure.functions import HttpRequest, QueueMessage
from pydantic import ValidationError
from starlette.requests import Request

from azure_functions_agents import HostedSkillRecoveryError
from azure_functions_agents import _hosted_recovery_input as recovery_input
from azure_functions_agents._hosted_recovery_http import RecoveryRequest
from azure_functions_agents._hosted_recovery_input import (
    HttpRetryArguments,
    QueueRetryArguments,
    prepare_recovery_input,
)
from azure_functions_agents._hosted_recovery_queue import (
    MAX_QUEUE_RECOVERY_BYTES,
    RecoveryQueueMessage,
)
from azure_functions_agents._hosted_skill_recovery import recover_handler
from azure_functions_agents.hosted_skill_binding import HostedSkill
from azure_functions_agents.runner import AgentResult

_LIMIT = 32 * 1024
_PRIVATE_ERROR = "PRIVATE-ORIGINAL-ERROR"
_PRIVATE_RETRY = "PRIVATE-RETRY-ERROR"
_PRIVATE_PROVIDER = "PRIVATE-PROVIDER-ERROR"
_FALLBACK = "Recovery could not complete. Review the handler inputs and project source, then retry."
_SOURCE_TOOLS = {"list_files", "read_file", "search_source"}
_AUDIT_LOG = "smart_retry audit-only remediation: %s"


class _DeliveredMessage(QueueMessage):
    """Public delivery properties intentionally differ from constructor state."""

    def __init__(self, body=b'{"original":true}'):
        super().__init__(id="constructor-id", pop_receipt="constructor-receipt", body=body)
        self.deliveries = 7
        self.inserted = datetime(2026, 10, 21, 10, tzinfo=UTC)
        self.expires = self.inserted + timedelta(days=7)
        self.visible = self.inserted + timedelta(minutes=5)

    @property
    def id(self):
        return "PRIVATE-PUBLIC-MESSAGE-ID"

    @property
    def pop_receipt(self):
        return "PRIVATE-PUBLIC-POP-RECEIPT"

    @property
    def dequeue_count(self):
        return self.deliveries

    @property
    def insertion_time(self):
        return self.inserted

    @property
    def expiration_time(self):
        return self.expires

    @property
    def time_next_visible(self):
        return self.visible


def _metadata(message):
    return (
        message.id, message.pop_receipt, message.dequeue_count, message.insertion_time,
        message.expiration_time, message.time_next_visible,
    )


async def _handler(_candidate):
    return None


async def _recover(
    monkeypatch, root, model, handler=_handler, *, candidate=None, error=None,
    source_handler=None, timeout=1, max_retries=1, audit_only=False,
):
    assertions = []
    captured_tools = {}
    calls = 0
    expected_audit = audit_only

    async def run(_self, prompt, *, tools, timeout, audit_only=False):
        nonlocal calls
        calls += 1
        available = {tool.name: tool for tool in tools}
        captured_tools.update(available)
        try:
            assert audit_only is expected_audit
            return await model(prompt, available, timeout)
        except AssertionError as assertion:
            assertions.append(assertion)
            raise

    async def checked_handler(message):
        try:
            return await handler(message)
        except AssertionError as assertion:
            assertions.append(assertion)
            raise

    monkeypatch.setattr(HostedSkill, "_run_recovery", run)
    try:
        return await recover_handler(
            HostedSkill(Mock(), Mock()),
            candidate if candidate is not None else RecoveryQueueMessage.create(_DeliveredMessage()),
            error if error is not None else ValueError(_PRIVATE_ERROR),
            checked_handler, source_handler or handler, root, timeout, max_retries,
            audit_only=audit_only,
        )
    finally:
        # Recovery catches Exception, including assertions inside a fake model or handler.
        if assertions:
            raise assertions[0]
        assert calls == 1
        assert set(captured_tools) == (
            _SOURCE_TOOLS if audit_only else _SOURCE_TOOLS | {"retry_function"}
        )
        arguments_by_name = {
            "list_files": {}, "read_file": {"path": "function_app.py"},
            "search_source": {"query": "handler"},
        }
        if not audit_only:
            retry_properties = captured_tools["retry_function"].parameters()["properties"]
            arguments_by_name["retry_function"] = {"body": "fixed"} if "body" in retry_properties else {}
        for name, arguments in arguments_by_name.items():
            with pytest.raises(RuntimeError, match="no longer active"):
                await captured_tools[name].invoke(arguments=arguments)


def _assert_safe_failure(error, expected):
    assert error.remediation == expected
    assert len(error.remediation) <= 2000
    assert str(error) == f"FUNCTION_RECOVERY_FAILED: {expected}"
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    formatted = "".join(traceback.format_exception(error))
    assert "HostedSkillRecoveryError" in formatted
    assert "FUNCTION_RECOVERY_FAILED" in formatted
    for secret in (_PRIVATE_ERROR, _PRIVATE_RETRY, _PRIVATE_PROVIDER,
                   "PRIVATE-PUBLIC-MESSAGE-ID", "PRIVATE-PUBLIC-POP-RECEIPT"):
        assert secret not in formatted
    return formatted


def test_queue_adapter_retains_original_and_is_frozen():
    original = _DeliveredMessage()
    recovery = RecoveryQueueMessage.create(original)
    assert recovery.request is original
    assert recovery.retry_arguments is QueueRetryArguments
    assert MAX_QUEUE_RECOVERY_BYTES == _LIMIT
    assert "32 KiB" in recovery.retry_description
    assert "Delivery metadata cannot be changed" in recovery.retry_description
    with pytest.raises(FrozenInstanceError):
        recovery.request = QueueMessage(body="other")


@pytest.mark.parametrize("text", [
    pytest.param("", id="empty"),
    pytest.param("not JSON {", id="plain-text"),
    pytest.param("\ufeffline one\n\x00café 🙂", id="utf8-with-bom-and-nul"),
    pytest.param("\ufffd", id="literal-replacement-character"),
    pytest.param("a" * _LIMIT, id="ascii-exact-limit"),
    pytest.param("🙂" * (_LIMIT // 4), id="four-byte-exact-limit"),
    pytest.param("€" * (_LIMIT // 3) + "ab", id="mixed-exact-limit"),
])
def test_original_and_replacement_accept_exact_utf8_text(text):
    encoded = text.encode("utf-8")
    assert len(encoded) <= _LIMIT
    original = QueueMessage(body=encoded)
    recovery = RecoveryQueueMessage.create(original)
    assert recovery.diagnostic() == {"body": text}
    corrected = recovery.corrected_arguments(QueueRetryArguments(body=text))
    assert isinstance(corrected, QueueMessage)
    assert corrected is not original
    assert corrected.get_body() == encoded
    assert original.get_body() == encoded
    assert _metadata(corrected) == _metadata(original) == (None,) * 6


@pytest.mark.parametrize("body, message", [
    pytest.param(b"\xff", "UTF-8", id="invalid-byte"),
    pytest.param(b"\xc0\xaf", "UTF-8", id="overlong-encoding"),
    pytest.param(b"\xe2\x82", "UTF-8", id="truncated-sequence"),
    pytest.param(b"\xed\xa0\x80", "UTF-8", id="encoded-unpaired-surrogate"),
    pytest.param(b"a" * (_LIMIT + 1), "32 KiB", id="ascii-oversize"),
    pytest.param(("🙂" * (_LIMIT // 4) + "a").encode(), "32 KiB", id="multibyte-oversize"),
    pytest.param(b"\xff" * (_LIMIT + 1), "32 KiB", id="oversize-invalid-utf8"),
])
def test_original_rejects_oversize_or_non_strict_utf8_without_mutation(body, message):
    original = _DeliveredMessage(body)
    before = _metadata(original)
    with pytest.raises(ValueError, match=message) as caught:
        RecoveryQueueMessage.create(original)
    assert original.get_body() == body
    assert _metadata(original) == before
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("text, error_type", [
    pytest.param("a" * (_LIMIT + 1), ValueError, id="ascii-oversize"),
    pytest.param("🙂" * (_LIMIT // 4) + "a", ValueError, id="multibyte-oversize"),
    pytest.param("\ud800", UnicodeEncodeError, id="unpaired-high-surrogate"),
    pytest.param("\udfff", UnicodeEncodeError, id="unpaired-low-surrogate"),
])
def test_replacement_rejects_oversize_and_unpaired_surrogates(text, error_type):
    original = _DeliveredMessage()
    recovery = RecoveryQueueMessage.create(original)
    before = _metadata(original)
    with pytest.raises(error_type):
        recovery.corrected_arguments(QueueRetryArguments(body=text))
    assert original.get_body() == b'{"original":true}'
    assert recovery.diagnostic() == {"body": '{"original":true}'}
    assert _metadata(original) == before


def test_corrected_message_uses_native_sdk_body_and_public_delivery_metadata():
    original = _DeliveredMessage()
    recovery = RecoveryQueueMessage.create(original)
    before = _metadata(original)
    corrected = recovery.corrected_arguments(QueueRetryArguments(body='{"value":"café 🙂"}'))
    assert isinstance(corrected, QueueMessage)
    assert corrected is not original
    assert corrected.get_body() == '{"value":"café 🙂"}'.encode()
    assert corrected.get_json() == {"value": "café 🙂"}
    assert _metadata(corrected) == before
    assert corrected.insertion_time is original.insertion_time
    assert corrected.expiration_time is original.expiration_time
    assert corrected.time_next_visible is original.time_next_visible
    assert original.get_json() == {"original": True}
    assert _metadata(original) == before
    plain = recovery.corrected_arguments(QueueRetryArguments(body="not JSON"))
    with pytest.raises(ValueError):
        plain.get_json()
    assert plain.get_body() == b"not JSON"
    assert corrected.get_json() == {"value": "café 🙂"}


def test_corrected_delivery_properties_delegate_to_original():
    original = _DeliveredMessage()
    corrected = RecoveryQueueMessage.create(original).corrected_arguments(
        QueueRetryArguments(body="replacement"),
    )
    original.deliveries += 1
    original.inserted += timedelta(seconds=1)
    original.expires += timedelta(seconds=2)
    original.visible += timedelta(seconds=3)
    assert _metadata(corrected) == _metadata(original)
    assert corrected.dequeue_count == 8
    assert corrected.get_body() == b"replacement"
    assert original.get_body() == b'{"original":true}'


def test_diagnostic_contains_only_text_and_returns_independent_mappings():
    original = _DeliveredMessage(b"reviewed synthetic body")
    recovery = RecoveryQueueMessage.create(original)
    diagnostic = recovery.diagnostic()
    assert diagnostic == {"body": "reviewed synthetic body"}
    diagnostic["body"] = "mutated"
    diagnostic["pop_receipt"] = "injected"
    assert recovery.diagnostic() == {"body": "reviewed synthetic body"}
    assert original.get_body() == b"reviewed synthetic body"
    assert "PRIVATE" not in json.dumps(recovery.diagnostic())
    assert "reviewed synthetic body" not in repr(recovery)
    assert "PRIVATE" not in repr(recovery)


@pytest.mark.parametrize("arguments", [
    pytest.param({}, id="missing-body"),
    pytest.param({"body": None}, id="null-body"),
    pytest.param({"body": b"text"}, id="bytes-body"),
    pytest.param({"body": 1}, id="integer-body"),
    pytest.param({"body": True}, id="boolean-body"),
    pytest.param({"body": []}, id="array-body"),
    pytest.param({"body": {}}, id="object-body"),
    pytest.param({"body": "new", "route_args": {}}, id="route-args"),
    pytest.param({"body": "new", "route_params": {}}, id="http-routes"),
    pytest.param({"body": "new", "query_params": []}, id="http-query"),
    pytest.param({"body": "new", "id": "changed"}, id="message-id"),
    pytest.param({"body": "new", "pop_receipt": "changed"}, id="pop-receipt"),
    pytest.param({"body": "new", "dequeue_count": 0}, id="delivery-count"),
])
def test_queue_retry_arguments_require_strict_body_and_forbid_other_fields(arguments):
    with pytest.raises(ValidationError):
        QueueRetryArguments.model_validate(arguments)


@pytest.mark.parametrize("content, expected", [
    pytest.param("  Review the synthetic input.\n", "Review the synthetic input.", id="trimmed"),
    pytest.param(" \n\t", "Review the handler inputs and retry.", id="blank-fallback"),
    pytest.param("x" * 2000, "x" * 2000, id="exact-limit"),
    pytest.param("🙂" * 2001, "🙂" * 2000, id="bounded-unicode"),
])
def test_adapter_failure_raises_public_bounded_error_and_suppresses_context(content, expected):
    recovery = RecoveryQueueMessage.create(_DeliveredMessage(b"PRIVATE-QUEUE-BODY"))
    try:
        raise ValueError(_PRIVATE_ERROR)
    except ValueError:
        with pytest.raises(HostedSkillRecoveryError) as caught:
            recovery.failure(content)
    formatted = _assert_safe_failure(caught.value, expected)
    assert "PRIVATE-QUEUE-BODY" not in formatted
    assert expected in formatted


@pytest.mark.asyncio
async def test_prepare_recovery_input_selects_queue_adapter_and_original():
    original = _DeliveredMessage()
    recovery = await prepare_recovery_input(original)
    assert isinstance(recovery, RecoveryQueueMessage)
    assert recovery.request is original
    assert recovery.retry_arguments is QueueRetryArguments
    assert recovery.diagnostic() == {"body": '{"original":true}'}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["azure", "starlette"])
async def test_prepare_recovery_input_preserves_http_dispatch(kind):
    if kind == "azure":
        original = HttpRequest(method="GET", url="https://example.test", body=b"")
    else:
        original = Request({
            "type": "http", "method": "HEAD", "scheme": "https", "path": "/",
            "query_string": b"", "headers": [(b"content-length", b"0")],
        })
    recovery = await prepare_recovery_input(original)
    assert isinstance(recovery, RecoveryRequest)
    assert recovery.request is original
    assert recovery.retry_arguments is HttpRetryArguments


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    pytest.param(b"\xff", id="invalid-utf8"),
    pytest.param(b"a" * (_LIMIT + 1), id="oversize"),
])
async def test_prepare_recovery_input_rejects_unsafe_queue_before_recovery(body):
    with pytest.raises(ValueError):
        await prepare_recovery_input(QueueMessage(body=body))


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate", [None, {}, "body", object()])
async def test_prepare_recovery_input_rejects_unsupported_types(candidate):
    with pytest.raises(TypeError, match="Unsupported recovery request"):
        await prepare_recovery_input(candidate)


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, {"actual": "handler result"}])
async def test_retry_returns_actual_handler_result_including_none(monkeypatch, tmp_path, response):
    original = _DeliveredMessage()
    recovery = RecoveryQueueMessage.create(original)
    calls = []

    async def handler(candidate):
        calls.append(candidate)
        assert isinstance(candidate, QueueMessage)
        assert candidate is not original
        assert candidate.get_json() == {"fixed": True}
        assert _metadata(candidate) == _metadata(original)
        return response

    async def model(_prompt, tools, timeout):
        assert timeout == 1
        assert await tools["retry_function"].invoke(arguments={"body": '{"fixed":true}'}) == {
            "status": "succeeded",
        }
        return AgentResult(session_id="queue-recovery", content="Not the handler result")

    assert await _recover(monkeypatch, tmp_path, model, handler, candidate=recovery) is response
    assert len(calls) == 1
    assert original.get_json() == {"original": True}
    assert recovery.diagnostic() == {"body": '{"original":true}'}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["queue", "http"])
async def test_retry_tool_uses_input_specific_schema(monkeypatch, tmp_path, kind):
    if kind == "queue":
        candidate = RecoveryQueueMessage.create(_DeliveredMessage())
        invalid = [{}, {"route_args": {}}, {"body": "fixed", "route_params": {}}]
        valid = {"body": "fixed"}
    else:
        candidate = RecoveryRequest.create(HttpRequest(
            method="GET", url="https://example.test", body=b"", route_params={"count": "bad"},
        ))
        invalid = [{"body": "fixed"}, {"route_args": {}}]
        valid = {"route_params": {"count": 1}}
    calls = []

    async def handler(message):
        calls.append(message)
        return None

    async def model(_prompt, tools, _timeout):
        retry = tools["retry_function"]
        schema = retry.parameters()
        assert schema == candidate.retry_arguments.model_json_schema()
        assert schema["additionalProperties"] is False
        if kind == "queue":
            assert schema["required"] == ["body"]
            assert set(schema["properties"]) == {"body"}
            assert schema["properties"]["body"]["type"] == "string"
        else:
            assert "body" not in schema["properties"]
        for arguments in invalid:
            with pytest.raises(TypeError):
                await retry.invoke(arguments=arguments)
        assert await retry.invoke(arguments=valid) == {"status": "succeeded"}
        return AgentResult(session_id="queue-recovery", content="done")

    assert await _recover(monkeypatch, tmp_path, model, handler, candidate=candidate) is None
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments, error_type", [
    pytest.param({}, TypeError, id="missing-body"),
    pytest.param({"body": None}, TypeError, id="null-body"),
    pytest.param({"body": b"text"}, TypeError, id="bytes-body"),
    pytest.param({"body": 1}, TypeError, id="integer-body"),
    pytest.param({"body": True}, TypeError, id="boolean-body"),
    pytest.param({"body": []}, TypeError, id="array-body"),
    pytest.param({"body": {}}, TypeError, id="object-body"),
    pytest.param({"body": "fixed", "route_args": {}}, TypeError, id="route-args"),
    pytest.param({"body": "fixed", "query_params": []}, TypeError, id="query-params"),
    pytest.param({"body": "fixed", "pop_receipt": "new"}, TypeError, id="pop-receipt"),
    pytest.param({"body": "x" * (_LIMIT + 1)}, "ValueError", id="ascii-oversize"),
    pytest.param({"body": "🙂" * (_LIMIT // 4) + "a"}, "ValueError", id="multibyte-oversize"),
    pytest.param({"body": "\ud800"}, "UnicodeEncodeError", id="unpaired-surrogate"),
])
async def test_invalid_retry_arguments_do_not_consume_budget(
    monkeypatch, tmp_path, arguments, error_type,
):
    calls = []

    async def handler(message):
        calls.append(message)
        assert message.get_body() == b""
        return None

    async def model(_prompt, tools, _timeout):
        retry = tools["retry_function"]
        if error_type is TypeError:
            with pytest.raises(TypeError):
                await retry.invoke(arguments=arguments)
        else:
            assert await retry.invoke(arguments=arguments) == {
                "status": "failed", "error_type": error_type,
            }
        assert calls == []
        assert await retry.invoke(arguments={"body": ""}) == {"status": "succeeded"}
        return AgentResult(session_id="queue-recovery", content="done")

    assert await _recover(monkeypatch, tmp_path, model, handler) is None
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("max_retries", [1, 3])
@pytest.mark.parametrize("succeed", [False, True])
async def test_sequential_retries_refine_body_and_enforce_budget(
    monkeypatch, tmp_path, max_retries, succeed,
):
    original = _DeliveredMessage()
    calls = []

    async def handler(message):
        calls.append(message)
        assert message.get_json() == {"attempt": len(calls)}
        assert _metadata(message) == _metadata(original)
        if not succeed or len(calls) < max_retries:
            raise LookupError(_PRIVATE_RETRY)
        return None

    async def model(prompt, tools, _timeout):
        assert json.loads(prompt.split("\n", 1)[1])["max_retries"] == max_retries
        retry = tools["retry_function"]
        assert str(max_retries) in retry.description
        for attempt in range(1, max_retries + 1):
            assert await retry.invoke(arguments={"body": "x" * (_LIMIT + 1)}) == {
                "status": "failed", "error_type": "ValueError",
            }
            assert await retry.invoke(arguments={"body": json.dumps({"attempt": attempt})}) == (
                {"status": "succeeded"} if succeed and attempt == max_retries else
                {"status": "failed", "error_type": "LookupError"}
            )
        assert await retry.invoke(arguments={"body": "never executed"}) == {
            "status": "failed", "error_type": "RuntimeError",
        }
        return AgentResult(session_id="queue-recovery", content="Review the work item.")

    if succeed:
        assert await _recover(
            monkeypatch, tmp_path, model, handler, candidate=RecoveryQueueMessage.create(original),
            max_retries=max_retries,
        ) is None
    else:
        with pytest.raises(HostedSkillRecoveryError) as caught:
            await _recover(
                monkeypatch, tmp_path, model, handler, candidate=RecoveryQueueMessage.create(original),
                max_retries=max_retries,
            )
        _assert_safe_failure(caught.value, "Review the work item.")
    assert len(calls) == max_retries
    assert len({id(message) for message in calls}) == max_retries
    assert original.get_json() == {"original": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["normal", "provider-error", "timeout"])
@pytest.mark.parametrize("after_success", [False, True])
async def test_provider_exit_retains_none_only_after_success(
    monkeypatch, tmp_path, outcome, after_success,
):
    calls = []
    finished = asyncio.Event()

    async def handler(message):
        calls.append(message)
        return None

    async def model(_prompt, tools, _timeout):
        try:
            if after_success:
                assert await tools["retry_function"].invoke(arguments={"body": "fixed"}) == {
                    "status": "succeeded",
                }
                assert await tools["retry_function"].invoke(arguments={"body": "not executed"}) == {
                    "status": "failed", "error_type": "RuntimeError",
                }
            if outcome == "provider-error":
                raise RuntimeError(_PRIVATE_PROVIDER)
            if outcome == "timeout":
                await asyncio.Event().wait()
            return AgentResult(session_id="queue-recovery", content="Review input.")
        finally:
            finished.set()

    if after_success:
        assert await _recover(
            monkeypatch, tmp_path, model, handler, timeout=0.05, max_retries=3,
        ) is None
    else:
        with pytest.raises(HostedSkillRecoveryError) as caught:
            await _recover(monkeypatch, tmp_path, model, handler, timeout=0.05, max_retries=3)
        _assert_safe_failure(caught.value, "Review input." if outcome == "normal" else _FALLBACK)
    assert len(calls) == int(after_success)
    assert finished.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("content, expected", [
    pytest.param("  Fix the reviewed work item.\n", "Fix the reviewed work item.", id="trimmed"),
    pytest.param(" \t\n", _FALLBACK, id="blank-fallback"),
    pytest.param("🙂" * 2100, "🙂" * 2000, id="bounded-unicode"),
])
async def test_no_retry_raises_bounded_advice_without_raw_error_chain(
    monkeypatch, tmp_path, content, expected,
):
    async def model(_prompt, _tools, _timeout):
        return AgentResult(session_id="queue-recovery", content=content)

    try:
        raise ValueError(_PRIVATE_ERROR)
    except ValueError as original_error:
        with pytest.raises(HostedSkillRecoveryError) as caught:
            await _recover(
                monkeypatch, tmp_path, model, error=original_error,
                candidate=RecoveryQueueMessage.create(_DeliveredMessage(b"PRIVATE-QUEUE-BODY")),
            )
    formatted = _assert_safe_failure(caught.value, expected)
    assert "PRIVATE-QUEUE-BODY" not in formatted


@pytest.mark.asyncio
@pytest.mark.parametrize("audit_only", [False, True])
@pytest.mark.parametrize("body", [
    "reviewed synthetic body",
    '{"operation":"sum","values":[1,2,3]}}',
    '{{literal}} ${NOT_AN_ENVIRONMENT_TEMPLATE} \\"quoted\\"',
    " leading and trailing whitespace \r\n",
])
async def test_queue_prompt_excludes_delivery_metadata_and_uses_real_source_tools(
    monkeypatch, tmp_path, body, audit_only,
):
    path = tmp_path / "function_app.py"
    path.write_text(
        f"def handler(msg):\n    raise ValueError({_PRIVATE_ERROR!r})\n",
        encoding="utf-8", newline="\n",
    )
    namespace = {}
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)
    source_handler = namespace["handler"]
    try:
        source_handler(None)
    except ValueError as error:
        original_error = error
    original = _DeliveredMessage(body.encode("utf-8"))

    async def model(prompt, tools, _timeout):
        instructions, diagnostic_json = prompt.split("\n", 1)
        assert instructions == (
            recovery_input.AUDIT_INSTRUCTIONS if audit_only else recovery_input.RECOVERY_INSTRUCTIONS
        )
        assert str(tmp_path) not in prompt
        assert _PRIVATE_ERROR not in prompt
        assert original.id not in prompt
        assert original.pop_receipt not in prompt
        assert original.insertion_time.isoformat() not in prompt
        diagnostic = json.loads(diagnostic_json)
        assert diagnostic["max_retries"] == (0 if audit_only else 3)
        if audit_only:
            assert diagnostic["audit_only"] is True
            assert set(tools) == _SOURCE_TOOLS
        assert diagnostic["request"] == {"body": body}
        assert diagnostic["error_type"] == "ValueError"
        assert diagnostic["handler"] == "handler"
        assert diagnostic["source_hint"] == "function_app.py"
        assert diagnostic["traceback"] == [{"path": "function_app.py", "function": "handler", "line": 2}]
        assert await tools["list_files"].invoke(arguments={}) == ["function_app.py"]
        assert await tools["read_file"].invoke(arguments={"path": "function_app.py"}) == path.read_text(encoding="utf-8")
        assert await tools["search_source"].invoke(arguments={"query": "def handler"}) == [
            "function_app.py:1:def handler(msg):",
        ]
        assert await tools["read_file"].invoke(arguments={"path": "../private.py"}) == {
            "status": "failed", "error_type": "ValueError",
        }
        return AgentResult(session_id="queue-recovery", content="Review the work item.")

    with pytest.raises(HostedSkillRecoveryError) as caught:
        await _recover(
            monkeypatch, tmp_path, model, candidate=RecoveryQueueMessage.create(original),
            error=original_error, source_handler=source_handler,
            audit_only=audit_only, max_retries=3,
        )
    _assert_safe_failure(caught.value, "Review the work item.")
    assert original.get_body() == body.encode("utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("max_retries", [1, 3])
async def test_audit_only_queue_failure_never_replays_corrects_or_acknowledges(
    monkeypatch, tmp_path, caplog, max_retries,
):
    original = _DeliveredMessage(b'{"operation":"sum","values":[1,2,3]}}')
    candidate = RecoveryQueueMessage.create(original)
    before = original.get_body(), _metadata(original), candidate.diagnostic()
    calls = []
    retry_handler = AsyncMock(return_value=None)
    corrected = Mock(side_effect=AssertionError("Audit must not prepare replacement messages"))
    monkeypatch.setattr(RecoveryQueueMessage, "corrected_arguments", corrected)

    async def original_handler(message):
        calls.append(message)
        raise ValueError(_PRIVATE_ERROR)

    with pytest.raises(ValueError) as original_failure:
        await original_handler(original)

    async def model(prompt, tools, _timeout):
        diagnostic = json.loads(prompt.split("\n", 1)[1])
        assert diagnostic["audit_only"] is True
        assert diagnostic["max_retries"] == 0
        assert diagnostic["request"] == {"body": original.get_body().decode("utf-8")}
        assert set(tools) == _SOURCE_TOOLS
        with pytest.raises(KeyError, match="retry_function"):
            await tools["retry_function"].invoke(arguments={"body": '{"operation":"sum","values":[1,2,3]}'})
        return AgentResult(session_id="audit", content="Please call retry_function with a fixed body.")

    with pytest.raises(HostedSkillRecoveryError) as caught:
        await _recover(
            monkeypatch, tmp_path, model, retry_handler, candidate=candidate,
            error=original_failure.value, source_handler=original_handler,
            max_retries=max_retries, audit_only=True,
        )
    _assert_safe_failure(caught.value, "Please call retry_function with a fixed body.")
    assert calls == [original]
    retry_handler.assert_not_called()
    corrected.assert_not_called()
    assert candidate.request is original
    assert (original.get_body(), _metadata(original), candidate.diagnostic()) == before
    [record] = [record for record in caplog.records if record.msg == _AUDIT_LOG]
    assert record.levelno == logging.WARNING
    assert record.args == (caught.value.remediation,)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome, content, expected", [
    pytest.param("return", "  Review the work item.\n", "Review the work item.", id="trimmed"),
    pytest.param("return", "", _FALLBACK, id="empty"),
    pytest.param("return", " \t\n", _FALLBACK, id="blank"),
    pytest.param("return", "x" * 2000, "x" * 2000, id="exact-limit"),
    pytest.param("return", "🙂" * 2100, "🙂" * 2000, id="bounded-unicode"),
    pytest.param("provider", "", _FALLBACK, id="provider-error"),
    pytest.param("timeout", "", _FALLBACK, id="timeout"),
])
async def test_audit_queue_logs_once_after_analysis_before_preserving_typed_failure(
    monkeypatch, tmp_path, caplog, outcome, content, expected,
):
    events = []
    retry_handler = AsyncMock()
    original_failure = RecoveryQueueMessage.failure

    def failure(candidate, advice):
        assert events == ["analysis-cleaned"]
        [record] = [record for record in caplog.records if record.msg == _AUDIT_LOG]
        assert record.name == "azure.functions.AgentRuntime"
        assert record.levelno == logging.WARNING
        assert record.args == (expected,)
        assert len(record.args[0]) <= 2000
        assert advice == expected
        events.append("failure")
        return original_failure(candidate, advice)

    monkeypatch.setattr(RecoveryQueueMessage, "failure", failure)

    async def model(_prompt, _tools, _timeout):
        try:
            if outcome == "provider":
                raise RuntimeError(_PRIVATE_PROVIDER)
            if outcome == "timeout":
                await asyncio.Event().wait()
            return AgentResult(session_id="audit", content=content)
        finally:
            events.append("analysis-cleaned")

    with pytest.raises(HostedSkillRecoveryError) as caught:
        await _recover(
            monkeypatch, tmp_path, model, retry_handler, audit_only=True, max_retries=3,
            timeout=0.02 if outcome == "timeout" else 1,
        )
    _assert_safe_failure(caught.value, expected)
    assert events == ["analysis-cleaned", "failure"]
    assert len([record for record in caplog.records if record.msg == _AUDIT_LOG]) == 1
    assert _PRIVATE_PROVIDER not in caplog.text
    retry_handler.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("on_cancel", ["propagate", "swallow", "replace"])
async def test_cancelled_queue_audit_propagates_without_typed_failure_or_remediation_log(
    monkeypatch, tmp_path, caplog, on_cancel,
):
    entered, finished = asyncio.Event(), asyncio.Event()
    retry_handler, failure, corrected = AsyncMock(), Mock(), Mock()
    monkeypatch.setattr(RecoveryQueueMessage, "failure", failure)
    monkeypatch.setattr(RecoveryQueueMessage, "corrected_arguments", corrected)

    async def model(_prompt, _tools, _timeout):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if on_cancel == "propagate":
                raise
            if on_cancel == "replace":
                raise RuntimeError(_PRIVATE_PROVIDER) from None
        finally:
            finished.set()
        return AgentResult(session_id="audit", content="Must not log cancelled advice")

    task = asyncio.create_task(_recover(
        monkeypatch, tmp_path, model, retry_handler, audit_only=True, timeout=5,
    ))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        assert task.cancelled()
        assert finished.is_set()
        assert [record for record in caplog.records if record.msg == _AUDIT_LOG] == []
        assert "smart_retry audit-only remediation" not in caplog.text
        assert "Must not log cancelled advice" not in caplog.text
        retry_handler.assert_not_called()
        corrected.assert_not_called()
        failure.assert_not_called()
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_overlapping_retry_is_rejected_without_consuming_remaining_attempt(monkeypatch, tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handler(message):
        calls.append(message.get_body())
        if len(calls) == 1:
            entered.set()
            await release.wait()
            raise ValueError(_PRIVATE_RETRY)
        return None

    async def model(_prompt, tools, _timeout):
        retry = tools["retry_function"]
        first = asyncio.create_task(retry.invoke(arguments={"body": "first"}))
        try:
            await entered.wait()
            assert await retry.invoke(arguments={"body": "overlapping"}) == {
                "status": "failed", "error_type": "RuntimeError",
            }
        finally:
            release.set()
        assert await first == {"status": "failed", "error_type": "ValueError"}
        assert await retry.invoke(arguments={"body": "second"}) == {"status": "succeeded"}
        return AgentResult(session_id="queue-recovery", content="done")

    assert await _recover(monkeypatch, tmp_path, model, handler, max_retries=2) is None
    assert calls == [b"first", b"second"]


@pytest.mark.asyncio
async def test_timeout_during_retry_cancels_handler_and_raises_fallback(monkeypatch, tmp_path):
    entered, finished = asyncio.Event(), asyncio.Event()

    async def handler(_message):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    async def model(_prompt, tools, _timeout):
        await tools["retry_function"].invoke(arguments={"body": "fixed"})
        raise AssertionError("Timed-out retry must not return")

    with pytest.raises(HostedSkillRecoveryError) as caught:
        await _recover(monkeypatch, tmp_path, model, handler, timeout=0.05)
    _assert_safe_failure(caught.value, _FALLBACK)
    assert entered.is_set()
    assert finished.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["agent", "agent-after-success", "retry"])
@pytest.mark.parametrize("on_cancel", ["propagate", "swallow", "replace"])
async def test_external_cancellation_wins_during_agent_or_retry(
    monkeypatch, tmp_path, phase, on_cancel,
):
    entered, finished = asyncio.Event(), asyncio.Event()
    calls = []

    async def blocked():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if on_cancel == "propagate":
                raise
            if on_cancel == "replace":
                raise RuntimeError(_PRIVATE_PROVIDER) from None
        finally:
            finished.set()

    async def handler(message):
        calls.append(message)
        if phase == "retry":
            await blocked()
        return None

    async def model(_prompt, tools, _timeout):
        if phase != "agent":
            await tools["retry_function"].invoke(arguments={"body": "fixed"})
        if phase != "retry":
            await blocked()
        return AgentResult(session_id="queue-recovery", content="Not remediation on cancellation")

    task = asyncio.create_task(_recover(monkeypatch, tmp_path, model, handler, timeout=5))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        assert task.cancelled()
        assert finished.is_set()
        assert len(calls) == int(phase != "agent")
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["return", "error", "timeout", "cancel"])
async def test_background_retry_is_cancelled_and_tools_revoked_on_every_exit(monkeypatch, tmp_path, mode):
    entered, finished = asyncio.Event(), asyncio.Event()
    retries = []

    async def handler(_message):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    async def model(_prompt, tools, _timeout):
        retries.append(asyncio.create_task(tools["retry_function"].invoke(arguments={"body": "fixed"})))
        await entered.wait()
        if mode == "error":
            raise RuntimeError(_PRIVATE_PROVIDER)
        if mode in {"timeout", "cancel"}:
            await asyncio.Event().wait()
        return AgentResult(session_id="queue-recovery", content="Review the work item.")

    task = asyncio.create_task(_recover(
        monkeypatch, tmp_path, model, handler, timeout=5 if mode == "cancel" else 0.05,
    ))
    try:
        if mode == "cancel":
            await asyncio.wait_for(entered.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)
        else:
            with pytest.raises(HostedSkillRecoveryError) as caught:
                await asyncio.wait_for(task, timeout=2)
            _assert_safe_failure(caught.value, "Review the work item." if mode == "return" else _FALLBACK)
        assert entered.is_set()
        assert finished.is_set()
        assert len(retries) == 1
        assert retries[0].cancelled()
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
@pytest.mark.parametrize("max_retries", [1, 3])
async def test_concurrent_queue_invocations_isolate_bodies_results_and_budgets(
    monkeypatch, tmp_path, max_retries,
):
    both_entered = asyncio.Event()
    entered = 0
    attempts = {"one": [], "two": []}

    async def handler(message):
        payload = message.get_json()
        identity = payload["identity"]
        attempts[identity].append(payload["attempt"])
        if len(attempts[identity]) < max_retries:
            raise ValueError(_PRIVATE_RETRY)
        return identity

    async def model(prompt, tools, _timeout):
        nonlocal entered
        identity = json.loads(prompt.split("\n", 1)[1])["request"]["body"]
        entered += 1
        if entered == 2:
            both_entered.set()
        await both_entered.wait()
        for attempt in range(1, max_retries + 1):
            assert await tools["retry_function"].invoke(arguments={
                "body": json.dumps({"identity": identity, "attempt": attempt}),
            }) == (
                {"status": "succeeded"} if attempt == max_retries else
                {"status": "failed", "error_type": "ValueError"}
            )
        return AgentResult(session_id="queue-recovery", content="Not a handler result")

    originals = [_DeliveredMessage(identity.encode()) for identity in attempts]
    results = await asyncio.gather(*[
        _recover(
            monkeypatch, tmp_path, model, handler, candidate=RecoveryQueueMessage.create(original),
            max_retries=max_retries,
        ) for original in originals
    ])
    assert results == ["one", "two"]
    assert attempts == {"one": list(range(1, max_retries + 1)), "two": list(range(1, max_retries + 1))}
    assert [original.get_body() for original in originals] == [b"one", b"two"]


@pytest.mark.asyncio
async def test_concurrent_queue_audit_and_retry_keep_capabilities_bodies_and_outcomes_isolated(
    monkeypatch, tmp_path, caplog,
):
    source_text = "def handler(msg):\n    return msg.get_body()\n"
    (tmp_path / "function_app.py").write_text(source_text, encoding="utf-8", newline="\n")
    audit_entered, retry_finished = asyncio.Event(), asyncio.Event()
    captured, attempts, corrections = {}, [], []
    audit_original = _DeliveredMessage(b"audit-original")
    retry_original = _DeliveredMessage(b"retry-original")
    audit_handler = AsyncMock(return_value=None)
    original_corrected = RecoveryQueueMessage.corrected_arguments

    def corrected(candidate, arguments):
        corrections.append(candidate.request)
        return original_corrected(candidate, arguments)

    monkeypatch.setattr(RecoveryQueueMessage, "corrected_arguments", corrected)

    async def handler(message):
        attempts.append(message.get_json()["attempt"])
        assert _metadata(message) == _metadata(retry_original)
        if len(attempts) < 3:
            raise ValueError(_PRIVATE_RETRY)
        return None

    async def model(prompt, tools, _timeout):
        diagnostic = json.loads(prompt.split("\n", 1)[1])
        audit_only = diagnostic.get("audit_only", False)
        captured[audit_only] = tools
        if audit_only:
            assert diagnostic["max_retries"] == 0
            assert diagnostic["request"] == {"body": "audit-original"}
            assert set(tools) == _SOURCE_TOOLS
            audit_entered.set()
            await asyncio.wait_for(retry_finished.wait(), timeout=1)
            with pytest.raises(RuntimeError, match="no longer active"):
                await captured[False]["read_file"].invoke(arguments={"path": "function_app.py"})
            assert await tools["read_file"].invoke(arguments={"path": "function_app.py"}) == source_text
            return AgentResult(session_id="audit", content="Audit advice only.")
        assert diagnostic["max_retries"] == 3
        assert diagnostic["request"] == {"body": "retry-original"}
        assert set(tools) == _SOURCE_TOOLS | {"retry_function"}
        await asyncio.wait_for(audit_entered.wait(), timeout=1)
        for attempt in range(1, 4):
            assert await tools["retry_function"].invoke(arguments={
                "body": json.dumps({"attempt": attempt}),
            }) == (
                {"status": "succeeded"} if attempt == 3 else
                {"status": "failed", "error_type": "ValueError"}
            )
        assert await tools["retry_function"].invoke(arguments={"body": "not replayed"}) == {
            "status": "failed", "error_type": "RuntimeError",
        }
        return AgentResult(session_id="retry", content="Must not replace the actual None result")

    async def audit():
        with pytest.raises(HostedSkillRecoveryError) as caught:
            await _recover(
                monkeypatch, tmp_path, model, audit_handler,
                candidate=RecoveryQueueMessage.create(audit_original),
                timeout=2, max_retries=3, audit_only=True,
            )
        return caught.value

    async def retry():
        try:
            return await _recover(
                monkeypatch, tmp_path, model, handler,
                candidate=RecoveryQueueMessage.create(retry_original),
                timeout=2, max_retries=3,
            )
        finally:
            retry_finished.set()

    audit_error, retry_result = await asyncio.gather(audit(), retry())
    _assert_safe_failure(audit_error, "Audit advice only.")
    assert retry_result is None
    assert attempts == [1, 2, 3]
    assert corrections == [retry_original] * 3
    audit_handler.assert_not_called()
    assert audit_original.get_body() == b"audit-original"
    assert retry_original.get_body() == b"retry-original"
    assert all(captured[True][name] is not captured[False][name] for name in _SOURCE_TOOLS)
    [record] = [record for record in caplog.records if record.msg == _AUDIT_LOG]
    assert record.args == ("Audit advice only.",)


@pytest.mark.asyncio
@pytest.mark.parametrize("audit_only, origin", [(False, "model"), (False, "handler"), (True, "model")])
async def test_helper_surfaces_assertions_swallowed_by_recovery(
    monkeypatch, tmp_path, origin, audit_only,
):
    async def handler(_message):
        raise AssertionError("captured handler assertion")

    async def model(_prompt, tools, _timeout):
        if origin == "model":
            raise AssertionError("captured model assertion")
        await tools["retry_function"].invoke(arguments={"body": "fixed"})
        return AgentResult(session_id="queue-recovery", content="Would otherwise mask the assertion")

    with pytest.raises(AssertionError, match=f"captured {origin} assertion"):
        await _recover(monkeypatch, tmp_path, model, handler, audit_only=audit_only)