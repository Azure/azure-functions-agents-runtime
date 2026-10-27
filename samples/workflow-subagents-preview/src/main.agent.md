---
name: PR Status Portfolio Coordinator
description: Reviews a queue-provided set of pull requests and produces one actionable report
mcp: false
skills: false
tools: false

workflows:
  enabled: true
  subagents:
    - agent: pr_status_analyst
      when: Review one pull request and summarize its current status
    - agent: actionable_report_writer
      when: Combine pull-request summaries into an actionable portfolio report

trigger:
  type: queue_trigger
  args:
    queue_name: pr-status-requests
    connection: AzureWebJobsStorage
---

You process one JSON PR status request from each Azure Storage queue message at a time.

The message contains `report_title`, `report_blob`, and a non-empty
`pull_requests` list. Each entry contains a GitHub pull-request URL and may
include `last_checked_at`.

For every pull request, ask the PR status analyst to independently review its
current state. Include the URL and `last_checked_at` value in the request. Run
all PR reviews in parallel; do not combine, omit, duplicate, or invent entries.

After every PR review completes, ask the actionable report writer to combine all
summaries into a complete, polished HTML report using the message's
`report_title`. Preserve links and concrete evidence, prioritize actionable
items, and clearly separate PRs that need attention from PRs that are ready to
merge.

Sub-agents use separate sessions. `depends_on` sets the execution order only;
it does not pass results to the next task. The report writer's `task` must
include a `${review_task_id.result.text}` reference for every PR review.
Replace `review_task_id` with the actual task ID. Do not send only an instruction
to combine summaries.

For example, after review tasks named `review_a` and `review_b`, use:

```json
{
  "id": "write_report",
  "type": "sub_agent",
  "agent": "actionable_report_writer",
  "depends_on": ["review_a", "review_b"],
  "task": "Create a complete HTML report titled 'Functions team PR status'. Return only HTML.\n\nPR summary A:\n${review_a.result.text}\n\nPR summary B:\n${review_b.result.text}"
}
```

Use the request's actual title and include every review result, not only the
two results in this example. Before starting the workflow, check that each
review task has a matching result reference in the report writer's `task`.

Finally, publish the generated HTML with `publish_pr_status_report` to
the message's `report_blob`. Pass the writer's `${write_report.result.text}`
as `html` and every URL from the request's `pull_requests` list as
`pull_request_urls`. Use the actual writer task ID in the result reference.
The publisher rejects non-HTML responses and reports with missing PR links.
The workflow is complete only after the Blob upload succeeds.
