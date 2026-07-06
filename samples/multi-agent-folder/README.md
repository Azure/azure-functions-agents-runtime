# Multi-Agent Folder Organization

A sample demonstrating how to organize multiple agents using the `agents/` folder convention.

| Trigger | Built-in Endpoints | Custom Tools | Connectors | MCP Servers | Skills | Sandbox | Chat UI |
|---|---|---|---|---|---|---|---|
| ✅ HTTP | ✅ | | | | | | ✅ |

## Features

- **Organized agents** — agents are organized in a dedicated `agents/` folder
- **Multiple agents** — demonstrates chat, research, and summary agents
- **Hybrid support** — shows how top-level and folder agents can coexist

## Project Structure

```
src/
├── function_app.py
├── host.json
├── agents.config.yaml
├── main.agent.md              # Top-level main agent (is_main=true)
├── agents/                    # Organized agents folder
│   ├── chat.agent.md          # Chat assistant agent
│   ├── research.agent.md      # Research agent
│   └── summary.agent.md       # Summary agent
└── requirements.txt
```

## How It Works

The runtime automatically discovers agents in both locations:
1. **Top-level** (`*.agent.md` at the root) — backward compatible
2. **agents/ folder** (`agents/*.agent.md`) — new organizational option

Both locations are merged, so you can:
- Keep a simple `main.agent.md` at the top level
- Organize feature-specific agents in the `agents/` folder
- Migrate incrementally without breaking existing setups

## Prerequisites

- [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local)
- Python 3.13+

## Run Locally

1. **Create local settings:**
   ```bash
   cd samples/multi-agent-folder/src
   cp local.settings.template.json local.settings.json
   ```

2. **Configure your provider:**
   Edit `local.settings.json` to set your AI provider credentials.

3. **Start the function app:**
   ```bash
   func start
   ```

4. **Test the agents:**
   - Main agent UI: `http://localhost:7071/agents/main/`
   - Chat agent: `POST http://localhost:7071/chat`
   - Research agent: `POST http://localhost:7071/research`
   - Summary agent: `POST http://localhost:7071/summary`

## Key Concepts

### The agents/ folder

The `agents/` folder is a convention for organizing agent definitions:
- Must be at the same level as `host.json` (app root)
- Case-insensitive (`agents/` or `Agents/`)
- Only immediate children are discovered (no recursion)
- `main.agent.md` in either location is marked `is_main=true`

### Hybrid organization

You can have agents in both locations:
```
src/
├── main.agent.md          # Top-level, is_main=true
└── agents/
    ├── helper.agent.md    # In folder
    └── worker.agent.md    # In folder
```

All agents are discovered and registered normally.
