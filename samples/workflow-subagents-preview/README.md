# PR status portfolio report

> [!IMPORTANT]
> This is a design preview, not a runnable sample. The proposed
> `workflows.allowed_sub_agents` field and `sub_agent` task type are not
> implemented. This directory intentionally omits `host.json`,
> `function_app.py`, and deployment files.

An Azure Storage Queue message supplies a list of pull requests and a Blob
destination. The coordinator creates one independent PR review for every list
entry, runs those reviews in parallel, asks a final specialist to turn the
summaries into a polished HTML report, and publishes the document to Blob
Storage.

```json
{
  "report_title": "Functions team PR status",
  "report_blob": "reports/functions-pr-status.html",
  "pull_requests": [
    {
      "url": "https://github.com/Azure/azure-functions-host/pull/123",
      "last_checked_at": "2026-07-22T17:00:00Z"
    },
    {
      "url": "https://github.com/Azure/azure-functions-python-worker/pull/456",
      "last_checked_at": "2026-07-22T17:00:00Z"
    }
  ]
}
```

## Workflow shape

```text
Queue message
  ├─ PR status analyst: PR 123 ─┐
  └─ PR status analyst: PR 456 ─┴─ HTML report writer ─ publish to Blob
```

The map phase is useful because each PR can have noisy, unrelated checks,
reviews, comments, and merge requirements. Every analyst gets a clean context
and investigates one PR with the deterministic `get_pull_request_status` and
`get_pull_request_activity` sample tools plus the `pr-status-analysis` skill.
These independent model calls can run in parallel without GitHub credentials.

The reduce phase receives the compact analyst summaries instead of all raw
GitHub data. It can compare the portfolio and group items into:

- ready to merge;
- author action required;
- reviewer action required;
- failing or pending checks;
- recently changed or newly commented.

This keeps the final agent's context focused while Durable Workflows preserves
the fan-out/fan-in execution after the queue-triggered function has returned.
Because a Queue trigger has no response channel, the terminal
`publish_pr_status_report` workflow tool uploads the generated HTML to the
requested Blob path.

## Agent capabilities

The PR analyst inherits the two fake GitHub tools and uses the
`pr-status-analysis` skill. The report writer disables normal tools and uses
only the summarized inputs plus the `actionable-pr-report` skill. The Blob
publisher is workflow-only, so it is not exposed as a normal tool to either Sub
Agent.

The current exclude-based capability model makes this distinction possible, but
does not positively allow only selected normal tools or skills. The illustrative
`allow` syntax remains a separate reviewer question in FRD 0004.

## Sample tools

- `src/tools/pr_status_tool.py` and `src/tools/pr_activity_tool.py` each define
  one normal agent tool that returns stable synthetic data. The one-tool-per-file
  layout matches current tool discovery, makes the sample repeatable, and avoids
  a deployed GitHub authentication dependency.
- `src/tools/report_publisher.py` defines the workflow-only terminal sink. It
  writes HTML with the `text/html` content type using `AzureWebJobsStorage`.
  The container defaults to `workflow-reports` and can be changed with
  `PR_STATUS_REPORT_CONTAINER`. Keep this stored-HTML container private and
  grant readers access through the application's normal Azure authorization.
