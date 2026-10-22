from __future__ import annotations

import importlib.util
import inspect
import json
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import get_type_hints
from unittest.mock import AsyncMock, Mock

import pytest
from azure.functions import AuthLevel, HttpMethod, QueueMessage
from azurefunctions.extensions.http.fastapi import JSONResponse, Request

from azure_functions_agents import HostedSkill, HostedSkillRecoveryError, hosted_skill_binding
from azure_functions_agents.config import paths
from azure_functions_agents.discovery import tools as tool_discovery
from azure_functions_agents.harness import _harness_binding as harness
from azure_functions_agents.runner import AgentResult

SAMPLE_SRC = Path(__file__).resolve().parents[1] / "samples/hosted-skill-remediation/src"
RECOVERY_TOOLS = {"list_files", "read_file", "search_source", "retry_function"}


@pytest.fixture
def sample(monkeypatch):
    monkeypatch.syspath_prepend(str(SAMPLE_SRC))
    monkeypatch.setattr(paths, "_app_root", None)
    monkeypatch.setattr(harness, "_HARNESSES", {})
    monkeypatch.setattr(tool_discovery, "_DISCOVERED_TOOLS_CACHE", {})
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_APP_ROOT", raising=False)
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT", raising=False)
    monkeypatch.setenv("AzureWebJobsScriptRoot", str(SAMPLE_SRC))
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "foundry")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://fixture.services.ai.azure.com/api/projects/test")
    monkeypatch.setenv("FOUNDRY_MODEL", "fixture-model")
    name = "_test_hosted_skill_remediation_function_app"
    spec = importlib.util.spec_from_file_location(name, SAMPLE_SRC / "function_app.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    functions = {function.get_function_name(): function for function in module.app.get_functions()}
    assert set(functions) == {"add_numbers", "process_work_item"}
    function = functions["add_numbers"]
    queue_function = functions["process_work_item"]

    # Observe the captured handler's real reads, not calls to a replacement handler.
    reads = []
    path_params = Request.path_params.fget

    def read_path_params(candidate):
        values = path_params(candidate)
        reads.append((candidate, dict(values)))
        return values

    monkeypatch.setattr(Request, "path_params", property(read_path_params))
    run, stream = AsyncMock(), AsyncMock()
    monkeypatch.setattr(HostedSkill, "run", run)
    monkeypatch.setattr(HostedSkill, "stream", stream)
    bodies = []
    process_body = module.process_work_item_body

    def observe_body(body):
        bodies.append(body)
        return process_body(body)

    monkeypatch.setattr(module, "process_work_item_body", observe_body)
    log = Mock()
    monkeypatch.setattr(module.logger, "info", log)
    yield SimpleNamespace(
        module=module, function=function, handler=function.get_user_function(), reads=reads,
        queue_function=queue_function, queue_handler=queue_function.get_user_function(),
        bodies=bodies, log=log,
    )
    run.assert_not_called()
    stream.assert_not_called()


def _request(a="1", b="2"):
    return Request({
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": f"/api/add/{a}/{b}", "query_string": b"",
        "headers": [(b"content-length", b"0")], "path_params": {"a": a, "b": b},
    })


def _runner(monkeypatch, model, *, agent_name="error_advisor"):
    calls, assertions = [], []

    async def run(prompt, **kwargs):
        calls.append((prompt, kwargs))
        try:
            descriptors = kwargs["tools"]
            assert len(descriptors) == len(RECOVERY_TOOLS)
            tools = {tool.name: tool for tool in descriptors}
            assert set(tools) == RECOVERY_TOOLS
            for name in (
                "mcp_tools", "skill_paths", "skills", "skill_catalog",
                "sandbox_tools", "web_request_tools",
            ):
                assert kwargs[name] == []
            assert kwargs["agent_name"] == agent_name
            assert kwargs["timeout"] == 60
            return await model(prompt, tools)
        except AssertionError as error:
            # Recovery catches Exception; surface assertions outside that boundary.
            assertions.append(error)
            raise

    monkeypatch.setattr(hosted_skill_binding, "run_agent", run)
    return calls, assertions


def _check_runner(calls, assertions, expected=1):
    if assertions:
        raise assertions[0]
    assert len(calls) == expected


def _failure(response, advice):
    assert response.status_code == 500
    assert json.loads(response.body) == {
        "error": "FUNCTION_RECOVERY_FAILED", "requires_user_action": True, "remediation": advice,
    }


def test_sample_registers_http_route_without_authored_tools(sample):
    assert sample.function.get_function_name() == "add_numbers"
    [trigger] = [
        binding for binding in sample.function.get_bindings_dict()["bindings"]
        if binding["type"] == "httpTrigger"
    ]
    assert trigger["route"] == "add/{a}/{b}"
    assert trigger["methods"] == [HttpMethod.GET]
    assert trigger["authLevel"] == AuthLevel.FUNCTION
    signature = inspect.signature(sample.handler)
    assert tuple(signature.parameters) == ("req",)
    assert signature == inspect.signature(inspect.unwrap(sample.handler))
    hints = get_type_hints(sample.handler)
    assert hints["req"] is Request
    assert hints["return"] is JSONResponse
    assert "skill" not in hints
    assert not tool_discovery.discover_user_tools(SAMPLE_SRC).tools


@pytest.mark.asyncio
@pytest.mark.parametrize(("a", "b", "total"), [("1", "2", 3), ("1.5", "-2", -0.5)])
async def test_normal_path_does_not_call_agent(sample, monkeypatch, a, b, total):
    async def model(_prompt, _tools):
        pytest.fail("Normal execution must not invoke the agent")

    calls, assertions = _runner(monkeypatch, model)
    original = _request(a, b)
    response = await sample.handler(original)
    _check_runner(calls, assertions, expected=0)
    assert response.status_code == 200
    assert json.loads(response.body) == {"sum": total}
    assert [candidate for candidate, _ in sample.reads] == [original, original]
    assert original.scope["path_params"] == {"a": a, "b": b}


@pytest.mark.asyncio
async def test_quoted_number_retries_whole_handler_with_real_tools(sample, monkeypatch):
    original = _request('"1"', "2")
    routes = original.scope["path_params"]
    observations = {}

    async def model(prompt, tools):
        diagnostic = json.loads(prompt.split("\n", 1)[1])
        observations["diagnostic"] = diagnostic
        assert diagnostic["max_retries"] == 1
        assert diagnostic["handler"] == "add_numbers"
        files = await tools["list_files"].invoke(arguments={})
        assert diagnostic["source_hint"] in files
        source = await tools["read_file"].invoke(arguments={"path": diagnostic["source_hint"]})
        assert source.splitlines() == (
            SAMPLE_SRC / diagnostic["source_hint"]
        ).read_text(encoding="utf-8").splitlines()
        assert "async def add_numbers(" in source
        assert 'float(req.path_params["a"])' in source
        assert await tools["search_source"].invoke(arguments={"query": "async def add_numbers("})
        observations["retry"] = await tools["retry_function"].invoke(
            arguments={"route_params": {"a": 1}}
        )
        return AgentResult(session_id="sample-recovery", content="Not the handler response")

    calls, assertions = _runner(monkeypatch, model)
    response = await sample.handler(original)
    _check_runner(calls, assertions)
    assert observations["diagnostic"]["error_type"] == "ValueError"
    assert observations["diagnostic"]["source_hint"] == "function_app.py"
    assert observations["diagnostic"]["request"]["route_params"] == {"a": '"1"', "b": "2"}
    assert observations["retry"] == {"status": "succeeded"}
    assert len(sample.reads) == 3
    first, retry_a, retry_b = sample.reads
    assert first == (original, {"a": '"1"', "b": "2"})
    assert retry_a[0] is retry_b[0] and retry_a[0] is not original
    assert retry_a[1] == retry_b[1] == {"a": 1, "b": "2"}
    assert response.status_code == 200
    assert response.body == b'{"sum":3.0}'
    assert original.scope["path_params"] is routes
    assert routes == {"a": '"1"', "b": "2"}


@pytest.mark.asyncio
async def test_unknown_input_returns_guidance_without_retry(sample, monkeypatch):
    original = _request("unknown")
    advice = "Supply the intended number; unknown is ambiguous."

    async def model(prompt, _tools):
        diagnostic = json.loads(prompt.split("\n", 1)[1])
        assert diagnostic["error_type"] == "ValueError"
        assert diagnostic["request"]["route_params"] == {"a": "unknown", "b": "2"}
        return AgentResult(session_id="sample-recovery", content=advice)

    calls, assertions = _runner(monkeypatch, model)
    response = await sample.handler(original)
    _check_runner(calls, assertions)
    _failure(response, advice)
    assert sample.reads == [(original, {"a": "unknown", "b": "2"})]
    assert original.scope["path_params"] == {"a": "unknown", "b": "2"}


@pytest.mark.asyncio
async def test_wrong_correction_fails_once_and_cannot_retry_again(sample, monkeypatch):
    original = _request('"1"')
    advice = "Use a number instead of a quoted string."
    results = []

    async def model(_prompt, tools):
        retry = tools["retry_function"]
        results.append(await retry.invoke(arguments={"route_params": {"a": '"still wrong"'}}))
        results.append(await retry.invoke(arguments={"route_params": {"a": 1}}))
        return AgentResult(session_id="sample-recovery", content=advice)

    calls, assertions = _runner(monkeypatch, model)
    response = await sample.handler(original)
    _check_runner(calls, assertions)
    assert results == [
        {"status": "failed", "error_type": "ValueError"},
        {"status": "failed", "error_type": "RuntimeError"},
    ]
    _failure(response, advice)
    assert len(sample.reads) == 2
    assert sample.reads[0][0] is original
    assert sample.reads[1][0] is not original
    assert sample.reads[1][1] == {"a": '"still wrong"', "b": "2"}
    assert original.scope["path_params"] == {"a": '"1"', "b": "2"}


def test_sample_registers_queue_trigger_without_authored_tools(sample):
    [trigger] = sample.queue_function.get_bindings_dict()["bindings"]
    assert trigger["type"] == "queueTrigger"
    assert trigger["name"] == "message"
    assert trigger["queueName"] == "recovery-work-items"
    assert trigger["connection"] == "AzureWebJobsStorage"
    signature = inspect.signature(sample.queue_handler)
    assert tuple(signature.parameters) == ("message",)
    assert signature == inspect.signature(inspect.unwrap(sample.queue_handler))
    hints = get_type_hints(sample.queue_handler)
    assert hints["message"] is QueueMessage
    assert hints["return"] is type(None)
    assert "skill" not in hints
    assert not tool_discovery.discover_user_tools(SAMPLE_SRC).tools


@pytest.mark.asyncio
@pytest.mark.parametrize(("body", "total"), [
    (b'{"operation":"sum","values":[1,2,3]}', 6.0),
    (b'{"operation":"sum","values":[1.5,-2]}', -0.5),
    (b'{"operation":"sum","values":[0]}', 0.0),
])
async def test_queue_valid_body_does_not_call_agent(sample, monkeypatch, body, total):
    async def model(_prompt, _tools):
        pytest.fail("Valid queue processing must not invoke the agent")

    calls, assertions = _runner(monkeypatch, model, agent_name="queue_advisor")
    original = QueueMessage(body=body)
    result = await sample.queue_handler(original)
    _check_runner(calls, assertions, expected=0)
    assert result is None
    assert sample.bodies == [body]
    sample.log.assert_called_once_with("Queue work item processed: total=%s", total)
    assert original.get_body() == body


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    b'{"operation":"sum","values":[1,2,3]}}',
    b'{"operation":"sum","values":[1,2,3],}',
])
async def test_queue_representation_repair_uses_real_source_and_retry_tools(
    sample, monkeypatch, body,
):
    original = QueueMessage(body=body)
    corrected = '{"operation":"sum","values":[1,2,3]}'
    observations = {}

    async def model(prompt, tools):
        diagnostic = json.loads(prompt.split("\n", 1)[1])
        assert diagnostic["error_type"] == "JSONDecodeError"
        assert diagnostic["max_retries"] == 3
        assert diagnostic["handler"] == "process_work_item"
        assert diagnostic["request"]["body"] == body.decode("utf-8")
        assert diagnostic["source_hint"] == "function_app.py"
        source = await tools["read_file"].invoke(arguments={"path": diagnostic["source_hint"]})
        assert source.splitlines() == (SAMPLE_SRC / "function_app.py").read_text(
            encoding="utf-8"
        ).splitlines()
        assert "process_work_item_body(message.get_body())" in source
        assert "from queue_work_item import process_work_item_body" in source
        helper = await tools["read_file"].invoke(arguments={"path": "queue_work_item.py"})
        assert helper.splitlines() == (SAMPLE_SRC / "queue_work_item.py").read_text(
            encoding="utf-8"
        ).splitlines()
        assert 'operation: Literal["sum"]' in helper
        assert 'extra="forbid"' in helper
        assert "math.fsum(item.values)" in helper
        schema = tools["retry_function"].parameters()
        assert schema["required"] == ["body"]
        assert set(schema["properties"]) == {"body"}
        assert schema["properties"]["body"]["type"] == "string"
        observations["retry"] = await tools["retry_function"].invoke(arguments={"body": corrected})
        observations["after_success"] = await tools["retry_function"].invoke(
            arguments={"body": corrected}
        )
        return AgentResult(session_id="queue-recovery", content="Not the handler result")

    calls, assertions = _runner(monkeypatch, model, agent_name="queue_advisor")
    result = await sample.queue_handler(original)
    _check_runner(calls, assertions)
    assert result is None
    assert observations == {
        "retry": {"status": "succeeded"},
        "after_success": {"status": "failed", "error_type": "RuntimeError"},
    }
    assert sample.bodies == [body, corrected.encode("utf-8")]
    sample.log.assert_called_once_with("Queue work item processed: total=%s", 6.0)
    assert original.get_body() == body


@pytest.mark.asyncio
async def test_queue_unknown_property_requires_bounded_advice_without_retry(sample, monkeypatch):
    body = b'{"operation":"sum","values":[1,2,3],"unfamiliar":"clarify intent"}'
    original = QueueMessage(body=body)
    advice = "Clarify the unfamiliar property with the producer; do not discard it. " * 50

    async def model(prompt, tools):
        diagnostic = json.loads(prompt.split("\n", 1)[1])
        assert diagnostic["error_type"] == "ValidationError"
        assert diagnostic["request"]["body"] == body.decode("utf-8")
        helper = await tools["read_file"].invoke(arguments={"path": "queue_work_item.py"})
        assert 'extra="forbid"' in helper
        return AgentResult(session_id="queue-recovery", content=advice)

    calls, assertions = _runner(monkeypatch, model, agent_name="queue_advisor")
    with pytest.raises(HostedSkillRecoveryError) as failure:
        await sample.queue_handler(original)
    _check_runner(calls, assertions)
    assert failure.value.remediation == advice[:2000]
    assert failure.value.__suppress_context__ is True
    formatted = "".join(traceback.format_exception(failure.value))
    assert "HostedSkillRecoveryError" in formatted
    assert "ValidationError" not in formatted
    assert body.decode("utf-8") not in formatted
    assert sample.bodies == [body]
    sample.log.assert_not_called()
    assert original.get_body() == body


@pytest.mark.asyncio
async def test_queue_wrong_corrections_exhaust_budget_without_success(sample, monkeypatch):
    body = b'{"operation":"sum","values":[1,2,3]}}'
    original = QueueMessage(body=body)
    corrections = [
        '{"operation":"sum","values":[]}',
        '{"operation":"sum","values":[true]}',
        '{"operation":"sum","values":[1],"unfamiliar":2}',
    ]
    outcomes = []
    advice = "Review the producer contract before retrying. " * 60

    async def model(prompt, tools):
        assert json.loads(prompt.split("\n", 1)[1])["max_retries"] == 3
        for correction in corrections:
            outcomes.append(await tools["retry_function"].invoke(arguments={"body": correction}))
        outcomes.append(await tools["retry_function"].invoke(arguments={
            "body": '{"operation":"sum","values":[1,2,3]}',
        }))
        return AgentResult(session_id="queue-recovery", content=advice)

    calls, assertions = _runner(monkeypatch, model, agent_name="queue_advisor")
    with pytest.raises(HostedSkillRecoveryError) as failure:
        await sample.queue_handler(original)
    _check_runner(calls, assertions)
    assert outcomes == [
        *({"status": "failed", "error_type": "ValidationError"} for _ in corrections),
        {"status": "failed", "error_type": "RuntimeError"},
    ]
    assert failure.value.remediation == advice[:2000]
    assert failure.value.__suppress_context__ is True
    assert sample.bodies == [body, *(correction.encode("utf-8") for correction in corrections)]
    sample.log.assert_not_called()
    assert original.get_body() == body