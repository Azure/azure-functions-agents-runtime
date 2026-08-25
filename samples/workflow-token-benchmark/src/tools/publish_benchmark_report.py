from __future__ import annotations

import os
from typing import Any

from _benchmark_core import build_canonical_report, canonical_json_bytes
from azure.storage.blob import BlobServiceClient, ContentSettings

from azure_functions_agents import tool, workflow_tool


@tool
@workflow_tool(
    description=(
        "Reduce complete service evidence and upload the canonical benchmark report. "
        "Args: {trial_id: str, report_blob: str, service_reports: [<whole "
        "inspect_service_evidence result>, ...]}. Preserve service order."
    )
)
def publish_benchmark_report(args: dict[str, Any]) -> dict[str, Any]:
    trial_id = args.get("trial_id")
    report_blob = args.get("report_blob")
    service_reports = args.get("service_reports")
    if not isinstance(trial_id, str) or not trial_id.strip():
        raise ValueError("trial_id must be a non-empty string")
    if not isinstance(report_blob, str) or not report_blob.strip():
        raise ValueError("report_blob must be a non-empty string")
    if not isinstance(service_reports, list) or not all(
        isinstance(report, dict) for report in service_reports
    ):
        raise ValueError("service_reports must be a list of whole inspection results")

    report = build_canonical_report(trial_id, service_reports)
    content = canonical_json_bytes(report)
    connection = os.environ.get("AzureWebJobsStorage")  # noqa: SIM112
    if not connection:
        raise RuntimeError("AzureWebJobsStorage is required")
    container_name = os.environ.get(
        "TOKEN_BENCHMARK_CONTAINER", "token-benchmark-reports"
    )
    container = BlobServiceClient.from_connection_string(
        connection
    ).get_container_client(container_name)
    try:
        container.create_container()
    except Exception as exc:
        if getattr(exc, "status_code", None) != 409:
            raise
    container.get_blob_client(report_blob).upload_blob(
        content,
        overwrite=True,
        content_settings=ContentSettings(content_type="application/json"),
    )
    return {
        "event": "token_benchmark_report_published",
        "trial_id": trial_id,
        "container": container_name,
        "blob_name": report_blob,
        "service_count": report["service_count"],
        "bytes": len(content),
    }
