---
name: component-codegen
purpose: Generate the runtime code for a single workflow component (a tool's Python or an agent's instructions) from the workflow's intent.
model: any (provider-agnostic — see ../../llm)
---

# Component Codegen skill

Stage 2 of generation. Where [`composer-plan`](../composer-plan/SKILL.md) turns a
prompt into a **graph**, this skill turns a single **node** in that graph into the
**code** the runtime executes:

- a `tool` node → a Python `@tool` function body implementing the step;
- an `agent` node → focused `*.agent.md` instructions for that step.

Like every skill here it is only a prompt template ([`prompt.md`](prompt.md)) and
is **model-independent** — the generator renders it and sends it to whichever
provider is configured under [`../../llm`](../../llm/provider.js). Editing this
skill changes the code style/quality; it never changes which model runs.

## Contract

- **Input (user message):** a JSON spec of the node — its `type`, `kind`, `name`,
  `signature` (for tools), the parent `workflowName` + `workflowPrompt`, and the
  names of `upstream`/`downstream` nodes (so the code fits the data flow).
- **Output:** a single JSON object — `{ "code": "..." }` for tools or
  `{ "instructions": "..." }` for agents. No prose, no code fences.
</content>
