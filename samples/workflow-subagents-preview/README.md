# PR status portfolio report

> [!IMPORTANT]
> This is a design preview, not a runnable sample. The proposed
> `workflows.allowed_sub_agents` field and `sub_agent` task type are not
> implemented. This directory intentionally omits `host.json`,
> `function_app.py`, and deployment files.

An Azure Storage Queue message supplies a list of pull requests. The coordinator
creates one independent PR review for every list entry, runs those reviews in
parallel, and then asks a final specialist to turn the summaries into one
actionable portfolio report.

```json
{
  "report_title": "Functions team PR status",
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
  └─ PR status analyst: PR 456 ─┴─ actionable report writer ─ final report
```

The map phase is useful because each PR can have noisy, unrelated checks,
reviews, comments, and merge requirements. Every analyst gets a clean context
and can investigate one PR with its GitHub MCP tools and `pr-status-analysis`
skill. These independent model calls can run in parallel.

The reduce phase receives the compact analyst summaries instead of all raw
GitHub data. It can compare the portfolio and group items into:

- ready to merge;
- author action required;
- reviewer action required;
- failing or pending checks;
- recently changed or newly commented.

This keeps the final agent's context focused while Durable Workflows preserves
the fan-out/fan-in execution after the queue-triggered function has returned.

## Agent capabilities

The PR analyst inherits the GitHub MCP server and uses the
`pr-status-analysis` skill. The report writer disables MCP access and uses only
the summarized inputs plus the `actionable-pr-report` skill.

The current exclude-based capability model makes this distinction possible, but
does not positively allow only one MCP server or skill. The illustrative
`allow` syntax remains a separate reviewer question in FRD 0004.
