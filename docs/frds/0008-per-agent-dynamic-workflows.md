---
frd: 0008
title: Per-agent Dynamic Workflows
status: Finalized
author: TsuyoshiUshio
created: 2026-07-17
updated: 2026-08-05
issues:
  - "Azure/azure-functions-agents-runtime#109"
  - "Azure/azure-functions-bucees-planning#1274"
pull_requests:
  - "Azure/azure-functions-agents-runtime#111"
  - "Azure/azure-functions-agents-runtime#112"
  - "Azure/azure-functions-agents-runtime#117"
  - "Azure/azure-functions-agents-runtime#119"
branch: tsuyoshiushio-dynamic-workflow-support
---

# FRD 0008 — Per-agent Dynamic Workflows

## 1. Summary

Allow every eligible Markdown agent, not only `main.agent.md`, to own Dynamic
Workflows independently across built-in chat, MCP, HTTP trigger, and
non-interactive Markdown-declared trigger invocations. The app registers one
Durable engine, one complete workflow-tool handler catalog, and one immutable
`AgentCatalog`; each enabled owner receives a separate immutable
`WorkflowPlanPolicy`, prompt addenda, workflow-management tools, and workflow
ownership namespace keyed by its canonical `ResolvedAgent.slug`.

This FRD is the architecture deliverable for planning issue
`Azure/azure-functions-bucees-planning#1274`. Product implementation is tracked
by `#1275` under roadmap parent `#1123`.

## 2. Motivation / problem

The current runtime already supports:

- `tool`, `wait`, and stateless leaf `sub_agent` DAG tasks;
- workflow initiation from built-in chat/MCP and every supported
  Markdown-declared trigger;
- an immutable owner-specific `WorkflowPlanPolicy` containing
  `allowed_tools`, `allowed_subagents`, and `subagent_guidance`;
- two-pass composition and an immutable app-wide `AgentCatalog`; and
- a canonical, app-wide unique `ResolvedAgent.slug` used for function names,
  built-in routes, delegation, and Sub Agent references.

However, `app.py` still selects only `main.agent.md` as the workflow owner.
`build_workflow_integration()` combines three different lifetimes in one call:
it registers the Durable blueprint, registers workflow handlers, and constructs
the one owner's policy/addenda. The workflow registry also retains one
process-global effective tool allowlist. Simply removing the `is_main` check
would therefore:

- register the same Durable functions more than once;
- let one owner's `workflows.exclude` affect another owner's handler
  availability or fallback authorization;
- collapse distinct `workflows.subagents` grants into one policy; and
- leave workflow IDs scoped only by `session_id`, allowing two agents that
  receive the same session ID to inspect or control each other's workflows.

The architecture must separate app-wide execution catalogs from per-owner
authorization and must work for both interactive sessions and short-lived
trigger starters.

## 3. Goals / Non-goals

**Goals**

- Honor `workflows.enabled: true` for any agent with at least one supported
  invocation channel: `chat_api`, MCP, or a Markdown-declared trigger.
- Use canonical `ResolvedAgent.slug` as the stable owner identity on every
  invocation channel.
- Create a `DFApp` when at least one eligible agent enables workflows.
- Register the Durable orchestrator and Activities exactly once per app.
- Register the complete, unfiltered workflow-tool handler inventory once so
  one owner's excludes never remove another owner's handler.
- Build one immutable `WorkflowPlanPolicy` per enabled owner from that owner's
  effective workflow tools and deny-by-default `workflows.subagents` grants.
- Generate chat and trigger prompt addenda from the same owner policy used for
  runtime plan validation.
- Isolate workflow IDs, active-workflow limits, list/status/cancel/terminate,
  HTTP polling, and non-existence semantics by `(owner_slug, session_id)`.
- Revalidate owner authorization at tool and Sub Agent Activity dispatch as a
  defense-in-depth boundary around shared app-wide catalogs.
- Preserve the existing asynchronous trigger-starter contract: the initiating
  Function ends after the agent turn while Durable execution continues.
- Keep malformed workflow configuration and cross-agent references fail-fast
  during composition.

**Non-goals**

- New frontmatter keys or positive workflow-tool allowlists. Broader positive
  capability allowlists, governance controls, and Blob offload are deferred to
  planning issue `#1279`.
- Changing the existing `tool`, `wait`, or leaf `sub_agent` task schemas.
- Stateful/nested Sub Agents, cross-app invocation, retries, or human approval.
- Changing chat history, MAF `AgentSession`, runner locking, sandbox session,
  or `x-ms-session-id` contracts outside Dynamic Workflow ownership.
- Making non-HTTP trigger-created session IDs externally discoverable through a
  new runtime index or callback surface.
- Supporting legacy session-only workflow IDs through the new per-owner
  management APIs.

## 4. Proposed design

### 4.1 Pipeline alignment

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| discover | `discovery/tools.py` | No behavior change. Continue discovering one app-wide, unfiltered inventory of explicit `@workflow_tool` declarations. Discovery remains read-only and applies no owner policy. |
| translate | `config/schema.py`, `config/merge.py`, `config/validation.py`, `registration/capabilities.py` | Reuse typed `WorkflowConfig`, canonical `ResolvedAgent.slug`, validated `workflows.subagents`, and per-agent concrete workflow tools after `workflows.exclude`. Validate that an enabled owner has a trigger, chat API, or MCP starter surface. |
| compose (pass 1) | `app.py`, `registration/catalog.py`, `workflows/integration.py` | After duplicate-slug and cross-agent validation, freeze the existing `AgentCatalog`. Build and freeze a slug-keyed owner-policy catalog from each enabled owner's concrete workflow tools and Sub Agent grants. No `FunctionApp` mutation occurs in pass 1. |
| register (pass 2) | `app.py`, `workflows/integration.py`, `workflows/registry.py`, `workflows/engine.py`, `registration/endpoints.py`, `registration/triggers.py` | If the owner-policy catalog is non-empty, create one `DFApp`, register the complete handler catalog and Durable blueprint once, and thread each owner's policy/addenda into its built-in endpoints and/or declared trigger. |
| execute | `runner.py`, `workflows/tools.py`, `workflows/context.py`, `workflows/engine.py` | Capture owner slug, invocation session ID, and immutable policy in management-tool closures. Validate plans before start, namespace IDs by owner plus session, and revalidate each capability-bearing Activity against the owner-policy catalog before shared-catalog dispatch. |

This extends FRD 0007's two-pass composition rather than adding a parallel
registration model. Registration does not re-parse frontmatter or resolve
cross-agent references.

### 4.2 Eligibility and invocation channels

An enabled workflow owner must have at least one path that can run the
plan-authoring agent with a Durable client:

- built-in `chat_api`;
- built-in MCP; or
- any supported Markdown-declared trigger, including HTTP, timer, queue, Blob,
  Event Grid, Service Bus, Event Hubs, and connector triggers.

`debug_chat_ui` alone is not an invocation channel. An endpoint-less leaf
specialist referenced only by `subagents:` or `workflows.subagents` also cannot
be a workflow owner. Setting `workflows.enabled: true` without a supported
starter surface fails composition with an actionable configuration error
instead of creating inert policy.

Eligibility checks `resolved.trigger`, `builtin_endpoints.chat_api`, and
`builtin_endpoints.mcp` directly. It must not reuse
`_builtin_endpoints_enabled()`, which also counts `debug_chat_ui`.

If an agent has both triggers and built-in endpoints, every registered
invocation channel receives the same owner policy. Chat/MCP use the chat
addendum; Markdown-declared triggers use the trigger-specific addendum from
FRD 0004. No channel receives a different authorization set.

Trigger behavior remains asynchronous:

- HTTP triggers use the caller-provided `x-ms-session-id` or return the
  generated ID through the existing response contract.
- Non-HTTP triggers generate a fresh invocation session ID. Their initiating
  agent may start a workflow during that turn, but the generated ID is not
  exposed through a new user-facing index. Applications deliver final results
  through workflow tasks, while operators use Durable/DTS surfaces.
- The short-lived trigger Function never waits for terminal workflow status.

This explicitly replaces the original session-backed-only scope in Decision
#1; trigger initiation merged in PR #112 and is now part of the supported
architecture.

### 4.3 Stable owner identity

`ResolvedAgent.slug` is the sole workflow owner identity. It is derived during
composition from the normalized source filename and is already:

- guaranteed unique app-wide by `_fail_on_duplicate_slugs()`;
- the key of `AgentCatalog`;
- the Azure Function name for declared triggers;
- the `/agents/{slug}/` built-in route identity; and
- the identity used by delegation and Workflow Sub Agent references.

No workflow code re-derives, allocates, or suffixes a second identity. A missing
slug after composition is an invariant violation and fails startup. The
configured display name remains telemetry/audit metadata only.

Each invocation constructs a workflow owner key from
`(resolved.slug, session_id)`. Instance IDs use a deterministic hash of an
unambiguous serialized pair plus the existing random UUID suffix. The raw slug
and session ID are not embedded in the visible ID.

The implementation retains the existing 12-hex ownership-prefix width, so the
hash collision domain is unchanged from the experimental session-only scheme;
this change adds owner separation but does not claim stronger collision
resistance.

All workflow management paths require both owner-key components:

- workflow tool closures capture `resolved.slug` and the runner's resolved
  session ID;
- chat workflow polling endpoints capture `resolved.slug` and read the request
  session ID;
- count/list/status/cancel/terminate helpers compare the owner-scoped prefix;
  and
- mismatches retain 404/not-found semantics to prevent cross-owner probing.

The owner key applies only to Dynamic Workflows. Existing chat history, MAF
sessions, locks, sandbox sessions, and request semantics remain unchanged.

### 4.4 App-wide runtime catalogs

The runtime has two app-wide read-only catalogs:

1. **`AgentCatalog`** — the existing slug-keyed catalog from FRD 0007, used by
   Workflow Sub Agent Activities.
2. **Workflow handler catalog** — every valid discovered `@workflow_tool`
   handler and its metadata, registered from the unfiltered discovery result.

These catalogs describe what exists, not what an owner may invoke. Owner A
excluding tool X must not unregister X because owner B may allow it. Likewise,
an agent's presence in `AgentCatalog` does not authorize it as a Workflow Sub
Agent.

The Durable blueprint closes over the immutable `AgentCatalog`, handler
catalog, and owner-policy catalog and is registered once. The process-global
effective `_APP_ALLOWLIST` compatibility fallback is removed from production
paths; workflow-management tools always receive an explicit owner policy.

### 4.5 Immutable per-owner policy catalog

Pass 1 builds a read-only mapping:

```text
owner slug -> WorkflowPlanPolicy(
    allowed_tools=frozenset(...),
    allowed_subagents=frozenset(...),
    subagent_guidance=((slug, guidance), ...),
)
```

`allowed_tools` is the effective set of public workflow tools after applying
the owner's existing `workflows.exclude` deny list.
`allowed_subagents` and `subagent_guidance` come from that owner's independent,
deny-by-default `workflows.subagents` declarations and the immutable
`AgentCatalog`.

The mapping and every `WorkflowPlanPolicy` are immutable after pass 1. The same
policy instance:

- generates the owner's chat and trigger addenda;
- is captured by the owner's `start_workflow` closure;
- validates every authored `tool` and `sub_agent` node before Durable start;
  and
- is available to Activity dispatch for defense-in-depth checks.

Positive allowlists for workflow tools are not introduced here. Issue `#1279`
will design positive capability allowlists and governance together. This FRD
preserves the existing effective deny/exclude policy for workflow tools and the
existing positive, deny-by-default grant for Workflow Sub Agents.

### 4.6 App creation and one-time registration

After pass 1, `app.py` creates:

- `df.DFApp` when the owner-policy catalog has at least one entry; or
- plain `func.FunctionApp` otherwise.

Before registering individual agents, the workflow integration registers:

- every compatible handler from the complete unfiltered workflow-tool
  inventory; and
- one Durable blueprint containing the orchestrator, tool Activity, and Sub
  Agent Activity.

Per-agent registration then looks up `owner_policies[resolved.slug]`. When
present, it threads enabled state, the policy, and channel-specific addendum
through `register_agent()` and `register_builtin_endpoints()`. When absent, the
existing non-workflow handler signatures remain unchanged.

`build_workflow_integration()` should be split or reshaped so app-wide runtime
registration cannot be accidentally called once per owner. One function builds
pure per-owner integration values during pass 1; a separate one-time function
mutates the `DFApp` during pass 2.

### 4.7 Plan validation and Activity dispatch authorization

`start_workflow` validates the complete LLM-authored plan with the captured
immutable owner policy before calling Durable. The Durable input includes the
owner slug alongside the owner/session audit metadata and normalized tasks.

Because tool and Sub Agent Activities dispatch through shared app-wide
catalogs, each capability-bearing Activity performs a defense-in-depth owner
check immediately before dispatch:

- tool Activity requires `task.tool in owner_policy.allowed_tools`;
- Sub Agent Activity requires
  `task.agent in owner_policy.allowed_subagents`; and
- missing owner policy, handler, or catalog entry fails closed with a
  non-sensitive error and correlated owner/workflow/node telemetry.

The orchestrator passes `owner_slug` into each Activity payload. It does not
read mutable process state or re-run Pydantic validation during replay.
`wait` nodes have no capability dispatch and rely on the already validated,
bounded plan.

Activity checks use the policy catalog from the currently deployed app. A
deployment that removes an owner, disables its workflows, or tightens a grant
intentionally prevents pending Activities from using the removed capability;
an in-flight workflow may fail at its next affected node. This fail-closed
revocation behavior is preferred over persisting an indefinitely authoritative
policy snapshot inside forgeable Durable input.

A privileged direct orchestration start is bounded by the deployed policy of
whichever `owner_slug` it claims: it cannot invoke a capability absent from
that selected deployed owner policy, but it can select among deployed owners
by forging the field. Direct starts therefore remain a privileged Durable
control-plane operation and are not protected by the application-level
per-owner authentication boundary.

### 4.8 Ownership migration and compatibility

Existing workflow IDs use a session-only hash prefix. New IDs use an
owner-plus-session prefix. As previously accepted for this experimental
feature, no legacy fallback is added:

- pre-upgrade workflows continue running in Durable;
- new per-owner tools and HTTP polling endpoints cannot list, inspect, cancel,
  or terminate those legacy IDs;
- operators can still inspect/control them through Durable/DTS; and
- deployment guidance must recommend draining or terminating active workflows
  before upgrading when continued agent-level management is required.

Other compatibility guarantees:

- `main.agent.md` remains an ordinary canonical owner with slug `main`;
- frontmatter keys (`enabled`, `exclude`, `subagents`) do not change;
- task schemas and management tool names do not change;
- Durable orchestrator and Activity names do not change;
- built-in route shapes do not change; and
- non-workflow session behavior does not change.

## 5. Decisions log

Append-only. Decisions #1-#11 record the original July design. Later rows
explicitly supersede assumptions invalidated by merged PRs rather than
rewriting history.

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Eligible non-main agents | All triggers / session-backed built-in endpoints | Limit #109 to agents with built-in chat API or MCP endpoints; defer trigger-only workflows | Human | 2026-07-17 |
| 2 | Durable runtime registration | Once per enabled agent / once per app | Register orchestrator, Activity, and handler inventory once per app | Agent | 2026-07-17 |
| 3 | Workflow allowlist storage | Last-writer app global / map keyed by agent / capture in request tool closures | Capture each immutable allowlist in the agent's workflow management-tool closures | Agent | 2026-07-17 |
| 4 | Agent ownership identity | Configured name / source path / allocated endpoint slug | Use the unique allocated built-in endpoint slug; retain name only as display metadata | Agent | 2026-07-17 |
| 5 | Workflow ownership scope | Session only / agent only / `(agent, session)` | Scope workflow IDs and management operations to `(agent identity, session_id)` | Human | 2026-07-17 |
| 6 | Existing workflow IDs | Legacy fallback for main / migration map / no fallback | Accept that pre-upgrade workflow IDs become inaccessible because the feature is experimental v1 | Human | 2026-07-17 |
| 7 | Non-workflow sessions | Namespace all session state by agent / change only workflows | Keep chat history, MAF sessions, locks, sandbox sessions, and request semantics unchanged | Human | 2026-07-17 |
| 8 | Ineligible workflow request | Fail startup / silently ignore / warn and continue | Warn clearly, disable workflows for that agent, and preserve normal trigger registration | Agent | 2026-07-17 |
| 9 | Authoritative agent identity | App-level allocated function name / independently derived name / final built-in endpoint slug | Resolve once inside built-in endpoint registration after collision handling and thread that exact slug to runner and polling closures | Agent | 2026-07-17 |
| 10 | App-wide handler registration input | First enabled agent's filtered tools / union of filtered tools / full discovered inventory | Register the full unfiltered discovered workflow-tool inventory once; authorize from per-agent filtered subsets | Agent | 2026-07-17 |
| 11 | MCP-only status access | Add HTTP polling routes / management tools only | Keep HTTP polling routes tied to chat API; MCP-only agents use workflow management tools | Agent | 2026-07-17 |
| 12 | FRD number after rebase | Keep duplicate 0006 / renumber to next available | Renumber to FRD 0008 after main added FRDs 0006 and 0007 | Agent | 2026-08-05 |
| 13 | Invocation scope after PR #112 | Preserve session-backed-only Decision #1 / support all existing workflow starter channels | Supersede Decision #1: enable each owner's built-in chat, MCP, HTTP trigger, and non-interactive Markdown-declared trigger paths | Agent | 2026-08-05 |
| 14 | Canonical owner identity after FRD 0007 | Preserve allocated endpoint identity / `ResolvedAgent.slug` | Supersede Decisions #4 and #9: use globally unique canonical `ResolvedAgent.slug` on every channel | Agent | 2026-08-05 |
| 15 | Owner policy representation | Per-request ad hoc values / mutable map / immutable slug-keyed catalog | Freeze one `WorkflowPlanPolicy` per enabled owner after `AgentCatalog` construction in pass 1 | Agent | 2026-08-05 |
| 16 | Workflow policy contents after PR #117 | Tool names only / tools plus Sub Agent grants and guidance | Preserve immutable `allowed_tools`, `allowed_subagents`, and `subagent_guidance` for every owner | Agent | 2026-08-05 |
| 17 | Shared runtime registration | Register from each filtered policy / union filtered sets / full catalogs once | Register one Durable engine and full unfiltered handler plus Agent catalogs once; authorize per owner | Agent | 2026-08-05 |
| 18 | Activity authorization | Trust start-time plan validation / revalidate current owner policy at dispatch | Revalidate tool and Sub Agent authorization at Activity dispatch and fail closed on policy removal/tightening | Agent | 2026-08-05 |
| 19 | Policy changes during in-flight workflows | Persist old policy snapshot / current deployment policy / cancel all on deploy | Use current deployed owner policy; restrictive changes may fail pending nodes and act as revocation | Agent | 2026-08-05 |
| 20 | Positive workflow-tool allowlists | Add in this feature / replace excludes / defer with current effective policy | Defer to planning issue #1279; preserve `workflows.exclude` and deny-by-default `workflows.subagents` | Agent | 2026-08-05 |
| 21 | Legacy workflow IDs | Main-only fallback / dual-format lookup / no fallback | Reaffirm Decision #6: no application-level fallback; document drain guidance and Durable/DTS operator access | Human (prior decision) + Agent | 2026-08-05 |
| 22 | Trigger-created ownership | Owner-only namespace / `(owner, generated invocation session)` / shared synthetic session | Use `(ResolvedAgent.slug, invocation session_id)`; non-HTTP triggers do not gain a new management index | Agent | 2026-08-05 |
| 23 | Ineligible enabled owner after trigger support | Preserve Decision #8 warn-and-continue / fail composition | **Proposed, pending human ratification:** fail composition with an actionable error; supersedes Decision #8 because every valid owner must have a usable starter surface | Agent | 2026-08-05 |
| 24 | Ratify enabled-owner eligibility | Warn and disable per Decision #8 / fail composition per proposed Decision #23 | Fail composition when an enabled agent has no Markdown-declared trigger, chat API, or MCP starter; supersedes Decision #8 and ratifies Decision #23 | Human | 2026-08-05 |
| 25 | Ratify Activity reauthorization and revocation | Trust start-time validation / current deployed policy / persisted policy snapshot | Reauthorize every tool and Sub Agent Activity against the currently deployed owner policy; owner removal or policy tightening may fail pending nodes as fail-closed revocation, ratifying Decisions #18 and #19 | Human | 2026-08-05 |
| 26 | Ratify non-HTTP trigger ownership management | Add owner workflow index/reconnect API / generated non-discoverable invocation sessions | Keep generated non-discoverable sessions, add no application-level owner workflow index or reconnect API, and leave operator management to Durable/DTS, ratifying Decision #22 | Human | 2026-08-05 |

## 6. Test plan

- [ ] Unit: composition and policy catalog
  - any enabled agent with chat API, MCP, HTTP trigger, or non-HTTP trigger
    receives one owner policy;
  - `debug_chat_ui`-only and endpoint-less specialists with workflows enabled
    fail validation;
  - duplicate slugs still fail before policy construction;
  - distinct owners receive independent tool excludes, Sub Agent grants, and
    guidance;
  - policy/catalog mappings are immutable.
- [ ] Unit: one-time runtime registration
  - multiple enabled owners register one orchestrator and one copy of each
    Activity;
  - the full unfiltered workflow handler inventory is registered;
  - owner A excluding tool X does not remove X for owner B;
  - no production path reads a process-global effective allowlist.
- [ ] Unit: plan and Activity authorization
  - prompt guidance and `start_workflow` validation use the same owner policy;
  - tool and Sub Agent Activities reject another owner's capability;
  - missing/disabled owner policy fails closed;
  - a restrictive redeployment policy rejects a pending disallowed node;
  - the orchestrator includes `owner_slug` in every tool and Sub Agent Activity
    payload, while replay does not read mutable policy state;
  - wait tasks retain existing bounded behavior.
- [ ] Unit: owner-scoped context and management
  - the same session ID under two slugs generates different workflow prefixes;
  - list/status/cancel/terminate and active limits match both slug and session;
  - cross-owner attempts return empty/404 without existence disclosure;
  - raw non-workflow session IDs remain unchanged.
- [ ] Integration: invocation channels
  - two workflow-enabled agents register distinct chat/MCP/HTTP routes and
    Durable client bindings with independent policies/addenda;
  - multiple non-interactive trigger agents index with Durable client bindings;
  - chat and trigger addenda differ only by channel guidance, not authorization;
  - trigger Functions end after the authoring turn while orchestration continues;
  - HTTP polling routes cannot observe another slug's workflows under the same
    `x-ms-session-id`.
- [ ] Workflow Sub Agent isolation
  - each owner can schedule only its own `workflows.subagents` grants;
  - the same specialist may be granted to multiple owners without duplicate
    Activity registration;
  - leaf specialists still receive no workflow-management or delegation tools.
- [ ] Migration
  - new owner-scoped helpers reject legacy session-only IDs;
  - legacy Durable instances are not deleted or mutated.
- [ ] Fixture scenario:
  `tests/fixtures/config_scenarios/<next>_multi_owner_workflows/`
  - chat/MCP, HTTP-trigger, and non-HTTP-trigger owners with distinct excludes
    and Sub Agent grants.
- [ ] E2E for implementation issue #1275
  - Storage and DTS runs demonstrate concurrent owners, overlapping session IDs,
    isolated tool/Sub Agent policy, status/control isolation, and continued
    execution after trigger completion.
- [ ] Canonical gate:
  - `python -m ruff check src tests`;
  - `python -m mypy src`;
  - `python -m pytest --cache-clear --cov=./src/azure_functions_agents
    --cov-report=xml --cov-branch tests`.

## 7. Docs impact

Implementation issue `#1275` updates:

- [ ] `docs/architecture.md` — owner-policy catalog, one-time runtime
  registration, owner-scoped execution, and Activity revalidation.
- [ ] `docs/front-matter-spec.md` — remove the main-only restriction and
  document all eligible starter channels.
- [ ] `docs/workflows.md` — multi-owner policy, trigger ownership, migration,
  and operator guidance.
- [ ] `docs/triggers.md` — every workflow-enabled declared trigger uses its own
  owner policy and Durable client.
- [ ] `README.md` and workflow samples — show non-main and multi-owner usage.
- [ ] `docs/front-matter-reference.md` — no schema change expected.

This architecture-only PR updates the FRD and FRD index. Product docs remain
unchanged until implementation matches the finalized design.

## 8. Status & sign-off

- **Original architecture review:** Completed by `frd-reviewer` on 2026-07-17
  for the pre-PR #112/#117 design. That review had no remaining blocking or
  important findings, but its session-backed-only and allocated-endpoint-slug
  assumptions were superseded by merged architecture.
- **Current architecture review:** Completed against current `main`, FRD 0004,
  FRD 0007, planning issue #1274, trigger semantics,
  authorization/isolation, compatibility, and this test plan. Initial review by
  `current-frd-reviewer` (rubber-duck), 2026-08-05, found one blocking
  Decisions-log contradiction plus important gaps around explicit human
  ratification, privileged direct-start wording, and baseline PR traceability.
  The revision adds append-only Decision #23, explicit sign-off questions,
  accurate direct-start boundaries, baseline PR links, a direct eligibility
  predicate, ownership-prefix scope, and Activity payload coverage. Re-review
  by `current-frd-reviewer` on 2026-08-05 found no remaining blocking or
  important findings and declared the architecture-review gate complete. The
  FRD was ready for human ratification.
- **Human sign-off:** Completed, 2026-08-05. The human ratified all three
  previously open questions: fail composition for enabled owners without a
  starter surface (Decision #24), Activity reauthorization against current
  deployed policy with fail-closed in-flight revocation (Decision #25), and
  generated non-discoverable non-HTTP trigger sessions without an
  application-level owner index or reconnect API (Decision #26). Status set to
  `Finalized`. Product implementation remains tracked separately by
  `Azure/azure-functions-bucees-planning#1275`.
