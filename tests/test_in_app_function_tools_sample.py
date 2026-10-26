from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from azure_functions_agents.config import paths
from azure_functions_agents.harness import _harness_binding as harness


@pytest.fixture
def sample(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    source = (
        Path(__file__).resolve().parents[1]
        / "samples"
        / "in-app-function-tools"
        / "src"
        / "function_app.py"
    )
    monkeypatch.setattr(paths, "_app_root", None)
    monkeypatch.setattr(harness, "_HARNESSES", {})
    monkeypatch.setenv("AzureWebJobsScriptRoot", str(source.parent))
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "foundry")
    monkeypatch.setenv(
        "FOUNDRY_PROJECT_ENDPOINT", "https://fixture.services.ai.azure.com/api/projects/test"
    )
    monkeypatch.setenv("FOUNDRY_MODEL", "gpt-4.1")
    spec = importlib.util.spec_from_file_location("in_app_function_tools_sample", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sample_indexes_trigger_handlers_but_not_standalone_tool(sample: ModuleType) -> None:
    names = {function.get_function_name() for function in sample.app.get_functions()}
    assert {"get_order", "ship_order", "agent_main_builtin_chat"} <= names
    assert "business_days_between" not in names


@pytest.mark.asyncio
async def test_sample_tools_change_and_reread_order(sample: ModuleType) -> None:
    capabilities = sample.app._hosted_skill_catalog["main"].capabilities
    tools = {tool.name: tool for tool in capabilities.execution_user_tools()}
    assert set(tools) == {"get_order", "ship_order", "business_days_between"}
    before = await tools["get_order"].invoke(arguments={"order_id": "1002"})
    assert json.loads(before["body"])["status"] == "pending"
    assert (
        await tools["ship_order"].invoke(arguments={"order_id": "1002", "carrier": "UPS"})
        == "completed"
    )
    after = await tools["get_order"].invoke(arguments={"order_id": "1002"})
    assert json.loads(after["body"])["status"] == "shipped via UPS"


@pytest.mark.asyncio
async def test_sample_standalone_tool_accepts_json_dates(sample: ModuleType) -> None:
    capabilities = sample.app._hosted_skill_catalog["main"].capabilities
    tools = {tool.name: tool for tool in capabilities.execution_user_tools()}
    assert (
        await tools["business_days_between"].invoke(
            arguments={"start": "2026-10-23", "end": "2026-10-27"}
        )
        == 2
    )
