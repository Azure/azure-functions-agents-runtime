# Serverless Agent Portal — Mental Model Proposal

> **Status:** Draft for group review. Compares three candidate mental models for
> how customers organize agents, tools, skills, and hosting in the portal.
> Companion concept mockups live in [`../mocks/concepts/`](../mocks/concepts/index.html).

## 1. Why this decision matters

The portal's whole UX hangs on one question: **what is the primary object the
customer manages?** That choice drives navigation, the create flow, how tools and
skills are shown, and how much backend state we must keep.

### The physical reality (from the runtime)

Grounding in how `azure-functions-agents-runtime` actually works:

- A **Function App project** contains many `*.agent.md` files **plus shared
  inventories**: `tools/*.py`, `skills/<name>/SKILL.md`, and `mcp.json`.
- Discovery is **read-only and app-wide** — tools/skills/MCP are discovered once
  for the whole app (`discovery/*`).
- Each agent gets a **per-agent filter** over that shared inventory
  (`registration/capabilities.py`), i.e. an allow/deny list — **not a private
  copy**.

**Key consequence:** tools and skills are *physically shared at the app level*.
Selecting tools for an agent is a **filter**; editing a tool's code or a skill's
content changes it **for every agent that uses it**. Any model we pick has to be
honest about that.

## 2. The three models

### Model 1 — Function App-centric
The **Function App** is the primary object. Inside it: agents, and a shared pool
of tools, skills, and triggers. The customer works at the app level and **sees the
shared tools/skills directly**, so the blast radius of a change is visible. A
playground lets them edit and test.

- Primary nav: Function Apps → (app) → Agents + Shared tools/skills.
- Mockup: [`model1-functionapp.html`](../mocks/concepts/model1-functionapp.html)

### Model 2 — Agent-centric
The **Agent** is the front-facing object. Creating an agent asks which Function
App to host it in (existing or new). Each agent shows **only the tools/skills it
uses**. Simple and focused — but because tools/skills are shared underneath, a
customer editing them **may not realize other agents are affected**.

- Primary nav: Agents → (agent) → its tools/skills + playground.
- Mockup: [`model2-agent.html`](../mocks/concepts/model2-agent.html)

### Model 3 — Project-centric
Introduce a **Project** as the top object. A project contains agents and shared
tools/triggers, and can span multiple Azure resources (one or more Function Apps,
storage, Foundry, connectors). Cleanest customer mental model for grouping related
work, but a project is a **new logical entity** that needs its **own persistence
layer** (membership, shared resources, cross-app references) — added state and
cost.

- Primary nav: Projects → (project) → Agents + Shared tools/triggers + Resources.
- Mockup: [`model3-project.html`](../mocks/concepts/model3-project.html)

## 3. Side-by-side comparison

| Dimension | 1 · Function App | 2 · Agent | 3 · Project |
| --- | --- | --- | --- |
| Primary object | Function App | Agent | Project |
| Mental simplicity for new users | Medium | **High** | Medium |
| Maps to runtime reality | **Exact** | Indirect | Abstraction on top |
| Shared tools/skills visible? | **Yes, explicit** | No (hidden) | Yes, at project level |
| Blast-radius safety on edits | **High** (visible) | **Low** (surprising) | High (scoped to project) |
| Groups heterogeneous resources | Weak (1 app) | Weak | **Strong** |
| New persistence layer needed | No | No | **Yes** (project store) |
| Added cost | None | None | **Yes** (stateful service) |
| Cross-app / multi-resource | No | No | **Yes** |
| Isolation between agents | App-shared | App-shared | Project-scoped |
| Implementation effort (v1) | **Low** | Low–Medium | High |
| Migration risk later | Low | Medium | — |

## 4. Pros & cons

### Model 1 — Function App-centric
**Pros**
- Matches the runtime 1:1 — no leaky abstraction; what you see is what deploys.
- Shared tools/skills are first-class and visible → **safe edits** (customer knows
  the blast radius).
- No new backend state; cheapest to build.

**Cons**
- "Function App" is an Azure/infra noun, not how a non-developer thinks.
- The agent — the thing customers actually care about — is one level down.
- Weak story for grouping resources beyond a single app.

### Model 2 — Agent-centric
**Pros**
- Lowest friction and the most intuitive front door: "I want an agent."
- Clean, focused per-agent view (only its tools/skills).
- Still lets power users pick/create a Function App under the hood.

**Cons**
- **Hides the shared reality.** Editing a shared tool/skill silently affects other
  agents — a real footgun given the runtime shares them app-wide.
- Harder to reason about cost/scaling because the hosting boundary is de-emphasized.
- Needs strong "used by N other agents" cues to be safe.

### Model 3 — Project-centric
**Pros**
- Cleanest customer story: a Project is a workspace that holds agents **and** the
  resources they need (multiple apps, storage, Foundry, connectors).
- Natural home for shared tools/triggers with a clear, bounded blast radius.
- Scales to real orgs (many apps, many resources) better than 1 & 2.

**Cons**
- A Project is a **new entity** with no runtime counterpart → needs a persistence
  layer (e.g. Cosmos DB / Table / a blob index) to track membership and shared
  resources. **Added cost and a stateful component to operate.**
- More concepts to learn; heavier to build and keep consistent with the files
  that are the real source of truth.

## 5. The crux — shared tools & skills

This is the pivot between the models:

- **Model 1** surfaces sharing honestly: the customer edits a tool *knowing* it's
  shared. Safe, if a little more technical.
- **Model 2** optimizes for focus but **obscures sharing** — the highest-risk
  option unless we add explicit safeguards.
- **Model 3** contains sharing inside a project boundary, which is conceptually the
  cleanest but the most expensive.

**Mitigations (apply to whichever model wins):**
- Show **"used by N agents"** on every tool/skill, and an **impact warning** before
  saving a shared tool/skill.
- Offer **copy-on-edit** ("make a private copy for this agent") when a customer
  edits a shared item from an agent view — turns a shared edit into a safe fork.
- Distinguish **filter changes** (safe — only this agent's selection) from
  **content edits** (shared — affects all).

## 6. Persistence & cost note (Model 3)

Models 1 & 2 can run entirely off the existing **blob working copy** (the files
are the source of truth). Model 3 adds a **project index** that must track:
project → agents, project → Function Apps, project → shared resources, and
membership/ownership. That means a queryable store (Cosmos DB / Azure Table /
a maintained blob index) — an extra always-on, billable component and a
consistency surface to keep in sync with the files. Worth it only if
multi-resource grouping is a real requirement.

## 7. Recommendation

**Start with Model 1's truth, wear Model 2's face, keep Model 3 in reserve.**

- **v1:** Build on the **Function App = project** reality (Model 1) — no new
  persistence, honest about sharing — but make the **create flow agent-first**
  (Model 2 ergonomics): the customer says "create an agent," picks/creates a
  Function App, and lands in an agent-focused view.
- **Safety:** Always render shared tools/skills with **"used by N agents"** and an
  **impact warning** + **copy-on-edit** so Model 2's footgun is defused.
- **Later:** Introduce **Project** (Model 3) as a *logical grouping over* Function
  Apps once customers need multiple apps/resources under one workspace — and only
  then pay for the project persistence layer.

This gets the intuitive front door now, stays faithful to the runtime, avoids new
cost, and leaves a clean path to Projects.

## 8. Open questions for the group

- **Q1** — Is single-Function-App grouping enough for v1, or do early customers
  already need multiple apps/resources (pushing us toward Model 3 sooner)?
- **Q2** — For shared-tool edits from an agent view: **warn-only** or **copy-on-edit
  by default**?
- **Q3** — Is "Function App" acceptable customer vocabulary, or should we always
  say "app/workspace/project" in the UI even under Model 1?
- **Q4** — If we adopt Projects later, what backs the project store (Cosmos vs.
  Table vs. blob index), and who owns its cost?
- **Q5** — Do we need per-agent isolation (separate apps) for any customer segment,
  which would tilt away from heavy sharing entirely?
