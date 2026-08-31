"""Deterministic tools for retry-policy end-to-end validation."""

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
_CONTAINER = "workflow-retry-policy-e2e"


def _incident_blob(workflow_id: str) -> BlobClient:
    connection_string = os.environ.get("AzureWebJobsStorage")  # noqa: SIM112
    if not connection_string:
        raise ValueError("AzureWebJobsStorage must be configured")
    service = BlobServiceClient.from_connection_string(connection_string)
    container = service.get_container_client(_CONTAINER)
    with suppress(ResourceExistsError):
        container.create_container()
    incident_id = sha256(workflow_id.encode()).hexdigest()[:32]
    return container.get_blob_client(f"{incident_id}.json")


def _read_or_create_incident(workflow_id: str) -> tuple[dict[str, Any], str]:
    blob = _incident_blob(workflow_id)
    initial = {"failures_remaining": 2, "transient_failures_observed": 0}
    with suppress(ResourceExistsError):
        blob.upload_blob(json.dumps(initial), overwrite=False)
    download = blob.download_blob()
    state = json.loads(download.readall())
    etag = download.properties.get("etag")
    if (
        not isinstance(state, dict)
        or not isinstance(state.get("failures_remaining"), int)
        or not isinstance(state.get("transient_failures_observed"), int)
        or not isinstance(etag, str)
    ):
        raise ValueError("retry E2E state is invalid")
    return state, etag


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
        backoff=WorkflowRetryBackoff(initial="PT0.1S", multiplier=1.0, max="PT0.1S"),
    ),
)
def reserve_inventory(args: dict[str, Any]) -> dict[str, Any]:
    order = args.get("order")
    if not isinstance(order, dict) or order.get("order_id") != _ORDER_ID:
        raise ValueError("complete load_order result is required")
    context = current_workflow_task_context()
    if context is None:
        raise RuntimeError("reserve_inventory must run as a policy-aware workflow task")
    for _ in range(5):
        state, etag = _read_or_create_incident(context.workflow_id)
        if state["failures_remaining"] == 0:
            return {
                "order_id": _ORDER_ID,
                "sku": order["sku"],
                "reserved": True,
                "transient_failures_observed": state["transient_failures_observed"],
            }
        state["failures_remaining"] -= 1
        state["transient_failures_observed"] += 1
        try:
            _incident_blob(context.workflow_id).upload_blob(
                json.dumps(state),
                overwrite=True,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except ResourceModifiedError:
            continue
        raise WorkflowRetryableError(
            "inventory_temporarily_unavailable",
            "Inventory reservation is temporarily unavailable.",
        )
    raise WorkflowRetryableError(
        "inventory_state_conflict",
        "Inventory incident state is changing concurrently.",
    )


@workflow_tool(
    description="E2E-only Activity that always returns a sanitized retryable failure.",
    timeout="PT5S",
    retry=WorkflowRetryPolicy(
        max_attempts=3,
        backoff=WorkflowRetryBackoff(initial="PT0.1S", multiplier=1.0, max="PT0.1S"),
    ),
)
def always_fail_inventory(args: dict[str, Any]) -> None:
    raise WorkflowRetryableError(
        "inventory_retry_exhausted",
        "Inventory remains temporarily unavailable.",
    )


@workflow_tool(description="Confirm an order after inventory reservation.")
def confirm_order(args: dict[str, Any]) -> dict[str, Any]:
    reservation = args.get("reservation")
    if not isinstance(reservation, dict) or not reservation.get("reserved"):
        raise ValueError("complete reserve_inventory result is required")
    return {
        "order_id": reservation["order_id"],
        "status": "confirmed",
        "transient_failures_observed": reservation["transient_failures_observed"],
    }
