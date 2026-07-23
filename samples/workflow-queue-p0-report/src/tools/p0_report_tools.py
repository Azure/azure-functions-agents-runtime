"""Workflow tools for the queue-triggered P0 portfolio report sample."""

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
    healthy_repositories = 0
    for report in reports:
        repository = html.escape(str(report.get("repository", "unknown")))
        issues = report.get("issues")
        if not isinstance(issues, list):
            raise ValueError(f"issues for {repository} must be a list")
        valid_issues = [issue for issue in issues if isinstance(issue, dict)]
        total += len(valid_issues)
        if valid_issues:
            rows = "".join(
                '<article class="issue">'
                '<span class="priority">P0</span>'
                '<div class="issue-content">'
                f'<a class="issue-title" href="{html.escape(str(issue.get("url", "")))}">'
                f'{html.escape(str(issue.get("title", "Untitled issue")))}</a>'
                '<div class="issue-meta">'
                f'<span>#{html.escape(str(issue.get("number", "")))}</span>'
                f'<span>Owner: {html.escape(str(issue.get("owner", "unassigned")))}</span>'
                f'<span>Age: {html.escape(str(issue.get("age_hours", "?")))}h</span>'
                "</div></div></article>"
                for issue in valid_issues
            )
            issue_markup = f'<div class="issue-list">{rows}</div>'
            status_class = "status-alert"
            status_text = f"{len(valid_issues)} open"
        else:
            healthy_repositories += 1
            issue_markup = (
                '<div class="empty-state">'
                '<span class="checkmark" aria-hidden="true">&#10003;</span>'
                "<div><strong>No open P0 issues</strong>"
                "<p>This repository is clear of critical incidents.</p></div></div>"
            )
            status_class = "status-clear"
            status_text = "Healthy"
        sections.append(
            '<section class="repository-card">'
            '<div class="repository-header">'
            f"<h2>{repository}</h2>"
            f'<span class="repo-status {status_class}">{status_text}</span>'
            f"</div>{issue_markup}</section>"
        )

    overall_status = "Attention required" if total else "All systems clear"
    overall_class = "overall-alert" if total else "overall-clear"
    document = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>P0 issue portfolio report</title>"
        "<style>"
        ":root{color-scheme:light;--ink:#172033;--muted:#65718b;--line:#e4e9f2;"
        "--surface:#fff;--canvas:#f4f7fb;--accent:#5b5bd6;--danger:#c9364f;"
        "--danger-soft:#fff0f2;--success:#16845b;--success-soft:#e9f8f1}"
        "*{box-sizing:border-box}body{margin:0;background:var(--canvas);color:var(--ink);"
        "font:15px/1.5 Inter,Segoe UI,system-ui,sans-serif}"
        ".hero{background:linear-gradient(135deg,#181b3a 0%,#34346f 55%,#5b5bd6 100%);"
        "color:#fff;padding:48px 24px 86px}.hero-inner,.content{max-width:1120px;margin:auto}"
        ".eyebrow{margin:0 0 10px;color:#c7c9ff;font-size:12px;font-weight:800;"
        "letter-spacing:.16em;text-transform:uppercase}.hero-row{display:flex;"
        "align-items:flex-end;justify-content:space-between;gap:24px}.hero h1{margin:0;"
        "font-size:clamp(30px,5vw,48px);line-height:1.08;letter-spacing:-.035em}"
        ".subtitle{max-width:660px;margin:14px 0 0;color:#dfe1ff;font-size:17px}"
        ".overall{flex:none;border:1px solid rgba(255,255,255,.24);border-radius:999px;"
        "padding:9px 15px;background:rgba(255,255,255,.12);font-weight:700}"
        ".overall-alert:before{content:'';display:inline-block;width:8px;height:8px;"
        "margin-right:8px;border-radius:50%;background:#ff8fa3;box-shadow:0 0 0 4px "
        "rgba(255,143,163,.18)}.overall-clear:before{content:'\\2713';margin-right:8px}"
        ".content{padding:0 24px 48px}.metrics{display:grid;"
        "grid-template-columns:repeat(3,1fr);gap:16px;margin:-46px 0 28px}"
        ".metric{min-height:118px;padding:22px 24px;border:1px solid var(--line);"
        "border-radius:16px;background:var(--surface);box-shadow:0 10px 32px "
        "rgba(28,38,69,.09)}.metric-label{color:var(--muted);font-size:12px;"
        "font-weight:800;letter-spacing:.08em;text-transform:uppercase}"
        ".metric-value{display:block;margin-top:6px;font-size:34px;font-weight:800;"
        "line-height:1}.metric-danger{color:var(--danger)}"
        ".repository-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));"
        "gap:18px}.repository-card{overflow:hidden;border:1px solid var(--line);"
        "border-radius:16px;background:var(--surface);box-shadow:0 4px 18px "
        "rgba(28,38,69,.05)}.repository-header{display:flex;align-items:center;"
        "justify-content:space-between;gap:12px;padding:20px 22px;border-bottom:1px "
        "solid var(--line)}.repository-header h2{overflow-wrap:anywhere;margin:0;"
        "font-size:17px}.repo-status{flex:none;border-radius:999px;padding:5px 10px;"
        "font-size:12px;font-weight:800}.status-alert{color:var(--danger);"
        "background:var(--danger-soft)}.status-clear{color:var(--success);"
        "background:var(--success-soft)}.issue-list{padding:8px 22px 14px}"
        ".issue{display:flex;gap:12px;padding:15px 0;border-bottom:1px solid var(--line)}"
        ".issue:last-child{border-bottom:0}.priority{align-self:flex-start;border-radius:6px;"
        "padding:3px 7px;background:var(--danger);color:#fff;font-size:11px;font-weight:900}"
        ".issue-content{min-width:0}.issue-title{color:var(--ink);font-weight:750;"
        "text-decoration:none}.issue-title:hover{color:var(--accent);text-decoration:underline}"
        ".issue-meta{display:flex;flex-wrap:wrap;gap:5px 14px;margin-top:5px;"
        "color:var(--muted);font-size:12px}.empty-state{display:flex;align-items:center;"
        "gap:14px;padding:28px 22px}.empty-state p{margin:3px 0 0;color:var(--muted)}"
        ".checkmark{display:grid;place-items:center;width:38px;height:38px;border-radius:50%;"
        "background:var(--success-soft);color:var(--success);font-size:20px;font-weight:900}"
        ".footer{max-width:1120px;margin:4px auto 0;padding:0 24px 36px;color:var(--muted);"
        "font-size:12px;text-align:center}"
        "@media(max-width:720px){.hero{padding-top:34px}.hero-row{display:block}"
        ".overall{display:inline-block;margin-top:22px}.metrics{grid-template-columns:1fr;"
        "margin-top:-52px}.repository-grid{grid-template-columns:1fr}}"
        "</style></head><body>"
        '<header class="hero"><div class="hero-inner"><p class="eyebrow">'
        "Operational intelligence</p>"
        '<div class="hero-row"><div><h1>P0 issue portfolio</h1>'
        '<p class="subtitle">A prioritized view of critical issues across the registered '
        "repository portfolio.</p></div>"
        f'<span class="overall {overall_class}">{overall_status}</span>'
        "</div></div></header>"
        '<main class="content"><section class="metrics" aria-label="Portfolio summary">'
        '<div class="metric"><span class="metric-label">Repositories</span>'
        f'<strong class="metric-value">{len(reports)}</strong></div>'
        '<div class="metric"><span class="metric-label">Open P0 issues</span>'
        f'<strong class="metric-value metric-danger">{total}</strong></div>'
        '<div class="metric"><span class="metric-label">Healthy repositories</span>'
        f'<strong class="metric-value">{healthy_repositories}</strong></div>'
        f'</section><div class="repository-grid">{"".join(sections)}</div></main>'
        '<footer class="footer">Generated from deterministic sample data. '
        "Replace the inspection tool with the GitHub API for production use.</footer>"
        "</body></html>"
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
