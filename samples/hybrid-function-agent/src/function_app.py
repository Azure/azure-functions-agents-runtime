import json

import azure.functions as func
from agent_framework import Agent
from azurefunctions.extensions.http.fastapi import Request, Response

from azure_functions_agents import AiApp

app = AiApp()


@app.route(route="orders/{orderId}", methods=["POST"])
@app.agent_input(arg_name="order_agent", agent_name="order-fulfillment")
async def process_order(
    req: Request,
    order_agent: Agent,
) -> Response:
    order_id = req.path_params["orderId"]
    order = await req.json()
    if not isinstance(order, dict) or "items" not in order:
        return Response(
            content="Request body must contain an items array.",
            status_code=400,
        )

    response = await order_agent.run(
        json.dumps({"order_id": order_id, "items": order["items"], "task": "validate"})
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
@app.agent_input(arg_name="order_agent", agent_name="order-fulfillment")
async def process_order_event(
    message: func.QueueMessage,
    order_agent: Agent,
) -> None:
    await order_agent.run(
        json.dumps(
            {
                "event": json.loads(message.get_body().decode("utf-8")),
                "task": "triage",
            }
        )
    )
