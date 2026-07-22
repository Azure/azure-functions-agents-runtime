# Claude Agent - Code Review Assistant

A code review assistant powered by the flexible `CLAUDE.md` and `*.claude.md` naming conventions. This sample demonstrates how to use `CLAUDE.md` for the main agent and `*.claude.md` for additional agents, providing a Claude-themed alternative to standard `*.agent.md` naming.

| Trigger | Built-in Endpoints | Custom Tools | Connectors | MCP Servers | Skills | Sandbox | Chat UI |
|---|---|---|---|---|---|---|---|
| | ✅ HTTP + MCP | | | | | | ✅ |

## Features

- **CLAUDE.md Convention** — uses `CLAUDE.md` as the single-agent file (internally treated as `default.agent.md`)
- **Chat UI** — built-in single-page interface at `/agents/default/`
- **HTTP API** — `POST /agents/default/chat` (JSON) and `POST /agents/default/chatstream` (SSE)
- **MCP Tool** — exposed through `/runtime/webhooks/mcp` for connecting from VS Code, Claude Desktop, etc.
- **Code Review Expertise** — specialized in providing constructive code feedback and best practices
- **Session Persistence** — multi-turn conversations stored in Azure Blob Storage

## About CLAUDE.md and *.claude.md

This sample demonstrates two Claude-themed naming patterns:

### CLAUDE.md (Single Agent)
`CLAUDE.md` works as the main agent definition file. The runtime recognizes `CLAUDE.md` (case-insensitive) as a special single-agent file, similar to `agent.md` and `main.agent.md`:

- `CLAUDE.md` → internally normalized to `default.agent.md`
- Function name becomes `default`
- Marked as `is_main=True` for main agent behavior
- Works at top-level or in `agents/` folder

### *.claude.md (Multi-Agent)
For additional agents, use the `*.claude.md` pattern:

- `summarizer.claude.md` → internally normalized to `summarizer.agent.md`
- Function name becomes `summarizer`
- Works alongside other `*.agent.md` or `*.claude.md` files
- Preserves the prefix in the function name

Both patterns are case-insensitive and interoperate seamlessly with standard `*.agent.md` files.

## Prerequisites

- [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local)
- Python 3.13+
- An AI provider (Microsoft Foundry, Azure OpenAI, or OpenAI)

## Run Locally

1. **Create local settings:**
   ```bash
   cd samples/claude-agent/src
   cp local.settings.template.json local.settings.json
   ```

2. **Configure your AI provider:**
   
   Edit `local.settings.json` to set your provider credentials.
   
   **For Microsoft Foundry:**
   ```json
   {
     "Values": {
       "AZURE_FUNCTIONS_AGENTS_PROVIDER": "foundry",
       "FOUNDRY_PROJECT_ENDPOINT": "https://your-project.api.azureml.ms",
       "FOUNDRY_MODEL": "gpt-5.4"
     }
   }
   ```
   
   **For Azure OpenAI:**
   ```json
   {
     "Values": {
       "AZURE_FUNCTIONS_AGENTS_PROVIDER": "azure_openai",
       "AZURE_OPENAI_ENDPOINT": "https://your-resource.openai.azure.com",
       "AZURE_OPENAI_DEPLOYMENT": "your-deployment-name"
     }
   }
   ```
   
   **For OpenAI:**
   ```json
   {
     "Values": {
       "AZURE_FUNCTIONS_AGENTS_PROVIDER": "openai",
       "OPENAI_API_KEY": "your-api-key",
       "OPENAI_MODEL": "gpt-4.5"
     }
   }
   ```

3. **Start the function app:**
   ```bash
   func start
   ```

4. **Test the agents:**
   
   **Code Review Agent (default):**
   - **Chat UI:** `http://localhost:7071/agents/default/`
   - **Chat API:** `POST http://localhost:7071/agents/default/chat` with JSON body `{"prompt": "Review this code: def add(a,b): return a+b"}`
   - **Streaming API:** `POST http://localhost:7071/agents/default/chatstream` (Server-Sent Events)
   - **MCP Webhook:** `http://localhost:7071/runtime/webhooks/mcp` (for VS Code, Claude Desktop)
   
   **Text Summarizer Agent (summarizer):**
   - **Chat UI:** `http://localhost:7071/agents/summarizer/`
   - **Chat API:** `POST http://localhost:7071/agents/summarizer/chat` with JSON body `{"prompt": "Summarize this article: [long text]"}`
   - **Streaming API:** `POST http://localhost:7071/agents/summarizer/chatstream`

## Example Usage

### Code Review Request (CLAUDE.md)

```bash
curl -X POST http://localhost:7071/agents/default/chat \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Review this Python function:\n\ndef process_user_data(data):\n    result = []\n    for item in data:\n        if item != None:\n            result.append(item.upper())\n    return result"
  }'
```

The agent will provide a structured code review with:
- Security and bug analysis
- Performance improvements
- Style and best practice suggestions
- Positive feedback on well-written code

### Text Summarization Request (summarizer.claude.md)

```bash
curl -X POST http://localhost:7071/agents/summarizer/chat \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Summarize this article:\n\nArtificial intelligence continues to transform software development. Recent advances in large language models have enabled new capabilities in code generation, review, and documentation. Developers are increasingly adopting AI assistants to augment their workflows, leading to productivity gains across the industry. However, challenges remain around code quality, security, and the need for human oversight in critical systems."
  }'
```

The summarizer agent will provide:
- Main points extracted from the text
- A concise 2-3 paragraph summary
- Key insights and implications

## How It Works

- [`CLAUDE.md`](src/CLAUDE.md) defines the code review agent using the flexible single-agent naming convention
- [`summarizer.claude.md`](src/summarizer.claude.md) defines the text summarizer agent using the `*.claude.md` prefix pattern
- Both agents are discovered and registered automatically by the runtime
- The framework registers built-in HTTP chat endpoints, an MCP tool, and a chat UI for each agent
- The code review agent uses `default` as the function name; the summarizer uses `summarizer`

## Project Structure

```
src/
├── CLAUDE.md                      # Code review agent (single-agent convention)
├── summarizer.claude.md           # Text summarizer agent (*.claude.md pattern)
├── function_app.py                # Azure Functions app entry point
├── host.json                      # Functions host configuration
├── agents.config.yaml             # Global agent configuration
├── requirements.txt               # Python dependencies
└── local.settings.template.json   # Local settings template
```

## Key Concepts

### Claude Naming Conventions

The runtime supports Claude-themed naming patterns alongside standard `*.agent.md`:

**Single-Agent Files:**
- `agent.md` / `AGENT.MD` → `default` function
- `CLAUDE.md` / `claude.md` → `default` function  
- `main.agent.md` → `main` function

**Multi-Agent Files:**
- `<name>.agent.md` → `<name>` function
- `<name>.claude.md` → `<name>` function

All patterns are case-insensitive and can coexist in the same project.

### Code Review Specialization

The code review agent (CLAUDE.md) is pre-configured with:
- Structured review format (Summary, Critical Issues, Improvements, Positives)
- Focus on constructive feedback
- Context-aware suggestions
- Best practices for multiple programming languages

### Text Summarization

The summarizer agent (summarizer.claude.md) provides:
- Extraction of main points
- Concise multi-paragraph summaries
- Key insights and implications
- Structured output format

## Next Steps

- Customize the agents' personalities and instructions in their respective `.claude.md` files
- Add more agents using the `*.claude.md` pattern for different specializations
- Add custom tools for linting, static analysis, or content processing
- Deploy to Azure using Azure Developer CLI (`azd`)
- Connect the MCP tool to VS Code or Claude Desktop for IDE integration
