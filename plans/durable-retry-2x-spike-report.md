# Durable retry 2.x spike report

## Scope

This preserved spike evaluates replacing the Dynamic Workflow runtime-managed
retry scheduler from PR #170 with `azure-functions-durable==2.0.0b2` and
`durabletask==1.9.0`. It does not change the approved FRD or production
documentation, does not switch the production scheduler, and is not proposed
for merge.

The prototype retains the public `WorkflowRetryPolicy` authoring and precedence
rules. It stores a native Durable policy shape beside the existing precomputed
Option A delay sequence, registers a dormant per-node retry sub-orchestrator,
and exercises that sub-orchestrator end to end against durabletask's in-memory
backend.

## Code delta

- `pyproject.toml` and `uv.lock` pin `azure-functions-durable==2.0.0b2` and its
  required `azure-functions==2.3.0b2`; `durabletask==1.9.0` is transitive.
- The focused retry sample and workflow startup fixture use
  `Microsoft.Azure.Functions.ExtensionBundle.Preview` `[4.*, 5.0.0)`.
- `workflows/schema.py` persists `durable_retry_policy` with first interval,
  attempt limit, coefficient, and maximum interval beside
  `retry_delays_ms`. Old payloads without this optional field remain accepted.
- `workflows/durable_retry_2x_spike.py` maps that JSON shape to
  `durabletask.task.RetryPolicy`, bridges Activity outcomes, and defines the
  dormant `agents_workflow_retry_node_2x_spike` sub-orchestrator.
- Small typing changes account for 2.x's typed decorators and Durable client.
- `tests/test_workflow_durable_retry_2x_spike.py` supplies executable evidence.

`azure-functions==2.3.0b3` was initially selected by a lower-bound constraint
and changed Azure Functions trigger object serialization enough to break seven
unrelated tests. Pinning the exact `2.3.0b2` version required by this preview
restored compatibility. This is direct evidence of preview dependency risk.

## Findings

### Policy mapping and timing

The existing public policy maps directly:

- `backoff.initial` -> `first_retry_interval`
- `max_attempts` -> `max_number_of_attempts`
- `backoff.multiplier` -> `backoff_coefficient`
- `backoff.max` -> `max_retry_interval`

The precedence decision remains upstream of the driver, so decorator-authoritative
whole-object retry selection and task fallback are unchanged. The persisted
native shape avoids reverse-engineering a multiplier from Option A's flattened
delay list.

The shapes are equivalent for ordinary millisecond inputs, but not bit-for-bit:
Option A precomputes and floors every delay to integer milliseconds, while
durabletask computes with floating-point seconds and can retain sub-millisecond
precision. The prototype leaves `retry_timeout` unset because the authoring
surface has no equivalent field and the existing one-hour admission bound is
not the same semantic.

### Activity contract and failure behavior

A workable Activity boundary has three outcomes:

1. success returns the existing structured success envelope;
2. terminal, unknown, authorization, and contract failures return their
   existing structured terminal envelope and are not retried;
3. retryable or timeout outcomes are converted to a private, sanitized
   exception so Durable performs the retry.

After built-in retries are exhausted, `TaskFailedError` exposes the internal
exception type but not the original structured failure envelope. The
sub-orchestrator can return a contract-valid, sanitized
`workflow_task_retry_exhausted` failure and preserve timeout versus transient
kind, but it loses the application-selected error code and message. Parsing an
exception message to reconstruct them would be a fragile history contract and
was intentionally not attempted.

Infrastructure failures are also exception-driven and therefore receive the
same built-in policy. Unknown application exceptions remain non-retryable only
because the existing Activity wrapper catches and converts them before they
cross the Durable boundary.

### Attempts, status, and idempotency

Attempt metadata is not available to an Azure Functions Activity in this
preview. The native durabletask `ActivityContext` exposes only
`orchestration_id` and `task_id`; the Functions adapter supplies no usable
Activity context at all. The observed `task_id` was identical across all three
deliveries. Therefore the current `WorkflowTaskContext.attempt`, per-attempt
telemetry, and attempt-dependent sample behavior cannot be preserved accurately
with built-in retry.

Durable does not publish built-in retry state through the parent custom status.
The existing `retry_wait`, `attempt`, `next_retry_time`, and last-failure status
fields cannot be reproduced without retaining a manual scheduler or adding a
second state channel. The same authoring policy can be used if those status
fields are intentionally removed or downgraded.

The node id and Activity input remain stable across retries, so the existing
idempotency key can remain stable. It can no longer distinguish a policy retry
from an at-least-once redelivery, which is already true at the side-effect
deduplication boundary.

### Fan-out, replay, cancel, and terminate

- Separate per-node sub-orchestrations gave two `for_each`-shaped instances
  independent retry state; one retried while its sibling completed once.
- Native retry replayed deterministically in the in-memory backend and executed
  the Activity exactly twice for one failure followed by success.
- Cooperative cancellation of the parent did not cancel a retrying child. The
  parent returned its canceled result while the child continued and performed
  its second Activity delivery.
- The client API defaults hard termination to `recursive=True`, but the
  durabletask in-memory backend explicitly does not implement recursive
  termination. The parent became `TERMINATED` while the child remained
  `RUNNING`; real-host recursive child termination remains unverified.
- A local Functions host using Core Tools 4.10.0, Python 3.13.15, the preview
  extension bundle, and Azurite indexed ten functions, started its Durable
  listeners, and returned HTTP 200 from the workflow endpoint. Azurite 3.35.0
  required `--skipApiVersionCheck` because the preview host requested storage
  API version `2026-02-06`.

Per-node sub-orchestration adds one child instance and extra history/dispatch
round trips per materialized node. It provides a clean normalization boundary,
but increases latency and makes cooperative cancellation weaker unless the
runtime tracks and explicitly terminates every child.

## Exact replacement seam

The parent DAG scheduler should own authorization before dispatch,
materialization, `for_each` ordering, `continue_on_error`, cooperative control,
and applying one final structured node outcome. It should delegate only one
operation:

    schedule_node(context, activity_name, activity_input, effective_policy)
        -> Durable task yielding one final structured Activity outcome

An extracted 1.x compatibility driver can implement that operation with the
current explicit Activity-attempt loop and Durable timers. A 2.x driver can
implement it with the per-node sub-orchestrator in this spike:

    call_activity(..., retry_policy=RetryPolicy(...))
    catch TaskFailedError
    return one sanitized exhaustion outcome

The common Activity wrapper must keep authorization, timeout, exception
classification, sanitization, idempotency setup, and actual handler execution.
The parent must keep `continue_on_error` and `for_each` aggregation. Replacing
the driver would then delete Option A's attempt increment, retry timer creation,
retry deadline bookkeeping, and retry-wave selection.

This seam is mechanically realistic, but behavior-equivalent deletion is not:
the 2.x driver cannot currently provide accurate attempt context or retry-wait
status, loses the original failure code on exhaustion, and requires a deliberate
child-cancellation design.

## Verification

- `uv run pytest tests/test_workflow_durable_retry_2x_spike.py -q`
  -> 9 passed.
- `uv run pytest --cache-clear --cov=./src/azure_functions_agents
  --cov-report=xml --cov-branch tests`
  -> 1170 passed, 53 deselected.
- `uv run ruff check src tests`
  -> clean.
- `uv run mypy src`
  -> clean.
- Local preview-bundle Functions startup -> 10 functions loaded, host and job
  host started, workflow endpoint HTTP 200.

## Recommendation

Preview 2.x is **not viable today as a behavior-preserving replacement** for
Option A. Keep the runtime-managed selective retry implementation for PR #170.

The staged migration direction is still useful: isolate retry driving behind
the per-node seam above and retain the native policy shape so stable 2.x can be
adopted later. Re-evaluate only when the stable Functions provider and bundle
are available and after explicitly deciding whether to remove attempt/retry-wait
observability, how to preserve exhaustion diagnostics, and how cooperative and
hard cancellation must affect child retries.
