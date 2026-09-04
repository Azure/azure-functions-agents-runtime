# Hosted Skills

Author markdown-first Hosted Skills agents (`.agent.md`) the easy way — scaffolding,
an **Add Agent** wizard, front-matter IntelliSense/validation, and opening the built-in chat UI.

This extension targets the
[Azure Functions Agents Runtime](https://github.com/Azure/azure-functions-agents-runtime)
(`azurefunctions-agents-runtime` on PyPI), where an agent is a single `.agent.md` file:
YAML front matter describes the agent, and the markdown body **is** the system prompt.

## Features

### 📝 Front-matter IntelliSense & validation

- **Completions** for top-level keys, structured snippets (triggers, subagents, workflows,
  builtin endpoints), and trigger-type values — only inside the `---` front-matter block.
- **Live diagnostics** validate `.agent.md` front matter (and whole-file `agents.config.yaml`)
  against a JSON Schema generated directly from the runtime's Pydantic models, so IntelliSense
  stays in lockstep with the runtime.
- **Semantic checks** beyond the schema: unsupported trigger types and **slug collisions**
  across the app (two agents that would resolve to the same `/agents/<slug>/` route).
- **Hover** documentation for every front-matter field.

### 🏗️ Scaffolding & the Add Agent wizard

- **Scaffold New Agent App** — generate a ready-to-run app (`function_app.py`, `host.json`,
  `requirements.txt`, `local.settings.json`, `.funcignore`, a starter agent).
- **Add Agent** — a guided wizard that creates a new `.agent.md` with the front matter you
  choose (name, description, trigger, model, endpoints), placed correctly in the app.

### ▶️ Open Chat UI

- **Open Chat UI** — open the built-in browser chat UI for an agent that has
  `builtin_endpoints.debug_chat_ui` enabled (`/agents/<slug>/`). Point it at your running
  host with the `agentsAuthoring.localRun.baseUrl` setting.

### 🌳 Agents explorer

- A dedicated **Hosted Skills** view in the Activity Bar lists every agent in the workspace,
  grouped by app, with badges for the trigger type, chat UI / endpoints, and subagent count.
  Click an agent to open it; use the title-bar buttons to **Add Agent** or **Refresh**.

### 🧩 Tool, Skill & Subagent scaffolders

- **Add Tool** — create a discoverable `tools/<name>.py` (a plain sync/async function whose
  docstring becomes the tool description; the runtime auto-wraps it).
- **Add Skill** — create `skills/<name>/SKILL.md` with `name` / `description` front matter and
  a starter body.
- **Add Subagent** — pick another agent in the app and inject a `subagents:` entry (with an
  optional `when:` routing hint) into the active agent's front matter, preserving comments.

### 🔍 CodeLens

- Above the front matter of any `.agent.md`, inline actions show the agent **slug**, an
  **Open Chat UI** link (when the chat UI is enabled), and an **Add Subagent** shortcut.

## Using the features

### 1. Scaffold a new agent app

1. Open the Command Palette (<kbd>Ctrl/Cmd+Shift+P</kbd>) → **Hosted Skills: Scaffold New Agent App**.
2. Pick a parent folder and enter a subfolder name (or `.` to scaffold in place).
3. Choose whether to include an interactive **main** chat agent (chat UI + API + MCP).
4. Confirm the default model — it defaults to **`gpt-5.4`** and is written as `FOUNDRY_MODEL`
   in `local.settings.json`.

You get a ready-to-run project: `function_app.py`, `host.json`, `requirements.txt`,
`agents.config.yaml`, `local.settings.json`, `.funcignore`, and (optionally) `main.agent.md`.

### 2. Add an agent

1. Command Palette → **Hosted Skills: Add Agent**.
2. Enter a **name** and **description**.
3. Choose how it's invoked:
   - **Trigger** — event/HTTP/schedule-driven (http, timer, queue, blob, event grid, service
     bus, connector). You'll be prompted for the trigger's arguments.
   - **Endpoints only** — an interactive chat UI / REST API / MCP tool.
   - **Trigger + endpoints** — both.
4. Pick a **file stem**. This becomes `<stem>.agent.md` and the agent's globally-unique **slug**
   (the `/agents/<slug>/` route). The wizard blocks stems that would collide with an existing slug.
5. The new `.agent.md` opens in the editor. The markdown body below the front matter **is** the
   system prompt — edit it to define the agent's behavior.

### 3. Author with IntelliSense

Inside the `---` front-matter block of any `.agent.md`:

- **Completions** — start typing a key or press <kbd>Ctrl+Space</kbd> to insert fields. Structured
  fields (`trigger`, `subagents`, `builtin_endpoints`, `mcp`, `skills`, `tools`, `workflows`, …)
  expand as snippets with tab stops; `type:` offers the supported trigger types as an enum.
- **Hover** — hover any front-matter key or trigger type for its documentation.
- **Diagnostics** — invalid or unknown fields, wrong types, unsupported triggers, and
  **slug collisions** across the app are flagged inline (squiggles + Problems panel) as you type.
  The same validation applies to a whole-file `agents.config.yaml`.

> **Note:** VS Code's built-in `chatagent` language claims the `.agent.md` extension and applies
> its own (incompatible) agent-file schema. For agent files inside a Hosted Skills app, this
> extension re-maps them to **Markdown** so that schema doesn't flag Hosted Skills fields like
> `trigger` and `builtin_endpoints`. Disable this via `agentsAuthoring.treatAgentFilesAsMarkdown`
> if you'd rather keep VS Code's `chatagent` mode.

### 4. Run locally and open the chat UI

1. Ensure an agent has `builtin_endpoints.debug_chat_ui: true` (the scaffolded **main** agent does).
2. Start storage and the host from the app folder in a terminal:
   ```bash
   azurite --skipApiVersionCheck
   func start
   ```
3. Command Palette → **Hosted Skills: Open Chat UI**, then pick the agent. Your browser opens
   `http://localhost:7071/agents/<slug>/`. Change the host via the
   `agentsAuthoring.localRun.baseUrl` setting.

### 5. Add tools, skills, and subagents

- **Add a tool** — Command Palette → **Hosted Skills: Add Tool**. Enter a name and description,
  pick sync or async, and a `tools/<name>.py` is created and opened. Fill in the function body;
  the runtime discovers it automatically (first public function per file, docstring = description).
- **Add a skill** — Command Palette → **Hosted Skills: Add Skill**. Enter a name and description
  to create `skills/<name>/SKILL.md`. Edit the body with the instructions the agent should follow
  when it loads the skill.
- **Add a subagent** — open a coordinator `.agent.md`, then Command Palette →
  **Hosted Skills: Add Subagent** (or use the **Add Subagent** CodeLens). Pick another agent in
  the app and optionally add a `when:` hint; a `subagents:` entry is inserted into the front matter.

### 6. Browse agents in the explorer

Open the **Hosted Skills** view in the Activity Bar (the robot icon) to see all agents grouped by
app. Each row shows the slug, trigger type, and endpoint/subagent badges. Click a row to open the
agent; use the title-bar **Add Agent** (+) and **Refresh** buttons as needed.

## Commands

All commands are available from the Command Palette under the **Hosted Skills** category:

| Command | ID |
| --- | --- |
| Scaffold New Agent App | `agentsAuthoring.scaffoldApp` |
| Add Agent | `agentsAuthoring.addAgent` |
| Add Tool | `agentsAuthoring.addTool` |
| Add Skill | `agentsAuthoring.addSkill` |
| Add Subagent | `agentsAuthoring.addSubagent` |
| Open Chat UI | `agentsAuthoring.openChatUI` |

## Settings

| Setting | Default | Description |
| --- | --- | --- |
| `agentsAuthoring.validation.enabled` | `true` | Validate `.agent.md` front matter and `agents.config.yaml` against the Hosted Skills runtime schema. |
| `agentsAuthoring.treatAgentFilesAsMarkdown` | `true` | Treat Hosted Skills `.agent.md` / `.claude.md` files as Markdown instead of VS Code's built-in `chatagent` language, so VS Code's incompatible agent-file schema doesn't flag fields like `trigger` and `builtin_endpoints`. Applies only to files inside a Hosted Skills app. |
| `agentsAuthoring.localRun.baseUrl` | `http://localhost:7071` | Base URL used by **Open Chat UI**. |

## Requirements

The authoring features (IntelliSense, validation, hover, scaffolding, Add Agent) work with no
extra tooling installed.

To actually **run** an agent app locally (so **Open Chat UI** has a host to reach) you'll need
the tooling the runtime expects:

- [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local) (`func`) — start the host with `func start` from the app folder.
- [Azurite](https://learn.microsoft.com/azure/storage/common/storage-use-azurite) (`azurite`) — local storage emulator.
- Python 3.13+ and the `azurefunctions-agents-runtime` package (installed via the app's `requirements.txt`).

## Development

```bash
npm install
npm run gen-schemas   # regenerate schemas/*.json from the runtime Pydantic models
npm run compile       # typecheck + esbuild bundle -> dist/extension.js
npm test              # run core unit tests (node --test)
```

Press <kbd>F5</kbd> in VS Code to launch the **Extension Development Host** and try it live.

### How IntelliSense stays in sync

`schemas/agent.schema.json` and `schemas/agents-config.schema.json` are **generated**, not
hand-written. `eng/scripts/generate_frontmatter_jsonschema.py` reads the runtime's `AgentSpec`
and `GlobalConfig` Pydantic models and emits JSON Schema. Re-run `npm run gen-schemas`
(or the script directly) after any change to the runtime's `config/schema.py`.

## License

MIT
