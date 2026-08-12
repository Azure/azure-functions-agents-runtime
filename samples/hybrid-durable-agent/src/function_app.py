import json
from typing import cast

import azure.durable_functions as df
from agent_framework import Agent
from azurefunctions.extensions.http.fastapi import Request, Response

from azure_functions_agents import DurableAiAgent, DurableAiApp

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
@app.agent_input(
    arg_name="order_agent",
    agent_name="order-fulfillment",
    mode="activity",
)
async def assess_order_activity(order: dict, order_agent: Agent) -> str:
    response = await order_agent.run(
        json.dumps({"order": order, "task": "assess risk"})
    )
    return response.text


@app.orchestration_trigger(context_name="context")
@app.agent_input(
    arg_name="planner",
    agent_name="order-fulfillment",
    mode="orchestrator",
)
def order_orchestrator(
    context: df.DurableOrchestrationContext,
    planner: DurableAiAgent,
):
    plan = yield planner.run(
        json.dumps({"order": context.get_input(), "task": "create a plan"})
    )
    return plan["text"]
