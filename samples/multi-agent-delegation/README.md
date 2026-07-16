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

At startup, the runtime resolves each `agent:` reference to the specialist's
file-stem slug (`billing.agent.md` → `billing`), fails fast if any slug
collides or a reference is unknown/duplicate/self-referential, and then
builds one `delegate_billing` and one `delegate_tech` function tool for the
coordinator, via Microsoft Agent Framework's `BaseAgent.as_tool()`.

At chat time, the coordinator's own model decides whether to call a
specialist — routing is **model-selected, not deterministic**. When it does:

1. The coordinator sends a single, self-contained `task` string — the
   specialist has no access to the coordinator's conversation history.
2. The specialist runs **as itself**: its own instructions, model, and static
   tools, inside the normal `agent.run()` tool-calling loop (no hand-off, no
   human-in-the-loop, no new `Workflow`).
3. The specialist's answer returns as an ordinary tool result. The
   coordinator folds it into its own reply.

Delegation is **single-level**: `tech` and `billing` do not get their own
`delegate_*` tools even if they declared `subagents:` themselves — a
delegated specialist never expands its own references.

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
   - Coordinator HTTP endpoint: `POST http://localhost:7071/main`
   - Billing specialist's own endpoint: `POST http://localhost:7071/billing`
   - Ask the coordinator something like *"Why was I charged twice this
     month?"* and it should delegate to `billing`; ask *"How do I reset my
     password?"* and it should delegate to `tech`.
   - `tech` has no endpoint of its own — it is only reachable through the
     coordinator.

## Key Concepts

### The `subagents:` field

`subagents:` is an optional, front-matter-only field on any independently
runnable agent (own trigger or `builtin_endpoints`). Each entry is an object,
`{agent: <slug>, when?: <hint>}` — there is no string shorthand and no `id`
or `tool_name` field. The delegated tool is always named `delegate_<slug>`.

### Trust boundary

`subagents` is an explicit **capability grant** from the app author, not a
per-request permission check. A delegated call runs in-process and does not
pass through the specialist's own endpoint authorization — anyone who can
invoke the coordinator can reach everything its declared specialists can do
(their tools, MCP servers, and skills). Treat one deployed app as one trust
domain, and only delegate to specialists you're comfortable exposing this
way.

### Self-contained `task`

A specialist receives exactly one string argument, `task`
(`propagate_session=False` — no shared chat history, no shared session
state). Write coordinator instructions (as in `main.agent.md` above) that
encourage the model to pack every fact the specialist needs into that one
request, since the specialist cannot ask the coordinator or the user a
follow-up question mid-delegation (no human-in-the-loop in v1).

### Single-level delegation

A specialist runs in the **delegated** execution role: its own
instructions/model/static tools, but never its own `delegate_*` tools, even
if it declares `subagents:` of its own. `billing` and `tech` could reference
each other with no risk of recursion — the runtime never wires a delegated
agent's own references.

### Observability

No extra tracing code is needed to see a delegation in Application
Insights: `execute_tool delegate_billing` and the nested
`invoke_agent billing` span it produces already share the coordinator run's
trace/`OperationId`. The runtime adds `af.delegate.*` attributes (specialist
slug, outcome) and delegate call/error metrics for parity with the sandbox
and `web_request` tools. See [`docs/observability.md`](../../docs/observability.md)
for the full span tree and sampling guidance for delegation-heavy apps.
