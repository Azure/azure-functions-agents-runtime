# Agent Console — concept mocks (artifact-first)

A **second direction** for the Serverless Agent Portal that deliberately drops the
node/edge **workflow canvas** in favor of an **artifact-first "create & manage"**
experience:

> **Bring your markdown → attach tools & connectors → provision a live session.**

The bet: most customers don't want to *draw* an orchestration. They already think
in the runtime's own building blocks — a `*.agent.md` file, some `tools/*.py`, a
trigger, a couple of output connectors — and they want to **manage** those, test
in a real session, and ship. No canvas, no auto-layout, no edges to untangle.

These are static, clickable HTML mockups (no backend, no real data). They reuse
the shared [`../styles.css`](../styles.css) plus a small [`console.css`](console.css)
for the new components.

## View them

Open [`index.html`](index.html) in any browser (or right-click → **Open with Live
Preview** in VS Code) and use the left nav.

## Screens

| File | Screen | Idea |
| --- | --- | --- |
| `index.html` | **Agents** — manage home | A list you operate: each agent shows its trigger/connectors, tool count, model, and whether a session is live. Manage, don't visualize. |
| `new-agent.html` | **Bring your markdown** | Paste or drop a `.agent.md`; we parse the front matter (name, model, tools, skills, trigger) and tell you what still needs wiring. |
| `agent.html` | **Manage an agent** | The markdown editor next to *stacked artifact panels*: Connectors, Tools, Skills, and Dynamic session — the canvas replaced by editable attachments. |
| `tools.html` | **Upload tool files** | Drop `tools/*.py`; we discover `@tool` functions and let you attach them to agents. |
| `session.html` | **Dynamic session** | Provision an ephemeral sandbox (ACA Dynamic Sessions), chat/test the agent live, and watch the turn's trace. Auto-expires. |

Configure surfaces (Connectors hub, Providers, Settings) link back to the existing
portal mocks in [`../`](../).

## How this differs from the workflow mocks

| | Workflow canvas ([`../workflows/`](../workflows)) | Agent Console (this folder) |
| --- | --- | --- |
| **Primary object** | A graph of nodes & edges | The `.agent.md` file + its attachments |
| **Authoring** | Drag nodes, draw connections, auto-layout | Paste/upload markdown, toggle attachments |
| **Mental model** | "Design the orchestration" | "Manage the agent + run it" |
| **Connectors** | Nodes on a canvas | Rows attached to an agent (trigger + outputs) |
| **Tools** | Tool nodes wired by edges | Uploaded `.py` files, discovered `@tool`s, attach toggles |
| **Testing** | Test-run the graph | **Provision a dynamic session** and chat live |
| **Best when** | Multi-agent hand-offs you want to *see* | Single/few agents you want to *ship & operate* |

Both can coexist — the console is the fast path for "I have my markdown, get me
running"; the canvas remains for genuinely branching multi-agent orchestration.

## Notes for reviewers

- `new-agent.html` live-parses the pasted markdown into the preview panel.
- `agent.html` and `session.html` have light interactivity (provision a session,
  toggle attachments) to convey flow — not real behavior.
- Names, metrics, and session ids are invented to make screens feel real.
