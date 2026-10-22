from __future__ import annotations

import asyncio
import inspect
import json
import logging
from contextlib import suppress
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from azure.functions import HttpRequest, HttpResponse

from azure_functions_agents import _hosted_recovery_input as recovery_input
from azure_functions_agents import _hosted_skill_recovery as recovery_module
from azure_functions_agents import hosted_skill_binding as binding_module
from azure_functions_agents._hosted_recovery_http import RecoveryRequest
from azure_functions_agents._hosted_skill_recovery import _validate_retry_options, recover_handler
from azure_functions_agents.hosted_skill_binding import HostedSkill
from azure_functions_agents.runner import AgentResult

_SOURCE_TOOLS = {"list_files", "read_file", "search_source"}
_AUDIT_LOG = "smart_retry audit-only remediation: %s"
_FALLBACK = "Recovery could not complete. Review the handler inputs and project source, then retry."

_INVALID_RETRY_OPTIONS = [
    {"request_arg": ""}, {"request_arg": "not a parameter"}, {"request_arg": 123},
    {"request_arg": "class"},
    {"timeout": 0}, {"timeout": -1}, {"timeout": float("nan")},
    {"timeout": float("inf")}, {"timeout": True}, {"timeout": "60"},
    {"max_retries": 0}, {"max_retries": -1}, {"max_retries": True},
    {"max_retries": 1.0}, {"max_retries": "3"}, {"max_retries": None},
    {"audit_only": 0}, {"audit_only": 1}, {"audit_only": "true"},
    {"audit_only": "false"}, {"audit_only": ""}, {"audit_only": None},
    {"audit_only": []}, {"audit_only": {}},
]


def _request(method="GET"):
    return RecoveryRequest.create(HttpRequest(
        method=method, url="https://example.test/private?code=SECRET-CREDENTIAL", body=b"",
        route_params={"count": "bad"}, params={"view": "summary", "code": "SECRET-CREDENTIAL"},
        headers={"Authorization": "SECRET-CREDENTIAL"},
    ))


async def _handler(_candidate):
    return HttpResponse("private response")


async def _recover(monkeypatch, root, model, handler=_handler, **kwargs):
    assertions = []
    calls = 0
    expected_audit = kwargs.get("audit_only", False)

    async def run(_self, prompt, *, tools, timeout, audit_only=False):
        nonlocal calls
        calls += 1
        try:
            assert audit_only is expected_audit
            return await model(prompt, {tool.name: tool for tool in tools}, timeout)
        except AssertionError as error:
            assertions.append(error)
            raise

    async def checked_handler(candidate):
        try:
            return await handler(candidate)
        except AssertionError as error:
            assertions.append(error)
            raise

    monkeypatch.setattr(HostedSkill, "_run_recovery", run, raising=False)
    try:
        return await recover_handler(
            HostedSkill(Mock(), Mock()), kwargs.pop("candidate", _request()),
            kwargs.pop("error", ValueError("private error message")), checked_handler,
            kwargs.pop("source_handler", handler), root, kwargs.pop("timeout", 1), **kwargs,
        )
    finally:
        if assertions:
            raise assertions[0]
        assert calls == 1


def _failure(response):
    assert isinstance(response, HttpResponse)
    assert response.status_code == 500
    payload = json.loads(response.get_body())
    assert payload["error"] == "FUNCTION_RECOVERY_FAILED"
    assert payload["requires_user_action"] is True
    return payload["remediation"]


def _audit_records(caplog):
    return [record for record in caplog.records if record.msg == _AUDIT_LOG]


async def _assert_source_tools_expired(tools):
    assert set(tools) == _SOURCE_TOOLS
    for name, arguments in (
        ("list_files", {}), ("read_file", {"path": "function_app.py"}),
        ("search_source", {"query": "original"}),
    ):
        with pytest.raises(RuntimeError, match="no longer active"):
            await tools[name].invoke(arguments=arguments)


def test_retry_option_defaults_are_keyword_only():
    parameters = inspect.signature(_validate_retry_options).parameters
    assert {name: parameter.default for name, parameter in parameters.items()} == {
        "request_arg": None, "timeout": 60, "max_retries": 1, "audit_only": False,
    }
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY for parameter in parameters.values()
    )


@pytest.mark.parametrize("values", [
    {}, {"request_arg": "http_input", "timeout": 0.01}, {"max_retries": 3},
    {"audit_only": False}, {"audit_only": True}, {"audit_only": True, "max_retries": 3},
])
def test_valid_retry_options(values):
    assert _validate_retry_options(**values) is None


@pytest.mark.parametrize("values", _INVALID_RETRY_OPTIONS)
def test_invalid_retry_options(values):
    with pytest.raises(ValueError):
        _validate_retry_options(**values)


@pytest.mark.parametrize("max_retries", [0, -1, True, False, 1.0, "3", None])
def test_audit_mode_still_requires_positive_integer_max_retries(max_retries):
    with pytest.raises(ValueError, match="max_retries"):
        _validate_retry_options(audit_only=True, max_retries=max_retries)


def test_engine_audit_option_is_keyword_only_and_retry_default_is_unchanged():
    parameters = inspect.signature(recover_handler).parameters
    assert parameters["max_retries"].default == 1
    assert parameters["audit_only"].default is False
    assert parameters["audit_only"].kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.asyncio
@pytest.mark.parametrize("audit_only", [False, True])
@pytest.mark.parametrize("values", [
    values for values in _INVALID_RETRY_OPTIONS if "request_arg" not in values
])
async def test_engine_rejects_invalid_options_before_running_recovery(
    monkeypatch, tmp_path, values, audit_only,
):
    run = AsyncMock()
    handler = AsyncMock()
    monkeypatch.setattr(HostedSkill, "_run_recovery", run)
    with pytest.raises(ValueError, match=next(iter(values))):
        await recover_handler(
            HostedSkill(Mock(), Mock()), _request(), ValueError("original failure"),
            handler, handler, tmp_path, **{"timeout": 1, "audit_only": audit_only, **values},
        )
    run.assert_not_called()
    handler.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, HttpResponse("secret body", status_code=503)])
async def test_retry_returns_actual_response_not_model_output(monkeypatch, tmp_path, response):
    calls = []
    original = _request()

    async def handler(candidate):
        calls.append(candidate)
        assert candidate.route_params["count"] == 4
        assert candidate.params == {"view": "full", "code": "SECRET-CREDENTIAL"}
        return response

    async def model(_prompt, tools, timeout):
        assert timeout == 1
        assert set(tools) == {"list_files", "read_file", "search_source", "retry_function"}
        result = await tools["retry_function"].invoke(arguments={
            "route_params": {"count": 4}, "query_params": [["view", "full"]],
        })
        assert result == {"status": "succeeded"}
        return AgentResult(session_id="recovery", content="Not the response")

    assert await _recover(monkeypatch, tmp_path, model, handler, candidate=original) is response
    assert len(calls) == 1
    assert original.diagnostic()["route_params"] == {"count": "bad"}
    assert original.diagnostic()["query_params"] == [("view", "summary")]


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [
    {"route_params": {"count": []}}, {"route_params": {"count": float("inf")}},
    {"route_params": {"count": float("nan")}}, {"query_params": [["view", 1]]},
    {"query_params": [["view"]]}, {"query_params": {"view": "full"}},
    {"headers": {"x": "y"}}, {"route_params": {"new_key": 1}},
    {"query_params": [["code", "new"]]},
    {"query_params": [["view", "a"], ["view", "b"]]},
])
async def test_invalid_arguments_do_not_claim_attempt(monkeypatch, tmp_path, arguments):
    calls = []

    async def handler(candidate):
        calls.append(candidate)
        return "actual"

    async def model(_prompt, tools, _timeout):
        retry = tools["retry_function"]
        if "headers" in arguments:
            with pytest.raises(TypeError, match="unexpected"):
                await retry.invoke(arguments=arguments)
        else:
            assert await retry.invoke(arguments=arguments) == {
                "status": "failed", "error_type": "ValueError",
            }
        assert await retry.invoke(arguments={}) == {"status": "succeeded"}
        return AgentResult(session_id="recovery", content="done")

    assert await _recover(monkeypatch, tmp_path, model, handler) == "actual"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_failed_retry_is_safe_and_cannot_repeat(monkeypatch, tmp_path):
    calls = 0

    async def handler(_candidate):
        nonlocal calls
        calls += 1
        raise LookupError("private retry failure")

    async def model(_prompt, tools, _timeout):
        retry = tools["retry_function"]
        assert await retry.invoke(arguments={}) == {"status": "failed", "error_type": "LookupError"}
        assert await retry.invoke(arguments={}) == {"status": "failed", "error_type": "RuntimeError"}
        return AgentResult(session_id="recovery", content="Review the count input.")

    response = await _recover(monkeypatch, tmp_path, model, handler)
    assert _failure(response) == "Review the count input."
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("max_retries", [1, 3])
@pytest.mark.parametrize("succeed", [False, True])
async def test_configured_retry_budget_refines_corrections_and_stops(
    monkeypatch, tmp_path, max_retries, succeed,
):
    calls = []
    response = HttpResponse("actual")
    original = _request()

    async def handler(candidate):
        calls.append(candidate)
        if not succeed or len(calls) < max_retries:
            raise ValueError("SECRET-CREDENTIAL")
        return response

    async def model(prompt, tools, _timeout):
        assert json.loads(prompt.split("\n", 1)[1])["max_retries"] == max_retries
        assert str(max_retries) in tools["retry_function"].description
        retry = tools["retry_function"]
        for attempt in range(1, max_retries + 1):
            # Invalid patches must not consume this attempt's budget.
            assert await retry.invoke(arguments={"route_params": {"missing": 0}}) == {
                "status": "failed", "error_type": "ValueError",
            }
            outcome = await retry.invoke(arguments={
                "route_params": {"count": attempt},
                "query_params": [["view", "full"]] if attempt == 1 else [],
            })
            assert outcome == (
                {"status": "succeeded"} if succeed and attempt == max_retries else
                {"status": "failed", "error_type": "ValueError"}
            )
        assert await retry.invoke(arguments={}) == {
            "status": "failed", "error_type": "RuntimeError",
        }
        return AgentResult(session_id="recovery", content="Review the count.")

    result = await _recover(
        monkeypatch, tmp_path, model, handler, candidate=original, max_retries=max_retries,
    )
    if succeed:
        assert result is response
    else:
        assert _failure(result) == "Review the count."
    assert len(calls) == max_retries
    assert [candidate.route_params["count"] for candidate in calls] == list(range(1, max_retries + 1))
    assert calls[-1].params["view"] == ("full" if max_retries == 1 else "summary")
    assert original.request.route_params["count"] == "bad"


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, HttpResponse("retained")])
async def test_retry_budget_stops_at_first_success_even_with_remaining_attempts(
    monkeypatch, tmp_path, response,
):
    calls = []

    async def handler(candidate):
        calls.append(candidate)
        return response

    async def model(_prompt, tools, _timeout):
        assert await tools["retry_function"].invoke(arguments={}) == {"status": "succeeded"}
        assert await tools["retry_function"].invoke(arguments={}) == {
            "status": "failed", "error_type": "RuntimeError",
        }
        raise RuntimeError("Provider failed after success")

    assert await _recover(monkeypatch, tmp_path, model, handler, max_retries=3) is response
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_exhausted_multiple_retries_preserve_empty_head_failure(monkeypatch, tmp_path):
    calls = []

    async def handler(candidate):
        calls.append(candidate)
        raise ValueError("Needs human remediation")

    async def model(_prompt, tools, _timeout):
        for _ in range(3):
            assert await tools["retry_function"].invoke(arguments={}) == {
                "status": "failed", "error_type": "ValueError",
            }
        return AgentResult(session_id="recovery", content="Review the inputs.")

    candidate = RecoveryRequest.create(HttpRequest(
        method="HEAD", url="https://example.test/", body=b"",
    ))
    response = await _recover(
        monkeypatch, tmp_path, model, handler, candidate=candidate, max_retries=3,
    )
    assert response.status_code == 500
    assert response.get_body() == b""
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_overlapping_retry_is_rejected_without_consuming_spare_attempts(monkeypatch, tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handler(candidate):
        calls.append(candidate)
        if len(calls) == 1:
            entered.set()
            await release.wait()
            raise ValueError("First correction failed")
        return "actual"

    async def model(_prompt, tools, _timeout):
        retry = tools["retry_function"]
        first = asyncio.create_task(retry.invoke(arguments={}))
        try:
            await entered.wait()
            assert await retry.invoke(arguments={}) == {
                "status": "failed", "error_type": "RuntimeError",
            }
        finally:
            release.set()
        assert await first == {"status": "failed", "error_type": "ValueError"}
        assert await retry.invoke(arguments={}) == {"status": "succeeded"}
        return AgentResult(session_id="recovery", content="done")

    assert await _recover(monkeypatch, tmp_path, model, handler, max_retries=2) == "actual"
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("external_cancel", [False, True])
async def test_later_retry_shares_deadline_and_propagates_cancellation(
    monkeypatch, tmp_path, external_cancel,
):
    entered, finished = asyncio.Event(), asyncio.Event()
    captured = {}
    calls = []

    async def handler(candidate):
        calls.append(candidate)
        if len(calls) == 1:
            raise ValueError("Try another correction")
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    async def model(_prompt, tools, _timeout):
        captured.update(tools)
        assert await tools["retry_function"].invoke(arguments={}) == {
            "status": "failed", "error_type": "ValueError",
        }
        await tools["retry_function"].invoke(arguments={})
        pytest.fail("Later retry should be cancelled")

    task = asyncio.create_task(_recover(
        monkeypatch, tmp_path, model, handler, max_retries=3,
        timeout=1 if external_cancel else 0.2,
    ))
    await entered.wait()
    if external_cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        _failure(await task)
    assert finished.is_set()
    assert len(calls) == 2
    with pytest.raises(RuntimeError, match="no longer active"):
        await captured["retry_function"].invoke(arguments={})


@pytest.mark.asyncio
async def test_concurrent_retry_claims_before_await(monkeypatch, tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def handler(_candidate):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return "actual"

    async def model(_prompt, tools, _timeout):
        first = asyncio.create_task(tools["retry_function"].invoke(arguments={}))
        await entered.wait()
        assert await tools["retry_function"].invoke(arguments={}) == {
            "status": "failed", "error_type": "RuntimeError",
        }
        release.set()
        assert await first == {"status": "succeeded"}
        return AgentResult(session_id="recovery", content="done")

    assert await _recover(monkeypatch, tmp_path, model, handler) == "actual"
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("after_success", [False, True])
@pytest.mark.parametrize("outcome", ["provider_error", "timeout"])
async def test_model_failures_preserve_success_only(monkeypatch, tmp_path, after_success, outcome):
    async def model(_prompt, tools, _timeout):
        if after_success:
            await tools["retry_function"].invoke(arguments={})
        if outcome == "provider_error":
            raise RuntimeError("private provider credentials")
        await asyncio.Event().wait()

    response = await _recover(monkeypatch, tmp_path, model, timeout=0.02)
    if after_success:
        assert response.get_body() == b"private response"
    else:
        assert "Review the handler inputs" in _failure(response)
        assert "private" not in _failure(response)


@pytest.mark.asyncio
@pytest.mark.parametrize("content, expected", [("  ", None), ("x" * 2100, "x" * 2000)])
async def test_no_retry_remediation_is_bounded(monkeypatch, tmp_path, content, expected):
    async def model(_prompt, _tools, _timeout):
        return AgentResult(session_id="recovery", content=content)

    advice = _failure(await _recover(monkeypatch, tmp_path, model))
    assert advice == expected if expected else "Review the handler inputs" in advice


@pytest.mark.asyncio
@pytest.mark.parametrize("audit_only", [False, True])
async def test_source_tools_and_safe_diagnostics(monkeypatch, tmp_path, audit_only):
    path = tmp_path / "function_app.py"
    path.write_text("def original(req):\n    raise ValueError('private error message')\n")
    namespace = {}
    exec(compile(path.read_text(), str(path), "exec"), namespace)
    source_handler = namespace["original"]
    try:
        source_handler(None)
    except ValueError as error:
        original_error = error
    captured = {}

    async def model(prompt, tools, _timeout):
        captured.update(tools)
        instructions, diagnostic_json = prompt.split("\n", 1)
        assert instructions == (
            recovery_input.AUDIT_INSTRUCTIONS if audit_only else recovery_input.RECOVERY_INSTRUCTIONS
        )
        assert set(tools) == (_SOURCE_TOOLS if audit_only else _SOURCE_TOOLS | {"retry_function"})
        assert str(tmp_path) not in prompt
        assert "private error message" not in prompt
        assert "SECRET-CREDENTIAL" not in prompt
        assert "example.test" not in prompt
        diagnostic = json.loads(diagnostic_json)
        assert diagnostic["max_retries"] == (0 if audit_only else 1)
        if audit_only:
            assert diagnostic["audit_only"] is True
        assert diagnostic["error_type"] == "ValueError"
        assert diagnostic["source_hint"] == "function_app.py"
        assert diagnostic["traceback"] == [
            {"path": "function_app.py", "function": "original", "line": 2},
        ]
        assert await tools["list_files"].invoke(arguments={}) == ["function_app.py"]
        assert "raise ValueError" in await tools["read_file"].invoke(arguments={"path": "function_app.py"})
        assert await tools["search_source"].invoke(arguments={"query": "original"}) == [
            "function_app.py:1:def original(req):",
        ]
        assert await tools["read_file"].invoke(arguments={"path": "../secret.py"}) == {
            "status": "failed", "error_type": "ValueError",
        }
        return AgentResult(session_id="recovery", content="Fix inputs")

    response = await _recover(
        monkeypatch, tmp_path, model, error=original_error, source_handler=source_handler,
        audit_only=audit_only,
    )
    assert _failure(response) == "Fix inputs"
    arguments_by_name = {
        "list_files": {}, "read_file": {"path": "function_app.py"},
        "search_source": {"query": "original"}, "retry_function": {},
    }
    for name, descriptor in captured.items():
        with pytest.raises(RuntimeError, match="no longer active"):
            await descriptor.invoke(arguments=arguments_by_name[name])


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("max_retries", [1, 3])
async def test_audit_only_cannot_replay_or_correct_original_http_input(
    monkeypatch, tmp_path, caplog, method, max_retries,
):
    candidate = _request(method)
    original = candidate.request
    before = (
        original.method, original.url, original.get_body(), dict(original.headers),
        dict(original.route_params), dict(original.params), candidate.diagnostic(),
    )
    calls = []
    retry_handler = AsyncMock(return_value=HttpResponse("Must not succeed"))
    corrected = Mock(side_effect=AssertionError("Audit must not prepare replacement inputs"))
    monkeypatch.setattr(RecoveryRequest, "corrected_arguments", corrected)

    async def original_handler(request):
        calls.append(request)
        raise ValueError("private original failure")

    # The app wrapper, not recover_handler, owns the original invocation.
    with pytest.raises(ValueError) as caught:
        await original_handler(original)

    async def model(prompt, tools, _timeout):
        diagnostic = json.loads(prompt.split("\n", 1)[1])
        assert diagnostic["audit_only"] is True
        assert diagnostic["max_retries"] == 0
        assert set(tools) == _SOURCE_TOOLS
        with pytest.raises(KeyError, match="retry_function"):
            await tools["retry_function"].invoke(arguments={"route_params": {"count": 4}})
        return AgentResult(session_id="audit", content="Please call retry_function with count=4.")

    response = await _recover(
        monkeypatch, tmp_path, model, retry_handler, candidate=candidate,
        source_handler=original_handler, error=caught.value, max_retries=max_retries,
        audit_only=True,
    )
    assert response.status_code == 500
    if method == "HEAD":
        assert response.get_body() == b""
    else:
        assert _failure(response) == "Please call retry_function with count=4."
    assert calls == [original]
    retry_handler.assert_not_called()
    corrected.assert_not_called()
    assert candidate.request is original
    assert before == (
        original.method, original.url, original.get_body(), dict(original.headers),
        dict(original.route_params), dict(original.params), candidate.diagnostic(),
    )
    [record] = _audit_records(caplog)
    assert record.levelno == logging.WARNING
    assert record.args == ("Please call retry_function with count=4.",)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome, content, expected", [
    pytest.param("return", "  Fix the count input.\n", "Fix the count input.", id="trimmed"),
    pytest.param("return", "", _FALLBACK, id="empty"),
    pytest.param("return", " \n\t", _FALLBACK, id="blank"),
    pytest.param("return", "x" * 2000, "x" * 2000, id="exact-limit"),
    pytest.param("return", "🙂" * 2100, "🙂" * 2000, id="bounded-unicode"),
    pytest.param("provider", "", _FALLBACK, id="provider-error"),
    pytest.param("timeout", "", _FALLBACK, id="timeout"),
])
async def test_audit_logs_bounded_advice_once_after_analysis_before_http_failure(
    monkeypatch, tmp_path, caplog, outcome, content, expected,
):
    captured, events, invocations = {}, [], []
    retry_handler = AsyncMock()
    original_failure = RecoveryRequest.failure
    original_invocation = recovery_module._Invocation
    original_warning = recovery_module.logger.warning

    def invocation(*args, **kwargs):
        state = original_invocation(*args, **kwargs)
        invocations.append(state)
        return state

    def warning(message, *args, **kwargs):
        if message == _AUDIT_LOG:
            assert events == ["analysis-cleaned"]
            [state] = invocations
            assert state.active is False
            assert not state.tasks
            with pytest.raises(RuntimeError, match="no longer active"):
                state.check_active()
            events.append("audit-logged")
        return original_warning(message, *args, **kwargs)

    def failure(candidate, advice):
        assert events == ["analysis-cleaned", "audit-logged"]
        [record] = _audit_records(caplog)
        assert record.name == "azure.functions.AgentRuntime"
        assert record.levelno == logging.WARNING
        assert record.args == (expected,)
        assert len(record.args[0]) <= 2000
        assert advice == expected
        events.append("failure")
        return original_failure(candidate, advice)

    monkeypatch.setattr(RecoveryRequest, "failure", failure)
    monkeypatch.setattr(recovery_module, "_Invocation", invocation)
    monkeypatch.setattr(recovery_module.logger, "warning", warning)

    async def model(_prompt, tools, _timeout):
        captured.update(tools)
        try:
            if outcome == "provider":
                raise RuntimeError("private provider credentials")
            if outcome == "timeout":
                await asyncio.Event().wait()
            return AgentResult(session_id="audit", content=content)
        finally:
            events.append("analysis-cleaned")

    response = await _recover(
        monkeypatch, tmp_path, model, retry_handler, audit_only=True, max_retries=3,
        timeout=0.02 if outcome == "timeout" else 1,
    )
    assert _failure(response) == expected
    assert events == ["analysis-cleaned", "audit-logged", "failure"]
    assert len(_audit_records(caplog)) == 1
    assert "private provider credentials" not in caplog.text
    retry_handler.assert_not_called()
    await _assert_source_tools_expired(captured)


@pytest.mark.asyncio
@pytest.mark.parametrize("on_cancel", ["propagate", "swallow", "replace"])
async def test_cancelled_audit_has_no_remediation_log_or_failure_response(
    monkeypatch, tmp_path, caplog, on_cancel,
):
    entered, finished = asyncio.Event(), asyncio.Event()
    captured = {}
    retry_handler, failure, corrected = AsyncMock(), Mock(), Mock()
    monkeypatch.setattr(RecoveryRequest, "failure", failure)
    monkeypatch.setattr(RecoveryRequest, "corrected_arguments", corrected)

    async def model(_prompt, tools, _timeout):
        captured.update(tools)
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if on_cancel == "propagate":
                raise
            if on_cancel == "replace":
                raise RuntimeError("private cancellation replacement") from None
        finally:
            finished.set()
        return AgentResult(session_id="audit", content="Must not be logged")

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
        assert _audit_records(caplog) == []
        assert "smart_retry audit-only remediation" not in caplog.text
        assert "Must not be logged" not in caplog.text
        failure.assert_not_called()
        corrected.assert_not_called()
        retry_handler.assert_not_called()
        await _assert_source_tools_expired(captured)
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
@pytest.mark.parametrize("audit_only", [False, True])
async def test_real_recovery_runner_selects_instructions_without_authored_capabilities(
    monkeypatch, tmp_path, caplog, audit_only,
):
    source_text = "def original(req):\n    return req.route_params['count']\n"
    (tmp_path / "function_app.py").write_text(source_text, encoding="utf-8", newline="\n")
    entry, harness = Mock(), Mock()
    entry.resolved.model = "configured-model"
    entry.resolved.slug = "advisor"
    entry.resolved.instructions = "AUTHORED INSTRUCTIONS MUST NOT RUN"
    capabilities = entry.capabilities
    capabilities.filtered_user_tools = ["authored-tool"]
    capabilities.filtered_mcp_tools = ["authored-mcp"]
    capabilities.enabled_skill_paths = [tmp_path / "skills"]
    capabilities.skills = ("authored-skill",)
    capabilities.skill_catalog = ("catalog-skill",)
    capabilities.web_request_tools = ["authored-web"]
    skill = HostedSkill(entry, harness)
    captured, forwarded = {}, []
    original_run = HostedSkill._run_recovery
    sandbox = Mock(side_effect=AssertionError("Recovery cannot build sandbox capabilities"))

    async def run(self, prompt, **kwargs):
        forwarded.append(kwargs)
        return await original_run(self, prompt, **kwargs)

    async def run_agent(prompt, **kwargs):
        captured.update(kwargs, prompt=prompt)
        tools = {tool.name: tool for tool in kwargs["tools"]}
        captured["source"] = await tools["read_file"].invoke(arguments={"path": "function_app.py"})
        return AgentResult(session_id=kwargs["session_id"], content="Review the count.")

    runner = AsyncMock(side_effect=run_agent)
    monkeypatch.setattr(HostedSkill, "_run_recovery", run)
    monkeypatch.setattr(binding_module, "run_agent", runner)
    monkeypatch.setattr(binding_module, "build_sandbox_tools_for_session", sandbox)
    retry_handler = AsyncMock()
    response = await recover_handler(
        skill, _request(), ValueError("private error"), retry_handler, _handler,
        tmp_path, 1, 3, audit_only=audit_only,
    )
    assert _failure(response) == "Review the count."
    runner.assert_awaited_once()
    assert forwarded[0]["audit_only"] is audit_only
    assert captured["instructions"] == (
        recovery_input.AUDIT_INSTRUCTIONS if audit_only else recovery_input.RECOVERY_INSTRUCTIONS
    )
    assert captured["prompt"].split("\n", 1)[0] == captured["instructions"]
    assert "AUTHORED INSTRUCTIONS" not in captured["prompt"]
    assert captured["source"] == source_text
    tools = {tool.name: tool for tool in captured["tools"]}
    assert set(tools) == (_SOURCE_TOOLS if audit_only else _SOURCE_TOOLS | {"retry_function"})
    assert captured["tools"] is not forwarded[0]["tools"]
    for name in ("mcp_tools", "skill_paths", "skills", "skill_catalog", "sandbox_tools", "web_request_tools"):
        assert captured[name] == []
    assert captured["model"] == "configured-model"
    assert captured["agent_configuration"] is entry.resolved.agent_configuration
    assert captured["agent_name"] == "advisor"
    assert captured["_harness"] is harness
    assert captured["_session_is_new"] is True
    assert captured["session_id"]
    assert captured["timeout"] == 1
    assert capabilities.filtered_user_tools == ["authored-tool"]
    assert capabilities.filtered_mcp_tools == ["authored-mcp"]
    assert capabilities.enabled_skill_paths == [tmp_path / "skills"]
    assert capabilities.skills == ("authored-skill",)
    assert capabilities.skill_catalog == ("catalog-skill",)
    assert capabilities.web_request_tools == ["authored-web"]
    sandbox.assert_not_called()
    retry_handler.assert_not_called()
    assert len(_audit_records(caplog)) == int(audit_only)
    if audit_only:
        assert recovery_input.AUDIT_INSTRUCTIONS != recovery_input.RECOVERY_INSTRUCTIONS
        await _assert_source_tools_expired(tools)
    else:
        assert "smart_retry audit-only remediation" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["return", "error", "timeout", "cancel"])
async def test_background_retry_is_cancelled_and_awaited(monkeypatch, tmp_path, mode):
    entered, finished = asyncio.Event(), asyncio.Event()
    tasks = []
    captured = {}

    async def handler(_candidate):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    async def model(_prompt, tools, _timeout):
        captured.update(tools)
        tasks.append(asyncio.create_task(tools["retry_function"].invoke(arguments={})))
        await entered.wait()
        if mode == "error":
            raise RuntimeError("private")
        if mode in {"timeout", "cancel"}:
            await asyncio.Event().wait()
        return AgentResult(session_id="recovery", content="Review inputs")

    recovery = asyncio.create_task(_recover(monkeypatch, tmp_path, model, handler, timeout=0.05))
    if mode == "cancel":
        await entered.wait()
        recovery.cancel()
        with pytest.raises(asyncio.CancelledError):
            await recovery
    else:
        _failure(await recovery)
    assert finished.is_set()
    assert tasks[0].cancelled()
    with pytest.raises(RuntimeError, match="no longer active"):
        await captured["retry_function"].invoke(arguments={})


@pytest.mark.asyncio
@pytest.mark.parametrize("swallow", [False, True])
async def test_external_cancellation_overrides_retained_success(monkeypatch, tmp_path, swallow):
    succeeded = asyncio.Event()

    async def model(_prompt, tools, _timeout):
        await tools["retry_function"].invoke(arguments={})
        succeeded.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if not swallow:
                raise
        return AgentResult(session_id="recovery", content="done")

    task = asyncio.create_task(_recover(monkeypatch, tmp_path, model))
    await succeeded.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("max_retries", [1, 3])
async def test_concurrent_invocations_keep_results_and_attempts_isolated(
    monkeypatch, tmp_path, max_retries,
):
    entered = 0
    both_entered = asyncio.Event()
    attempts = {"one": 0, "two": 0}

    async def model(prompt, tools, _timeout):
        nonlocal entered
        count = json.loads(prompt.split("\n", 1)[1])["request"]["route_params"]["count"]
        entered += 1
        if entered == 2:
            both_entered.set()
        await both_entered.wait()
        for attempt in range(1, max_retries + 1):
            outcome = await tools["retry_function"].invoke(arguments={
                "route_params": {"count": count + attempt},
            })
            assert outcome == (
                {"status": "succeeded"} if attempt == max_retries else
                {"status": "failed", "error_type": "ValueError"}
            )
        return AgentResult(session_id="recovery", content="done")

    async def handler(candidate):
        identity = candidate.route_params["identity"]
        attempts[identity] += 1
        if attempts[identity] < max_retries:
            raise ValueError("Try another correction")
        return candidate.route_params["count"]

    candidates = [RecoveryRequest.create(HttpRequest(
        method="GET", url="https://example.test", body=b"",
        route_params={"count": count, "identity": identity},
    )) for identity, count in (("one", 1), ("two", 10))]
    results = await asyncio.gather(*[
        _recover(monkeypatch, tmp_path, model, handler, candidate=candidate, max_retries=max_retries)
        for candidate in candidates
    ])
    assert results == [1 + max_retries, 10 + max_retries]
    assert attempts == {"one": max_retries, "two": max_retries}


@pytest.mark.asyncio
async def test_concurrent_audit_and_retry_share_no_tools_budget_results_or_runner_policy(
    monkeypatch, tmp_path, caplog,
):
    source_text = "def original(req):\n    return req.route_params['count']\n"
    (tmp_path / "function_app.py").write_text(source_text, encoding="utf-8", newline="\n")
    audit_entered, retry_finished = asyncio.Event(), asyncio.Event()
    captured, assertions, attempts = {}, [], []
    skill = HostedSkill(Mock(), Mock())
    audit_handler = AsyncMock(return_value=HttpResponse("Audit must not succeed"))
    actual = HttpResponse("actual retry result")
    audit_input, retry_input = _request(), _request()

    async def handler(candidate):
        attempts.append(candidate.route_params["count"])
        if len(attempts) < 3:
            raise ValueError("Try another correction")
        return actual

    async def run_agent(prompt, **kwargs):
        try:
            diagnostic = json.loads(prompt.split("\n", 1)[1])
            audit_only = diagnostic.get("audit_only", False)
            tools = {tool.name: tool for tool in kwargs["tools"]}
            captured[audit_only] = kwargs
            if audit_only:
                assert diagnostic["max_retries"] == 0
                assert kwargs["instructions"] == recovery_input.AUDIT_INSTRUCTIONS
                assert set(tools) == _SOURCE_TOOLS
                audit_entered.set()
                await asyncio.wait_for(retry_finished.wait(), timeout=1)
                retry_tools = {tool.name: tool for tool in captured[False]["tools"]}
                with pytest.raises(RuntimeError, match="no longer active"):
                    await retry_tools["list_files"].invoke(arguments={})
                assert await tools["read_file"].invoke(arguments={"path": "function_app.py"}) == source_text
                return AgentResult(session_id=kwargs["session_id"], content="Audit advice only.")
            assert diagnostic["max_retries"] == 3
            assert kwargs["instructions"] == recovery_input.RECOVERY_INSTRUCTIONS
            assert set(tools) == _SOURCE_TOOLS | {"retry_function"}
            await asyncio.wait_for(audit_entered.wait(), timeout=1)
            for attempt in range(1, 4):
                assert await tools["retry_function"].invoke(arguments={
                    "route_params": {"count": attempt},
                }) == (
                    {"status": "succeeded"} if attempt == 3 else
                    {"status": "failed", "error_type": "ValueError"}
                )
            assert await tools["retry_function"].invoke(arguments={}) == {
                "status": "failed", "error_type": "RuntimeError",
            }
            return AgentResult(session_id=kwargs["session_id"], content="Not the handler result")
        except AssertionError as error:
            assertions.append(error)
            raise

    monkeypatch.setattr(binding_module, "run_agent", run_agent)

    async def retry():
        try:
            return await recover_handler(
                skill, retry_input, ValueError("retry failure"), handler, _handler,
                tmp_path, 2, 3,
            )
        finally:
            retry_finished.set()

    try:
        audit_response, retry_response = await asyncio.gather(
            recover_handler(
                skill, audit_input, ValueError("audit failure"), audit_handler, _handler,
                tmp_path, 2, 3, audit_only=True,
            ),
            retry(),
        )
    finally:
        if assertions:
            raise assertions[0]
    assert _failure(audit_response) == "Audit advice only."
    assert retry_response is actual
    assert attempts == [1, 2, 3]
    audit_handler.assert_not_called()
    assert audit_input.request.route_params["count"] == "bad"
    assert retry_input.request.route_params["count"] == "bad"
    assert captured[True]["session_id"] != captured[False]["session_id"]
    audit_tools = {tool.name: tool for tool in captured[True]["tools"]}
    retry_tools = {tool.name: tool for tool in captured[False]["tools"]}
    assert all(audit_tools[name] is not retry_tools[name] for name in _SOURCE_TOOLS)
    await _assert_source_tools_expired(audit_tools)
    [record] = _audit_records(caplog)
    assert record.args == ("Audit advice only.",)


@pytest.mark.asyncio
@pytest.mark.parametrize("audit_only, origin", [(False, "model"), (False, "handler"), (True, "model")])
async def test_helper_rethrows_model_and_handler_assertions(monkeypatch, tmp_path, audit_only, origin):
    async def handler(_candidate):
        raise AssertionError("captured handler assertion")

    async def model(_prompt, tools, _timeout):
        if origin == "model":
            raise AssertionError("captured model assertion")
        await tools["retry_function"].invoke(arguments={})
        return AgentResult(session_id="recovery", content="Would mask a handler assertion")

    with pytest.raises(AssertionError, match=f"captured {origin} assertion"):
        await _recover(monkeypatch, tmp_path, model, handler, audit_only=audit_only)


@pytest.mark.asyncio
@pytest.mark.parametrize("after_success", [False, True])
@pytest.mark.parametrize("cancel_source", ["external", "timeout", "both"])
async def test_provider_replacement_exception_preserves_cancellation(
    monkeypatch, tmp_path, after_success, cancel_source,
):
    entered = asyncio.Event()
    replaced = []

    async def model(_prompt, tools, _timeout):
        if after_success:
            await tools["retry_function"].invoke(arguments={})
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if cancel_source == "both":
                asyncio.current_task().cancel()
            replaced.append(True)
            raise RuntimeError("provider replaced cancellation") from None

    task = asyncio.create_task(_recover(
        monkeypatch, tmp_path, model, timeout=1 if cancel_source == "external" else 0.02,
    ))
    await entered.wait()
    if cancel_source == "external":
        task.cancel()
    if cancel_source != "timeout":
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
    else:
        response = await task
        assert task.cancelling() == 0
        if after_success:
            assert response.get_body() == b"private response"
        else:
            _failure(response)
    assert replaced == [True]


@pytest.mark.asyncio
async def test_existing_cancellation_count_is_only_a_baseline(monkeypatch, tmp_path):
    async def model(_prompt, tools, _timeout):
        await tools["retry_function"].invoke(arguments={})
        return AgentResult(session_id="recovery", content="done")

    async def invocation():
        owner = asyncio.current_task()
        owner.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.sleep(0)
        response = await _recover(monkeypatch, tmp_path, model)
        assert owner.cancelling() == 1
        return response

    response = await asyncio.create_task(invocation())
    assert response.get_body() == b"private response"


@pytest.mark.asyncio
async def test_external_cancellation_during_cleanup_cannot_be_replaced(monkeypatch, tmp_path):
    entered, cleaning = asyncio.Event(), asyncio.Event()
    tasks = []

    async def handler(_candidate):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise RuntimeError("cleanup replaced cancellation") from None

    async def model(_prompt, tools, _timeout):
        tasks.append(asyncio.create_task(tools["retry_function"].invoke(arguments={})))
        await entered.wait()
        return AgentResult(session_id="recovery", content="done")

    task = asyncio.create_task(_recover(monkeypatch, tmp_path, model, handler))
    await cleaning.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    _, pending = await asyncio.wait(tasks, timeout=1)
    assert not pending


@pytest_asyncio.fixture
async def blocked_cleanup():
    release, cleaning = asyncio.Event(), asyncio.Event()
    tasks = []

    async def handler(_candidate):
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            while not release.is_set():
                with suppress(asyncio.CancelledError):
                    await release.wait()

    try:
        yield handler, cleaning, tasks
    finally:
        release.set()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=1)
            for task in done:
                if not task.cancelled():
                    task.exception()
            assert not pending


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["return", "error", "timeout", "cancel", "cleanup_cancel"])
async def test_blocked_tool_cleanup_is_bounded(
    monkeypatch, tmp_path, caplog, blocked_cleanup, mode,
):
    handler, cleaning, tasks = blocked_cleanup
    entered = asyncio.Event()
    captured = {}

    async def model(_prompt, tools, _timeout):
        captured.update(tools)
        tasks.append(asyncio.create_task(tools["retry_function"].invoke(arguments={})))
        # This ready callback runs after the retry task reaches its first await.
        asyncio.get_running_loop().call_soon(entered.set)
        await entered.wait()
        if mode == "error":
            raise RuntimeError("SECRET-CREDENTIAL")
        if mode in {"timeout", "cancel"}:
            await asyncio.Event().wait()
        return AgentResult(session_id="recovery", content="Review inputs")

    async def invocation():
        if mode == "cleanup_cancel":
            asyncio.current_task().cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.sleep(0)
        return await _recover(monkeypatch, tmp_path, model, handler, timeout=0.02)

    recovery = asyncio.create_task(invocation())
    tasks.append(recovery)
    if mode == "cancel":
        await entered.wait()
        recovery.cancel()
    elif mode == "cleanup_cancel":
        await cleaning.wait()
        asyncio.get_running_loop().call_later(0, recovery.cancel)
    done, _ = await asyncio.wait({recovery}, timeout=2)
    assert recovery in done, "Recovery waited indefinitely for tool cancellation cleanup"
    assert cleaning.is_set()
    retry = tasks[1]
    assert not retry.done()
    if mode in {"cancel", "cleanup_cancel"}:
        with pytest.raises(asyncio.CancelledError):
            recovery.result()
        assert recovery.cancelling() == (2 if mode == "cleanup_cancel" else 1)
    else:
        _failure(recovery.result())
        assert recovery.cancelling() == 0
    assert "cooperative cancellation" in caplog.text
    assert "SECRET-CREDENTIAL" not in caplog.text
    for name, arguments in [("list_files", {}), ("read_file", {"path": "function_app.py"}),
                            ("search_source", {"query": "original"}), ("retry_function", {})]:
        with pytest.raises(RuntimeError, match="no longer active"):
            await captured[name].invoke(arguments=arguments)