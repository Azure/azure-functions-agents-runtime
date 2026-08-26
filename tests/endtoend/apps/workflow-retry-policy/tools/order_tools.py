"""Deterministic tools for retry-policy end-to-end validation."""

from typing import Any

from azure_functions_agents import (
    WorkflowRetryableError,
    WorkflowRetryBackoff,
    WorkflowRetryPolicy,
    current_workflow_task_context,
    workflow_tool,
)

_ORDER_ID = "ORD-1001"


@workflow_tool(description="Load order ORD-1001. Args: {order_id: str}.")
def load_order(args: dict[str, Any]) -> dict[str, Any]:
    if args.get("order_id") != _ORDER_ID:
        raise ValueError(f"only {_ORDER_ID!r} is available")
    return {"order_id": _ORDER_ID, "sku": "trail-shoes-blue-42"}


@workflow_tool(
    description="Reserve inventory for a loaded order.",
    timeout="PT5S",
    retry=WorkflowRetryPolicy(
        max_attempts=3,
        backoff=WorkflowRetryBackoff(initial="PT0S", multiplier=1.0, max="PT0S"),
    ),
)
def reserve_inventory(args: dict[str, Any]) -> dict[str, Any]:
    order = args.get("order")
    if not isinstance(order, dict) or order.get("order_id") != _ORDER_ID:
        raise ValueError("complete load_order result is required")
    context = current_workflow_task_context()
    if context is None:
        raise RuntimeError("reserve_inventory must run as a policy-aware workflow task")
    if context.attempt < 3:
        raise WorkflowRetryableError(
            "inventory_temporarily_unavailable",
            "Inventory reservation is temporarily unavailable.",
        )
    return {"order_id": _ORDER_ID, "sku": order["sku"], "reserved": True}


@workflow_tool(description="Confirm an order after inventory reservation.")
def confirm_order(args: dict[str, Any]) -> dict[str, Any]:
    reservation = args.get("reservation")
    if not isinstance(reservation, dict) or not reservation.get("reserved"):
        raise ValueError("complete reserve_inventory result is required")
    return {"order_id": reservation["order_id"], "status": "confirmed"}
