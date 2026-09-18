# Workflow Retry, Timeout, and Continuation Policy

This sample shows retry, per-attempt timeout, and failure continuation for
Dynamic Workflow tasks. An operations agent loads delayed order `ORD-1001`,
reserves inventory, verifies the carrier, tries an optional customer
notification, and confirms the order. The inventory tool reports two transient
failures before it succeeds. The carrier tool exceeds its first attempt
deadline and succeeds on its second attempt. The customer notification reports
a terminal failure, but the plan continues.

| Trigger | Custom Tools | Connectors | MCP Servers | Skills | Sandbox | Chat UI |
|---|---|---|---|---|---|---|
| HTTP | ✅ (workflow-safe) | | | | | ✅ |

## Declare retry on the workflow tool

The tool author declares retry where the knowledge that repeating the operation
is safe lives:

```python
@workflow_tool(
    retry=WorkflowRetryPolicy(
        max_attempts=3,
        backoff=WorkflowRetryBackoff(
            initial="PT1S",
            multiplier=2.0,
            max="PT4S",
        ),
    )
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

The runtime validates and persists the effective policy when the workflow
starts. A tool declaration overrides plan-authored `execution.retry`; tasks
without either declaration retain their policy-free behavior. See
[Workflow task execution policy](../../docs/workflows.md#task-execution-policy).

## Combine a tool timeout with plan retry

The carrier tool declares the deadline that is safe for each call:

```python
@workflow_tool(timeout="PT1S")
def verify_carrier(args: dict[str, Any]) -> dict[str, Any]:
    ...
```

The agent adds a two-attempt retry policy and a longer plan timeout. Timeout and
retry precedence apply per field. The tool's `PT1S` timeout replaces the plan's
`PT10S` timeout, while the plan retry policy remains effective.

The first carrier attempt sleeps for two seconds. The runtime returns
`workflow_task_timeout` after one second and Durable starts the second attempt
after the retry delay. The synchronous first call can continue in its worker
thread after the deadline. Before it sleeps, the tool updates the workflow-scoped
Blob with an entity-tag condition. Concurrent deliveries retry that update after
a conflict, so each delivery records one attempt.

## Continue after an optional failure

Continuation is a plan decision. The agent sets
`execution.continue_on_error: true` on `notify_customer` because order
confirmation does not require the optional notification.

The tool raises a terminal application failure. The workflow commits this
bounded result and runs `confirm_order`:

```json
{
  "failed": true,
  "error_code": "customer_notice_unavailable",
  "error": "Customer notification is unavailable.",
  "kind": "handler_terminal"
}
```

The same plan enables continuation on `verify_carrier`. If all timeout attempts
are used, `confirm_order` can record that manual carrier verification is
required. A continued failure keeps the task state `completed`. Workflow
completion means that control flow finished. It does not mean that every task
succeeded.

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
a virtual environment and install `src/requirements.txt`. Start Azurite for the
sample's Blob state. The default `host.json` uses Azure Storage so the sample can
run in standard development and CI environments.

To use the Durable Task Scheduler emulator, start a container that registers this
sample's `workflowretrypolicy` Task Hub:

```powershell
docker rm -f workflow-retry-policy-dts 2>$null
docker run --rm --name workflow-retry-policy-dts `
  -e DTS_TASK_HUB_NAMES=workflowretrypolicy `
  -p 8080:8080 -p 8082:8082 `
  mcr.microsoft.com/dts/dts-emulator:latest
```

Then run these commands from `src/`:

```powershell
Copy-Item local.settings.template.json local.settings.json
Copy-Item host.dts.json host.json
func start
```

The DTS configuration reads its endpoint and task hub from
`DURABLE_TASK_SCHEDULER_CONNECTION_STRING` and `TASKHUB_NAME`. Its extension
bundle starts at `4.32.0` because that version first includes the `azureManaged`
provider. Restore the committed `host.json` to return to Azure Storage.

Open <http://localhost:7071/agents/main/> and ask:

> Recover delayed order ORD-1001 and complete it safely.

The agent uses its ordinary instructions to generate a five-task DAG:

```text
load_order → reserve_inventory → verify_carrier ──┐
                           └→ notify_customer ────┴→ confirm_order
```

The workflow should finish `Completed`. The `confirm_order` result reports
`timeout_attempts_observed: 1`, `customer_notice_failed: true`, and
`carrier_verification_failed: false`. The inventory state also records the two
transient failures before `reserve_inventory` succeeded.