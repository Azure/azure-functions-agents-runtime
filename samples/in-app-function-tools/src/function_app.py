import json
from datetime import date, timedelta

import azure.functions as func
from azurefunctions.extensions.http.fastapi import JSONResponse, Request
from pydantic import BaseModel, Field

from azure_functions_agents import create_function_app

app = create_function_app()

ORDERS: dict[str, dict[str, str]] = {
    "1001": {"customer": "Contoso", "status": "packed"},
    "1002": {"customer": "Fabrikam", "status": "pending"},
}


class OrderLookup(BaseModel):
    order_id: str = Field(description="Order ID, for example 1001")


class ShipOrder(BaseModel):
    order_id: str = Field(description="ID of the order to ship")
    carrier: str = Field(description="Carrier name, for example UPS")


# An ordinary HTTP Function that is also a tool. The agent supplies `schema` fields as the
# JSON body, and the runtime builds the Request and calls this handler in-process.
@app.tool(schema=OrderLookup)
@app.route(route="orders", methods=["POST"])
async def get_order(req: Request) -> JSONResponse:
    """Look up an order's customer and status by order ID."""
    order_id = (await req.json())["order_id"]
    order = ORDERS.get(order_id)
    if order is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"order_id": order_id, **order})


# A queue-triggered Function that is also a tool. Real queue messages still invoke it
# normally; the agent calls the same handler directly with the schema's fields as the body.
# Queue handlers return nothing, so the agent re-reads state with `get_order` to confirm.
@app.tool(schema=ShipOrder)
@app.queue_trigger(arg_name="msg", queue_name="ship-orders", connection="AzureWebJobsStorage")
def ship_order(msg: func.QueueMessage) -> None:
    """Mark an order as shipped with the given carrier."""
    body = json.loads(msg.get_body())
    order = ORDERS.get(body["order_id"])
    if order is None:
        raise ValueError(f"Order {body['order_id']} not found")
    order["status"] = f"shipped via {body['carrier']}"


# A standalone tool: the schema comes from the type hints.
@app.tool(agents=["main"])
def business_days_between(start: str, end: str) -> int:
    """Count weekdays from start (inclusive) to end (exclusive), using YYYY-MM-DD dates."""
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)
    if end_date < start_date:
        raise ValueError("end must be on or after start")
    days = (end_date - start_date).days
    return sum(1 for i in range(days) if (start_date + timedelta(days=i)).weekday() < 5)
