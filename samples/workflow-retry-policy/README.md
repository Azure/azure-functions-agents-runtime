# Workflow Retry Policy

This sample shows the normal customer setup for retrying a Dynamic Workflow
task. An operations agent loads delayed order `ORD-1001`, reserves inventory,
and confirms the order. The inventory tool reports two transient failures
before succeeding on its third attempt.

| Trigger | Custom Tools | Connectors | MCP Servers | Skills | Sandbox | Chat UI |
|---|---|---|---|---|---|---|
| HTTP | ✅ (workflow-safe) | | | | | ✅ |

## Configure retry on a workflow tool

The retry policy belongs directly on the operation whose transient failures are
safe to retry:

```python
@workflow_tool(
    retry=WorkflowRetryPolicy(
        max_attempts=3,
        backoff=WorkflowRetryBackoff(
            initial="PT1S",
            multiplier=2.0,
            max="PT4S",
        ),
    ),
)
def reserve_inventory(args: dict[str, Any]) -> dict[str, Any]:
    ...
```

When the dependency is temporarily unavailable, the handler raises the public
retryable error:

```python
raise WorkflowRetryableError(
    "inventory_temporarily_unavailable",
    "Inventory reservation is temporarily unavailable.",
)
```

No retry fields are required in the model-generated DAG. At workflow start, the
runtime applies the `reserve_inventory` decorator policy to that task. A DAG may
instead provide an `execution.retry` policy when the tool author has not
declared one; see [Workflow task execution policy](../../docs/workflows.md#task-execution-policy).

## Sample-only failure simulation

Real tools fail because their external dependency is unavailable. This sample
needs a repeatable failure both locally and on Azure, so `reserve_inventory`
stores a workflow-scoped incident counter in the Storage account configured by
`AzureWebJobsStorage`. The first two deliveries decrement the counter and raise
`WorkflowRetryableError`; the third succeeds.

The Blob state and its concurrency handling are only a deterministic substitute
for a transient inventory service. **Blob Storage and an incident-setup task are
not required to use retry.** The failure simulation is entirely inside the tool
and does not appear in the agent-authored workflow.

## Run locally

Follow the [shared local development guide](../README.md#run-locally) to create
a virtual environment, install `src/requirements.txt`, start Azurite, create
`src/local.settings.json`, and run `func start` from `src/`.

Open <http://localhost:7071/agents/main/> and ask:

> Recover delayed order ORD-1001 and complete it safely.

The agent uses its ordinary instructions to generate a three-task DAG:

```text
load_order → reserve_inventory → confirm_order
```

The workflow should finish `Completed`, and `confirm_order` reports
`transient_failures_observed: 2` — the two attempts Durable retried before
`reserve_inventory` succeeded.