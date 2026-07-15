---
frd: 0006
title: Multi-agent delegation (agent-as-tool)
status: Draft            # Draft → In review → Finalized  (→ Implemented after merge)
author: larohra
created: 2026-07-14
updated: 2026-07-15
issues: []
pull_requests: []
branch: larohra/multi-agent-delegation
---

# FRD 0006 — Multi-agent delegation (agent-as-tool)

## 1. Summary

> **TL;DR:** Today, an app can contain several agents, but users or app
> authors must route work between them. This feature lets one coordinator ask
> declared specialists for help, much like a lead asking subject-matter experts
> and then giving one final answer. It is the lighter coordination option: the
> coordinator stays in control, it uses an API already in the pinned dependency,
> and the runtime keeps one normal `agent.run()` path. True handoff, where a
> specialist takes control, is a committed fast-follow rather than part of v1.

v1 adds **delegation** to the markdown-first runtime. A coordinator
`*.agent.md` declares existing agents as specialists in a new `subagents:`
front-matter field. At runtime, the coordinator calls those specialists as
tools through Microsoft Agent Framework (MAF) `BaseAgent.as_tool()`. The user
still interacts with "one assistant that consults specialists, then answers."

Delegation needs **no new dependency**. `as_tool()` already exists, unchanged,
on `BaseAgent` in the pinned `agent-framework-core==1.3.*`. It fits the existing
tool assembly, registration, SSE streaming, and single-`agent.run()` path.

True **handoff** is different: control moves between agents through MAF's
`HandoffBuilder`, which returns a `Workflow`. Handoff is not part of v1. It is a
committed, chat-scoped fast-follow with its own FRD. This design prepares for
that work through stable participant identities, an immutable in-memory
`AgentCatalog`, and a reusable helper that builds a MAF `Agent` from a
`ResolvedAgent` for an explicit execution role. These choices avoid later
changes to the authoring surface or the per-`session_id` storage model.

Tracking issue: `Azure/azure-functions-bucees-planning#1185` — "[Serverless
Agents] Multi-agent: Handoff via HandoffBuilder + workflows
(agents.config.yaml)".

**Glossary**

- **MAF:** Microsoft Agent Framework, the agent library used by this runtime.
- **Coordinator:** the agent that receives the request, consults specialists, and gives the final answer.
- **Specialist / sub-agent:** an existing `*.agent.md` agent that the coordinator can call for focused work.
- **Delegation:** the coordinator calls a specialist as a tool and keeps control.
- **Handoff:** control transfers to another agent; this is the planned fast-follow.
- **`as_tool()`:** the MAF API that exposes an agent as a callable tool.
- **`direct` / `delegated` role:** the same agent entered through its endpoint or called as a coordinator tool.
- **Dynamic Workflows:** the repo's existing single-agent Durable Functions feature; it is not MAF multi-agent orchestration.

## 2. Motivation / problem

Today, each `*.agent.md` file registers independently with its own trigger(s)
and/or built-in endpoint(s). The runtime does not compose agents. The
`samples/multi-agent-folder` sample makes the user choose an endpoint, for
example by suggesting the research endpoint. An app author who wants automatic
routing must build it themselves.

Customers building AgentApps with several specialists want one agent to route
work automatically instead of exposing N endpoints and asking the user to
choose. The confirmed v1 interaction is **"one assistant throughout — it
consults specialists, then answers"** (Decisions log #2). The coordinator stays
in control. Delegation is therefore the smallest pattern that meets the need.
Both delegation and handoff are new to this runtime; nothing coordinates agents
today.

Two existing concepts use the word "workflow" but mean different things:

- This repo's **Dynamic Workflows** (FRD 0004, `docs/workflows.md`) are
  LLM-authored DAGs of tool calls. Durable Functions executes them. An agent
  opts in through `workflows.enabled` front matter, and the feature is
  explicitly single-agent.
- MAF's orchestration **`Workflow`** layer supports multi-agent control transfer
  and shared-context collaboration. `HandoffBuilder`, `GroupChatBuilder`, and
  `MagenticBuilder` all return a `Workflow`. True handoff uses
  `HandoffBuilder` and full-mesh shared broadcast context, so every participant
  sees the running conversation.

Delegation through `as_tool()` is the only MAF multi-agent pattern that runs
inside plain `agent.run()` with no `Workflow`. Because the two workflow concepts
are unrelated, this FRD must not reuse `workflows:` for its new field (Decisions
log #5). The future handoff FRD will add the MAF orchestration `Workflow` path.

The three routing patterns are:

1. **Manual routing (today):** separate endpoints; the user chooses.
2. **Delegation / agent-as-tool (`as_tool()`):** the coordinator stays in
   control, calls specialists as tools, and combines their work into the final
   answer. It uses plain `agent.run()` and no new dependency. **This is v1.**
3. **True handoff (`HandoffBuilder`):** control transfers to the specialist,
   which owns later turns. Participants share broadcast context. This needs a
   `Workflow` execution path and the `agent-framework-orchestrations`
   dependency. **This is the fast-follow.**

## 3. Goals / Non-goals

**Goals**

- Add an optional, object-only `subagents:` front-matter field. A coordinator
  can use it to declare one or more existing agents as specialists.
- Let the coordinator call each specialist through MAF
  `BaseAgent.as_tool()` inside the existing `agent.run()` path, with no MAF
  `Workflow`.
- Run a specialist as itself in the **delegated** role: with its own
  instructions, model, static user tools, MCP servers, and skills.
  Request-scoped capabilities are naturally absent: a delegated call has no
  per-request sandbox of its own, and Dynamic-Workflow tools are already
  main-only. Delegation does not strip them.
- Add no dependency. `as_tool()` is present and unchanged in the pinned
  `agent-framework-core==1.3.*`.
- Make same-stem agent slugs **fail fast app-wide** (replacing today's silent
  auto-suffixing), so every agent has a unique slug — one collision contract,
  consistent with how the runtime already rejects duplicate skill and
  workflow-tool names. *(Breaking change — see §4 Compatibility and Decisions
  log #17.)*
- Use the existing `tool_start` and `tool_end` SSE events for delegated calls.
  The tool name is the `as_tool()` name. Do not add an event type or require a
  client/UI change.
- Establish reusable groundwork for the handoff fast-follow: stable participant
  identities, an immutable catalog, and role-based agent construction. Avoid
  coordinator-only APIs that handoff would have to replace.

**Non-goals**

- True handoff or control transfer through `HandoffBuilder`, or any MAF
  orchestration `Workflow` execution path.
- Workflow checkpointing plus `request_info` pause/resume across Function
  invocations.
- **Any human-in-the-loop (HITL) flow**, including tool approvals or user-input
  pause/resume. The runtime has none today. This was verified by finding no
  `user_input_requests`, `approval_mode`, `request_info`, or
  `UserInputRequired` handling in `src/`; `run_agent` awaits `agent.run(...)`
  once and has no approval loop. v1 specialists must use autonomous tools.
  HITL is deferred to the handoff fast-follow.
- `propagate_session=True`, shared context, or shared-state tools. In v1 the
  specialist receives only the tool-call argument, not the coordinator's chat
  history.
- Nested delegation. v1 is single-level: a delegated specialist cannot delegate
  onward. This is a deliberate v1 boundary, not a permanent limit. A later
  bounded-nesting change can stay localized to agent construction, so v1 adds
  no depth-counter machinery.
- String shorthand such as `subagents: [billing, tech]`. v1 accepts only the
  object form. Shorthand can be added later as non-breaking sugar.

## 4. Proposed design

### 4.1 Runtime foundation

**TL;DR:** Delegation uses an API already available in the pinned MAF package.

The existing API is:

```python
BaseAgent.as_tool(
    *,
    name=None,
    description=None,
    arg_name="task",
    arg_description=None,
    approval_mode="never_require",
    stream_callback=None,
    propagate_session=False,
) -> FunctionTool
```

It is defined on root `BaseAgent`, inherited by concrete `Agent`, and present
unchanged in pinned `agent-framework-core==1.3.*` (verified at tag
`python-1.3.0`). No new dependency is needed.

### 4.2 Pipeline and two-pass composition

**TL;DR:** Resolve and validate all agents before registering any of them.

`subagents:` uses the existing four-stage pipeline from
`docs/architecture.md` §2: discover → translate → register → execute. The
runtime now needs a global view because one agent can refer to another.
Therefore, the app factory replaces its interleaved validate/register loop with
an explicit multi-pass order.

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| discover | — | No change. Coordinators and specialists are ordinary `*.agent.md` files found by the existing top-level and `agents/` folder scan (FRD 0001). Discovery stays read-only. |
| translate | `config/schema.py`, `config/merge.py`, `config/validation.py` | Add `SubagentRef` (`{agent, when?}`) and `AgentSpec.subagents: list[SubagentRef] \| None`. Carry `subagents` and the resolved identity (`agent_id`, the file-stem slug) onto `ResolvedAgent`. `merge.py` normalizes and validates each reference, and derives canonical identity from the file-stem slug. `validation.py` uses the complete global index. It rejects **duplicate agent slugs (app-wide, fail-fast)**, unknown/duplicate/self references, and tool-name collisions. It relaxes the trigger-or-`builtin_endpoints` rule for a referenced internal specialist. |
| register | `app.py`, `registration/_handlers.py`, `registration/endpoints.py`, `registration/_naming.py` | `app.py` becomes the composition root. It builds the identity index (slug → agent) and **fails fast if two agents share a slug** (`_naming.py` now rejects duplicates instead of auto-suffixing). After capabilities and capability-aware validation, it builds the immutable `AgentCatalog` (`agent_id -> (ResolvedAgent, AgentCapabilities)`) and the global referenced-subagent set. All of this happens before any `FunctionApp` mutation. `_handlers.py` and `endpoints.py` pass immutable catalog data, not live MAF agents, into `run_agent` and `run_agent_stream` for coordinators with `subagents`. Registration parses no YAML/front matter and resolves no references; it consumes validated data. |
| execute | `runner.py` | Add `build_subagent_tools(subagents, catalog)`. For each reference, build the specialist's MAF `Agent` in the `delegated` role through the shared role-based client/tool assembly path. `ClientManager` creates the chat client for the specialist's own resolved model while reusing provider and credential state process-wide. Expose the agent with `.as_tool(name=delegate_<slug>, description=<when \| specialist description>, arg_name="task", arg_description=…, approval_mode="never_require", propagate_session=False)` and a thin adapter for failure and concurrency rules. Append the resulting tools to the coordinator's `resolved_tools`. Build the tool eagerly, but run the specialist model only if the coordinator selects it. |

The ordered composition pipeline is:

1. Resolve every discovered agent into a typed `ResolvedAgent`.
2. Build the identity index (slug → agent) and the global set of referenced
   ids. Two agents that resolve to the same slug are a fail-fast error here.
3. Validate schema and references against that index: unknown, duplicate, and
   self-references; tool-name collisions; and the internal-specialist rule.
4. Build `AgentCapabilities` for each agent.
5. Run capability-aware validation, including tool-name checks against each
   coordinator's known tool set.
6. Build the immutable runtime `AgentCatalog`:
   `agent_id → (ResolvedAgent, AgentCapabilities)`.
7. Mutate the single `FunctionApp` by adding triggers and built-in endpoints.

An early agent can depend on a later one. For example, a triggerless internal
specialist is valid only if another agent references it. The ordered pipeline
keeps validity independent of discovery order and prevents partial Azure
registration before global validation finishes. Cross-agent awareness in steps
2–5 is the only genuinely new plumbing requirement; current validation rules
are local to one agent.

The runtime checks tool-name collisions again during final tool assembly because
MCP and sandbox tool names may not be known earlier.

### 4.3 Participant identity (Decisions log #9)

**TL;DR:** A specialist is referenced by its file-stem slug, and every agent's slug is unique — the app fails fast at startup if two collide.

Each agent's identity for `subagents` references is its **file-stem slug**:
`billing.agent.md` becomes `billing`. Translation derives it from the file stem
the same way `_naming.py` derives slugs. Because duplicate slugs now fail fast
app-wide (see below and Decisions log #17), a reference resolves to exactly one
agent — there is no suffixing to disambiguate against. (There is no separate
`id` field in v1 — see Decisions log #16.)

Slugs are **globally unique**. Two agents that resolve to the same slug — e.g.
one in the app root and one in `agents/`, or two names that sanitize to the same
slug — are a **fail-fast startup error** ("rename one of these files"). This
replaces today's silent auto-suffixing and makes agent-slug handling consistent
with how the runtime already rejects duplicate skill and workflow-tool names
(Decisions log #17; this is a breaking change — see *Compatibility*).

Because slugs are unique, a `subagents` reference resolves to exactly one agent
or fails as an "unknown reference" — no ambiguity handling is needed. Display
`name` is never an identity.

### 4.4 Direct and delegated execution roles (Decisions log #13)

**TL;DR:** A delegated specialist is the same agent with its own configuration, entered through a different path.

A referenced specialist remains an ordinary `*.agent.md` agent. It runs with
its own instructions, model, timeout, static user tools, MCP servers, and
skills. Delegation does not copy coordinator tools onto it or remove its static
tools. MAF `BaseAgent.as_tool()` runs the wrapped agent unchanged, as verified
against `agent_framework` source. The same full-capability pattern appears in
the surveyed OpenAI Agents SDK, LangGraph, AutoGen, CrewAI, and Google ADK.

| Capability | `direct` role | `delegated` role |
| --- | --- | --- |
| Entry point | Own trigger or endpoint | Coordinator's `as_tool()` call |
| Instructions, model, timeout, static tools, MCP, skills | Specialist's own | Specialist's own |
| Per-request sandbox | Attached by the top-level handler | No new sandbox session |
| Dynamic-Workflow tools | Main-agent-only rule applies | Absent because they are already main-only |
| Conversation context | Direct request/session behavior | Only the `task` string; `propagate_session=False` |
| Own `subagents` | Full direct-role behavior | Not wired in v1 |

The sandbox/code-interpreter is request-scoped. A trigger or endpoint handler
attaches it with `build_sandbox_tools_for_session`, using an ACA session id
created for that top-level invocation. A delegated call is not another
top-level request, so it opens no specialist sandbox session. A separate
delegated sandbox session could be a future enhancement.

Dynamic-Workflow tools are already restricted to `main.agent.md` by FRD 0004,
regardless of delegation. No delegation-specific removal is needed.

Conversation history is also isolated. With `propagate_session=False`, the
specialist receives only the `task` string. This is the standard default in
OpenAI, LangGraph, and AutoGen. In pinned MAF 1.3.0,
`propagate_session=True` can merge parent and child histories, so keeping it off
is the safe, conventional choice. The coordinator includes needed context in
the task.

One reusable helper builds a MAF `Agent` from a `ResolvedAgent`, parameterized
by the `direct` or `delegated` execution role. It never mutates
`ResolvedAgent`. Therefore, the same specialist still gets its complete direct
capabilities through its own endpoint.

### 4.5 Delegation depth

**TL;DR:** v1 permits one coordinator-to-specialist level and removes recursion structurally.

When the runtime builds a specialist for `as_tool()`, it does not wire that
specialist's own `subagents`. The resulting agent has no `delegate_*` tools.
There is no runtime refusal or cycle check because the capability is absent.
Mutual references are therefore safe: A may reference B and B may reference A,
but neither expands the other's references during delegation.

Single-level delegation matches the confirmed interaction and common
coordinator-to-specialist designs. Google ADK's `task`/`single_turn` modes use
single-level "leaf agents"; CrewAI's default path is effectively single-level.
MAF itself has no recursion, depth, or cycle guard. Its only runaway bound is a
per-run limit of about 40 iterations, which does not compose across nesting
levels. A future nested design must therefore add its own bound.

Deeper composition remains a natural extension. Mature frameworks use small
budgets rather than free recursion: Anthropic caps sub-agent nesting at 5,
LangGraph uses `recursion_limit=25`, and ADK uses global `max_llm_calls`.
Because v1's block is one construction decision, future bounded nesting can be
a localized builder change: wire `subagents` below a limit and pass depth+1.
v1 adds no depth counter, and `AgentCatalog` and `run_agent` need no change now
to preserve that option.

The single-level guarantee covers native `subagents` expansion. An arbitrary
MCP or user tool could still call another endpoint over HTTP. That behavior is
outside delegation's control.

### 4.6 Failure, timeout, and cancellation (Decisions log #12)

**TL;DR:** Child failures return to the coordinator; parent cancellation stops the request.

The runtime treats two failure classes differently:

- **Specialist failure or specialist-local timeout is recoverable.** Return it
  as a recoverable tool error in `tool_end`. The coordinator can retry, choose
  another route, or apologize.
- **Parent or request cancellation propagates.** A coordinator-turn timeout,
  client disconnect, or host shutdown cancels the coordinator and all in-flight
  delegated calls. Do not turn it into a recoverable tool result.

The effective specialist timeout is
`min(specialist timeout, coordinator's remaining budget)`. The recoverable path
returns a stable, sanitized string to the model/client. Internal telemetry keeps
full diagnostic detail: redacted exception type, correlation id, and outcome.
Existing SSE order remains `tool_start` then `tool_end`, with recoverable errors
inside the `tool_end` result.

A thin adapter may be needed to guarantee this split around pinned
`as_tool()` behavior. Implementation must verify that version's real exception
and cancellation surface rather than assume it. The same adapter serializes
same-specialist calls.

### 4.7 Build live agents per request

**TL;DR:** Cache immutable definitions and shared clients, but build each live MAF agent fresh for every request.

Do not build specialists once at startup:

- Some state exists only for a request. A top-level agent's sandbox uses a
  one-time secure session, and conversation history belongs to one user's chat.
  A startup agent would need per-call patching, which defeats caching.
- A MAF `Agent` is mutable, not a frozen share-safe object. On first use,
  `agent.run()` attaches an in-memory history provider to the instance. This was
  confirmed in pinned `agent-framework-core==1.3.*` and remains true upstream.
  One warm Functions worker can serve concurrent requests, so sharing one live
  agent would create the race class already guarded by the per-session lock.
- Fresh construction is cheap. `ClientManager` already reuses the expensive
  model client process-wide. A lightweight `Agent` wrapper adds little work,
  prevents cross-request state sharing, and uses less cold-start time and memory
  than pre-building every agent.

The runtime caches `AgentCatalog` (`ResolvedAgent` + `AgentCapabilities`) and the
shared client. It builds the coordinator and delegated specialist `Agent`
objects per call, matching today's execution model.

### 4.8 Authoring and routing

**TL;DR:** Coordinators declare object-form specialist references with an optional routing hint.

`subagents` is an optional per-agent front-matter field:

```yaml
# agents/coordinator.agent.md
---
name: Support Coordinator          # human-readable display name (not identity)
description: Routes customer questions to the right specialist
builtin_endpoints: true
subagents:
  - agent: billing                 # references billing.agent.md by its slug
    when: Invoices, charges, refunds, or subscription questions   # → as_tool(description=...)
  - agent: tech                    # when omitted → uses tech's own `description`
---
You are a support coordinator. Use the billing and tech specialists when
relevant, then give the customer a single consolidated answer.
```

`SubagentRef` has two fields:

- **`agent`** is required and names the specialist by slug.
- **`when`** is an optional routing hint. It defaults to the specialist's own
  required `description`.

The coordinator model reads `when` to decide whether to call a specialist.
Routing is **model-selected, not deterministic**. A weak hint can skip
delegation or route incorrectly; this is an accepted limitation.

The delegated tool is named `delegate_<slug>`. That name must be a valid
identifier and unique in the coordinator's final tool set. A collision with a
user, MCP, sandbox, workflow, or another specialist tool fails fast and is never
silently suffixed, because the name is a prompt-visible API. Validate known
names after capabilities are built, then check again during final runtime
assembly for late MCP/sandbox names.

Construct specialist agents while assembling one invocation's coordinator
tools because `as_tool()` is an instance method. `ClientManager` builds each
specialist client for that specialist's own resolved model and reuses
provider/credential state process-wide. Do not cache mutable MAF agents between
Functions requests. Declaring a specialist creates one cheap wrapper and adds
its schema to the coordinator prompt; the specialist's model does not run
until selected. Prompt-visible tool-schema size is the real cost of declaring
many specialists.

These fields are front-matter-only. They have no `agents.config.yaml`
equivalent, so there is no global/front-matter merge or precedence rule as there
is for `model`, `timeout`, and `tools`. This follows
`docs/front-matter-spec.md`; §7 covers documentation and reference regeneration.

### 4.9 Task and context contract

**TL;DR:** The coordinator must send a self-contained task because no conversation history is shared implicitly.

`as_tool()` gives the specialist one string argument named `task`
(`arg_name="task"`). Its `arg_description` tells the coordinator to send a
self-contained request. Isolation does not mean that no data can move: the
coordinator must include all needed context in this string.

`propagate_session=True` is deliberately off. It would share a session id and
mutable state dictionary, but not chat history. The specialist's output returns
only as the coordinator's `tool_end` result.

### 4.10 HITL and trust boundary

**TL;DR:** Specialists run autonomously inside the coordinator's app trust boundary.

The runtime has no approval or user-input flow today, so v1 uses
`approval_mode="never_require"`. A specialist tool that requires approval, or a
specialist that raises `UserInputRequiredException`, cannot pause or surface a
question to the user. Such tools are unsupported in v1 specialists. Real HITL
(pause → ask → resume) belongs to the handoff fast-follow.

`subagents` is an explicit **capability grant** from the app author. A delegated
call runs in-process and does not pass through the specialist endpoint's
authorization. Anyone who can invoke the coordinator effectively gains access
to the declared specialist's tools, MCP servers, and skills; prompt injection
can widen that exposure. v1 treats one deployed app as one trust domain and
relies on the declaration. Specialist opt-in, such as a `delegatable` flag or
allow-list, is possible future hardening, not v1.

### 4.11 Breadth and concurrency (Decisions log #14)

**TL;DR:** Declare any number of specialists; run different ones in parallel and serialize repeated calls to the same one.

v1 sets no hard cap on declared specialists. MAF's tool-calling loop bounds the
number of delegations in one turn. Different specialists may run in parallel.
The adapter uses a per-specialist lock for concurrent calls to the same
specialist, so v1 does not assume one delegated `Agent` is reentrant.

Delegated calls are ephemeral, use `propagate_session=False`, and have no
persistent session. They do not contend for the coordinator session lock, which
serializes turns rather than tool calls inside one turn. The only shared
components are process-wide `ClientManager` and per-app cached MCP tool objects.
Both are already shared across concurrent top-level requests, so delegation
adds no new cross-request sharing model. Tests cover parallel specialists and
repeated calls to one specialist.

### 4.12 Observability

**TL;DR:** Existing SSE shows the outer delegated call; correlated telemetry shows the specialist run.

The coordinator's SSE stream exposes only the outer `as_tool()` boundary through
`tool_start` and `tool_end`. Specialist token deltas and internal tool calls do
not appear unless `stream_callback` is wired, which is out of scope for v1.
This is an accepted limitation.

Existing observability must record correlated logs/traces for the delegated
run: coordinator slug, specialist slug, delegated tool name, duration, and outcome.

### 4.13 Compatibility and handoff groundwork

**TL;DR:** Adding `subagents` is additive, but one change is breaking — same-stem agent slugs now fail fast instead of auto-suffixing.

Adding `subagents` is additive: apps that omit it behave as before, and
`GlobalConfig`/`AgentSpec` keep `extra="forbid"`, so unrelated unknown keys still
fail fast.

**One deliberate breaking change (Decisions log #17):** two agent files that
resolve to the same slug — e.g. `billing.agent.md` in both the app root and
`agents/`, or names that sanitize identically — now cause a **fail-fast startup
error** instead of today's silent auto-suffixing (`billing`, `billing_2`).
Migration is a one-line fix: rename one file. This unifies the collision
contract app-wide (matching duplicate skill/workflow-tool handling) and is what
lets `subagents` references resolve unambiguously — but because it can reject a
previously-booting app, it must be called out in release notes.

The implementation is not zero-touch: the app factory changes to two-pass
composition, validation gains cross-agent rules, and `_naming.py` changes from
auto-suffix to fail-fast. Regression tests must cover existing no-subagent
registration names, triggers, endpoints, and tool assembly, plus the new
fail-fast path.

The field is not named `workflows:` for the reasons in §2 and Decisions log #5.
Stable identities, immutable `AgentCatalog`, and the role-based agent builder
are reusable for handoff. They are not all that handoff needs:
`HandoffBuilder` also requires a `Workflow` execution path, checkpointing,
session ownership, request-info/HITL, and event semantics. The handoff FRD owns
that work.

Delegation and handoff will coexist and compose (Decisions log #7). An app may
mix delegating coordinators with handoff coordinators. After the fast-follow, a
handoff participant may itself use `as_tool()` specialists.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | v1 pattern | manual routing / delegation (`as_tool`) / true handoff (`HandoffBuilder`) | **Delegation** | Human (user) | 2026-07-14 |
| 2 | Target interaction | "one assistant throughout" vs "specialist takes over" | **One assistant throughout** (→ delegation) | Human (user) | 2026-07-14 |
| 3 | Dependency for v1 | none (`as_tool` in core 1.3.x) / orchestrations beta / orchestrations stable + core bump | **None** (use `as_tool`, no new dep) | Agent (proposed) | 2026-07-14 |
| 4 | Handoff disposition | drop / "maybe later" / committed fast-follow / v1 preview | **Committed fast-follow, chat-scoped, designed-for in v1** | Human (user) | 2026-07-14 |
| 5 | Authoring field name | `subagents` / `delegates_to` / `agents` / reuse `workflows` | **`subagents`** (avoids `workflows` collision) | Human (user) | 2026-07-15 |
| 6 | Mutual / peer references (A↔B) | reject cycles / single-level (structural) / nested + depth cap | **Single-level in v1**, enforced structurally (a delegated specialist has no `delegate_*` tools wired), so mutual refs are benign and no cycle detection is needed; nesting is a later extension | Human (user) | 2026-07-14 |
| 7 | Delegation vs handoff coexistence | either-or / both coexist + compose | **Coexist + compose** (a handoff participant may hold `as_tool` sub-agents) | Human (user) | 2026-07-14 |
| 8 | HITL in v1 | support / none | **None** — no HITL/approval exists in the runtime today (verified); v1 sub-agents run autonomously; HITL is net-new and lands with the handoff fast-follow | Agent (proposed — confirm) | 2026-07-14 |
| 9 | Participant identity | file-stem slug only / display `name` / stable `id` field / relative path | **File-stem slug only** (no `id` field — see #16); computed pre-registration; slugs are globally unique — any collision fails fast at startup (see #17) — so a reference resolves to exactly one agent | Human (user) | 2026-07-15 |
| 10 | Delegation trust model | coordinator-authority / specialist opt-in / auth-level guardrail | **Coordinator-authority** — `subagents` is an author capability grant; one app = one trust domain; specialist endpoint auth not consulted | Human (user) | 2026-07-14 |
| 11 | `subagents` schema shape | object-only / string-list / mixed `str \| SubagentRef` union | **Object-only `SubagentRef`** with fields `{agent, when?}` (see #16); string shorthand deferrable later as non-breaking sugar | Human (user) | 2026-07-14 |
| 12 | Delegated failure / timeout / cancellation | abort coordinator / recoverable tool error / split the two classes | **Split**: child failure or specialist-local timeout → recoverable `tool_end` error; parent/request cancellation → propagate + abort; effective timeout = `min(specialist, coordinator remaining)`; stable sanitized error to the model, full detail to telemetry | Agent (proposed — confirm) | 2026-07-14 |
| 13 | How a specialist runs when delegated (the delegated role) | strip a delegated agent's tools / inherit-then-restrict / **runs as itself** with request-scoped caps naturally absent | **Runs as itself** — own instructions/model/static tools/MCP/skills, via a builder `direct`/`delegated` execution-role param (no mutation); sandbox is per-request and Dynamic-Workflow tools are already main-only, so both are *naturally* absent from a delegated call rather than stripped; context isolated (`propagate_session=False`) per industry-standard default | Agent (proposed — confirm) | 2026-07-15 |
| 14 | Delegation breadth / concurrency | no cap / explicit caps / serialize calls | **No hard cap**; different specialists run in parallel, concurrent same-specialist calls serialized per-specialist (no delegated-`Agent` reentrancy assumption); per-turn count bounded by MAF's tool loop; prompt-schema size is the documented cost | Human (user) | 2026-07-14 |
| 15 | Delegated-role capability scope, re-examined via cross-framework research | keep original restrictive framing / drop restrictions / reframe | **Reframe** — a survey of MAF + OpenAI, Anthropic, LangGraph, AutoGen, CrewAI, and Google ADK confirmed MAF imposes *none* of the originally-drafted restrictions and that a delegated sub-agent running with its own tools is the universal norm. Dropped the "workflow-tool strip" (redundant with FRD 0004's main-only gating); reframed sandbox/context as request-scoped defaults, not restrictions; kept **single-level** (#6) as the one real delegation rule — enforced structurally (a delegated specialist has no `delegate_*` tools wired), precedented (ADK task-mode, CrewAI), with bounded nesting left as a localized future change | Human (user) | 2026-07-15 |
| 16 | Drop `id` and `tool_name` for v1 (simplicity) | keep both / drop both / drop one | **Drop both** — identity is the file-stem slug (collisions fail fast app-wide — see #17); the delegated tool is always `delegate_<slug>`. Removes a field and the id/name/slug confusion; both are re-addable later as non-breaking additions if a real need appears | Human (user) | 2026-07-15 |
| 17 | Same-stem slug collision handling (base runtime) | keep auto-suffix+warn / fail-fast app-wide / open separate issue | **Fail-fast app-wide** — replace `_naming.py`'s silent auto-suffix with a startup error, unifying the contract with duplicate skill/workflow-tool handling and guaranteeing unique slugs for `subagents` references. **Breaking**: existing apps with same-stem files must rename one (release-note item) | Human (user) | 2026-07-15 |
| 18 | Who may declare `subagents` | any independently runnable agent / main-agent-only (mirror FRD 0004 `workflows.enabled`) | **Any independently runnable agent** may declare `subagents`. Single-level (#6) still applies, so when that agent is itself invoked as a sub-agent its `subagents` are not wired and it cannot delegate onward. Simpler than a main/non-main split and matches the cross-framework norm (#15) | Human (user) | 2026-07-15 |

## 6. Test plan

- [ ] Unit: `schema` — `SubagentRef` object form parses (fields `agent`, optional
      `when`); `extra="forbid"` remains; v1 rejects string shorthand.
- [ ] Unit: `merge` — normalize `subagents`; derive identity from the file-stem
      slug.
- [ ] Unit: `validation` / `_naming.py` — **fail fast on any duplicate agent
      slug app-wide** (same stem in root vs `agents/`, or names that sanitize
      alike), with an actionable rename error naming both files (replaces the
      old auto-suffix behavior); also reject unknown, duplicate, and
      self-references; accept A↔B with no cycle rejection; accept a referenced
      endpoint-less internal specialist regardless of file order.
- [ ] Unit: tool-name collision — fail fast when `delegate_<slug>`
      collides with coordinator user/MCP/sandbox/workflow tools or another
      specialist. Check during capability-aware validation and final assembly.
- [ ] Unit: `runner` / delegated role — assemble `as_tool()` specialist tools
      and return a result. Verify that a delegated specialist uses its own
      instructions, model, static user/MCP/skills tools; lacks a per-request
      sandbox and main-only workflow tools; and does not expand its own
      `subagents`. Verify the same agent in the direct role keeps its full
      capabilities.
- [ ] Unit: single-level structure — inspect the delegated specialist's tool
      list and confirm it has no `delegate_*` tools. Confirm A↔B cannot recurse
      without relying on a runtime guard.
- [ ] Unit: failure and cancellation — return specialist exceptions and local
      timeouts as sanitized recoverable `tool_end` errors without aborting the
      coordinator. Propagate parent/request cancellation and abort. Enforce
      `min(specialist, coordinator remaining)` timeout.
- [ ] Unit: concurrency — serialize concurrent calls to one specialist; run
      calls to different specialists in parallel; verify every result.
- [ ] Fixture: add
      `tests/fixtures/config_scenarios/<nn_delegation>/` with one coordinator
      and two specialists, including one endpoint-less internal specialist.
      Cover object form and app-wide duplicate-slug rejection at startup.
- [ ] Regression: existing collision-free no-subagent fixtures resolve and
      register identically under two-pass composition (triggers, endpoints, and
      tool assembly unchanged). A fixture with duplicate stems now fails fast at
      startup (previously auto-suffixed).
- [ ] Sample: add `samples/multi-agent-delegation/` with a coordinator and two
      specialists, one endpoint-less/internal. Show a self-contained `task`.

## 7. Docs impact

- [ ] `docs/front-matter-spec.md` — document object-form `subagents:`
      and the delegated role. Regenerate `docs/front-matter-reference.md` with
      `python eng/scripts/generate_config_reference.py`, then run the
      `update-schema-docs` skill to sync examples.
- [ ] `docs/architecture.md` — document two-pass composition, immutable
      `AgentCatalog`, and `direct`/`delegated` roles in the module map and
      pipeline. Add the coordination concept and distinguish it from Dynamic
      Workflows (FRD 0004).
- [ ] `docs/triggers.md` — explain that an otherwise triggerless/endpoint-less
      agent is valid only when another agent globally references it as an
      internal specialist.
- [ ] `README.md` — add a `subagents:` quickstart with the trust boundary and
      self-contained-`task` guidance.
- [ ] `docs/frds/README.md` — add FRD 0006 to the index.

## 8. Status & sign-off

- **Architecture review (phase 2): Complete (agent review).** Three
  `rubber-duck` passes are complete. **R1** (v1) drove two-pass composition,
  stable identity, tool-name collisions, failure semantics, delegated-role
  capability scope, and the trust boundary. **R2** (v2) drove catalog/capability
  ordering, the identity-collision model, corrected `Agent` construction timing,
  the failure/cancellation split, phased validation, and the breadth/concurrency
  decision (#14). **R3** (v3.1) confirmed the index construction, per-model
  client construction, and per-specialist concurrency serialization.
  A later cross-framework research pass covered MAF `as_tool()` internals plus
  OpenAI, Anthropic, LangGraph, AutoGen, CrewAI, and Google ADK. It confirmed
  that MAF imposes none of the original delegated-role restrictions and that a
  full-capability delegated specialist is the universal norm. §4 and Decisions
  log #13/#15 were reframed accordingly. A follow-up simplification then dropped
  the `id` and `tool_name` fields (#16) and unified slug-collision handling to
  fail-fast app-wide (#17), so identity is simply a globally-unique file-stem
  slug. Single-level delegation remains the one real rule and is enforced
  structurally.
  **No mechanical blockers remain; the final verdict is Go.** Both product
  choices previously open here are now decided by the reviewer (#5, #18).
- **Human sign-off: Pending.** No open questions remain. `status` stays `Draft`
  until the reviewer records final sign-off here; it then moves to `Finalized`.

### Resolved product questions

Both questions previously open here were decided on 2026-07-15:

- **Field name → `subagents`** (Decision #5) — chosen over `delegates_to` /
  `agents` / reusing `workflows`, to avoid colliding with FRD 0004's `workflows`
  field.
- **Who may declare `subagents` → any independently runnable agent**
  (Decision #18). Single-level delegation (#6) still holds: when such an agent is
  itself invoked as a sub-agent, its `subagents` are not wired and it cannot
  delegate onward. Simpler than a main-only rule and consistent with the
  cross-framework norm (#15).

### Deferred wording and fast-follow notes

- Finalize the exact `arg_description` wording during implementation so
  coordinators reliably produce a self-contained `task`.
- The handoff FRD, not v1, will choose between beta
  `agent-framework-orchestrations` (`1.0.0b260507`,
  `core>=1.3.0,<2`) and stable `1.0.0`, which requires `core>=1.9.0`.
