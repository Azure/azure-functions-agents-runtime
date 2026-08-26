"""Workflow-safe tools for the resilient order-recovery story."""

import json
import os
from contextlib import suppress
from hashlib import sha256
from typing import Any

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError
from azure.storage.blob import BlobClient, BlobServiceClient

from azure_functions_agents import (
    WorkflowRetryableError,
    WorkflowRetryBackoff,
    WorkflowRetryPolicy,
    current_workflow_task_context,
    workflow_tool,
)

_ORDER_ID = "ORD-1001"
_CONTAINER = "workflow-retry-policy"
_RETRY_POLICY = WorkflowRetryPolicy(
    max_attempts=3,
    backoff=WorkflowRetryBackoff(initial="PT0S", multiplier=1.0, max="PT0S"),
)


def _incident_blob(incident_id: str) -> BlobClient:
    if len(incident_id) != 32 or any(char not in "0123456789abcdef" for char in incident_id):
        raise ValueError("inventory incident id is invalid")
    connection_string = os.environ.get("AzureWebJobsStorage")  # noqa: SIM112
    if not connection_string:
        raise ValueError("AzureWebJobsStorage must be configured")
    service = BlobServiceClient.from_connection_string(connection_string)
    container = service.get_container_client(_CONTAINER)
    with suppress(ResourceExistsError):
        container.create_container()
    return container.get_blob_client(f"orders/{_ORDER_ID}/incidents/{incident_id}.json")


def _write_incident(
    incident_id: str,
    state: dict[str, Any],
    *,
    etag: str | None = None,
) -> None:
    options: dict[str, Any] = {"overwrite": True}
    if etag is not None:
        options.update(etag=etag, match_condition=MatchConditions.IfNotModified)
    _incident_blob(incident_id).upload_blob(json.dumps(state), **options)


def _create_incident(incident_id: str, state: dict[str, Any]) -> dict[str, Any]:
    try:
        _incident_blob(incident_id).upload_blob(json.dumps(state), overwrite=False)
    except ResourceExistsError:
        existing, _ = _read_incident(incident_id)
        return existing
    return state


def _read_incident(incident_id: str) -> tuple[dict[str, Any], str]:
    download = _incident_blob(incident_id).download_blob()
    raw = download.readall()
    state = json.loads(raw)
    if (
        not isinstance(state, dict)
        or state.get("order_id") != _ORDER_ID
        or not isinstance(state.get("failures_remaining"), int)
        or not isinstance(state.get("failed_attempts"), list)
        or not all(type(attempt) is int for attempt in state["failed_attempts"])
    ):
        raise ValueError("inventory incident state is invalid")
    etag = download.properties.get("etag")
    if not isinstance(etag, str):
        raise ValueError("inventory incident state has no entity tag")
    return state, etag


@workflow_tool(
    description=(
        "Open a simulated inventory-service incident for delayed order ORD-1001. "
        "The incident state is stored in Azure Blob Storage and causes the next "
        "two reservation calls to fail transiently. Args: {order_id: str}."
    ),
    retry=WorkflowRetryPolicy(max_attempts=1),
)
def open_inventory_incident(args: dict[str, Any]) -> dict[str, Any]:
    order_id = args.get("order_id")
    if order_id != _ORDER_ID:
        raise ValueError(f"open_inventory_incident: only {_ORDER_ID!r} is available")
    context = current_workflow_task_context()
    if context is None:
        raise RuntimeError("open_inventory_incident must run as a policy-aware workflow task")
    incident_id = sha256(context.workflow_id.encode()).hexdigest()[:32]
    incident = _create_incident(incident_id, {
        "order_id": _ORDER_ID,
        "failures_remaining": 2,
        "failed_attempts": [],
        "status": "active",
    })
    return {
        "order_id": _ORDER_ID,
        "incident_id": incident_id,
        "status": incident["status"],
        "failures_remaining": incident["failures_remaining"],
    }


@workflow_tool(
    description=(
        "Load the delayed sample order. "
        "Args: {order_id: str, incident: <open_inventory_incident result>}. "
        "Returns {order_id, incident_id, sku, quantity, status}."
    )
)
def load_order(args: dict[str, Any]) -> dict[str, Any]:
    order_id = args.get("order_id")
    if order_id != _ORDER_ID:
        raise ValueError(f"load_order: only sample order {_ORDER_ID!r} is available")
    incident = args.get("incident")
    if not isinstance(incident, dict) or not isinstance(incident.get("incident_id"), str):
        raise ValueError("load_order: 'incident' must be the complete incident result")
    return {
        "order_id": _ORDER_ID,
        "incident_id": incident["incident_id"],
        "sku": "trail-shoes-blue-42",
        "quantity": 1,
        "status": "awaiting_inventory",
    }


@workflow_tool(
    description=(
        "Reserve inventory for a loaded order. Args: {order: <load_order result>}. "
        "Reads the simulated inventory incident from Azure Blob Storage. "
        "Returns {order_id, sku, reserved, transient_failures_observed}."
    ),
    timeout="PT5S",
    retry=_RETRY_POLICY,
)
def reserve_inventory(args: dict[str, Any]) -> dict[str, Any]:
    order = args.get("order")
    if not isinstance(order, dict) or order.get("order_id") != _ORDER_ID:
        raise ValueError("reserve_inventory: 'order' must be the complete load_order result")

    incident_id = order.get("incident_id")
    if not isinstance(incident_id, str):
        raise ValueError("reserve_inventory: order is missing its incident id")
    context = current_workflow_task_context()
    if context is None:
        raise RuntimeError("reserve_inventory must run as a policy-aware workflow task")
    for _ in range(5):
        incident, etag = _read_incident(incident_id)
        if context.attempt in incident["failed_attempts"]:
            raise WorkflowRetryableError(
                "inventory_temporarily_unavailable",
                "Inventory reservation is temporarily unavailable.",
            )
        if incident["failures_remaining"] > 0:
            incident["failures_remaining"] -= 1
            incident["failed_attempts"].append(context.attempt)
            try:
                _write_incident(incident_id, incident, etag=etag)
            except ResourceModifiedError:
                continue
            raise WorkflowRetryableError(
                "inventory_temporarily_unavailable",
                "Inventory reservation is temporarily unavailable.",
            )
        if incident["status"] != "recovered":
            incident["status"] = "recovered"
            try:
                _write_incident(incident_id, incident, etag=etag)
            except ResourceModifiedError:
                continue
        break
    else:
        raise WorkflowRetryableError(
            "inventory_state_conflict",
            "Inventory incident state is changing concurrently.",
        )
    return {
        "order_id": _ORDER_ID,
        "sku": order["sku"],
        "reserved": True,
        "transient_failures_observed": 2,
    }


@workflow_tool(
    description=(
        "Confirm an order after inventory succeeds. "
        "Args: {reservation: <reserve_inventory result>}. "
        "Returns {order_id, status, transient_failures_observed}."
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
        "transient_failures_observed": reservation["transient_failures_observed"],
    }


__all__ = [
    "confirm_order",
    "load_order",
    "open_inventory_incident",
    "reserve_inventory",
]
