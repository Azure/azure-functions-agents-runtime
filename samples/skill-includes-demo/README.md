# Skill Resources Demo

Demonstrates MAF's progressive disclosure pattern for organizing skills with modular reference files.

| Trigger | Built-in Endpoints | Custom Tools | Connectors | MCP Servers | Skills | Sandbox | Chat UI |
|---|---|---|---|---|---|---|---|
| | ✅ HTTP | | | | ✅ | | ✅ |

## Features

- **Progressive Disclosure** — agent loads reference content on demand via `read_skill_resource`
- **Modular Skill Organization** — split large skills into maintainable reference files
- **Nested Directory Support** — organize assets and references in subdirectories
- **Token Efficient** — agent only loads what it needs, when it needs it
- **Chat UI** — built-in interface at `/agents/main/`

## Skill Structure

This sample showcases a skill organized with MAF's resource pattern:

```
skills/
└── api-assistant/
    ├── SKILL.md              # Main skill instructions (keep <500 lines)
    ├── references/
    │   ├── endpoints.md      # API endpoint documentation
    │   └── error-codes.md    # Error handling reference
    └── examples/
        └── requests.md       # Example API requests
```

The `SKILL.md` file lists available resources so the agent knows they exist:

```markdown
## Available Resources

- `references/endpoints.md` - Full API endpoint documentation
- `references/error-codes.md` - Error code reference and troubleshooting
- `examples/requests.md` - Example API requests with curl commands

For detailed endpoint documentation, read `references/endpoints.md`.
```

When the agent needs details, it calls `read_skill_resource` to fetch the relevant file.

## Benefits

- **Token Efficient** — agent only loads resources it actually needs
- **Maintainability** — edit reference docs independently without touching the main skill file
- **Clarity** — keep the main SKILL.md focused on high-level guidance
- **Version Control** — track changes to individual reference files separately
- **MAF Standard** — uses MAF's built-in progressive disclosure pattern

## Prerequisites

- [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local)
- Python 3.11+

## Run Locally

1. **Set up environment:**

   ```bash
   cd samples/skill-includes-demo/src
   cp local.settings.template.json local.settings.json
   # Edit local.settings.json with your settings
   ```

2. **Configure AI provider:**

   Set `AZURE_FUNCTIONS_AGENTS_PROVIDER` to `foundry` or `openai` and configure the corresponding endpoint/key.

3. **Start the function app:**

   ```bash
   func start
   ```

4. **Open the chat UI:**

   Navigate to `http://localhost:7071/agents/main/`

5. **Test the skill:**

   Ask questions like:
   - "What endpoints does the Widget API have?"
   - "How do I create a new widget?"
   - "What does error code 404 mean?"

## How It Works

1. The runtime discovers `skills/api-assistant/SKILL.md`
2. MAF advertises the skill name and description to the agent (~100 tokens)
3. When a task matches, the agent calls `load_skill` to get the full SKILL.md body
4. The agent sees the available resources listed and calls `read_skill_resource` as needed:
   - `read_skill_resource("api-assistant", "references/endpoints.md")` → endpoint details
   - `read_skill_resource("api-assistant", "references/error-codes.md")` → error reference
5. Only the resources actually needed are loaded into context

This progressive disclosure pattern keeps the agent's context window lean while giving it access to detailed reference material on demand.
