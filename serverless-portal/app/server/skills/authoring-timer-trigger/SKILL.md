---
name: authoring-timer-trigger
description: How to add a timer (scheduled) trigger to an azure-functions-agents-runtime agent. Timer triggers are declarative NCRONTAB in .agent.md front matter — never Python. Covers schedule syntax, common presets, and the required file layout.
---

# Authoring a timer trigger

A **timer trigger** runs an agent on a schedule. In the
azure-functions-agents-runtime it is **declarative**: a `timer_trigger` block in
the agent's `<name>.agent.md` front matter. Do **not** author a Python timer
function — the framework's `function_app.py` stays a single line:

```python
from azure_functions_agents import create_function_app

app = create_function_app()
```

The runtime reads each `.agent.md`, registers the matching Azure Functions
`timer_trigger` binding at startup, and runs the agent's instructions on every
tick.

## `.agent.md` shape

```yaml
---
name: <concise name>
description: <one line>
trigger:
  type: timer_trigger
  args:
    schedule: "0 0 9 * * *"        # required — 6-field NCRONTAB
    # run_on_startup: false        # optional
    # use_monitor: true            # optional
---

<Markdown instructions: what work to perform on each tick, which tools to
invoke, what to write / return / notify.>
```

Rules:

- `schedule` is an **NCRONTAB expression with 6 fields**:
  `second minute hour day month day-of-week`. A 5-field cron is accepted; the
  runtime prepends `0 ` for the seconds field.
- Do **not** set `arg_name` — the runtime injects it.
- String values under `trigger.*` support `$ENV_VAR` substitution.
- Use `timer_trigger` (not `schedule`) as the type.

## Common presets

| Cadence | NCRONTAB |
|---|---|
| Every 5 minutes | `"0 */5 * * * *"` |
| Every hour on the hour | `"0 0 * * * *"` |
| Every 6 hours | `"0 0 */6 * * *"` |
| Daily at 09:00 UTC | `"0 0 9 * * *"` |
| Weekdays at 14:30 UTC | `"0 30 14 * * 1-5"` |
| Every Monday at 00:00 UTC | `"0 0 0 * * 1"` |
| First of the month at midnight | `"0 0 0 1 * *"` |

Times are UTC. If you need a local time, convert at author-time.

## Writing the instructions

Timer agents have no HTTP payload to work with — the whole prompt is written
into the `.agent.md` body. Be explicit about:

- **What triggered it** — reference "this scheduled run" in the instructions.
- **What data to read** — which MCP tool(s) / storage / API to call.
- **What decisions to make** — filtering, aggregation, prioritisation.
- **What action to take** — send a message, write a report, kick off a workflow.
- **Idempotency** — how to avoid duplicate actions on retries or overlapping
  runs (`use_monitor: true` helps guard against overlap).

## Anti-patterns

- ❌ Creating a Python `timer_trigger` function that calls the agent over HTTP.
  The timer **is** the agent.
- ❌ Using `type: schedule` — the correct type is `timer_trigger`.
- ❌ Omitting `schedule` — it's required.
- ❌ Depending on a 5-field cron — always author 6-field NCRONTAB for clarity.
