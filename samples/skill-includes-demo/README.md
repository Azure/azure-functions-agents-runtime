# Skill Includes Demo

Demonstrates markdown link includes for organizing skills with modular reference files.

| Trigger | Built-in Endpoints | Custom Tools | Connectors | MCP Servers | Skills | Sandbox | Chat UI |
|---|---|---|---|---|---|---|---|
| | ✅ HTTP | | | | ✅ | | ✅ |

## Features

- **Markdown Link Includes** — `[file](./path)` syntax to inline content from separate files
- **Modular Skill Organization** — split large skills into maintainable reference files
- **Nested Directory Support** — organize assets and references in subdirectories
- **Chat UI** — built-in interface at `/agents/main/`

## Skill Structure

This sample showcases a skill organized with markdown link includes:

```
skills/
└── api-assistant/
    ├── SKILL.md              # Main skill with [file](./path) includes
    ├── references/
    │   ├── endpoints.md      # API endpoint documentation
    │   └── error-codes.md    # Error handling reference
    └── examples/
        └── requests.md       # Example API requests
```

The `SKILL.md` file uses markdown links on their own lines to pull in content:

```markdown
## API Endpoints

[endpoints.md](./references/endpoints.md)

## Error Handling

[error-codes.md](./references/error-codes.md)
```

At startup, the runtime resolves all includes and provides the fully-assembled skill content to the agent.

## Benefits

- **Maintainability** — edit reference docs independently without touching the main skill file
- **Reusability** — share reference files across multiple skills (copy or symlink)
- **Clarity** — keep the main SKILL.md focused on structure while details live in dedicated files
- **Version Control** — track changes to individual reference files separately
- **Native Markdown** — includes use standard markdown link syntax, so files render correctly in GitHub/VS Code

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
2. During startup, markdown link includes are resolved:
   - `[endpoints.md](./references/endpoints.md)` → inlines endpoint documentation
   - `[error-codes.md](./references/error-codes.md)` → inlines error code reference
   - `[requests.md](./examples/requests.md)` → inlines example requests
3. The fully-resolved skill content is provided to MAF's `SkillsProvider`
4. The agent can load and use the skill with all reference content available
