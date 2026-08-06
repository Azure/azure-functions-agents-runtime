---
name: authoring-triggers
description: How to add a trigger to an azure-functions-agents-runtime agent. Triggers are declarative .agent.md front matter (never Python) — covers every supported trigger type, its args, and the timer and connector specifics.
---

# Authoring agent triggers

In the azure-functions-agents-runtime, **triggers are declarative**. A trigger is
YAML in an agent's `<name>.agent.md` front matter — it is **never** Python code.

> The single most common mistake: writing (or generating) a Python function for a
> timer / queue / connector trigger. Do not. `function_app.py` in every app is only:
>
> ```python
> from azure_functions_agents import create_function_app
>
> app = create_function_app()
> ```
>
> The runtime reads each `.agent.md`, registers the matching Azure Functions
> binding at startup, and runs the agent's instructions when the trigger fires.

## `.agent.md` shape

```yaml
---
name: <concise name>
description: <one line>
trigger:
  type: <trigger_type>
  args:
    <param>: <value>
# optional, only for structured HTTP request/response:
# input_schema: { ...JSON Schema... }
# response_schema: { ...JSON Schema... }
---

<Markdown instructions: the agent's role and exactly what to do when the
trigger fires. For a scheduled agent, describe the work to perform on each run.>
```

Rules:

- `trigger.args` is passed straight to the Azure Functions decorator.
- Do **not** set `arg_name` — the runtime injects it for non-HTTP triggers.
- Use `http_trigger` (not `route`) and `timer_trigger` (not `schedule`).
- String values under `trigger.*` support `$ENV_VAR` substitution.
- Every `.agent.md` needs **either** a `trigger:` block **or** at least one
  `builtin_endpoints` value.

## Supported trigger types and args

| `trigger.type` | Required args | Optional args |
|---|---|---|
| `http_trigger` | `route` | `methods` (default `["POST"]`), `auth_level` (`anonymous` / `function` / `admin`, default `function`) |
| `timer_trigger` | `schedule` (NCRONTAB) | `run_on_startup`, `use_monitor` |
| `queue_trigger` | `queue_name`, `connection` | — |
| `blob_trigger` | `path` | `connection` (default `AzureWebJobsStorage`), `source` |
| `service_bus_queue_trigger` | `queue_name`, `connection` | — |
| `service_bus_topic_trigger` | `topic_name`, `subscription_name`, `connection` | — |
| `event_grid_trigger` | (none) | — |
| `event_hub_message_trigger` | `event_hub_name`, `connection` | `consumer_group`, `cardinality` |
| `connector_trigger` | (none) | — (see below) |

Also supported, mapping 1:1 to the Azure Functions decorator of the same name:
`cosmos_db_trigger`, `cosmos_db_trigger_v3`, `sql_trigger`, `mysql_trigger`,
`kafka_trigger`, `dapr_binding_trigger`, `dapr_service_invocation_trigger`,
`dapr_topic_trigger`, `generic_trigger`.

## Timer triggers (scheduled agents)

To "invoke the agent on a schedule", set a `timer_trigger` — nothing else. The
runtime runs the agent's instructions on each tick.

```yaml
trigger:
  type: timer_trigger
  args:
    schedule: "0 0 9 * * *"   # every day at 09:00 UTC
```

`schedule` is an NCRONTAB expression with 6 fields
(`second minute hour day month day-of-week`). A 5-field cron is accepted and the
runtime prepends `0 ` for the seconds field.

- `"0 */5 * * * *"` — every 5 minutes
- `"0 0 9 * * *"` — daily at 09:00 UTC
- `"0 30 14 * * 1-5"` — weekdays at 14:30 UTC

Do **not** create a Python timer function that calls the agent over HTTP — that is
an anti-pattern here. The timer *is* the agent.

## Connector triggers (Office 365, Teams, …)

```yaml
trigger:
  type: generic_trigger
  args:
    type: connectorTrigger
```

`connector_trigger` maps to `generic_trigger(type="connectorTrigger")`. Besides
the `.agent.md`, a connector agent also needs:

1. A connector **connection** configured in Azure.
2. The connector's **MCP tool** in `mcp.json` (e.g. Office 365 Outlook) so the
   agent can act on the event.

Write instructions that read every field of the trigger payload and say which MCP
tools to use.

## Not triggers (never author these as `.agent.md` triggers)

- `route`, `schedule` — use `http_trigger` / `timer_trigger` instead.
- Durable `activity_trigger` / `orchestration_trigger` / `entity_trigger` —
  Durable code lives outside `.agent.md`.
- `warm_up_trigger`, `mcp_tool_trigger` / `mcp_resource_trigger` /
  `mcp_prompt_trigger`, and dotted connector types such as
  `teams.new_channel_message_trigger`.
- MCP endpoints come from `builtin_endpoints.mcp: true`, not a trigger.
