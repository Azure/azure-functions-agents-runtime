# Durable loop privacy qualification

`eng/scripts/durable_loop_privacy_qualification.py` is the content-safe scanner
for the final private durable agent loop deployment. It reads canaries and any
environment overrides from bounded JSON on stdin or from an operator-protected
regular file. Canary values and exact provider, call, and sandbox identifiers
are never accepted as command-line arguments.

The scanner emits one compact JSON object per surface and one final aggregate.
Output is limited to surface names, scanned and violation counts, fixed status
values, bounded rule counts, and exception-class-based internal codes. It does
not emit inspected values, snippets, queries, credentials, resource IDs, URLs,
row or Blob names, sandbox IDs, request or response bodies, exception messages,
or canary values.

## Security boundary

The dedicated `durable-loop-content` Blob container relies on Azure Storage
service-side encryption and Azure RBAC. It does **not** provide
application-level envelope encryption, and the qualification result must not be
described as proving ciphertext at the application layer.

The live scanner uses `DefaultAzureCredential` and data-plane or ARM RBAC. It
does not retrieve account keys, publishing profiles, Function keys, connection
strings, application-setting values, or APIM subscription secrets. Run it only
from an operator environment whose identity has the minimum read permissions
for the selected resources.

## Operator input

The top-level JSON object contains:

- `canaries`: exactly the categories `prompt`, `human`, `tool_argument`,
  `tool_result`, `mcp_result`, `stdout_stderr`, `reasoning`,
  `synthetic_credential`, and `synthetic_blob_sas_url`. Each value is a bounded
  list of unique strings.
- `exact_ids`: optional bounded `provider`, `call`, and `sandbox` string lists.
- `resources`: required for `live`; contains `subscription_id` and optional
  strictly validated resource-name overrides. Defaults match the private
  `0904` environment.
- `sandbox_selector`: the exact app-owned label selector used to require final
  inventory zero.
- `fixtures`: required only by `scan-json`.

Duplicate JSON keys are rejected. Protect operator files with the host's
user-only ACL or mode before invoking the scanner.

```powershell
Get-Content -Raw .\operator-private.json |
  uv run python eng\scripts\durable_loop_privacy_qualification.py live `
    --start-utc 2026-09-07T16:00:00Z `
    --end-utc 2026-09-07T17:00:00Z
```

Use `scan-json` with the same input shape and a `fixtures` object for
deterministic offline validation. Neither mode performs destructive queue
reads. A non-empty Durable queue is `INCONCLUSIVE`.

## Covered surfaces

- Durable task-hub Instances, History, and entity Tables.
- Task-hub large-message and lease Blob containers, plus Durable control and
  work-item queue drain metadata.
- Dedicated content Blob metadata, tags, headers, expected content-class
  placement, hash-addressed names, SHA-256 integrity, and byte counts.
- Application Insights, workspace telemetry, and APIM logs through
  server-side aggregate-only Kusto queries.
- APIM child API diagnostic resources, including zero body capture, sensitive
  header exclusion, and the model-control sampling-zero override.
- ACA Sandbox inventory through the repository's existing adapter, with raw
  IDs retained only in memory and a bounded final-zero wait.
- Optional deployment or activity records supplied through an injected
  adapter.

## Deployment-only limitations

Qualification requires a deployed final application that has produced the
expected Durable and telemetry records inside the selected time window.
Missing expected data, authorization failures, unavailable optional SDKs,
service errors, truncation limits, and telemetry that remains absent after the
bounded lag retries are `INCONCLUSIVE`, never `PASS`.

The live path requires the development environment's optional Table, Queue, and
ACA Sandbox dependencies. Activity-log retrieval is intentionally not automatic;
records may be supplied through the adapter protocol when that evidence is part
of the qualification run. No live Azure calls are needed for `scan-json`.
