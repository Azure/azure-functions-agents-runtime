# Changelog

All notable changes to the **Hosted Skills** extension are documented here.

## [0.2.1] - 2026-09-04

### Changed

- Renamed the npm/VSIX package id from `azure-functions-agents-authoring` to
  **`hosted-skills-authoring`** (extension id is now `hosted-skills.hosted-skills-authoring`).
- **Add Agent** wizard: the *Built-in endpoints* step now shows an ⓘ info button on each option
  (**All**, **Debug chat UI**, **Chat API**, **MCP tool**). Hover the icon for a description, or
  click it to see the full explanation — so you don't have to guess what each surface does.

## [0.2.0] - 2026-09-02

### Added

- **Agents explorer** — a dedicated **Hosted Skills** Activity Bar view listing every agent in the
  workspace grouped by app, with trigger / endpoint / subagent badges and click-to-open.
- **Add Tool** (`agentsAuthoring.addTool`) — scaffold a discoverable `tools/<name>.py` (sync or
  async plain function; docstring becomes the tool description).
- **Add Skill** (`agentsAuthoring.addSkill`) — scaffold `skills/<name>/SKILL.md` with
  `name` / `description` front matter and a starter body.
- **Add Subagent** (`agentsAuthoring.addSubagent`) — pick another agent in the app and inject a
  `subagents:` entry (with optional `when:` hint) into the active agent's front matter, preserving
  existing keys and comments.
- **CodeLens** on `.agent.md` files — inline slug, **Open Chat UI**, and **Add Subagent** actions
  above the front matter.

## [0.1.3] - 2026-09-02

### Changed

- Renamed the extension to **Hosted Skills** and the command-palette category from
  "Azure Functions Agents" to **Hosted Skills** (commands now read e.g. "Hosted Skills: Add Agent").

### Fixed

- VS Code's built-in `chatagent` language schema no longer reports false errors
  (e.g. "Attribute 'trigger' is not supported in VS Code agent files") on Hosted Skills
  `.agent.md` files. Agent files inside a Hosted Skills app are re-mapped to the `markdown`
  language. Opt out with the new `agentsAuthoring.treatAgentFilesAsMarkdown` setting.

## [0.1.2] - 2026-09-02

### Changed

- Default scaffold model is now **`gpt-5.4`** (was `gpt-4.1`), matching the runtime samples.

### Removed

- **Start Azurite** (`agentsAuthoring.startAzurite`) and **Run Locally / func start**
  (`agentsAuthoring.funcStart`) commands. Start storage and the host from a terminal
  (`azurite` / `func start`); **Open Chat UI** remains for opening a running agent.

## [0.1.1] - 2026-09-02

### Fixed

- Front-matter IntelliSense (completions + hover) now appears in `.agent.md` files.
  VS Code's built-in `prompt-basics` extension assigns `*.agent.md` the `chatagent`
  language (not `markdown`), so the providers are now registered for `chatagent` too.
  Added an `onLanguage:chatagent` activation event.

## [0.1.0] - 2026-09-02

### Added

- Front-matter IntelliSense for `.agent.md`: completions, hover docs, and live diagnostics.
- Schema validation generated from the runtime's Pydantic models (`AgentSpec` / `GlobalConfig`),
  plus semantic checks for unsupported triggers and cross-app slug collisions.
- **Scaffold New Agent App** command (bundled templates).
- **Add Agent** wizard that emits a valid `.agent.md`.
- Local-run commands: **Start Azurite**, **Run Locally (`func start`)**, and **Open Chat UI**.
