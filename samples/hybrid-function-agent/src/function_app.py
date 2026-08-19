import json

import azure.functions as func
from agent_framework import Agent
from azurefunctions.extensions.http.fastapi import Request, Response
from order_processing import prepare_order_for_agent
from pydantic import ValidationError

from azure_functions_agents import AiApp

app = AiApp()


@app.route(route="orders/{orderId}", methods=["POST"])
@app.agent(arg_name="order_agent", agent_name="order-fulfillment")
async def process_order(
    req: Request,
    order_agent: Agent,
) -> Response:
    order_id = req.path_params["orderId"]
    order = await req.json()
    try:
        prepared_order = prepare_order_for_agent(order, order_id=order_id)
    except (ValidationError, ValueError):
        return Response(
            content=json.dumps({"error": "Order failed validation."}),
            status_code=400,
            media_type="application/json",
        )

    response = await order_agent.run(
        json.dumps(
            {
                "order": prepared_order,
                "task": "assess fulfillment readiness using the trusted calculated fields",
            }
        )
    )
    return Response(
        content=json.dumps({"order_id": order_id, "assessment": response.text}),
        media_type="application/json",
    )


@app.queue_trigger(
    arg_name="message",
    queue_name="orders",
    connection="AzureWebJobsStorage",
)
@app.agent(arg_name="order_agent", agent_name="order-fulfillment")
async def process_order_event(
    message: func.QueueMessage,
    order_agent: Agent,
) -> None:
    event = json.loads(message.get_body().decode("utf-8"))
    prepared_order = prepare_order_for_agent(event)
    await order_agent.run(
        json.dumps(
            {
                "order": prepared_order,
                "task": "triage fulfillment exceptions using the trusted calculated fields",
            }
        )
    )
