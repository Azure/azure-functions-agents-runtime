"""Workflow-safe tools for the resilient order-recovery story."""

import json
import os
import time
from contextlib import suppress
from hashlib import sha256
from typing import Any

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError
from azure.storage.blob import BlobClient, BlobServiceClient

from azure_functions_agents import (
    WorkflowRetryBackoff,
    WorkflowRetryPolicy,
    WorkflowRetryableError,
    WorkflowTerminalError,
    current_workflow_task_context,
    workflow_tool,
)

_ORDER_ID = "ORD-1001"
_CONTAINER = "workflow-retry-policy"


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


def _load_or_create_incident(incident_id: str) -> tuple[dict[str, Any], str]:
    initial = {
        "order_id": _ORDER_ID,
        "failures_remaining": 2,
        "transient_failures_observed": 0,
        "carrier_attempts": 0,
        "notice_attempts": 0,
        "status": "active",
    }
    try:
        _incident_blob(incident_id).upload_blob(json.dumps(initial), overwrite=False)
    except ResourceExistsError:
        pass
    return _read_incident(incident_id)


def _read_incident(incident_id: str) -> tuple[dict[str, Any], str]:
    download = _incident_blob(incident_id).download_blob()
    raw = download.readall()
    state = json.loads(raw)
    if (
        not isinstance(state, dict)
        or state.get("order_id") != _ORDER_ID
        or not isinstance(state.get("failures_remaining"), int)
        or not isinstance(state.get("transient_failures_observed"), int)
        or not isinstance(state.get("carrier_attempts"), int)
        or not isinstance(state.get("notice_attempts"), int)
    ):
        raise ValueError("inventory incident state is invalid")
    etag = download.properties.get("etag")
    if not isinstance(etag, str):
        raise ValueError("inventory incident state has no entity tag")
    return state, etag


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
        "Reads the simulated inventory incident from Azure Blob Storage. "
        "Returns {order_id, sku, reserved, transient_failures_observed}."
    ),
    retry=WorkflowRetryPolicy(
        max_attempts=3,
        backoff=WorkflowRetryBackoff(initial="PT1S", multiplier=2.0, max="PT4S"),
    ),
)
def reserve_inventory(args: dict[str, Any]) -> dict[str, Any]:
    order = args.get("order")
    if not isinstance(order, dict) or order.get("order_id") != _ORDER_ID:
        raise ValueError("reserve_inventory: 'order' must be the complete load_order result")

    context = current_workflow_task_context()
    if context is None:
        raise RuntimeError("reserve_inventory must run as a policy-aware workflow task")
    incident_id = sha256(context.workflow_id.encode()).hexdigest()[:32]
    for _ in range(5):
        incident, etag = _load_or_create_incident(incident_id)
        if incident["failures_remaining"] > 0:
            incident["failures_remaining"] -= 1
            incident["transient_failures_observed"] += 1
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
        "transient_failures_observed": incident["transient_failures_observed"],
    }


@workflow_tool(
    description=(
        "Verify the carrier for a reserved order. "
        "Args: {reservation: <reserve_inventory result>, always_timeout?: bool}. "
        "Returns {order_id, carrier, timeout_attempts_observed}."
    ),
    timeout="PT1S",
)
def verify_carrier(args: dict[str, Any]) -> dict[str, Any]:
    reservation = args.get("reservation")
    if not isinstance(reservation, dict) or not reservation.get("reserved"):
        raise ValueError(
            "verify_carrier: 'reservation' must be the complete successful reservation result"
        )
    always_timeout = args.get("always_timeout", False)
    if type(always_timeout) is not bool:
        raise ValueError("verify_carrier: 'always_timeout' must be a boolean")

    context = current_workflow_task_context()
    if context is None:
        raise RuntimeError("verify_carrier must run as a policy-aware workflow task")
    incident_id = sha256(context.workflow_id.encode()).hexdigest()[:32]
    for _ in range(5):
        incident, etag = _read_incident(incident_id)
        incident["carrier_attempts"] += 1
        attempt = incident["carrier_attempts"]
        try:
            _write_incident(incident_id, incident, etag=etag)
        except ResourceModifiedError:
            continue
        break
    else:
        raise WorkflowRetryableError(
            "carrier_state_conflict",
            "Carrier verification state is changing concurrently.",
        )

    if always_timeout or attempt == 1:
        time.sleep(2)
    return {
        "order_id": reservation["order_id"],
        "carrier": "Contoso Shipping",
        "timeout_attempts_observed": attempt - 1,
    }


@workflow_tool(
    description=(
        "Try to notify the customer after inventory succeeds. "
        "Args: {reservation: <reserve_inventory result>}. "
        "This sample reports a terminal notification failure."
    )
)
def notify_customer(args: dict[str, Any]) -> dict[str, Any]:
    reservation = args.get("reservation")
    if not isinstance(reservation, dict) or not reservation.get("reserved"):
        raise ValueError(
            "notify_customer: 'reservation' must be the complete successful "
            "reservation result"
        )

    context = current_workflow_task_context()
    if context is None:
        raise RuntimeError("notify_customer must run as a policy-aware workflow task")
    incident_id = sha256(context.workflow_id.encode()).hexdigest()[:32]
    for _ in range(5):
        incident, etag = _read_incident(incident_id)
        incident["notice_attempts"] += 1
        try:
            _write_incident(incident_id, incident, etag=etag)
        except ResourceModifiedError:
            continue
        break
    else:
        raise WorkflowRetryableError(
            "customer_notice_state_conflict",
            "Customer notification state is changing concurrently.",
        )
    raise WorkflowTerminalError(
        "customer_notice_unavailable",
        "Customer notification is unavailable.",
    )


@workflow_tool(
    description=(
        "Confirm an order after carrier verification and customer notification. "
        "Args: {carrier: <verify_carrier result or continued failure>, "
        "notice: <notify_customer continued failure>}. "
        "Returns {order_id, status, carrier, carrier_verification_failed, "
        "customer_notice_failed}."
    )
)
def confirm_order(args: dict[str, Any]) -> dict[str, Any]:
    carrier = args.get("carrier")
    notice = args.get("notice")
    if not isinstance(carrier, dict) or not isinstance(notice, dict):
        raise ValueError(
            "confirm_order: 'carrier' and 'notice' must be complete workflow results"
        )
    carrier_failed = carrier.get("failed") is True
    notice_failed = notice.get("failed") is True
    order_id = carrier.get("order_id", _ORDER_ID)
    return {
        "order_id": order_id,
        "status": "confirmed",
        "carrier": carrier.get("carrier", "manual verification required"),
        "timeout_attempts_observed": carrier.get("timeout_attempts_observed"),
        "carrier_verification_failed": carrier_failed,
        "customer_notice_failed": notice_failed,
    }


__all__ = [
    "confirm_order",
    "load_order",
    "notify_customer",
    "reserve_inventory",
    "verify_carrier",
]
