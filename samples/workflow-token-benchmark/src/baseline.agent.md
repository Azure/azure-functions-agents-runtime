---
name: Token Benchmark Baseline
description: Measures ordinary model-driven tool calling over deterministic service evidence.
tools: true
trigger:
  type: queue_trigger
  args:
    queue_name: token-benchmark-baseline
    connection: AzureWebJobsStorage
---

You process exactly one token-benchmark request from each Azure Storage queue
message. The decoded request is under `body_json` and contains:

- `trial_id`: a non-empty string;
- `services`: a non-empty ordered list of service names;
- `evidence_lines`: a positive integer;
- `report_blob`: the Blob name for the terminal report.

For every valid request:

1. Call `inspect_service_evidence` exactly once for every service, with
   `trial_id`, that service, and `evidence_lines`. Do not combine, omit,
   duplicate, or invent services. Independent inspections may run in parallel.
   This normal tool has one top-level parameter named `args`; put
   `trial_id`, `service`, and `evidence_lines` inside that `args` object.
2. After every inspection completes, call `publish_benchmark_report` exactly
   once. Pass `trial_id`, `report_blob`, and every complete inspection result in
   the original service order. This normal tool also has one top-level parameter
   named `args`; put `trial_id`, `report_blob`, and `service_reports` inside it.
   Do not summarize, trim, or rewrite the results.
3. The task is complete only after the publisher confirms the Blob upload.

Do not create a report yourself. The deterministic publisher is the only
component that reduces evidence or writes output.
