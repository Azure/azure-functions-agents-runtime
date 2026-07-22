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

For every valid request, call `start_workflow` exactly once with this DAG:

1. Create one `inspect_repository_p0_issues` tool task per repository. Pass that
   repository as `repository`. These tasks are independent: do not add
   `depends_on`, so Durable Functions can execute every repository check in
   parallel.
2. Create one `render_p0_html_report` tool task that depends on every inspection
   task. Its `repository_reports` argument must be a list containing each whole
   inspection result as `${inspection_task_id.result}`.
3. Create one terminal `publish_p0_html_report` tool task that depends on the
   render task. Pass the whole rendered result as
   `report: ${render_task_id.result}` and pass `body_json.report_blob` as
   `blob_name`.

Use short, unique task IDs derived from repository names. Preserve the exact
repository order from the request in `repository_reports`. After
`start_workflow` returns, log a short response containing the `workflow_id` and
end the turn immediately. Do not poll workflow status.

If `body_json.repositories` is missing, empty, or not a list of non-empty
strings, do not start a workflow; explain the validation error in the Function
log.
