from __future__ import annotations

import importlib.util
import inspect
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

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
    monkeypatch.syspath_prepend(str(sample_src))
    sys.modules.pop("order_processing", None)
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
async def test_ai_app_sample_preprocesses_order_before_agent_handoff(
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
            "body": json.dumps(
                {
                    "customer": {
                        "id": "C-42",
                        "email": "buyer@example.com",
                        "name": "Example Buyer",
                        "loyalty_tier": "gold",
                    },
                    "currency": "usd",
                    "shipping": {"country": "ca", "method": "overnight"},
                    "items": [
                        {"sku": " a-100 ", "quantity": 2, "unit_price": "24.95"},
                        {"sku": "b-200", "quantity": 30, "unit_price": "40.00"},
                    ],
                }
            ).encode(),
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
        "order": {
            "order_id": "2",
            "currency": "USD",
            "customer": {"id": "C-42", "loyalty_tier": "gold"},
            "shipping": {"country": "CA", "method": "overnight"},
            "items": [
                {
                    "sku": "A-100",
                    "quantity": 2,
                    "unit_price": "24.95",
                    "line_total": "49.90",
                },
                {
                    "sku": "B-200",
                    "quantity": 30,
                    "unit_price": "40.00",
                    "line_total": "1200.00",
                },
            ],
            "summary": {
                "line_items": 2,
                "total_quantity": 32,
                "subtotal": "1249.90",
            },
            "review_signals": [
                "high_value_order",
                "bulk_quantity",
                "expedited_shipping",
                "international_shipping",
            ],
        },
        "task": "assess fulfillment readiness using the trusted calculated fields",
    }
    assert "buyer@example.com" not in prompt
    assert "Example Buyer" not in prompt
    assert response.status_code == 200


def test_durable_ai_app_sample_indexes_explicit_agent_activities(
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
        "prepare_order_activity": ["activityTrigger"],
        "assess_order_activity": ["activityTrigger"],
        "plan_fulfillment_activity": ["activityTrigger"],
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


@pytest.mark.asyncio
async def test_durable_sample_keeps_agent_transcripts_out_of_activity_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_sample(
        DURABLE_SAMPLE_SRC,
        "hybrid_durable_agent_compact_results_sample",
        monkeypatch,
    )
    handlers = {
        function.get_function_name(): inspect.unwrap(function.get_user_function())
        for function in module.app.get_functions()
    }
    response = SimpleNamespace(
        text="Review required",
        messages=[{"role": "assistant", "text": "sensitive transcript"}],
        response_id="response-123",
        usage_details={"input_token_count": 100, "output_token_count": 20},
    )
    agent = SimpleNamespace(run=AsyncMock(return_value=response))

    assessment = await handlers["assess_order_activity"]({"order_id": "D-2048"}, agent)
    plan = await handlers["plan_fulfillment_activity"](
        {"order": {"order_id": "D-2048"}, "risk_assessment": assessment},
        agent,
    )

    assert assessment == "Review required"
    assert plan == {"text": "Review required"}
    assert "sensitive transcript" not in json.dumps([assessment, plan])
    assert [json.loads(awaited.args[0]) for awaited in agent.run.await_args_list] == [
        {
            "order": {"order_id": "D-2048"},
            "task": "assess fulfillment risk using the trusted calculated fields",
        },
        {
            "order": {"order_id": "D-2048"},
            "risk_assessment": "Review required",
            "task": "create a fulfillment plan with prioritized human-review actions",
        },
    ]


def test_durable_sample_orchestrator_owns_agent_activity_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_sample(
        DURABLE_SAMPLE_SRC,
        "hybrid_durable_agent_contract_sample",
        monkeypatch,
    )
    orchestrator = next(
        function
        for function in module.app.get_functions()
        if function.get_function_name() == "order_orchestrator"
    ).get_user_function()
    source_orchestrator = orchestrator.orchestrator_function
    context = Mock(spec=df.DurableOrchestrationContext)
    context.get_input.return_value = {"order_id": "D-2048"}
    context.call_activity.side_effect = ["prepare-task", "assess-task"]
    context.call_activity_with_retry.return_value = "plan-task"

    generator = source_orchestrator(context)
    assert next(generator) == "prepare-task"
    assert generator.send({"order_id": "D-2048", "summary": {}}) == "assess-task"
    assert generator.send("Review required") == "plan-task"
    with pytest.raises(StopIteration) as completed:
        generator.send({"text": "Route to a fulfillment specialist."})

    context.call_activity_with_retry.assert_called_once()
    activity_name, retry_options, payload = context.call_activity_with_retry.call_args.args
    assert activity_name == "plan_fulfillment_activity"
    assert retry_options.to_json() == {
        "firstRetryIntervalInMilliseconds": 5_000,
        "maxNumberOfAttempts": 3,
    }
    assert payload == {
        "order": {"order_id": "D-2048", "summary": {}},
        "risk_assessment": "Review required",
    }
    assert completed.value.value == {
        "order_id": "D-2048",
        "risk_assessment": "Review required",
        "fulfillment_plan": "Route to a fulfillment specialist.",
    }
