"""Workflow-safe tools for the resilient order-recovery story."""

from typing import Any

from azure_functions_agents import (
    WorkflowRetryableError,
    WorkflowRetryBackoff,
    WorkflowRetryPolicy,
    current_workflow_task_context,
    workflow_tool,
)

_ORDER_ID = "ORD-1001"
_RETRY_POLICY = WorkflowRetryPolicy(
    max_attempts=3,
    backoff=WorkflowRetryBackoff(initial="PT0S", multiplier=1.0, max="PT0S"),
)


@workflow_tool(
    description=(
        "Load the delayed sample order. Args: {order_id: str}. "
        "Returns {order_id, sku, quantity, status}."
    )
)
def load_order(args: dict[str, Any]) -> dict[str, Any]:
    order_id = args.get("order_id")
    if order_id != _ORDER_ID:
        raise ValueError(f"load_order: only sample order {_ORDER_ID!r} is available")
    return {
        "order_id": _ORDER_ID,
        "sku": "trail-shoes-blue-42",
        "quantity": 1,
        "status": "awaiting_inventory",
    }


@workflow_tool(
    description=(
        "Reserve inventory for a loaded order. Args: {order: <load_order result>}. "
        "The sample inventory service is transiently unavailable twice, then succeeds. "
        "Returns {order_id, sku, reserved, attempt, idempotency_key}."
    ),
    timeout="PT5S",
    retry=_RETRY_POLICY,
)
def reserve_inventory(args: dict[str, Any]) -> dict[str, Any]:
    order = args.get("order")
    if not isinstance(order, dict) or order.get("order_id") != _ORDER_ID:
        raise ValueError("reserve_inventory: 'order' must be the complete load_order result")

    context = current_workflow_task_context()
    if context is None:
        raise RuntimeError("reserve_inventory must run as a policy-aware workflow task")
    if context.attempt < 3:
        raise WorkflowRetryableError(
            "inventory_temporarily_unavailable",
            f"Inventory reservation is temporarily unavailable on attempt {context.attempt}.",
        )

    return {
        "order_id": _ORDER_ID,
        "sku": order["sku"],
        "reserved": True,
        "attempt": context.attempt,
        "idempotency_key": context.idempotency_key,
    }


@workflow_tool(
    description=(
        "Confirm an order after inventory succeeds. "
        "Args: {reservation: <reserve_inventory result>}. "
        "Returns {order_id, status, reservation_attempt}."
    )
)
def confirm_order(args: dict[str, Any]) -> dict[str, Any]:
    reservation = args.get("reservation")
    if not isinstance(reservation, dict) or not reservation.get("reserved"):
        raise ValueError(
            "confirm_order: 'reservation' must be the complete successful reservation result"
        )
    return {
        "order_id": reservation["order_id"],
        "status": "confirmed",
        "reservation_attempt": reservation["attempt"],
    }


__all__ = ["confirm_order", "load_order", "reserve_inventory"]
