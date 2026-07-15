---
frd: 0006
title: Multi-agent delegation (agent-as-tool)
status: Draft            # Draft → In review → Finalized  (→ Implemented after merge)
author: larohra
created: 2026-07-14
updated: 2026-07-14
issues: []
pull_requests: []
branch: larohra/multi-agent-delegation
---

# FRD 0006 — Multi-agent delegation (agent-as-tool)

## 1. Summary

Enable agent-to-agent coordination in the markdown-first runtime. **v1 ships
delegation**: any coordinator `*.agent.md` can declare a new `subagents:`
front-matter field naming other, already-existing agents as specialists. At
runtime the coordinator calls each declared specialist *as a tool*, via MAF's
`BaseAgent.as_tool()`, inside the existing single-`agent.run()` path — "one
assistant that consults specialists and answers." This needs **no new
dependency** (`as_tool` is already present, unchanged, on `BaseAgent` in the
pinned `agent-framework-core==1.3.*`) and reuses the runtime's existing tool
assembly, registration, and SSE streaming.

**True "handoff"** — control transfer between agents via MAF's `HandoffBuilder`
(which returns a `Workflow`) — is explicitly **not** built in v1. It is a
**committed, chat-scoped fast-follow**, tracked as its own FRD. The abstractions
introduced here — a stable participant identity, an immutable in-memory agent
catalog, and a reusable "build a MAF `Agent` from a `ResolvedAgent`" helper with
an explicit execution role — are chosen so that fast-follow can be layered on
later without reworking the authoring surface or the per-`session_id` storage
model.

Tracking issue: `Azure/azure-functions-bucees-planning#1185` — "[Serverless
Agents] Multi-agent: Handoff via HandoffBuilder + workflows
(agents.config.yaml)".

## 2. Motivation / problem

Today, every `*.agent.md` file registers **independently** — its own trigger(s)
and/or built-in endpoint(s) — and nothing composes agents at runtime. The
`samples/multi-agent-folder` sample makes this literal: it instructs the *user*,
not the model, to pick the right endpoint (e.g. "suggest using the research
endpoint"). Any app author who wants an agent to route work to the right
specialist has to build that routing themselves; the runtime has no primitive
for it.

Customers building AgentApps with several specialized agents want automatic
routing to the right specialist instead of exposing N separate endpoints and
asking the user to choose. The confirmed target interaction for v1 is **"one
assistant throughout — it consults specialists, then answers"** (coordinator
stays in control; Decisions log #2). That is *delegation*, not control transfer,
and it is the smallest change that satisfies the scenario. **Nothing is
coordinated today** — both delegation and true handoff are net-new to this
runtime.

**Concept clarification: two different "workflows".** This repo already has a
feature called **Dynamic Workflows** (FRD 0004, `docs/workflows.md`):
LLM-authored DAGs of *tool calls*, executed on Durable Functions, opted into
per-agent via `workflows.enabled` front matter, and explicitly **single-agent**.
MAF, separately, has an **orchestration `Workflow`** layer (`HandoffBuilder` /
`GroupChatBuilder` / `MagenticBuilder`, all of which return a `Workflow`) for
true **multi-agent** control transfer and shared-context collaboration.
`as_tool()` — delegation — is the *only* MAF multi-agent pattern that runs
inside a plain `agent.run()`, with **no** `Workflow` involved. These two
concepts share the word "workflow" but are otherwise unrelated, so **the new
authoring field introduced by this FRD must not be named `workflows:`** — see
Decisions log #5. This FRD is only about the delegation pattern; a future
handoff FRD is the one that will actually bring a MAF orchestration `Workflow`
into this runtime.

For shared vocabulary across this FRD and the future handoff FRD:

1. **Manual routing** (today) — separate endpoints; the user picks.
2. **Delegation / agent-as-tool** (`as_tool()`) — the coordinator stays in
   control, calls specialists as tools, and synthesizes the final answer. Plain
   `agent.run()`, no new dependency. **This FRD (v1).**
3. **True handoff** (`HandoffBuilder`) — control transfers to the specialist,
   which owns subsequent turns, with MAF's full-mesh **shared broadcast
   context** (every participant sees the running conversation). Needs a
   `Workflow` execution path and the `agent-framework-orchestrations`
   dependency. **Fast-follow.**

## 3. Goals / Non-goals

**Goals**
- Add a new, optional `subagents:` front-matter field so a coordinator
  `*.agent.md` can declare one or more existing agents as specialists (object
  form; see §4).
- Add a new, optional `id:` front-matter field giving each agent a **stable,
  registration-independent identity** used to reference it as a subagent
  (falls back to the file-stem slug when omitted).
- At runtime, the coordinator calls each declared specialist as a tool via MAF's
  `BaseAgent.as_tool()` — inside the existing plain `agent.run()` path, no MAF
  `Workflow`.
- Run each specialist under an explicit **leaf execution profile** (§4): its own
  instructions, model, static user tools, MCP servers, and skills — but not
  sandbox tools, workflow tools, or its own subagents.
- Ship with **no new dependency**: `as_tool` is present, unchanged, on
  `BaseAgent` in the pinned `agent-framework-core==1.3.*`.
- Surface delegated calls through the runtime's **existing** `tool_start` /
  `tool_end` SSE events (tool name = the `as_tool` name) — no new event type,
  no client/UI change.
- Choose abstractions — stable participant identity, an immutable agent catalog,
  reusable role-parameterized specialist construction — that let the planned
  handoff fast-follow (`HandoffBuilder`) compose with delegation later, instead
  of hardening coordinator-centric APIs that would need reworking.

**Non-goals**
- True handoff / control transfer via MAF `HandoffBuilder`, or any MAF
  orchestration `Workflow` execution path.
- Workflow-level checkpoint + `request_info` pause/resume across Function
  invocations.
- **Any HITL** — tool-approval prompts or user-input pause/resume. No such
  mechanism exists in the runtime today (verified: no `user_input_requests` /
  `approval_mode` / `request_info` / `UserInputRequired` handling anywhere in
  `src/`, and `run_agent` awaits `agent.run(...)` once with no approval loop).
  Building HITL is deferred to the handoff fast-follow; v1 sub-agents must be
  authored with autonomous, non-approval-requiring tools (see §4, *HITL*).
- `propagate_session=True` context sharing / shared-state tools — subagents stay
  isolated in v1 (a specialist receives only the tool-call argument string, not
  the coordinator's chat history; see §4, *Task contract*).
- Sandbox tools, Dynamic-Workflow tools, and further (nested) subagents *for a
  specialist invoked as a leaf* — see the leaf execution profile in §4.
- Nested delegation — v1 sub-agents run as a single **leaf** call (Decisions log
  #6). A specialist's own `subagents`, if any, are not expanded when that
  specialist is itself invoked as a tool.
- The **string-shorthand** authoring form (`subagents: [billing, tech]`) — v1 is
  object-only; the shorthand can be added later as non-breaking sugar
  (Decisions log #11).

## 4. Proposed design

Delegation is built entirely on an API that already exists and needs no new
dependency: `BaseAgent.as_tool(*, name=None, description=None, arg_name="task",
arg_description=None, approval_mode="never_require", stream_callback=None,
propagate_session=False) -> FunctionTool`, defined on the root `BaseAgent` and
inherited by the concrete `Agent`, present and unchanged in the pinned
`agent-framework-core==1.3.*` (verified at tag `python-1.3.0`).

`subagents:` rides the existing four-stage pipeline (`docs/architecture.md` §2:
discover → translate → register → execute). The core sequencing change is that
composition now requires a **global view** of all agents, so the app factory
moves to an explicit multi-pass order (see *Two-pass composition* below) instead
of validating/registering each agent in one interleaved loop.

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| discover | — | No change — coordinators and specialists are both ordinary `*.agent.md` files, already found by the existing top-level/`agents/`-folder scan (FRD 0001). Discovery stays read-only. |
| translate | `config/schema.py`, `config/merge.py`, `config/validation.py` | New `SubagentRef` model + `AgentSpec.subagents: list[SubagentRef] \| None`, plus optional `AgentSpec.id: str \| None`; carried onto `ResolvedAgent` (`subagents`, resolved `agent_id`). `merge.py` normalizes/validates each `SubagentRef` and derives each agent's canonical identity (`id` if set, else file-stem slug, computed **independently of registration suffixing**). `validation.py` runs against a completed global catalog: reject ambiguous identity collisions, unknown subagent references, duplicate references, self-reference, and tool-name collisions; relax the "trigger or `builtin_endpoints` required" rule for an agent referenced as another agent's subagent (an "internal specialist"). |
| register | `app.py`, `registration/_handlers.py`, `registration/endpoints.py` | `app.py` becomes the composition root. It first builds an **identity index** (a multimap `agent_id -> list[ResolvedAgent]`, to surface ambiguity) for validation; then, *after* capabilities are built and capability-aware validation passes, it assembles the immutable runtime **`AgentCatalog`** (`agent_id -> (ResolvedAgent, AgentCapabilities)`, keyed by unambiguous identity) and the global referenced-subagent set — all *before* any `FunctionApp` mutation — and threads the catalog into handlers/endpoints for coordinators that declare `subagents`. `_handlers.py` / `endpoints.py` pass the catalog (immutable resolved data, not live MAF `Agent`s) into `run_agent` / `run_agent_stream`. Registration performs **no** YAML/front-matter parsing or reference resolution — it consumes already-validated data. |
| execute | `runner.py` | New `build_subagent_tools(subagents, catalog)`: for each subagent it constructs the specialist's MAF `Agent` under the *leaf* execution role (reusing a role-parameterized version of the existing client/tool-assembly path — cheap, because the `ClientManager` builds the specialist's chat client for its **own** resolved model while reusing provider/credential state process-wide) and exposes it via `.as_tool(name=<tool_name>, description=<when \| specialist description>, arg_name="task", arg_description=…, approval_mode="never_require", propagate_session=False)`, wrapped in a thin adapter (see *Delegated failure* and *Breadth & concurrency*). What is deferred is each specialist's **model run** — it only happens if the coordinator's model selects that tool; the resulting tools are appended to the coordinator's `resolved_tools`. |

### Two-pass composition

`app.py` today composes, validates, builds capabilities, and mutates the
`FunctionApp` in a single loop over agents. Delegation makes an early agent's
validity depend on a *later* agent (e.g. an endpoint-less internal specialist is
valid only because something references it), so validity would otherwise become
discovery-order-dependent, and Azure registration could begin before global
validation completed. v1 therefore uses an explicit ordered pipeline:

1. Resolve every discovered agent into typed `ResolvedAgent` objects.
2. Build an **identity index** (a multimap `agent_id → list[ResolvedAgent]`;
   see *Participant identity*) and the global set of referenced-subagent ids.
3. Run identity + schema + reference validation against that index (identity /
   reference ambiguity, duplicate + self-reference, internal-specialist
   relaxation).
4. Build `AgentCapabilities` per agent.
5. Run capability-aware validation (tool-name collisions against each
   coordinator's now-known tool set).
6. Assemble the immutable runtime **`AgentCatalog`** (`agent_id →
   (ResolvedAgent, AgentCapabilities)`).
7. Mutate the single `FunctionApp` (triggers + built-in endpoints).

A final tool-name collision recheck also runs at runtime tool assembly, because
MCP/sandbox tool names may only be known then.

Cross-agent awareness (steps 2–5) is the one genuinely new plumbing
requirement: today every validation rule is local to a single agent's spec.

### Participant identity (Decisions log #9)

Each agent has a canonical identity used for `subagents` references:

- If the agent's front matter sets **`id:`** (a stable, slug-shaped identifier
  distinct from the human-readable, mutable `name:`), that is its identity.
- Otherwise its identity is the **file-stem slug** (`billing.agent.md` →
  `billing`), computed the same way `_naming.py` derives slugs **but resolved in
  the translate stage, before** the registration-time collision auto-suffixing.
  A subagent reference therefore never depends on which colliding agent happened
  to receive a `_2` suffix.
- The identity index is a **multimap** over the union of explicit `id`s and
  fallback slugs, and collisions are handled so backward-compatibility holds:
  - a **duplicate explicit `id`** is always a fail-fast error;
  - a **fallback-slug collision matters only if that identity is *referenced***
    as a subagent — an ambiguous reference (one that resolves to >1 agent) is a
    fail-fast, actionable error, while colliding stems that are **never
    referenced continue to work** (registration auto-suffixes their endpoints
    exactly as today).

  Only unambiguous, resolvable participants enter the runtime catalog. Display
  `name` is never used as identity.

### Leaf execution profile (Decisions log #13)

A specialist invoked as a subagent runs in an explicit **leaf role**, distinct
from how the same agent runs when invoked directly through its own
trigger/endpoint:

- **Included:** its own instructions, model/timeout, static user tools, MCP
  tools, and skills.
- **Excluded:** sandbox / code-interpreter tools, Dynamic-Workflow tools, and
  its own `subagents` (single-level only — no nesting; Decisions log #6).

This is implemented by parameterizing the shared "build a MAF `Agent` from a
`ResolvedAgent`" path with an **execution role** (`direct` vs `leaf`), rather
than mutating `ResolvedAgent`. It resolves the apparent tension between "a
specialist keeps its own capabilities" and "no sandbox/workflow tools for
subagents": the specialist keeps its *static* capabilities; role-scoped
capabilities (sandbox, workflows, delegation) are simply not part of a leaf.

Because leaf agents never expand their own `subagents`, a mutual/peer reference
(A references B *and* B references A) is **benign by construction** — each is
still independently rooted at its own endpoint and there is no runtime recursion
to cut off. `validation.py` therefore needs reference-resolution only, **not**
graph/cycle detection (Decisions log #6). (Note: the leaf guarantee covers
native `subagents` expansion; an arbitrary MCP/user tool a specialist holds
could still call out to another endpoint — that is out of scope for the leaf
invariant.)

### Delegated failure, timeout, and cancellation (Decisions log #12)

A delegated call is a tool call, and the **two failure classes are handled
differently**:

- **Child failure / specialist-local timeout → recoverable.** A specialist
  exception or its own timeout is surfaced to the coordinator as a **recoverable
  tool error** (returned as the `tool_end` result), not a hard abort — the
  coordinator's model can retry, route elsewhere, or apologize.
- **Parent / request cancellation → propagates.** A coordinator-turn timeout,
  request disconnect, or host shutdown **cancels and aborts** the coordinator
  (and any in-flight delegated calls); it is *not* swallowed as a recoverable
  tool result.

The effective per-call specialist timeout is `min(specialist timeout,
coordinator's remaining budget)`. The recoverable path returns a **stable,
sanitized** error string to the model/client, while full detail (redacted
exception type, correlation id, outcome) is recorded in internal telemetry — so
sanitization does not destroy diagnostics. SSE ordering is the existing
`tool_start` … `tool_end` pair, with recoverable failures carried in the
`tool_end` result. Guaranteeing the recoverable-vs-propagate split around the
pinned `as_tool()` behavior may require a thin wrapper/adapter around the
delegated run; the implementation must confirm the pinned version's actual
exception/cancellation surface rather than assume it. The same adapter also
serializes concurrent same-specialist calls (see *Breadth & concurrency*).

### Authoring / API surface

New optional front-matter field on any coordinator agent, plus the optional
`id:` identity field. Object form only:

```yaml
# agents/coordinator.agent.md
---
name: Support Coordinator          # human-readable display name (not identity)
id: support-coordinator            # optional stable identity (else file-stem slug)
description: Routes customer questions to the right specialist
builtin_endpoints: true
subagents:
  - agent: billing                 # references billing's id, else billing.agent.md slug
    when: Invoices, charges, refunds, or subscription questions   # → as_tool(description=...)
    tool_name: ask_billing         # optional; default = delegate_<agent-id>
  - agent: tech                    # when omitted → uses tech's own `description`
---
You are a support coordinator. Use the billing and tech specialists when
relevant, then give the customer a single consolidated answer.
```

- `SubagentRef` fields: **`agent`** (required — the referenced agent's identity),
  **`when`** (optional routing hint; defaults to the specialist's own
  `description`, which is always present since `description` is required),
  **`tool_name`** (optional; defaults to `delegate_<agent-id>`).
- The `when` text is exactly what the coordinator's model reads when deciding
  whether to call that specialist, so authoring it well is the main lever on
  routing quality. Routing is **model-selected, not deterministic** — a vague
  hint can cause misrouting or a skipped delegation; this is an accepted
  limitation of the pattern.
- `tool_name` (and the `delegate_<agent-id>` default) must be a valid tool
  identifier and **unique** within the coordinator's final tool set. Collisions
  with the coordinator's user/MCP/sandbox/workflow tools or another subagent are
  a **fail-fast validation error**, never silently suffixed (the name is
  prompt-visible API). Because MCP/sandbox tool names may only be known at
  runtime assembly, the collision check runs both at validation (against known
  names) and at final tool assembly.
- Specialist `Agent`s are constructed **while assembling the coordinator's tools
  for a given invocation** — required, since `as_tool()` is an instance method —
  which is cheap because the `ClientManager` builds each specialist's chat client
  for its **own** resolved model while reusing provider/credential state
  process-wide; they are **not** cached across Functions requests (avoids holding
  mutable MAF `Agent`s in global state). What is deferred is each specialist's
  **model run**, which only occurs if the coordinator's model selects that tool.
  Declaring many subagents therefore adds one cheap `Agent` construction each and
  enlarges the coordinator's prompt-visible tool schema (the real cost), but no
  specialist *runs* until selected.
- **Breadth & concurrency (Decisions log #14):** no hard cap on declared
  subagents; per-turn delegation count is bounded by MAF's tool-calling loop.
  Calls to *different* specialists run in parallel; concurrent calls to the
  *same* specialist are **serialized behind a per-specialist lock** in the thin
  adapter, so v1 never assumes a single leaf `Agent` instance is reentrant. Leaf
  runs share no conversation state (ephemeral, `propagate_session=False`, no
  persistent session — so no coordinator session-lock contention; that lock
  serializes *turns*, not intra-turn tool calls). The only shared components are
  the process-wide `ClientManager` and the per-app cached MCP tool objects, which
  are **already** shared across concurrent top-level requests today, so
  delegation introduces no new cross-request sharing model. A concurrency test
  covers parallel and repeated delegated calls.
- **Front-matter only.** `subagents` and `id` are per-agent front-matter fields
  with **no `agents.config.yaml` counterpart**, so no global/front-matter merge
  or precedence question arises (unlike `model` / `timeout` / `tools`).
- This stays aligned with the conventions in `docs/front-matter-spec.md` for
  optional, per-agent fields; §7 tracks the doc update and reference
  regeneration.

**Task contract & context isolation.** `as_tool` exposes the specialist as a
single-string tool argument named `task` (`arg_name="task"`), with an
`arg_description` instructing the coordinator to pass a **self-contained**
request. "Isolated" means the specialist receives **no implicit conversation
history** — not that no data is shared: the coordinator can (and must) put the
needed context into the `task` string. `propagate_session=True` is deliberately
off in v1 (it would share a session id + mutable state dict, still not chat
history). Delegated output re-enters only the coordinator's context as the
`tool_end` result.

**HITL.** Because the runtime has no approval or user-input handling today,
`as_tool` is invoked with `approval_mode="never_require"`. A specialist tool that
*requires* approval, or a specialist that raises `UserInputRequiredException`,
will **not** pause or surface to the user — such tools are unsupported inside a
v1 subagent. Author subagents with autonomous tools only. Genuine HITL (pause →
ask user → resume) is owned by the handoff fast-follow.

**Trust boundary (Decisions log #10).** `subagents` is an explicit
**capability grant** by the app author. A delegated call is an in-process tool
call, so the specialist's own endpoint authorization is **not** traversed: a
coordinator effectively exposes its specialists' tools/MCP/skills to anyone who
can invoke the coordinator (widened by prompt injection). v1 treats one deployed
app as one trust domain and relies on the author's explicit declaration.
Specialist opt-in (e.g. a `delegatable`/allow-list) is noted as possible future
hardening, not v1.

### Observability

Only the **outer** delegation boundary is visible in the coordinator's SSE
stream (`tool_start` / `tool_end` for the `as_tool` call). The specialist's own
token deltas and internal tool calls do **not** appear in the coordinator stream
unless `stream_callback` is deliberately wired (out of scope for v1). v1 accepts
this as a limitation and instead requires **correlated logs/traces** for the
delegated run (coordinator id, specialist id, delegated tool name, duration,
outcome) via the existing observability layer.

### Compatibility

Additive for any app that omits `subagents`/`id`: those agents behave exactly as
before. `GlobalConfig` and `AgentSpec` keep `extra="forbid"`, so this is a
**deliberate, additive** schema change (an unrelated unknown key still fails
fast). However, this is **not** a zero-touch change to shared code: the app
factory moves to two-pass composition and validation gains cross-agent rules, so
**regression coverage is required** for existing no-subagent behavior
(registration naming/suffixing, triggers, endpoints, tool assembly). The
identity rules can, in principle, reject a *previously accepted* app whose file
stems collide **and** are referenced as subagents; standalone colliding stems
that are never referenced continue to work (registration still auto-suffixes
their endpoints).

The field is deliberately **not** named `workflows:` (see §2 and Decisions log
#5). This design does not preclude the planned handoff fast-follow: the stable
identity, the immutable `AgentCatalog`, and the role-parameterized "build a MAF
`Agent` from a `ResolvedAgent`" helper are reusable groundwork for it. It does
**not** claim to be all handoff needs — `HandoffBuilder` additionally brings a
`Workflow` execution path, checkpointing, session ownership, request-info/HITL,
and event semantics, all owned by that FRD. Delegation and handoff are intended
to **coexist and compose** (Decisions log #7): an app can mix coordinators that
delegate with others that hand off, and — once the fast-follow lands — a handoff
participant may itself hold `as_tool` subagents.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | v1 pattern | manual routing / delegation (`as_tool`) / true handoff (`HandoffBuilder`) | **Delegation** | Human (user) | 2026-07-14 |
| 2 | Target interaction | "one assistant throughout" vs "specialist takes over" | **One assistant throughout** (→ delegation) | Human (user) | 2026-07-14 |
| 3 | Dependency for v1 | none (`as_tool` in core 1.3.x) / orchestrations beta / orchestrations stable + core bump | **None** (use `as_tool`, no new dep) | Agent (proposed) | 2026-07-14 |
| 4 | Handoff disposition | drop / "maybe later" / committed fast-follow / v1 preview | **Committed fast-follow, chat-scoped, designed-for in v1** | Human (user) | 2026-07-14 |
| 5 | Authoring field name | `subagents` / `delegates_to` / `agents` / reuse `workflows` | **`subagents`** (avoids `workflows` collision) | Agent (proposed — confirm) | 2026-07-14 |
| 6 | Mutual / peer references (A↔B) | reject cycles / single-level leaves / nested + depth cap | **Single-level leaves in v1** (mutual refs benign; nesting is a later extension) | Human (user) | 2026-07-14 |
| 7 | Delegation vs handoff coexistence | either-or / both coexist + compose | **Coexist + compose** (a handoff participant may hold `as_tool` sub-agents) | Human (user) | 2026-07-14 |
| 8 | HITL in v1 | support / none | **None** — no HITL/approval exists in the runtime today (verified); v1 sub-agents run autonomously; HITL is net-new and lands with the handoff fast-follow | Agent (proposed — confirm) | 2026-07-14 |
| 9 | Participant identity | file-stem slug only / display `name` / new stable `id` field / relative path | **Optional `id` field, else file-stem slug**; resolved pre-registration; reject ambiguous collisions | Human (user) | 2026-07-14 |
| 10 | Delegation trust model | coordinator-authority / specialist opt-in / auth-level guardrail | **Coordinator-authority** — `subagents` is an author capability grant; one app = one trust domain; specialist endpoint auth not consulted | Human (user) | 2026-07-14 |
| 11 | `subagents` schema shape | object-only / string-list / mixed `str \| SubagentRef` union | **Object-only `SubagentRef`**; string shorthand deferrable later as non-breaking sugar | Human (user) | 2026-07-14 |
| 12 | Delegated failure / timeout / cancellation | abort coordinator / recoverable tool error / split the two classes | **Split**: child failure or specialist-local timeout → recoverable `tool_end` error; parent/request cancellation → propagate + abort; effective timeout = `min(specialist, coordinator remaining)`; stable sanitized error to the model, full detail to telemetry | Agent (proposed — confirm) | 2026-07-14 |
| 13 | Leaf execution profile | inherit full capabilities / restricted leaf profile | **Restricted leaf**: static user + MCP + skills only; no sandbox / workflow / nested subagents; via a builder execution-role parameter | Agent (proposed — confirm) | 2026-07-14 |
| 14 | Delegation breadth / concurrency | no cap / explicit caps / serialize calls | **No hard cap**; different specialists run in parallel, concurrent same-specialist calls serialized per-specialist (no leaf-`Agent` reentrancy assumption); per-turn count bounded by MAF's tool loop; prompt-schema size is the documented cost | Human (user) | 2026-07-14 |

## 6. Test plan

- [ ] Unit: `schema` — `SubagentRef` (object form) parses; optional `id` parses;
      `extra="forbid"` still holds; string-shorthand entries are rejected in v1.
- [ ] Unit: `merge` — normalization of `subagents`; identity derivation
      (`id` when set, else file-stem slug), computed independent of registration
      suffixing.
- [ ] Unit: `validation` — unknown-reference error; **duplicate** reference
      error; **self-reference** error; **duplicate explicit `id`** error; an
      **ambiguous reference** (a referenced fallback-slug that resolves to >1
      agent) errors, while colliding stems that are **never referenced** still
      register (auto-suffixed) as today; a mutual/peer reference (A↔B) is
      **accepted** (no cycle rejection); an endpoint-less agent referenced as a
      subagent is a valid **internal specialist** (trigger/`builtin_endpoints`
      relaxed) regardless of file ordering.
- [ ] Unit: tool-name collision — `delegate_<id>` / `tool_name` collision with a
      coordinator user/MCP/sandbox/workflow tool or another subagent fails fast,
      at capability-aware validation and again at final runtime assembly.
- [ ] Unit: `runner` / leaf profile — coordinator assembles specialist `as_tool`
      tools; a delegated call returns a result; a leaf specialist gets its
      static user/MCP/skills tools but **no** sandbox tools, **no** workflow
      tools, and does **not** expand its own `subagents`; the same agent invoked
      directly still gets its full role.
- [ ] Unit: failure vs cancellation — a specialist error / specialist-local
      timeout surfaces as a recoverable `tool_end` error (coordinator not
      aborted, detail sanitized); a **parent/request cancellation propagates**
      and aborts the coordinator (not swallowed); effective timeout is
      `min(specialist, coordinator remaining)`.
- [ ] Unit: concurrency — concurrent calls to the **same** specialist within one
      turn are serialized (no reentrancy assumption) while calls to **different**
      specialists run in parallel; all results are correct.
- [ ] Fixture scenario: `tests/fixtures/config_scenarios/<nn_delegation>/` — a
      coordinator plus two specialists, one an endpoint-less internal specialist,
      exercising object form, `id` fallback, and identity-collision rejection.
- [ ] Regression: existing no-subagent fixtures still resolve/register
      identically under two-pass composition (naming/suffixing, triggers,
      endpoints, tool assembly unchanged).
- [ ] Sample: new `samples/multi-agent-delegation/` (coordinator + 2
      specialists, one endpoint-less/internal; coordinator shows a self-contained
      `task` handoff).

## 7. Docs impact

- [ ] `docs/front-matter-spec.md` — document the new `subagents:` (object form)
      and `id:` fields and the leaf execution profile; then regenerate
      `docs/front-matter-reference.md` via
      `python eng/scripts/generate_config_reference.py` and run the
      `update-schema-docs` skill to sync examples.
- [ ] `docs/architecture.md` — document the two-pass composition, the immutable
      `AgentCatalog`, and the `direct`/`leaf` execution role in the module map /
      pipeline; introduce "coordination" as a concept and its relationship to
      (and distinction from) Dynamic Workflows (FRD 0004).
- [ ] `docs/triggers.md` — note that an otherwise triggerless/endpoint-less agent
      is valid **only** when globally referenced as an internal specialist (the
      relaxed reachability rule).
- [ ] `README.md` — add a `subagents:` quickstart example, including the trust
      boundary and self-contained-`task` guidance.
- [ ] `docs/frds/README.md` — add FRD 0006 to the index.

## 8. Status & sign-off

- **Architecture review (phase 2):** **Complete (agent review).** Three
  `rubber-duck` passes: **R1** (v1) drove two-pass composition, stable identity,
  tool-name collisions, failure semantics, the leaf profile, and the trust
  boundary; **R2** (v2) drove catalog/capability ordering, the multimap identity
  model, corrected `Agent`-construction timing, the failure-vs-cancellation
  split, phased validation, and the breadth/concurrency decision (#14); **R3**
  (v3.1) confirmed the multimap index type, per-model client construction, and
  the per-specialist concurrency-serialization model. **No mechanical blockers
  remain** (final verdict: *Go*). The only residuals are the two product choices
  below, intentionally deferred to the human reviewer.
- **Human sign-off:** Pending. `status` stays `Draft` until the human reviewer
  resolves the open questions and records sign-off here (then → `Finalized`).

### Open questions (resolve before sign-off unless noted)

- **Field name** — `subagents` (proposed, #5) vs. `delegates_to` vs. `agents`.
- **Who may declare `subagents`** — any independently-runnable agent (proposed;
  always leaf when itself invoked as a subagent) vs. main-only (mirroring the
  Dynamic Workflows constraint on `workflows.enabled`, FRD 0004).
- **`arg_description` wording** — the exact instruction that best elicits a
  self-contained `task` from coordinators. *(Refine during implementation.)*
- **Handoff dependency path** (fast-follow FRD, *not* v1) — beta
  `agent-framework-orchestrations` (`1.0.0b260507`, `core>=1.3.0,<2`) vs. the
  stable `1.0.0` (needs `core>=1.9.0`). *(Deferred to the handoff FRD.)*
