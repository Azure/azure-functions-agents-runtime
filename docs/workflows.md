# Dynamic workflows (experimental v1)

> [!NOTE]
> **Status: public experimental v1.** The API is intentionally small and
> may change based on early feedback, but the behavior described here is
> the supported v1 surface. Run the
> [workflow-incident-triage sample](../samples/workflow-incident-triage/README.md)
> for the interactive experience, or the
> [timer-trigger sample](../samples/workflow-timer-trigger/README.md) for a
> non-interactive starter. Larger features such as sub-orchestrations,
> sub-agent tasks, configurable retry policies, and MCP Tasks integration
> are tracked as v2 follow-up work.

Dynamic workflows let a markdown agent author and run **distributed,
observable, durable** plans without writing orchestration code. Flip
`workflows.enabled: true` in the agent's frontmatter and the agent gains a
small set of built-in tools that author and launch
[Azure Durable Functions](https://learn.microsoft.com/azure/azure-functions/durable/)
orchestrations of workflow-safe tool calls and durable timers.

## Who this is for

Dynamic workflows are a fit when an agent needs to:

- **process large datasets** where only an aggregate or summary should
  reach the chat (e.g., scan 50 endpoints, summarize anomalies);
- **run multi-step plans** (3+ dependent tool calls) where each model
  round-trip would burn tokens and latency;
- **fan out** independent work across many parallel tool calls;
- **wait** on durable timers without holding a worker hot;
- **survive** worker restarts or long pauses (minutes to hours);
- **be observed and controlled** from outside the agent loop;

They are **not** the right tool for:

- work that fits comfortably inside a single chat turn — the
  orchestration overhead would dominate;
- tools that need an immediate user response (the workflow tool returns
  immediately with an ID; the *result* is fetched on a later turn);
- hand-authored orchestration DSLs — plans are LLM-authored only, by
  design, so there is no YAML/markdown workflow template format;
- cross-app or multi-agent coordination. Those are v2 scenarios; v1
  workflows live inside one Functions app and are enabled only by its
  `main.agent.md`.

## Why workflows (token, latency, context)

Dynamic workflows give an agent the same benefits that motivate
[programmatic tool calling][ptc] in other LLM platforms — the LLM authors
a *plan that calls tools* rather than calling them one-by-one through chat
round-trips — and add durability, observability, and cooperative control
on top.

Three concrete wins versus chaining tool calls in conversation:

- **Lower token cost.** Intermediate task results stay inside the
  orchestration. The agent sees only the final completion envelope (or a
  summary task you wired in), not every fan-out result. Anthropic
  [reports][ptc] roughly a 10× reduction on multi-tool workflows; the
  shape of the saving is the same here.
- **Lower latency.** Each direct tool call is a round-trip through the
  model. A 20-step plan is one model turn to author the workflow, not 20.
  The orchestrator drives the fan-out and sequencing in pure
  infrastructure.
- **Context-window discipline.** Hundreds of kilobytes of intermediate
  data — log lines, line items, search hits — never reach the model's
  context. The agent reasons over the *summary*, which is what it would
  have produced anyway after seeing the raw data.

…and three more that PTC's container-based model can't offer:

- **Survives worker restarts and long sleeps.** Workflows that take hours
  or days are first-class — no client connection has to stay open.
- **Operable from outside the agent loop.** `list_workflows`,
  `get_workflow_status`, `cancel_workflow`, and the optional Durable Task
  Scheduler portal give operators a way to see and steer in-flight work
  without going through the chat session.

[ptc]: https://platform.claude.com/docs/en/agents-and-tools/tool-use/programmatic-tool-calling

## How it works

1. You enable workflows on an agent with a one-line frontmatter flag.
2. The agent is given five built-in tools (see [Tools](#tools)).
3. When the agent decides the work is workflow-shaped, it calls
   `start_workflow` with a DAG of tasks. The DAG is validated and scheduled
   as a Durable orchestration; the tool returns immediately with a
   `workflow_id`.
4. The orchestration runs each task as a Durable activity (tool calls) or
   a Durable timer (waits), using `task_all` to fan out parallel tasks and
   `depends_on` edges for sequencing.
5. **`start_workflow` is fire-and-forget from the agent's perspective.**
   After receiving the `workflow_id`, the agent reports or records it as its
   invocation channel allows and ends its turn. The agent does not poll
   `get_workflow_status` to wait for completion.
6. The chat client (the built-in chat UI, or any external poller) polls
   `GET /agents/{slug}/workflows` on a short interval while the session is
   visible, renders a live per-task progress card alongside the chat
   thread, and updates the card with the final result envelope when the
   workflow terminates. The user sees per-task progress live without the
   agent doing any work.
7. When the workflow reaches a terminal state, the built-in chat UI
   detects the transition and **injects a synthetic user message
   containing one or more `<workflow-notification>` envelopes into
   the conversation**, prompting the agent to call
   `get_workflow_status` once per listed `<workflow-id>` and produce
   a short natural-language summary. The user gets a final
   conversational turn that closes the loop without having to type
   anything. See [Auto-notification](#auto-notification) below.
8. If the user later asks the agent about a previously-started
   workflow ("what did the incident workflow find?"), the agent calls
   `get_workflow_status` on demand and reports back. The on-demand
   call and the auto-notification turn are the two paths by which
   workflow output enters the agent's context window.

> [!NOTE]
> **Intermediate task results never enter the agent's context window.**
> The agent receives only the `workflow_id` from `start_workflow`. Per-task
> results stay in the workflow store; the chat client renders them next
> to the conversation. The only output the agent ever ingests is the
> single final-result envelope it fetches via `get_workflow_status` —
> either when the chat client posts a synthetic
> `<workflow-notification>` user message (see
> [Auto-notification](#auto-notification)) or when the user
> explicitly asks a follow-up question. This is the same context-window
> discipline that makes [programmatic tool calling][ptc] cheap.

The design is intentionally aligned with the
[MCP Tasks SEP-2557 proposal](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2557);
future direct MCP Tasks support will be a thin protocol shim.

## Frontmatter

```yaml
---
name: Incident Triage Assistant
description: ...
workflows:
  enabled: true
  # Optional deny-list of workflow tools to withhold from this agent.
  # Defaults to every public @workflow_tool discovered from tools/.
  exclude:
    - expensive_diagnostics
  # Future v2 knobs (not honored by v1):
  # max_nodes: 100
  # allowed_sub_agents: []
  #
  # Note: the Durable execution backend (Azure Storage vs Durable Task
  # Scheduler) and the task hub name are configured in host.json's
  # `extensions.durableTask.storageProvider` block (and matching app
  # settings), NOT here — the library never reads or routes on backend.
---
```

When `workflows.enabled: true`, the framework auto-injects the five
workflow tools into the agent's schema **and** appends a short
behavioral addendum to the agent's system prompt explaining when to
prefer `start_workflow` over direct tool calls. The agent author does
not need to document the tools or the heuristics in their markdown — the
agent markdown stays focused on the domain.

> [!IMPORTANT]
> **v1 constraint:** `workflows.enabled: true` is only honored on
> `main.agent.md`. That main agent may be invoked interactively or by a declared
> trigger. Other agents
> that set the flag get a startup warning and the tools are not injected.
> A future release will lift this constraint.

### Workflow tool authoring

Workflow tasks run later inside Durable Function activities, so they use
an explicit opt-in marker separate from normal MAF tools. Put workflow
handlers in the same `tools/` directory as normal tools and decorate each
Durable-activity-safe handler with `@workflow_tool`:

```python
# tools/incident_tools.py
from typing import Any

from azure_functions_agents import workflow_tool


@workflow_tool(description="Fetch recent log lines for a service.")
def fetch_logs(args: dict[str, Any]) -> dict[str, Any]:
    service = args["service"]
    return {"service": service, "lines": ["..."]}
```

The Activity runner calls the handler as `handler(args)`. v1 handlers
must be synchronous, accept a single dictionary argument, and return a
JSON-serializable value. Async handlers, reserved workflow-management
names, and duplicate workflow names are rejected or skipped during
startup.

Normal tools keep their existing behavior: a plain public function or an
`@tool`/`FunctionTool` in `tools/*.py` becomes a normal MAF tool. Use both
decorators when one callable should be available both directly in chat
and inside workflows:

```python
from azure_functions_agents import tool, workflow_tool


@tool
@workflow_tool(description="Summarize evidence collected by a workflow.")
def summarize(args: dict[str, object]) -> dict[str, object]:
    return {"summary": "..."}
```

Use `_`-prefixed helper functions for code that should be neither a
normal tool nor a workflow tool.

## Tools

Five tools are added to the agent's schema when `workflows.enabled: true`:

| Tool | Purpose |
| --- | --- |
| `start_workflow(plan)` | Validate a DAG, start an orchestration, return `{workflow_id}` immediately. |
| `get_workflow_status(workflow_id)` | Return the current status envelope (see below). |
| `list_workflows()` | List workflows owned by the current session. |
| `cancel_workflow(workflow_id, reason?)` | Cooperative cancel — raises an external event the orchestrator handles; the completion activity still runs. |
| `terminate_workflow(workflow_id, reason?)` | Hard terminate — stops the instance abruptly; final status is observable but no completion envelope is guaranteed. |

Workflow-management tools are never reachable as workflow-node targets —
a plan that tries to call `start_workflow` from inside a workflow fails
validation.

## DAG schema (v1)

A workflow plan is a list of tasks with `depends_on` edges. Task types:

- **`tool`** — call a discovered `@workflow_tool` by name with args.
- **`wait`** — durable timer. Accepts `duration` (ISO-8601, e.g. `PT30S`)
  or `until` (absolute ISO-8601 timestamp).

v1 does not support per-task timeout or retry fields yet. Those are v2
hardening controls.

```json
{
  "tasks": [
    { "id": "fetch_a", "type": "tool", "tool": "fetch_url", "args": {"url": "..."} },
    { "id": "fetch_b", "type": "tool", "tool": "fetch_url", "args": {"url": "..."} },
    { "id": "cool_down", "type": "wait", "duration": "PT30S",
      "depends_on": ["fetch_a", "fetch_b"] },
    { "id": "summarize", "type": "tool", "tool": "summarize",
      "args": {"sources": ["${fetch_a.result}", "${fetch_b.result}"]},
      "depends_on": ["cool_down"] }
  ]
}
```

### Templating

`${node_id.result}` and `${node_id.result.path.to.field}` are resolved
**inside the orchestrator** against JSON-normalized prior outputs.
The validator checks that template references are well-formed and point
to upstream tasks. Dotted-path traversal is resolved at orchestration
time; if a key or list index is missing, the workflow fails with a
deterministic template-resolution error that identifies the task and
path segment that could not be resolved.

### Caps

Enforced during plan validation and at runtime:

| Cap | Default |
|---|---|
| `max_nodes` | 50 |
| `max_parallelism` | 10 |
| `max_wait_duration` | 24h |
| `max_active_workflows_per_session` | 10 |
| `max_list_workflows_results` | 25 |

Future v2 hardening adds configurable frontmatter caps, per-tool timeout
caps, retry policy, storage hygiene, and large-output offloading.

### Determinism contract

The orchestrator holds these invariants:

- Ready tasks are scheduled in a deterministic order (sorted by task id).
- Time-dependent logic uses `context.current_utc_datetime` only.
- Activity results must be JSON-serializable; non-serializable results
  cause a hard, deterministic failure.
- Templating is evaluated over JSON-normalized prior outputs.

## Status envelope

Returned by `get_workflow_status` and (per-workflow, in an array) by
`GET /agents/{slug}/workflows`. The same shape is used everywhere a status is
read so external clients (operator dashboards, MCP Tasks bridges) can
consume a single contract:

```json
{
  "workflow_id": "...",
  "runtime_status": "Running|Completed|Failed|Terminated|Canceled|Pending",
  "custom_status": "3/7 tasks done, current=summarize",
  "output": { "...": "..." },
  "created_time": "...",
  "last_updated_time": "..."
}
```

`runtime_status` is the canonical value the chat UI cards and any
external poller render against. `output` is populated only when the
workflow has reached a terminal state and (for cooperative cancel)
includes any partial results gathered before the cancel signal landed.

## Completion delivery

Completion is channel-specific. Interactive chat uses polling and a synthetic
notification turn; declared triggers use an explicit terminal result sink.

### Interactive chat completion

Completion delivery is **poll-based**, by design. There is no push
channel from the orchestrator into the agent's chat thread.

- The chat client (the built-in chat UI under `/`, or any external
  poller) calls `GET /agents/{slug}/workflows` on a 2–5 second cadence while
  the chat session is visible. It receives an array of status
  envelopes for the calling session's workflows, renders a per-workflow
  progress card next to the chat thread, and updates the card when the
  workflow reaches a terminal state.
- The agent itself never receives the completion envelope as a tool
  result. After `start_workflow` returns the `workflow_id`, the agent's
  job is done; it should report the ID and end the turn. When the chat
  client detects a terminal-state transition it posts a synthetic user
  message containing one or more `<workflow-notification>` envelopes
  (see [Auto-notification](#auto-notification) below); that message —
  and any user-driven follow-up — are the only paths by which workflow
  output enters the agent's context window via `get_workflow_status`.
- The `GET /agents/{slug}/workflows` endpoint is scoped to the calling session
  via the `x-ms-session-id` request header and the per-workflow
  ownership scheme described in [Ownership](#ownership).

The data shape maps directly onto MCP Tasks SEP-2557 (`CreateTaskResult`,
`tasks/get`, `tasks/cancel`); future direct MCP Tasks support is a thin
protocol shim.

### Auto-notification

When the built-in chat UI's poll loop observes a workflow transition
to a terminal state (`Completed`, `Failed`, `Canceled`, `Terminated`),
it injects a synthetic user message into the conversation containing
one `<workflow-notification>` envelope per finished workflow plus a
single short reminder, of the form:

```text
<workflow-notification>
  <workflow-id>abc-123</workflow-id>
  <status>Completed</status>
  <summary>Workflow abc-123 finished with status Completed.</summary>
</workflow-notification>

Call `get_workflow_status` to retrieve the final result.
```

The injected message is deliberately data-only — modeled on the
`<task-notification>` shape used by Claude Code-style harnesses — and
carries **no prescriptive instructions** about how the agent should
respond. The agent's system prompt addendum already owns the contract
(call `get_workflow_status` once per `<workflow-id>`, summarize, no
follow-on workflows, race-handling, empty-output handling), so per
turn the model only needs the data plus a single reminder of the
relevant tool. This keeps notification turns lean and lets a future
chat-UI rendering layer parse the wrapper to display a richer
collapsed card without changing the agent contract.

This is a built-in-chat-UI convenience; it is **not** part of the
runtime contract enforced by the framework. External clients (e.g.
an MCP-Tasks-aware client) are free to adopt the same convention or
to drive completion handling some other way (e.g. a dedicated `task
completed` UI event with no synthetic prompt). The server-side
mechanics — `GET /agents/{slug}/workflows`, `get_workflow_status`, ownership
scoping — are the actual contract; the synthetic-prompt format is a
client-side detail.

The chat UI persists a per-`{baseUrl, sessionId}` set of already-
notified workflow ids in `sessionStorage`, so refreshing the page
after a summary turn has landed does not re-fire the notification.
Same-poll concurrent completions are batched into one notification
turn.

### Trigger-started workflows

Any supported Markdown-declared trigger on a workflow-enabled `main.agent.md`
can start a Dynamic Workflow:

1. The agent receives the trigger payload and authors a workflow plan.
2. `start_workflow` schedules the orchestration and immediately returns a
   `workflow_id`.
3. The trigger Function returns after the agent's initial turn; Durable
   Functions executes the workflow independently.

For an HTTP trigger, the caller receives the agent's immediate HTTP response,
not the eventual workflow result. The response may include `workflow_id` when
its authored schema/example permits it; response validation is unchanged.

Non-HTTP triggers have no response channel. Applications that need the eventual
result should provide a project workflow tool that writes or sends it to an
appropriate destination, such as a queue, database, webhook, or notification
service. The trigger-specific system guidance directs the agent to use that tool
as the workflow's final step. Use Durable Functions or Durable Task Scheduler
tooling for operational monitoring and control.

## Ownership

Every workflow's Durable instance ID is prefixed with
`sha256(session_id)[:12]` at creation. `get_workflow_status`,
`list_workflows`, `cancel_workflow`, and `terminate_workflow` filter
on that prefix; a workflow whose prefix does not match the calling
session's hash is treated as nonexistent (returns 404, never 403, so
existence cannot be probed by guessing IDs across sessions).

## Observability

- **Live-progress chat UI** — built-in poll loop renders per-node state
  in the chat session.
- **Terminal trigger sink** — non-interactive workflows publish their result
  from a final Activity chosen by the application.
- **Durable Task Scheduler portal** — when the app's
  `host.json` is configured with the DTS `storageProvider`, each
  workflow appears as a queryable instance with per-task state and retry
  history.
- **`customStatus`** — the orchestration emits a concise summary
  (`"3/7 tasks done, current=summarize"`) for low-cost polling.

## Requirements

- `azure-functions-durable` (installed transitively with
  `azure-functions-agents`).
- An Azure Storage connection string in `AzureWebJobsStorage` (already
  required for non-HTTP triggers; Azurite works locally). DTS is an
  optional Durable backend when configured in `host.json`.
- The default extension bundle (`[4.*, 5.0.0)`) already ships the Durable
  Task extension — no `host.json` changes are required.

## v1 scope and v2 backlog

v1 includes:

- five built-in workflow tools;
- DAG execution of `@workflow_tool` calls and wait tasks;
- fan-out/fan-in via `depends_on`;
- result templating with `${node_id.result}` and dotted paths;
- cooperative cancel and hard terminate;
- live progress in the built-in chat UI;
- workflow starts from supported Markdown-declared triggers;
- channel-specific chat notification and trigger terminal-sink guidance;
- Azure Storage and Durable Task Scheduler backends selected by
  `host.json`;
- fixed v1 guardrails for plan size, parallelism, wait duration, active
  workflows per session, and status-list result count.

v2 follow-up work includes enabling workflows for non-`main.agent.md`
agents, sub-orchestrations/sub-agent tasks, per-agent registry isolation,
configurable caps, retry and timeout policies, HMAC-backed workflow
ownership, blob-offloaded large outputs, an MCP Tasks bridge, richer error
taxonomy, and storage hygiene.
