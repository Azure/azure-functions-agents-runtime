from __future__ import annotations

import ast
import builtins
import importlib.util
import inspect
import json
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import get_type_hints
from unittest.mock import AsyncMock, Mock, call

import pytest
from azure.functions import QueueMessage
from azure.functions.decorators.core import BindingDirection

import azure_functions_agents._hosted_recovery_input as recovery_input
from azure_functions_agents import HostedSkill, HostedSkillRecoveryError, hosted_skill_binding
from azure_functions_agents.config import paths
from azure_functions_agents.discovery import tools as tool_discovery
from azure_functions_agents.harness import _harness_binding as harness
from azure_functions_agents.runner import AgentResult

SAMPLE_SRC = Path(__file__).resolve().parents[1] / "samples/hosted-skill-remediation/src"
SOURCE_TOOLS = {"list_files", "read_file", "search_source"}
RECOVERY_TOOLS = SOURCE_TOOLS | {"retry_function"}


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
    assert set(functions) == {"process_work_item", "process_work_item2"}
    bodies, recoveries = [], []
    process_body = module.process_work_item_body
    recover = HostedSkill._run_recovery

    def observe_body(body):
        bodies.append(body)
        return process_body(body)

    async def observe_recovery(skill, prompt, **kwargs):
        recoveries.append((skill._entry.resolved, kwargs["audit_only"]))
        return await recover(skill, prompt, **kwargs)

    monkeypatch.setattr(module, "process_work_item_body", observe_body)
    monkeypatch.setattr(HostedSkill, "_run_recovery", observe_recovery)
    run, stream = AsyncMock(), AsyncMock()
    monkeypatch.setattr(HostedSkill, "run", run)
    monkeypatch.setattr(HostedSkill, "stream", stream)
    log, warning = Mock(), Mock()
    monkeypatch.setattr(module.logger, "info", log)
    monkeypatch.setattr(module.logger, "warning", warning)
    yield SimpleNamespace(
        module=module, functions=functions,
        handler=functions["process_work_item"].get_user_function(),
        audit_handler=functions["process_work_item2"].get_user_function(),
        bodies=bodies, recoveries=recoveries, log=log, warning=warning,
    )
    run.assert_not_called()
    stream.assert_not_called()


def _runner(monkeypatch, model):
    calls = []

    async def run(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return await model(prompt, {tool.name: tool for tool in kwargs["tools"]})

    monkeypatch.setattr(hosted_skill_binding, "run_agent", run)
    return calls


def _check_runner(sample, calls, *, expected=1, audit_only=False):
    # Assertions stay outside production recovery/tool exception boundaries.
    assert len(calls) == expected
    assert len(sample.recoveries) == expected
    for (prompt, kwargs), (resolved, mode) in zip(calls, sample.recoveries, strict=True):
        expected_tools = SOURCE_TOOLS if audit_only else RECOVERY_TOOLS
        assert len(kwargs["tools"]) == len(expected_tools)
        assert {tool.name for tool in kwargs["tools"]} == expected_tools
        for name in (
            "mcp_tools", "skill_paths", "skills", "skill_catalog",
            "sandbox_tools", "web_request_tools",
        ):
            assert kwargs[name] == []
        assert mode is audit_only
        assert resolved.slug == kwargs["agent_name"] == "_smart_retry_advisor"
        assert resolved.instructions.strip()
        assert kwargs["instructions"] == recovery_input.recovery_instructions(
            resolved.instructions, audit_only=audit_only,
        )
        assert kwargs["timeout"] == 60
        assert json.loads(prompt.split("\n", 1)[1])["audit_only"] is audit_only


def _processing_logs(bodies, *, total=None):
    logs = [call("Processing queue work item: body=%s", body) for body in bodies]
    if total is not None:
        logs.append(call("Queue work item processed: total=%s", total))
    return logs


def _assert_source(source, filename):
    assert source.splitlines() == (SAMPLE_SRC / filename).read_text(encoding="utf-8").splitlines()


def _assert_failure(failure, advice, body, error_type):
    assert failure.remediation == advice[:2000]
    assert failure.__suppress_context__ is True
    formatted = "".join(traceback.format_exception(failure))
    assert "HostedSkillRecoveryError" in formatted
    assert error_type not in formatted
    assert body.decode("utf-8") not in formatted


def test_sample_registers_two_queue_handlers_with_default_advisor(sample):
    for name, queue in (
        ("process_work_item", "recovery-work-items"), ("process_work_item2", "bad-package"),
    ):
        [trigger] = sample.functions[name].get_bindings_dict()["bindings"]
        assert trigger == {
            "type": "queueTrigger", "direction": BindingDirection.IN, "name": "message",
            "queueName": queue, "connection": "AzureWebJobsStorage",
        }
        handler = sample.functions[name].get_user_function()
        signature = inspect.signature(handler)
        assert tuple(signature.parameters) == ("message",)
        assert signature == inspect.signature(inspect.unwrap(handler))
        assert get_type_hints(handler) == {"message": QueueMessage, "return": type(None)}
    source = ast.parse((SAMPLE_SRC / "function_app.py").read_text(encoding="utf-8"))
    retry_options = {}
    for node in source.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        [retry] = [
            decorator for decorator in node.decorator_list
            if isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "smart_retry"
        ]
        retry_options[node.name] = {
            keyword.arg: ast.literal_eval(keyword.value) for keyword in retry.keywords
        }
        assert retry.args == []
    assert retry_options == {
        "process_work_item": {"max_retries": 3}, "process_work_item2": {"audit_only": True},
    }
    assert not (SAMPLE_SRC / "queue-advisor.agent.md").exists()
    assert (SAMPLE_SRC / "error-advisor.agent.md").is_file()
    assert set(sample.module.app._hosted_skill_catalog) == {"error_advisor"}
    assert not tool_discovery.discover_user_tools(SAMPLE_SRC).tools
    assert sample.recoveries == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("body", "total"), [
    (b'{"operation":"sum","values":[1,2,3]}', 6.0),
    (b'{"operation":"sum","values":[1.5,-2]}', -0.5),
    (b'{"operation":"sum","values":[0]}', 0.0),
])
async def test_queue_valid_body_does_not_call_agent(sample, monkeypatch, body, total):
    model = AsyncMock()
    calls = _runner(monkeypatch, model)
    original = QueueMessage(body=body)
    result = await sample.handler(original)
    _check_runner(sample, calls, expected=0)
    model.assert_not_awaited()
    assert result is None
    assert sample.bodies == [body]
    assert sample.log.call_args_list == _processing_logs([body], total=total)
    sample.warning.assert_not_called()
    assert original.get_body() == body


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    b'{"operation":"sum","values":[1,2,3]}}',
    b'{"operation":"sum","values":[1,2,3],}',
], ids=["extra-brace", "trailing-comma"])
async def test_queue_representation_repair_uses_real_source_and_retry_tools(
    sample, monkeypatch, body,
):
    original = QueueMessage(body=body)
    corrected = '{"operation":"sum","values":[1,2,3]}'
    observed = {}

    async def model(prompt, tools):
        observed["diagnostic"] = json.loads(prompt.split("\n", 1)[1])
        observed["files"] = await tools["list_files"].invoke(arguments={})
        observed["source"] = await tools["read_file"].invoke(arguments={"path": "function_app.py"})
        observed["helper"] = await tools["read_file"].invoke(arguments={"path": "queue_work_item.py"})
        observed["matches"] = await tools["search_source"].invoke(
            arguments={"query": "process_work_item_body(message.get_body())"},
        )
        observed["schema"] = tools["retry_function"].parameters()
        observed["retry"] = await tools["retry_function"].invoke(arguments={"body": corrected})
        observed["after_success"] = await tools["retry_function"].invoke(
            arguments={"body": corrected},
        )
        return AgentResult(session_id="queue-recovery", content="Not the handler result")

    calls = _runner(monkeypatch, model)
    result = await sample.handler(original)
    _check_runner(sample, calls)
    diagnostic = observed["diagnostic"]
    assert diagnostic["error_type"] == "JSONDecodeError"
    assert diagnostic["max_retries"] == 3
    assert diagnostic["handler"] == "process_work_item"
    assert diagnostic["request"] == {"body": body.decode("utf-8")}
    assert diagnostic["source_hint"] == "function_app.py"
    assert {"function_app.py", "queue_work_item.py"} <= set(observed["files"])
    _assert_source(observed["source"], "function_app.py")
    _assert_source(observed["helper"], "queue_work_item.py")
    assert "from queue_work_item import process_work_item_body" in observed["source"]
    assert 'operation: Literal["sum"]' in observed["helper"]
    assert 'extra="forbid"' in observed["helper"]
    assert "math.fsum(item.values)" in observed["helper"]
    assert observed["matches"]
    assert observed["schema"]["required"] == ["body"]
    assert set(observed["schema"]["properties"]) == {"body"}
    assert observed["schema"]["properties"]["body"]["type"] == "string"
    assert observed["retry"] == {"status": "succeeded"}
    assert observed["after_success"] == {"status": "failed", "error_type": "RuntimeError"}
    assert result is None
    assert sample.bodies == [body, corrected.encode("utf-8")]
    assert sample.log.call_args_list == _processing_logs(sample.bodies, total=6.0)
    sample.warning.assert_not_called()
    assert original.get_body() == body


@pytest.mark.asyncio
async def test_queue_unknown_property_requires_bounded_advice_without_retry(sample, monkeypatch):
    body = b'{"operation":"sum","values":[1,2,3],"unfamiliar":"clarify intent"}'
    original = QueueMessage(body=body)
    advice = "Clarify the unfamiliar property with the producer; do not discard it. " * 50
    observed = {}

    async def model(prompt, tools):
        observed["diagnostic"] = json.loads(prompt.split("\n", 1)[1])
        observed["helper"] = await tools["read_file"].invoke(arguments={"path": "queue_work_item.py"})
        return AgentResult(session_id="queue-recovery", content=advice)

    calls = _runner(monkeypatch, model)
    with pytest.raises(HostedSkillRecoveryError) as failure:
        await sample.handler(original)
    _check_runner(sample, calls)
    assert observed["diagnostic"]["error_type"] == "ValidationError"
    assert observed["diagnostic"]["request"] == {"body": body.decode("utf-8")}
    assert 'extra="forbid"' in observed["helper"]
    _assert_failure(failure.value, advice, body, "ValidationError")
    assert sample.bodies == [body]
    assert sample.log.call_args_list == _processing_logs([body])
    sample.warning.assert_not_called()
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
    observed, outcomes = {}, []
    advice = "Review the producer contract before retrying. " * 60

    async def model(prompt, tools):
        observed["diagnostic"] = json.loads(prompt.split("\n", 1)[1])
        for correction in corrections:
            outcomes.append(await tools["retry_function"].invoke(arguments={"body": correction}))
        outcomes.append(await tools["retry_function"].invoke(arguments={
            "body": '{"operation":"sum","values":[1,2,3]}',
        }))
        return AgentResult(session_id="queue-recovery", content=advice)

    calls = _runner(monkeypatch, model)
    with pytest.raises(HostedSkillRecoveryError) as failure:
        await sample.handler(original)
    _check_runner(sample, calls)
    assert observed["diagnostic"]["max_retries"] == 3
    assert outcomes == [
        *({"status": "failed", "error_type": "ValidationError"} for _ in corrections),
        {"status": "failed", "error_type": "RuntimeError"},
    ]
    _assert_failure(failure.value, advice, body, "ValidationError")
    assert sample.bodies == [body, *(correction.encode("utf-8") for correction in corrections)]
    assert sample.log.call_args_list == _processing_logs(sample.bodies)
    sample.warning.assert_not_called()
    assert original.get_body() == body


@pytest.mark.asyncio
async def test_audit_missing_package_runs_original_once_and_preserves_typed_failure(
    sample, monkeypatch,
):
    body = b'{"operation":"sum","values":[1,2,3]}'
    original = QueueMessage(body=body, id="audit-message", pop_receipt="audit-receipt")
    advice = "Declare the missing package in requirements and redeploy; no retry was verified. " * 40
    observed, imports = {}, []
    real_import = builtins.__import__

    def missing_package(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "verybadpackage":
            imports.append(name)
            raise ModuleNotFoundError("No module named 'verybadpackage'", name=name)
        return real_import(name, globals, locals, fromlist, level)

    # Never import the intentionally broken dependency, even if installed locally.
    monkeypatch.setattr(builtins, "__import__", missing_package)

    async def model(prompt, tools):
        observed["diagnostic"] = json.loads(prompt.split("\n", 1)[1])
        observed["tools"] = set(tools)
        observed["files"] = await tools["list_files"].invoke(arguments={})
        observed["source"] = await tools["read_file"].invoke(arguments={"path": "function_app.py"})
        observed["matches"] = await tools["search_source"].invoke(
            arguments={"query": "import verybadpackage"},
        )
        return AgentResult(session_id="queue-audit", content=advice)

    calls = _runner(monkeypatch, model)
    with pytest.raises(HostedSkillRecoveryError) as failure:
        await sample.audit_handler(original)
    _check_runner(sample, calls, audit_only=True)
    diagnostic = observed["diagnostic"]
    assert diagnostic["handler"] == "process_work_item2"
    assert diagnostic["error_type"] == "ModuleNotFoundError"
    assert diagnostic["max_retries"] == 0
    assert diagnostic["source_hint"] == "function_app.py"
    assert diagnostic["request"] == {"body": body.decode("utf-8")}
    assert any(
        frame["path"] == "function_app.py" and frame["function"] == "process_work_item2"
        for frame in diagnostic["traceback"]
    )
    assert observed["tools"] == SOURCE_TOOLS
    assert "function_app.py" in observed["files"]
    _assert_source(observed["source"], "function_app.py")
    assert "import verybadpackage" in observed["source"]
    assert observed["matches"]
    assert imports == ["verybadpackage"]
    assert sample.bodies == []
    sample.log.assert_not_called()
    sample.warning.assert_called_once_with("smart_retry audit-only remediation: %s", advice[:2000])
    _assert_failure(failure.value, advice, body, "ModuleNotFoundError")
    assert original.get_body() == body
    assert original.id == "audit-message"
    assert original.pop_receipt == "audit-receipt"