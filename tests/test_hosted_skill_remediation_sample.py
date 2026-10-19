from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, Mock

import pytest
from azurefunctions.extensions.http.fastapi import Request
from starlette.datastructures import QueryParams

from azure_functions_agents import HostedSkill
from azure_functions_agents.config import paths
from azure_functions_agents.discovery import tools as tool_discovery
from azure_functions_agents.harness import _harness_binding as _harness
from azure_functions_agents.runner import AgentResult

SAMPLE_SRC = Path(__file__).resolve().parents[1] / "samples" / "hosted-skill-remediation" / "src"


@pytest.fixture
def sample(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    monkeypatch.syspath_prepend(str(SAMPLE_SRC))
    monkeypatch.setattr(paths, "_app_root", None)
    monkeypatch.setattr(_harness, "_HARNESSES", {})
    monkeypatch.setattr(tool_discovery, "_DISCOVERED_TOOLS_CACHE", {})
    monkeypatch.setenv("AzureWebJobsScriptRoot", str(SAMPLE_SRC))
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "foundry")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://fixture.services.ai.azure.com/api/projects/test")
    monkeypatch.setenv("FOUNDRY_MODEL", "fixture-model")
    loaded = {}
    for name in ("hosted_report", "report_recovery", "function_app"):
        spec = importlib.util.spec_from_file_location(name, SAMPLE_SRC / f"{name}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        loaded[name] = module
    module = loaded["function_app"]
    module.reports = loaded["hosted_report"]
    module.recovery = loaded["report_recovery"]
    monkeypatch.setattr(module.reports, "TEMPLATE_PATH", tmp_path / "weekly-report.txt")
    [module.correction_tool] = tool_discovery.discover_user_tools(SAMPLE_SRC).tools
    return module


def _handler(sample):
    [function] = sample.app.get_functions()
    return function.get_user_function()


def _request(query: bytes = b"") -> Request:
    return Request({"type": "http", "method": "GET", "path": "/api/reports/weekly",
                    "query_string": query, "headers": []})


def _result(corrected=None, advice="Repair steps") -> AgentResult:
    return AgentResult(session_id="test", content=json.dumps({
        "corrected_input": corrected, "remediation": advice,
    }))


@pytest.mark.asyncio
async def test_tool_discovered_and_usable(sample):
    assert sample.correction_tool.name == "correct_report_format"
    assert await sample.correction_tool.invoke(arguments={"value": "markdwon"}) == "markdown"
    assert not sample.reports.TEMPLATE_PATH.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [b"", b"format=text", b"format=markdown"])
async def test_success_bypasses_agent(sample, monkeypatch, query):
    run = AsyncMock()
    monkeypatch.setattr(HostedSkill, "run", run)
    sample.reports.TEMPLATE_PATH.write_text("{title}", encoding="utf-8")
    response = await _handler(sample)(_request(query))
    assert response.status_code == 200
    assert json.loads(response.body)["auto_corrected"] is False
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_human_remediation_then_manual_fix(sample, monkeypatch):
    run = AsyncMock(return_value=_result(advice="Supply template"))
    monkeypatch.setattr(HostedSkill, "run", run)
    handler = _handler(sample)
    response = await handler(_request())
    assert response.status_code == 500
    assert json.loads(response.body)["remediation"] == "Supply template"
    diagnostic = json.loads(run.await_args.args[0])
    assert diagnostic["exception_type"] == "FileNotFoundError"
    assert str(sample.reports.TEMPLATE_PATH.parent) not in run.await_args.args[0]
    sample.reports.TEMPLATE_PATH.write_text("{title}", encoding="utf-8")
    assert (await handler(_request())).status_code == 200
    run.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [b"format=markdwon", b"frmat=markdown&format=invalid", b"format=text&format=text"])
async def test_agent_decides_correction_without_tool_evidence(sample, monkeypatch, query):
    run = AsyncMock(return_value=_result({"format": "markdown"}))
    monkeypatch.setattr(HostedSkill, "run", run)
    sample.reports.TEMPLATE_PATH.write_text("{title}", encoding="utf-8")
    render = Mock(wraps=sample.render_report)
    monkeypatch.setattr(sample, "render_report", render)
    response = await _handler(sample)(_request(query))
    assert response.status_code == 200
    assert json.loads(response.body)["auto_corrected"] is True
    assert json.loads(response.body)["report"].startswith("# ")
    assert render.call_count == 2
    assert json.loads(run.await_args.args[0])["inputs"] == [list(p) for p in _request(query).query_params.multi_items()]


@pytest.mark.asyncio
async def test_actual_binding_can_use_optional_tool(sample, monkeypatch):
    from azure_functions_agents import hosted_skill_binding

    async def fake_runner(prompt, *, tools, **kwargs):
        diagnostic = json.loads(prompt)
        [tool] = tools
        corrected = await tool.invoke(arguments={"value": dict(diagnostic["inputs"])["format"]})
        return _result({"format": corrected})

    monkeypatch.setattr(hosted_skill_binding, "run_agent", fake_runner)
    sample.reports.TEMPLATE_PATH.write_text("{title}", encoding="utf-8")
    assert (await _handler(sample)(_request(b"format=markdwon"))).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [PermissionError, ConnectionError, RuntimeError])
async def test_generic_operation_can_be_corrected_regardless_of_exception_type(sample, error_type):
    skill = Mock(run=AsyncMock(return_value=_result({"region": "west", "count": "2"})))
    operation = Mock(return_value={"output": "done"})
    response = await sample.recovery.recover_operation(
        skill, error_type("raw private error"), QueryParams("region=wset"),
        operation=operation, context="Read-only lookup; valid regions: west, east.",
    )
    assert response.status_code == 200
    assert json.loads(response.body)["output"] == "done"
    assert operation.call_args.args[0]["region"] == "west"
    assert "raw private error" not in skill.run.await_args.args[0]


@pytest.mark.asyncio
async def test_agent_can_decline_ambiguous_input(sample, monkeypatch):
    run = AsyncMock(return_value=_result(advice="Choose one format"))
    monkeypatch.setattr(HostedSkill, "run", run)
    render = Mock(wraps=sample.render_report)
    monkeypatch.setattr(sample, "render_report", render)
    response = await _handler(sample)(_request(b"format=text&format=markdown"))
    assert response.status_code == 500
    assert json.loads(response.body)["remediation"] == "Choose one format"
    assert json.loads(run.await_args.args[0])["inputs"] == [["format", "text"], ["format", "markdown"]]
    render.assert_called_once()


@pytest.mark.asyncio
async def test_empty_corrected_inputs_are_a_valid_retry(sample, monkeypatch):
    monkeypatch.setattr(HostedSkill, "run", AsyncMock(return_value=_result({})))
    sample.reports.TEMPLATE_PATH.write_text("{title}", encoding="utf-8")
    response = await _handler(sample)(_request(b"format=invalid"))
    assert response.status_code == 200
    assert json.loads(response.body)["corrected_input"] == {}


@pytest.mark.asyncio
async def test_failed_retry_stops_without_another_agent_call(sample, monkeypatch):
    run = AsyncMock(return_value=_result({"format": "markdown"}))
    monkeypatch.setattr(HostedSkill, "run", run)
    render = Mock(wraps=sample.render_report)
    monkeypatch.setattr(sample, "render_report", render)
    response = await _handler(sample)(_request(b"format=markdwon"))
    assert response.status_code == 500
    assert json.loads(response.body)["auto_corrected"] is False
    assert render.call_count == 2
    run.assert_awaited_once()
    assert json.loads(response.body)["remediation"] == sample.recovery.FALLBACK_ADVICE


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["", "invalid json", "[]", '{"corrected_input": []}', '{"remediation": "Try", "corrected_input": {"x": 3}}'])
async def test_bad_wire_format_uses_fallback(sample, monkeypatch, content):
    monkeypatch.setattr(HostedSkill, "run", AsyncMock(return_value=AgentResult(session_id="test", content=content)))
    response = await _handler(sample)(_request())
    assert json.loads(response.body)["remediation"] == sample.recovery.FALLBACK_ADVICE


@pytest.mark.asyncio
async def test_model_failure_and_timeout_use_fallback(sample, monkeypatch):
    handler = _handler(sample)
    monkeypatch.setattr(HostedSkill, "run", AsyncMock(side_effect=RuntimeError("provider error")))
    assert json.loads((await handler(_request())).body)["remediation"] == sample.recovery.FALLBACK_ADVICE

    async def stalled(*args, **kwargs):
        await asyncio.Future()

    monkeypatch.setattr(HostedSkill, "run", AsyncMock(side_effect=stalled))
    monkeypatch.setattr(sample.recovery, "ADVICE_TIMEOUT_SECONDS", 0)
    assert json.loads((await handler(_request())).body)["remediation"] == sample.recovery.FALLBACK_ADVICE


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["operation", "advisor", "retry"])
async def test_cancellation_propagates(sample, monkeypatch, stage):
    run = AsyncMock(return_value=_result({"format": "markdown"}))
    monkeypatch.setattr(HostedSkill, "run", run)
    if stage == "advisor":
        run.side_effect = asyncio.CancelledError()
    else:
        errors = [asyncio.CancelledError()] if stage == "operation" else [ValueError(), asyncio.CancelledError()]
        monkeypatch.setattr(sample, "render_report", Mock(side_effect=errors))
    with pytest.raises(asyncio.CancelledError):
        await _handler(sample)(_request(b"format=markdwon"))