import json
from typing import cast

import azure.durable_functions as df
from agent_framework import Agent
from azurefunctions.extensions.http.fastapi import Request, Response
from order_processing import prepare_order_for_agent

from azure_functions_agents import DurableAiApp

app = DurableAiApp()


@app.durable_client_input(client_name="client")
@app.route(route="orders/orchestrations", methods=["POST"])
async def start_order_orchestration(
    req: Request,
    client: str,
) -> Response:
    durable_client = cast(df.DurableOrchestrationClient, client)
    instance_id = await durable_client.start_new(
        "order_orchestrator",
        client_input=await req.json(),
    )
    management = durable_client.create_http_management_payload(instance_id)
    return Response(
        content=json.dumps(management),
        status_code=202,
        media_type="application/json",
        headers={
            "Location": management["statusQueryGetUri"],
            "Retry-After": "10",
        },
    )


@app.activity_trigger(input_name="order")
def prepare_order_activity(order: dict) -> dict[str, object]:
    return prepare_order_for_agent(order)


@app.activity_trigger(input_name="order")
@app.agent(
    arg_name="order_agent",
    agent_name="order-fulfillment",
)
async def assess_order_activity(order: dict, order_agent: Agent) -> str:
    response = await order_agent.run(
        json.dumps(
            {
                "order": order,
                "task": "assess fulfillment risk using the trusted calculated fields",
            }
        )
    )
    return response.text


@app.activity_trigger(input_name="request")
@app.agent(
    arg_name="order_agent",
    agent_name="order-fulfillment",
)
async def plan_fulfillment_activity(
    request: dict,
    order_agent: Agent,
) -> dict[str, str]:
    response = await order_agent.run(
        json.dumps(
            {
                "order": request["order"],
                "risk_assessment": request["risk_assessment"],
                "task": "create a fulfillment plan with prioritized human-review actions",
            }
        )
    )
    return {"text": response.text}


@app.orchestration_trigger(context_name="context")
def order_orchestrator(
    context: df.DurableOrchestrationContext,
):
    prepared_order = yield context.call_activity(
        "prepare_order_activity",
        context.get_input(),
    )
    assessment = yield context.call_activity(
        "assess_order_activity",
        prepared_order,
    )
    plan = yield context.call_activity_with_retry(
        "plan_fulfillment_activity",
        df.RetryOptions(
            first_retry_interval_in_milliseconds=5_000,
            max_number_of_attempts=3,
        ),
        {
            "order": prepared_order,
            "risk_assessment": assessment,
        },
    )
    return {
        "order_id": prepared_order["order_id"],
        "risk_assessment": assessment,
        "fulfillment_plan": plan["text"],
    }
