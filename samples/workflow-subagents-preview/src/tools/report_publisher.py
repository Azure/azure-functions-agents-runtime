"""Workflow-only Blob publisher for the Workflow Sub Agents preview."""

from __future__ import annotations

import os
from typing import Any

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobServiceClient, ContentSettings

from azure_functions_agents import workflow_tool

_DEFAULT_CONTAINER = "workflow-reports"


def _require_string(args: dict[str, Any], name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


@workflow_tool(
    description=(
        "Upload a generated PR status HTML document to Azure Blob Storage. "
        "Args: {html: str, blob_name: str}. Uses AzureWebJobsStorage and the "
        "optional PR_STATUS_REPORT_CONTAINER setting. Returns Blob metadata."
    )
)
def publish_pr_status_report(args: dict[str, Any]) -> dict[str, Any]:
    html_document = _require_string(args, "html")
    blob_name = _require_string(args, "blob_name")
    connection_string = os.environ.get("AzureWebJobsStorage")  # noqa: SIM112
    if not connection_string:
        raise ValueError("AzureWebJobsStorage must be configured")
    container_name = os.environ.get("PR_STATUS_REPORT_CONTAINER", _DEFAULT_CONTAINER)

    service = BlobServiceClient.from_connection_string(connection_string)
    container = service.get_container_client(container_name)
    try:
        container.create_container()
    except ResourceExistsError:
        pass

    blob = container.get_blob_client(blob_name)
    blob.upload_blob(
        html_document.encode("utf-8"),
        overwrite=True,
        content_settings=ContentSettings(content_type="text/html; charset=utf-8"),
    )
    return {
        "container": container_name,
        "blob_name": blob_name,
        "content_type": "text/html; charset=utf-8",
        "url": blob.url,
    }
