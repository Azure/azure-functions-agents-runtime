# Azure Functions Agents Runtime

> **Public preview.** The features described here are available for preview use and may change before general availability.

A markdown-first programming model for building AI agents on Azure Functions, powered by the [Microsoft Agent Framework (MAF)](https://github.com/microsoft/agent-framework).

- **Build agents with markdown** — write instructions, configure triggers, and bind tools in `.agent.md` files
- **Run on any Azure Functions trigger** — trigger agents on timer, queue, blob, HTTP, Event Hub, Service Bus, Cosmos DB, and more
- **Connect to 1,400+ services** — use connector-backed MCP servers to let agents act through Office 365, Teams, SQL, Salesforce, SAP, and hundreds of other connectors
- **Extend with MCP servers** — plug in remote HTTP MCP servers, including MCP servers backed by connectors
- **Build custom tools in plain Python** — drop a `.py` file in `tools/`, decorate functions with `@tool`, and pull in any package you need
- **Run agents on durable workflows** *(experimental, see [Dynamic workflows](workflows.md))* — one frontmatter flag turns on a DAG-of-tools execution model that fans out, waits, and survives restarts, **without** burning tokens on intermediate results
- **Automatic HTTP and MCP endpoints** — optionally expose your agent as an HTTP chat API and MCP server with no extra code
- **Serverless with built-in session management** — scales to zero, persists multi-turn conversations in Azure Blob Storage
- **Pluggable model providers** — bring OpenAI, Azure OpenAI, or Microsoft Foundry credentials and the runtime auto-detects the right client
- **Explicit MAF execution modes** — use default agent behavior or harness execution with token-budget conversation compaction

## Installation

The package is published on PyPI as **`azurefunctions-agents-runtime`**.

```bash
pip install azurefunctions-agents-runtime
```

Add it to your function app's `requirements.txt`:

```
azurefunctions-agents-runtime
```

## Where to go next

- [Getting started](getting-started.md) — create your first agent and run it locally
- [Architecture](architecture.md) — module map and data flow pipeline
- [Front matter spec](front-matter-spec.md) — the `.agent.md` and `agents.config.yaml` field reference
- [Triggers](triggers.md) — supported trigger types and payload shapes
- [Observability](observability.md) — tracing and telemetry
- [Dynamic workflows](workflows.md) — experimental durable DAG execution

Source code and issues live on GitHub: [Azure/azure-functions-agents-runtime](https://github.com/Azure/azure-functions-agents-runtime).
