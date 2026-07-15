# Serverless Agent Portal — Mental Model Proposal

> **Decision:** Start **Agent-centric**. Defer the **Project** model (and its
> persistence layer) until the concept is validated.
> Concept mockups: [`../mocks/concepts/`](../mocks/concepts/index.html).

## 1. Context

We must choose the **primary object** the customer manages. One fact constrains
the choice: in the runtime, `tools/`, `skills/`, and `mcp.json` live at the
**Function App** level and are **shared** across its agents — each agent only
*filters* which ones it uses. So "shared tools/skills" is real, and editing a
shared item affects every agent that uses it.

## 2. The three approaches

- **Function App-centric** — the app is the object; agents + shared tools/skills
  sit inside it, and sharing is visible.
![alt text](image.png)
- **Agent-centric** — the agent is the front door; on create you pick/create a
  Function App; each agent shows only the tools/skills it uses.
![alt text](image-1.png)
- **Project-centric** — a new "Project" object groups agents, shared
  tools/triggers, and multiple Azure resources (apps, storage, Foundry,
  connectors).
![alt text](image-2.png)

## 3. Comparison

| Dimension | Function App | **Agent** | Project |
| --- | --- | --- | --- |
| Primary object | Function App | Agent | Project |
| Intuitive front door | Medium | **High** | Medium |
| Maps to runtime | **Exact** | Indirect | Abstraction |
| Sharing visible / safe edits | **Yes** | No (needs warnings) | Yes (project scope) |
| Groups many resources | Weak | Weak | **Strong** |
| New persistence needed | No | No | **Yes** |
| Added cost / moving parts | None | None | **High** |
| Build effort (v1) | Low | **Low–Med** | High |

## 4. Why Project-centric is costly

Function App and Agent models ride entirely on the **existing blob working copy** —
the files are the source of truth, so there is **no new state to run**. A Project
has **no runtime counterpart**, so it forces new moving pieces:

- **A project store** (Cosmos DB / Table / a maintained blob index) — an
  always-on, billable service to track project → agents → apps → resources.
- **A consistency layer** to keep that store in sync with the files that remain
  the real source of truth (two sources of truth = drift + reconciliation).
- **Cross-resource wiring** — membership, ownership, and references spanning
  multiple Function Apps and resources.
- **Project lifecycle** — create/rename/delete, orphan cleanup, and
  project-scoped RBAC.
- **Migration/backfill** — folding existing apps and agents into projects.

That's a stateful component plus more APIs and UI to keep coherent — real cost for
value we can't yet confirm customers need.

## 5. Proposed solution

**Ship Agent-centric on the Function App reality.**

- Front door is **"create an agent"**; the customer picks or creates a Function
  App underneath. No new persistence — blob working copy stays the source of truth.
- Defuse the one risk of hiding sharing: show **"used by N agents"** on tools/
  skills, **warn before editing a shared item**, and offer **copy-on-edit** (fork
  a private copy for this agent).
- **Add Projects later** — only if customers regularly need to group multiple
  apps/resources under one workspace; that's the trigger to pay for the project
  persistence layer.
