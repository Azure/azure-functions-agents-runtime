# Serverless Agents Runtime — Architecture overview

> A high-level, presentation-friendly tour of the **Serverless Agents Runtime**,
> the experience powered by the **Azure Functions Agent Runtime (AFAR)**. It is
> meant as an on-ramp: read this first, then dive into
> [`docs/architecture.md`](architecture.md) for the authoritative module map and
> the full startup pipeline.

## 1. The one-liner

You **write markdown**, and the runtime gives you **a running Azure Functions app**.

Serverless Agents Runtime lets you build event-driven and scheduled AI agents by
authoring `*.agent.md` files plus a little configuration. The **Azure Functions
Agent Runtime (AFAR)** — the `azurefunctions-agents-runtime` package — reads
those files at startup and translates them into Azure Functions triggers, HTTP
routes, and MCP surfaces. Every agent runs on the [Microsoft Agent Framework
(MAF)](https://github.com/microsoft/agent-framework).

- **Markdown-first** — instructions, trigger, and tool bindings live in `.agent.md`.
- **Any Azure Functions trigger** — timer, queue, blob, HTTP, Event Hub, Service Bus, Cosmos DB, connectors, and more.
- **Serverless** — scales to zero; multi-turn sessions persist in Azure Blob Storage.
- **One line of Python** — `app = create_function_app()`.

## 2. What the author provides → AFAR → what runs

A project is a folder of authoring inputs. AFAR consumes them and emits a single
`azure.functions.FunctionApp` that the Azure Functions host can run.

```mermaid
flowchart LR
    subgraph inputs["Author inputs (on disk)"]
        A1["*.agent.md<br/>(or agents/ folder)"]
        A2["mcp.json"]
        A3["tools/*.py"]
        A4["skills/&lt;name&gt;/SKILL.md"]
        A5["agents.config.yaml"]
    end

    A1 --> AFAR
    A2 --> AFAR
    A3 --> AFAR
    A4 --> AFAR
    A5 --> AFAR

    AFAR["<b>Azure Functions Agent Runtime (AFAR)</b><br/>create_function_app()"] --> OUT["azure.functions.FunctionApp<br/>triggers · HTTP routes · MCP endpoints"]

    OUT --> HOST["Azure Functions host"]
```

| Input | What it declares |
| --- | --- |
| `*.agent.md` | One agent: its instructions (markdown body) + trigger and options (front matter). |
| `agents.config.yaml` | Shared defaults for every agent (model, timeout, system tools). |
| `mcp.json` | Remote HTTP / connector-backed **MCP servers** the agents may call. |
| `tools/*.py` | Custom Python tools (`@tool`) auto-discovered and offered to agents. |
| `skills/<name>/SKILL.md` | Progressive-disclosure prompt modules loaded on demand. |

## 3. What's inside AFAR

AFAR is small and layered. At startup it does four jobs, then hands a finished
Function App back to the host.

```mermaid
flowchart TB
    subgraph AFAR["Azure Functions Agent Runtime"]
        direction TB
        CL["<b>Config loaders</b><br/>Read agents.config.yaml + *.agent.md,<br/>substitute env vars, produce typed config"]
        DISC["<b>Discovery</b><br/>Inventory tools/, skills/, mcp.json<br/>(read-only)"]
        CM["<b>Clients &amp; agents manager</b><br/>Build MAF chat clients + agent objects<br/>(pluggable ClientManager)"]
        HG["<b>Handler generators</b><br/>Generate the function code for HTTP and<br/>non-HTTP triggers — each closure calls run_agent"]
        EP["<b>Built-in endpoint handlers</b><br/>Optional HTTP chat API + MCP tool surfaces"]
    end

    CL --> DISC --> HG
    CL --> EP
    CM -.->|used at invocation| HG
    CM -.->|used at invocation| EP
```

| Responsibility | What it does | Where it lives |
| --- | --- | --- |
| **Config loaders** | Load configuration from the agent files; resolve env vars; build `AgentSpec`, `GlobalConfig`, `ResolvedAgent`. | `config/` |
| **Clients & agents manager** | Manage MAF chat clients and agent objects; swappable via `set_client_manager()`. | `client_manager.py`, `runner.py` |
| **Handler generators** | Generate the callable that turns trigger data / HTTP bodies into a prompt — internally calls `run_agent`. | `registration/_handlers.py`, `registration/triggers.py` |
| **Built-in endpoint creation** | Register optional debug chat UI, REST chat, SSE stream, and **MCP** tool. | `registration/endpoints.py` |

> Discovery is **read-only** (it only inventories what exists). Registration is the
> **only** Azure-aware stage. Execution is **deferred** — handlers call the runner
> lazily, only when a trigger or route actually fires.

## 4. The startup pipeline (9 steps)

When the host imports your `function_app.py` and calls `create_function_app()`,
AFAR runs these steps once. Left of the divider is **translation** (author input →
typed objects); right of it is **registration** (typed objects → Azure bindings).

```mermaid
flowchart TB
    S1["1 · Resolve app root<br/><i>explicit &gt; AZURE_FUNCTIONS_AGENTS_APP_ROOT<br/>&gt; AzureWebJobsScriptRoot &gt; cwd</i>"]
    S2["2 · Load global agents.config.yaml<br/><i>(optional)</i>"]
    S3["3 · Load all *.agent.md front matter<br/>→ AgentSpec objects"]
    S4["4 · Discover tools, skills, MCP servers<br/>from disk"]
    S5["5 · Compose a ResolvedAgent per spec<br/><i>global defaults + agent overrides</i>"]
    S6["6 · Validate each ResolvedAgent<br/><i>required fields, MCP exclude refs, ...</i>"]
    S7["7 · Build AgentCapabilities per agent<br/><i>apply mcp / skills / tools filters</i>"]
    S8["8 · Create the FunctionApp"]
    S9["9 · Register each agent's trigger (if any)<br/>+ built-in endpoints (if any)"]

    S1 --> S2 --> S3 --> S4 --> S5 --> S6 --> S7 --> S8 --> S9

    S9 --> OUT["Configured azure.functions.FunctionApp"]
```

Each step feeds the next: later stages trust that earlier ones already reduced
free-form author input into typed, validated objects. Registration never
re-parses YAML or front matter — it consumes `ResolvedAgent` and
`AgentCapabilities`. The passport objects that flow through are:

`Path` → `GlobalConfig` + `list[AgentSpec]` → `ResolvedAgent` → `AgentCapabilities` → `FunctionApp`

## 5. Behind the scenes — the Daily Tech News agent

Here is what AFAR does for a real agent. This is the sample Nick demos
([`samples/daily-tech-news-email`](../samples/daily-tech-news-email)).

**The author writes three small files:**

`daily_tech_news.agent.md`
```markdown
---
name: Daily Tech News Email
description: Fetches top tech news and emails a summary daily.
trigger:
  type: timer_trigger
  args:
    schedule: "0 0 15 * * *"
---

You are a news assistant. When triggered:
1. Find today's top tech headlines from reputable sources, with links.
2. Summarize them as a concise HTML email.
3. Email it to $TO_EMAIL with the subject "Daily Tech News Summary".
```

`mcp.json` (the Office 365 Outlook send-email tool, via a connector-backed MCP server)
```json
{
  "servers": {
    "office365-outlook": {
      "type": "http",
      "url": "$O365_MCP_SERVER_URL",
      "tools": ["office365_SendEmailV2"],
      "auth": { "scope": "https://apihub.azure.com/.default", "client_id": "$O365_MCP_CLIENT_ID" }
    }
  }
}
```

`agents.config.yaml` (shared defaults) + `function_app.py` (`app = create_function_app()`).

**What AFAR does with them at startup:**

1. Loads the front matter → an `AgentSpec` (name, description, `timer_trigger` at `0 0 15 * * *`, instructions body).
2. Discovers the `office365-outlook` MCP server from `mcp.json` and any `tools/` and `skills/`.
3. Composes a `ResolvedAgent`: applies the shared model (`$FOUNDRY_MODEL`) and `timeout: 900` from `agents.config.yaml`, resolves `$TO_EMAIL` in the instructions.
4. Validates it (has a trigger ✓, MCP references resolve ✓) and builds `AgentCapabilities` (the `office365_SendEmailV2` tool + code-interpreter sandbox).
5. Registers **one timer-triggered Azure Function** on the Function App, wiring a generated handler to it.

**What happens when the timer fires (15:00 UTC daily):**

```mermaid
sequenceDiagram
    participant Host as Azure Functions host
    participant H as Generated handler
    participant R as runner.run_agent
    participant CM as ClientManager
    participant MAF as Microsoft Agent Framework
    participant O365 as O365 Outlook MCP tool

    Host->>H: Timer fires (schedule 0 0 15 * * *)
    H->>R: Build prompt from trigger data + instructions
    R->>CM: Get chat client (Foundry model)
    R->>MAF: Run agent with tools (MCP + sandbox)
    MAF->>MAF: Search web, summarize as HTML
    MAF->>O365: office365_SendEmailV2(to=$TO_EMAIL, ...)
    O365-->>MAF: Sent
    MAF-->>R: Final result
```

The author never wrote a trigger binding, an HTTP handler, an MCP client, or
session-management code. AFAR generated all of it from the three files.

## 6. Where to go next

- [`docs/architecture.md`](architecture.md) — authoritative module map, data flow, and the full pipeline with implementing functions.
- [`docs/front-matter-spec.md`](front-matter-spec.md) — the `.agent.md` authoring format, field by field.
- [`docs/triggers.md`](triggers.md) — supported trigger types and their arguments.
- [`README.md`](../README.md) — install, quickstart, and model-provider configuration.
