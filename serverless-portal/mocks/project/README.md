# Project workbench — concept mocks (unifying umbrella)

A **third direction** that keeps *both* prior concepts and puts a **Project** on
top of them:

> **Define a project → add agents, tools, skills, connectors/triggers, and
> (optionally) workflows → deploy the whole project to one Function App.**

A **Project** is a *logical grouping* the customer creates and manages. It's the
unit of authoring **and** the unit of deployment. It reconciles the two earlier
brainstorms instead of replacing them:

- The **[agent console](../console/README.md)** becomes *how you edit a component*
  inside a project (bring markdown, attach tools, provision a session).
- The **[workflow canvas](../workflows)** becomes *one optional component type* in
  a project — for when you want an explicit orchestration.

These are static, clickable HTML mockups (no backend). They reuse the shared
[`../styles.css`](../styles.css) plus a self-contained [`project.css`](project.css).

## View them

Open [`projects.html`](projects.html) in a browser (or Live Preview) and use the
top tabs inside a project.

## Screens

| File | Screen | Idea |
| --- | --- | --- |
| `projects.html` | **Projects** home | Gallery of projects; each = a logical grouping mapped to one Function App, with component counts + deploy status. |
| `new-project.html` | **Create a project** | The easy grouping flow: name it, pick a starting point (blank / template / prompt / import markdown), pick the target Function App (existing or new — **1:1**). |
| `project.html` | **Project overview** | Component summary + a **topology** view: *logical project → physical Function App*, with a dashed "future: multi-app" box. |
| `components.html` | **Components** (editable) | A solution-explorer of Agents / Tools / Skills / Connectors / Workflows with an editor pane — agents, tools, and skills stay fully editable in project context. |
| `runs.html` | **Runs & sessions** | The answer to "run flow without a workflow": the **observed** path across components rebuilt from traces, per-session, with **Promote to workflow**. |
| `deploy.html` | **Deploy** | 1 project → 1 Function App mapping, build output (compiled runtime files), deploy history, and the multi-app future plan. |

## Design questions this explores

### 1. Easy "logical grouping" (the project)
A project is a lightweight record: `{ name, target functionApp, members[] }`. Creating
one is a single short form (`new-project.html`). Everything else is *adding members*.

### 2. Components stay editable
Membership doesn't freeze anything. In `components.html` the right pane is the same
editor you'd get standalone — `.agent.md`, `tools/*.py`, `SKILL.md` — just framed by
the project. Editing a **shared** tool shows a blast-radius hint ("used by N agents").

### 3. Run flow / session tracking **without** an authored workflow
In MAF, components compose at runtime even with no graph: a trigger starts a run, an
agent reasons and calls tools / skills / MCP / other agents. So a "run" is a **session**
— one correlation id — and its flow is **discovered from `gen_ai` traces + blob session
history**, not declared. `runs.html` shows this observed path and offers to **promote**
a frequently-seen path into an explicit workflow (the bridge to the canvas).

### 4. Components in different Function Apps (future) & deployment
- **Now (simple): 1 project → 1 Function App.** All members deploy together; "Deploy
  project" = deploy that one app. Sharing/blast-radius is bounded by the project.
- **Future (multi-app):** the project is a logical overlay; a member hosted in another
  app is called via its **published MCP / HTTP endpoint** (managed identity, no keys).
  The project records the remote **binding**; deployment **fans out per app** and wires
  cross-app references as app settings. Shown as a dashed box in the topology + a note
  on `deploy.html` — not built in these mocks.

## Relationship to the mental-model proposal

The team's [`proposals/mental-models.md`](../../proposals/mental-models.md) evaluated a
Project-centric model and **deferred** it (it needs a persistence layer — a project
store — since a project has no runtime counterpart). These mocks make that concept
concrete and lean on the **1:1 project→app** simplification to keep the store minimal
(membership + target app only). Reviewer's-eye view: does the grouping earn its store?

## Notes for reviewers

- Names, counts, session ids, and traces are invented to make screens feel real.
- `components.html` and `runs.html` have light interactivity; nothing is real behavior.
