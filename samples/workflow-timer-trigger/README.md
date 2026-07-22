# Timer-triggered Dynamic Workflow

This sample shows a Markdown-declared timer starting a Dynamic Workflow without
keeping the timer Function alive until the workflow finishes.

The timer's initial agent turn is only a **starter**. It calls
`start_workflow`, receives a `workflow_id`, logs its short response, and exits.
The Durable orchestration then runs independently:

1. `capture_timer_event` records the trigger payload.
2. A five-second durable timer pauses without holding a worker.
3. `publish_timer_result` acts as the terminal sink and writes a
   `TIMER_WORKFLOW_COMPLETED` structured log marker.

## Run locally

Follow the [shared sample setup](../README.md#run-locally-optional) to create a Python
environment, copy `src/local.settings.template.json` to
`src/local.settings.json`, start Azurite, and configure a supported model
provider. Then run:

```powershell
Set-Location samples\workflow-timer-trigger\src
func start
```

The committed sample runs every five minutes. For an immediate local demo, add
`run_on_startup: true` under `trigger.args`, start the host once, then remove the
setting before deployment. Look for these log messages in order:

1. `workflow started: id=...`
2. the triggered agent's response containing that workflow ID
3. `TIMER_WORKFLOW_COMPLETED ...`

The completion marker appears after the starter Function has returned. This is
the important lifetime boundary: the orchestration can continue for hours or
days, while the initial agent invocation only needs enough time to author and
start the plan.

> [!WARNING]
> `run_on_startup: true` is convenient for a one-off local demonstration but is
> rarely appropriate in production. The committed sample intentionally leaves
> it disabled.

## Operational notes

- The starter's model turn is still subject to the normal agent and Function
  timeout.
- Each workflow Activity is also a normal Function execution and must remain
  within the hosting plan's timeout. Split long work into bounded Activities and
  use durable timers for long waits.
- Timer invocations use an ephemeral agent session. Later agent turns cannot
  manage that workflow through session-scoped tools, so non-interactive
  workflows should include a terminal sink such as a queue, database, webhook,
  or notification Activity.
- Use Durable Functions or Durable Task Scheduler tooling for operational
  status, cancellation, and termination.
