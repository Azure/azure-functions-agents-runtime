"""Workflow Activities for the queue-triggered P0 portfolio report sample."""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
from typing import Any

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobServiceClient, ContentSettings

from azure_functions_agents import workflow_tool

logger = logging.getLogger(__name__)

_DEFAULT_CONTAINER = "workflow-reports"


def _require_string(args: dict[str, Any], name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


@workflow_tool(
    description=(
        "Return deterministic synthetic P0 issues for one repository. "
        "Args: {repository: 'owner/name'}. Returns {repository, issues: "
        "[{number, title, owner, age_hours, url}], p0_count}. Independent calls "
        "for different repositories should run in parallel."
    )
)
def inspect_repository_p0_issues(args: dict[str, Any]) -> dict[str, Any]:
    repository = _require_string(args, "repository")
    digest = hashlib.sha256(repository.encode("utf-8")).digest()
    issue_count = digest[0] % 3
    topics = ("availability regression", "data-loss risk", "security hotfix")
    owners = ("runtime-oncall", "storage-oncall", "security-response")
    issues = [
        {
            "number": 1000 + digest[index + 1],
            "title": f"P0: {topics[digest[index + 4] % len(topics)]}",
            "owner": owners[digest[index + 7] % len(owners)],
            "age_hours": 1 + digest[index + 10] % 48,
            "url": (
                f"https://github.com/{repository}/issues/"
                f"{1000 + digest[index + 1]}"
            ),
        }
        for index in range(issue_count)
    ]
    return {
        "repository": repository,
        "issues": issues,
        "p0_count": len(issues),
    }


@workflow_tool(
    description=(
        "Render repository P0 inspection results as a complete HTML document. "
        "Args: {repository_reports: [<whole inspect_repository_p0_issues result>, ...]}. "
        "Returns {html, repository_count, p0_count}. Preserve the input list order."
    )
)
def render_p0_html_report(args: dict[str, Any]) -> dict[str, Any]:
    reports = args.get("repository_reports")
    if not isinstance(reports, list) or not reports:
        raise ValueError("repository_reports must be a non-empty list")
    if not all(isinstance(report, dict) for report in reports):
        raise ValueError("repository_reports entries must be whole inspection results")

    sections: list[str] = []
    total = 0
    for report in reports:
        repository = html.escape(str(report.get("repository", "unknown")))
        issues = report.get("issues")
        if not isinstance(issues, list):
            raise ValueError(f"issues for {repository} must be a list")
        total += len(issues)
        if issues:
            rows = "".join(
                "<li>"
                f"<a href=\"{html.escape(str(issue.get('url', '')))}\">"
                f"#{html.escape(str(issue.get('number', '')))}</a> "
                f"{html.escape(str(issue.get('title', '')))} "
                f"(owner: {html.escape(str(issue.get('owner', 'unassigned')))}, "
                f"age: {html.escape(str(issue.get('age_hours', '?')))}h)"
                "</li>"
                for issue in issues
                if isinstance(issue, dict)
            )
            issue_markup = f"<ul>{rows}</ul>"
        else:
            issue_markup = "<p>No open P0 issues.</p>"
        sections.append(f"<section><h2>{repository}</h2>{issue_markup}</section>")

    document = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<title>P0 issue portfolio report</title>"
        "<style>body{font-family:sans-serif;max-width:960px;margin:2rem auto}"
        "section{border-top:1px solid #ddd;padding:1rem 0}</style></head><body>"
        f"<h1>P0 issue portfolio report</h1><p>Open P0 issues: {total}</p>"
        f"{''.join(sections)}</body></html>"
    )
    return {
        "html": document,
        "repository_count": len(reports),
        "p0_count": total,
    }


@workflow_tool(
    description=(
        "Upload the rendered HTML report to Azure Blob Storage as the terminal sink. "
        "Args: {report: <whole render_p0_html_report result>, blob_name: str}. "
        "Uses AzureWebJobsStorage and P0_REPORT_CONTAINER. Returns blob metadata."
    )
)
def publish_p0_html_report(args: dict[str, Any]) -> dict[str, Any]:
    report = args.get("report")
    if not isinstance(report, dict):
        raise ValueError("report must be the whole render_p0_html_report result")
    html_document = report.get("html")
    if not isinstance(html_document, str) or not html_document:
        raise ValueError("report.html must be a non-empty string")
    blob_name = _require_string(args, "blob_name")

    connection_string = os.environ.get("AzureWebJobsStorage")  # noqa: SIM112
    if not connection_string:
        raise ValueError("AzureWebJobsStorage must be configured")
    container_name = os.environ.get("P0_REPORT_CONTAINER", _DEFAULT_CONTAINER)

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

    result = {
        "event": "p0_report_published",
        "container": container_name,
        "blob_name": blob_name,
        "repository_count": report.get("repository_count"),
        "p0_count": report.get("p0_count"),
    }
    logger.warning("P0_REPORT_PUBLISHED %s", json.dumps(result))
    return result
