"""Workflow-only Blob publisher for the Workflow Sub Agents preview."""

from __future__ import annotations

import os
from html.parser import HTMLParser
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


class _ReportParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.started: set[str] = set()
        self.ended: set[str] = set()
        self.links: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.started.add(tag)
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.add(href)

    def handle_endtag(self, tag: str) -> None:
        self.ended.add(tag)


def _validate_report(html_document: str, urls: Any) -> None:
    if not isinstance(urls, list) or not urls or any(
        not isinstance(url, str) or not url.strip() for url in urls
    ):
        raise ValueError("pull_request_urls must be a non-empty list of requested PR URLs")
    document = html_document.lower()
    if not document.startswith(("<!doctype html", "<html")) or not document.endswith("</html>"):
        raise ValueError("html must be a complete HTML document without a Markdown code fence")
    parser = _ReportParser()
    parser.feed(html_document)
    parser.close()
    if not {"html", "body"} <= parser.started & parser.ended:
        raise ValueError("html must contain complete HTML and body elements")
    for url in urls:
        if url not in parser.links:
            raise ValueError(f"HTML report is missing the requested pull-request link: {url}")


@workflow_tool(
    description=(
        "Validate and upload a complete PR status HTML document to Azure Blob Storage. "
        "Args: {html: str, blob_name: str, pull_request_urls: [str, ...]}. "
        "Pass every PR URL from the queue request in pull_request_urls. "
        "Rejects incomplete HTML and missing PR links before upload. "
        "Uses AzureWebJobsStorage and the optional PR_STATUS_REPORT_CONTAINER setting. "
        "Returns Blob metadata."
    )
)
def publish_pr_status_report(args: dict[str, Any]) -> dict[str, Any]:
    html_document = _require_string(args, "html")
    blob_name = _require_string(args, "blob_name")
    _validate_report(html_document, args.get("pull_request_urls"))
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
