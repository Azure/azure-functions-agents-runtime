from __future__ import annotations

import importlib.util
import inspect
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import azure.durable_functions as df
import azure.functions as func
import pytest
from azurefunctions.extensions.http.fastapi import Request, Response

from azure_functions_agents.composition import compose_binding_target, load_project_snapshot
from azure_functions_agents.config import paths

SAMPLES_ROOT = Path(__file__).resolve().parents[1] / "samples"
FUNCTION_SAMPLE_SRC = SAMPLES_ROOT / "hybrid-function-agent" / "src"
DURABLE_SAMPLE_SRC = SAMPLES_ROOT / "hybrid-durable-agent" / "src"


def _load_sample(
    sample_src: Path,
    module_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> ModuleType:
    monkeypatch.setattr(paths, "_app_root", None)
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_APP_ROOT", raising=False)
    monkeypatch.delenv("AzureWebJobsScriptRoot", raising=False)
    monkeypatch.chdir(sample_src)
    spec = importlib.util.spec_from_file_location(
        module_name,
        sample_src / "function_app.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("sample_src", [FUNCTION_SAMPLE_SRC, DURABLE_SAMPLE_SRC])
def test_hybrid_binding_samples_use_minimal_definition(sample_src: Path) -> None:
    snapshot = load_project_snapshot(sample_src)

    definition = compose_binding_target(snapshot, "order-fulfillment").definition
    assert definition.name == "Order Fulfillment"
    assert definition.slug == "order_fulfillment"
    assert definition.instructions.startswith("You are an order fulfillment specialist.")
    assert [tool.name for tool in snapshot.discovery.user_tools] == [
        "summarize_order_quantities"
    ]
    assert [name for name, _ in snapshot.discovery.skills] == ["order-review"]
    assert [name for name, _ in snapshot.discovery.mcp_servers] == ["microsoft-learn"]


def test_ai_app_sample_indexes_standard_triggers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_sample(
        FUNCTION_SAMPLE_SRC,
        "hybrid_function_agent_sample",
        monkeypatch,
    )

    assert isinstance(module.app, func.FunctionApp)
    assert not isinstance(module.app, df.DFApp)
    indexed_functions = module.app.get_functions()
    functions = {
        function.get_function_name(): [
            binding.get_dict_repr().get("type") for binding in function.get_bindings()
        ]
        for function in indexed_functions
    }
    assert functions == {
        "process_order": ["httpTrigger", "http"],
        "process_order_event": ["queueTrigger"],
    }
    process_order = next(
        function
        for function in indexed_functions
        if function.get_function_name() == "process_order"
    ).get_user_function()
    assert process_order.__annotations__["req"] is Request
    assert process_order.__annotations__["return"] is Response


@pytest.mark.asyncio
async def test_ai_app_sample_sends_complete_order_as_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_sample(
        FUNCTION_SAMPLE_SRC,
        "hybrid_function_agent_prompt_sample",
        monkeypatch,
    )
    process_order = next(
        function
        for function in module.app.get_functions()
        if function.get_function_name() == "process_order"
    ).get_user_function()
    source_handler = inspect.unwrap(process_order)
    agent = SimpleNamespace(
        run=AsyncMock(return_value=SimpleNamespace(text="Order is ready."))
    )

    async def receive() -> dict[str, object]:
        return {
            "type": "http.request",
            "body": b'{"items":[{"sku":"A-100","quantity":2}]}',
            "more_body": False,
        }

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/orders/2",
            "path_params": {"orderId": "2"},
            "headers": [],
            "query_string": b"",
        },
        receive,
    )

    response = await source_handler(request, agent)

    [prompt] = agent.run.await_args.args
    assert isinstance(prompt, str)
    assert json.loads(prompt) == {
        "order_id": "2",
        "items": [{"sku": "A-100", "quantity": 2}],
        "task": "validate",
    }
    assert response.status_code == 200


def test_durable_ai_app_sample_indexes_durable_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_sample(
        DURABLE_SAMPLE_SRC,
        "hybrid_durable_agent_sample",
        monkeypatch,
    )

    assert isinstance(module.app, df.DFApp)
    indexed_functions = module.app.get_functions()
    functions = {
        function.get_function_name(): [
            binding.get_dict_repr().get("type") for binding in function.get_bindings()
        ]
        for function in indexed_functions
    }
    assert functions == {
        "start_order_orchestration": ["httpTrigger", "http", "durableClient"],
        "assess_order_activity": ["activityTrigger"],
        "_afa_agent_binding_run": ["activityTrigger"],
        "order_orchestrator": ["orchestrationTrigger"],
    }
    starter = next(
        function
        for function in indexed_functions
        if function.get_function_name() == "start_order_orchestration"
    ).get_user_function()
    assert starter.__annotations__["req"] is Request
    assert starter.__annotations__["client"] is str
    assert starter.__annotations__["return"] is Response
    activity = next(
        function
        for function in indexed_functions
        if function.get_function_name() == "assess_order_activity"
    ).get_user_function()
    assert activity.__annotations__["order"] is dict
