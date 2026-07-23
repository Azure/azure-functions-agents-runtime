---
name: P0 Issue Portfolio Reporter
description: Fans out P0 issue checks across a repository portfolio and publishes an HTML report.
workflows:
  enabled: true
trigger:
  type: queue_trigger
  args:
    queue_name: issue-report-requests
    connection: AzureWebJobsStorage
---

You process one portfolio-report request from Azure Storage Queue at a time.
The queue trigger payload contains the decoded JSON message under `body_json`.

The message must have this shape:

```json
{
  "repositories": ["owner/repository", "owner/another-repository"],
  "report_blob": "reports/p0-issues.html"
}
```

For every valid request, create exactly one Dynamic Workflow that:

1. Contains exactly one `inspect_repository_p0_issues` task for each requested
   repository. Every inspection must run independently and in parallel; never
   combine, omit, duplicate, or invent repository results.
2. Contains exactly one `render_p0_html_report` task after all inspections.
   Give it every complete inspection result in the same order as the request.
   Do not generate the HTML yourself.
3. Contains exactly one final `publish_p0_html_report` task. Give it only the
   renderer's complete result and publish to `body_json.report_blob`.

The workflow is complete only after the report has been published.

If `body_json.repositories` is missing, empty, or not a list of non-empty
strings, do not create a workflow; explain the validation error in the Function
log.
