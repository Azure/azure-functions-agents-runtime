# Multi-Agent Delegation

A sample demonstrating **chat-time delegation**: a coordinator agent asks declared
specialist agents for help and combines their answers into one consolidated
response, without the user ever choosing an endpoint themselves.

| Trigger | Built-in Endpoints | Custom Tools | Connectors | MCP Servers | Skills | Sandbox | Chat UI |
|---|---|---|---|---|---|---|---|
| ✅ HTTP | ✅ | | | | | | ✅ |

## Features

- **One assistant throughout** — the user talks to a single coordinator; it
  consults specialists internally and gives one final answer
- **Object-form `subagents:`** — the coordinator declares its specialists by
  file-stem slug, with an optional `when:` routing hint
- **An endpoint-less internal specialist** — `tech` has no trigger and no
  built-in endpoint; it is reachable only through delegation
- **A specialist that is also independently runnable** — `billing` has its
  own HTTP endpoint *and* can be delegated to; either way it runs as itself

## Project Structure

```
src/
├── function_app.py
├── host.json
├── agents.config.yaml
├── main.agent.md              # Coordinator (is_main, builtin_endpoints, subagents:)
├── agents/
│   ├── billing.agent.md       # Specialist with its own HTTP endpoint
│   └── tech.agent.md          # Specialist with NO trigger/endpoint (internal only)
└── requirements.txt
```

## How It Works

`main.agent.md` declares two specialists in its `subagents:` front matter:

```yaml
subagents:
  - agent: billing               # references agents/billing.agent.md by its file-stem slug
    when: Invoices, charges, refunds, or subscription questions
  - agent: tech                  # `when` omitted -> uses tech's own `description`
```

At chat time, the coordinator's own model decides whether a specialist is
needed and, if so, which one to call. The specialist runs the request and
returns its answer as a tool result, which the coordinator folds into its
own reply to the user.

## Prerequisites

- [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local)
- Python 3.13+

## Run Locally

1. **Create local settings:**
   ```bash
   cd samples/multi-agent-delegation/src
   cp local.settings.template.json local.settings.json
   ```

2. **Configure your provider:**
   Edit `local.settings.json` to set your AI provider credentials.

3. **Start the function app:**
   ```bash
   func start
   ```

4. **Try it out:**
   - Coordinator chat UI: `http://localhost:7071/agents/main/`
   - Coordinator HTTP endpoint: `POST http://localhost:7071/agents/main/chat`
   - Billing specialist's own endpoint: `POST http://localhost:7071/billing`
   - Ask the coordinator something like *"Why was I charged twice this
     month?"* and it should delegate to `billing`; ask *"How do I reset my
     password?"* and it should delegate to `tech`.
   - `tech` has no endpoint of its own — it is only reachable through the
     coordinator.

## Learn More

A delegated call runs in-process and bypasses the specialist's own endpoint
authorization — treat one deployed app as one trust domain, and only
delegate to specialists you're comfortable exposing this way. See the
[`subagents` front-matter reference](../../docs/front-matter-spec.md#subagents)
for the full field spec, trust boundary, and task-isolation details, and
[FRD 0006](../../docs/frds/0006-multi-agent-delegation.md) for the design
rationale.
