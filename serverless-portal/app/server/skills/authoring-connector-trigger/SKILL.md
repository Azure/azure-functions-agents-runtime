---
name: authoring-connector-trigger
description: How to add a connector trigger (e.g. new Outlook email, new Teams message) to an azure-functions-agents-runtime agent. Connector triggers are declarative — a generic_trigger with type=connectorTrigger in .agent.md, plus a matching Connector Gateway connection and the connector's MCP tool in mcp.json.
---

# Authoring a connector trigger

A **connector trigger** runs an agent when an event fires on an Azure
Connector Gateway connection (a new Outlook email, a new Teams channel
message, a new SharePoint item, …). In the azure-functions-agents-runtime it
is **declarative**: a `generic_trigger` block with `type: connectorTrigger` in
the agent's `<name>.agent.md` front matter. Do **not** author a Python
connector function — the framework's `function_app.py` stays a single line:

```python
from azure_functions_agents import create_function_app

app = create_function_app()
```

The runtime reads each `.agent.md`, registers the Azure Functions
`generic_trigger` at startup, and runs the agent's instructions each time the
connector fires. `connector_trigger` is an alias — either name is accepted.

## `.agent.md` shape

```yaml
---
name: <concise name>
description: <one line>
trigger:
  type: generic_trigger            # or `connector_trigger` (alias)
  args:
    type: connectorTrigger
---

<Markdown instructions: read every field of the trigger payload the connector
sends (subject, sender, body, item id, …), decide what to do, and act via the
matching MCP tool.>
```

Rules:

- The connector event's payload is passed straight to the agent as a JSON
  object — the instructions must reference it explicitly.
- Do **not** set `arg_name` — the runtime injects it.
- String values under `trigger.*` support `$ENV_VAR` substitution.

## Two prerequisites to go live

A `.agent.md` alone is **not** enough. Two Azure-side pieces must also exist:

1. **A Connector Gateway connection** — created in the Azure portal or via
   ARM. The connection defines which mailbox / team / site the trigger listens
   to and authenticates as. The Connector Gateway's namespace/gateway wires the
   connection to the Function App.
2. **The connector's MCP tool in `mcp.json`** — so the agent can *act* on the
   event (reply to the email, post a message, update the item). Every Azure
   Connector exposes an MCP tool with the same name as the connector, and the
   portal ships a preset for the common ones (Office 365 Outlook, Microsoft
   Teams). Add it to `mcp.json`:

   ```json
   {
     "servers": {
       "office365-outlook": {
         "type": "http",
         "url": "$O365_MCP_URL",
         "auth": { "scope": "https://apihub.azure.com/.default" }
       }
     }
   }
   ```

## Writing the instructions

Connector agents receive a rich payload. Be explicit about:

- **Every field to read** — subject, sender, body, message id, thread id.
- **When to skip** — filters on sender, subject, or body content.
- **Which MCP tool to invoke** — always name it (`office365-outlook`) and
  the specific tool method.
- **What context to pass through** — the message id and thread id, so the
  reply threads correctly.
- **Failure behaviour** — how to log or notify when a downstream call fails.

## Anti-patterns

- ❌ Creating a Python connector function that calls the agent over HTTP.
  The connector event **is** the agent invocation.
- ❌ Adding a connector trigger without also wiring the MCP tool — the agent
  can react but can't act.
- ❌ Adding an MCP tool without an Azure-side connection — the tool call
  will 401/404 at runtime.
- ❌ Assuming the payload shape — read it out of the trigger event object
  and validate every field.
