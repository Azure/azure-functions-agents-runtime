---
frd: 0009
title: Per-agent Dynamic Workflows
status: Finalized
author: TsuyoshiUshio
created: 2026-08-10
updated: 2026-08-10
issues:
  - "Azure/azure-functions-agents-runtime#109"
  - "Azure/azure-functions-bucees-planning#1274"
  - "Azure/azure-functions-bucees-planning#1275"
pull_requests:
  - "Azure/azure-functions-agents-runtime#151"
branch: tsuyoshiushio-per-agent-dynamic-workflows
---

# FRD 0009 — Per-agent Dynamic Workflows

## 1. Summary

Allow any eligible `*.agent.md` agent, rather than only `main.agent.md`, to own
Dynamic Workflows independently. One Function App will register one Durable
engine and complete workflow handler inventory, while every workflow-enabled
agent receives an immutable owner-specific policy, prompt guidance, management
tools, and workflow ownership namespace keyed by its canonical
`ResolvedAgent.slug`.

This change preserves the existing `workflows.enabled`, `workflows.exclude`, and
`workflows.subagents` authoring surface. It changes workflow identity and
management from session-only ownership to `(owner_slug, session_id)` ownership,
which cryptographically namespaces two agents receiving the same session ID so
they cannot see or control each other's workflows through application surfaces.

## 2. Motivation / problem

The runtime already supports Dynamic Workflow DAGs containing `tool`, `wait`,
and stateless leaf `sub_agent` tasks. Workflow-enabled agents can start those
plans from built-in chat, MCP, HTTP triggers, and non-interactive
Markdown-declared triggers. The runtime also already has:

- a canonical, app-wide unique `ResolvedAgent.slug`;
- an immutable `AgentCatalog`;
- owner-shaped `WorkflowPlanPolicy` values containing workflow tool and
  Workflow Sub Agent grants; and
- per-agent routes such as `/agents/{slug}/workflows`.

Despite those foundations, `app.py` still honors `workflows.enabled: true` only
when `resolved.is_main` is true. A non-main agent receives a warning and no
workflow integration.

Removing only that `is_main` check would be incorrect:

- `build_workflow_integration()` currently registers the Durable blueprint, so
  calling it for multiple agents would register the same Functions repeatedly;
- the workflow registry stores one process-global effective tool allowlist, so
  one agent's `workflows.exclude` could affect another agent;
- workflow IDs are namespaced only by `session_id`, so two agents using the same
  caller-provided session ID can pass each other's ownership-prefix checks; and
- Activities dispatch through shared handler and Agent catalogs without
  rechecking the workflow owner's policy.

The feature therefore requires an architectural separation between app-wide
execution inventory and per-owner authorization. It also needs a runnable sample
that proves the behavior and isolation rather than showing only a frontmatter
snippet.

## 3. Goals / Non-goals

**Goals**

- Honor `workflows.enabled: true` on every eligible discovered agent.
- Keep `main.agent.md` working as an ordinary owner with slug `main`.
- Use `ResolvedAgent.slug` as the stable workflow owner identity on chat, MCP,
  HTTP trigger, and non-interactive trigger paths.
- Create a `df.DFApp` when any eligible agent enables workflows.
- Register the Durable orchestrator and Activities exactly once per Function App.
- Register one complete, unfiltered workflow handler inventory so one owner's
  exclusions never unregister another owner's tools.
- Build one immutable `WorkflowPlanPolicy` per enabled owner.
- Use the same owner policy for prompt guidance, start-time plan validation, and
  defense-in-depth Activity authorization.
- Isolate workflow IDs, active-workflow limits, list, status, cancel, terminate,
  and HTTP polling by `(owner_slug, session_id)`.
- Preserve non-existence semantics for cross-owner access so a caller cannot
  probe whether another owner has a workflow.
- Preserve the asynchronous trigger starter contract: the initiating Function
  ends after the agent turn while Durable execution continues.
- Add a runnable, one-command-verifiable sample with multiple non-main workflow
  owners and no `main.agent.md`.

**Non-goals**

- New workflow frontmatter keys or a positive workflow-tool allowlist.
- Changes to the `tool`, `wait`, or `sub_agent` DAG schemas.
- Stateful or nested Workflow Sub Agents.
- Cross-app workflow invocation or ownership.
- Per-node retry, timeout, human approval, or compensation policy.
- An application-level index or reconnect API for workflows started by
  non-HTTP triggers with generated session IDs.
- Changing chat history, MAF session, runner lock, sandbox session, or general
  `x-ms-session-id` semantics outside Dynamic Workflows.
- Application-level management compatibility for legacy session-only workflow
  IDs.

## 4. Proposed design

### 4.1 Pipeline alignment

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| discover | `discovery/tools.py` | No behavior change. Continue returning one app-wide inventory of explicit `@workflow_tool` declarations. Discovery remains read-only and applies no owner policy. |
| translate | `config/schema.py`, `config/merge.py`, `config/validation.py`, `registration/capabilities.py` | Reuse `WorkflowConfig`, canonical `ResolvedAgent.slug`, validated Workflow Sub Agent references, and each agent's workflow tools after `workflows.exclude`. Validate that an enabled owner has a usable starter surface. No schema change is expected. |
| compose (pass 1) | `app.py`, `registration/catalog.py`, `workflows/integration.py` | After app-wide slug and reference validation, freeze the existing `AgentCatalog` and a new slug-keyed workflow owner-policy catalog. This pass remains side-effect-free and does not mutate a `FunctionApp`. |
| register (pass 2) | `app.py`, `workflows/integration.py`, `workflows/registry.py`, `workflows/engine.py`, `registration/endpoints.py`, `registration/triggers.py` | Create a `DFApp` when the policy catalog is non-empty. Register the complete handler inventory and Durable blueprint once, then thread each owner's policy and channel addendum into only that owner's surfaces. |
| execute | `runner.py`, `workflows/tools.py`, `workflows/context.py`, `workflows/engine.py`, `registration/_handlers.py` | Capture owner slug, session ID, Durable client, and explicit policy in workflow tool closures. Namespace management by owner plus session and reauthorize capability-bearing Activities before dispatch. |

This extends the existing two-pass composition model. Registration consumes
typed, validated, immutable objects and does not re-parse frontmatter.

### 4.2 Authoring and eligible starter surfaces

No new authoring syntax is introduced. Any descriptively named agent can opt in:

```yaml
---
name: Incident Triage Assistant
description: Investigates production incidents.
builtin_endpoints:
  debug_chat_ui: true
  chat_api: true
workflows:
  enabled: true
  exclude:
    - expensive_diagnostics
  subagents:
    - agent: log_analyst
      when: Analyze one bounded set of logs
---
```

An enabled owner must have at least one invocation channel that can run the
plan-authoring agent with a Durable client:

- built-in `chat_api`;
- built-in MCP; or
- any supported Markdown-declared trigger.

`debug_chat_ui` alone is not a starter because it is only a page surface. A
triggerless internal specialist referenced only through `subagents` or
`workflows.subagents` also has no starter surface.

The proposed behavior for `workflows.enabled: true` without a usable starter is
to fail composition with an actionable error. Silently ignoring the setting or
warning and disabling it would leave an apparently valid but inert owner. This
choice remains an architecture-review/sign-off item.

If one agent exposes multiple channels, every channel uses the same owner policy.
Chat and MCP receive chat-specific guidance; Markdown-declared triggers receive
trigger-specific guidance. Authorization does not vary by channel.

### 4.3 Stable owner identity and workflow IDs

`ResolvedAgent.slug` is the sole owner identity. It is already:

- derived during composition from the normalized source filename;
- guaranteed unique app-wide;
- the key of `AgentCatalog`;
- the built-in endpoint route identity; and
- the identity used by delegation and Workflow Sub Agent references.

Workflow code must not derive or allocate a second owner identity. Configured
display name remains metadata only.

Every invocation constructs an owner key from `(resolved.slug, session_id)`.
Instance IDs use SHA-256 over an unambiguous, length-delimited encoding of both
values, followed by the existing random UUID suffix:

```text
{32-hex-owner-and-session-hash-prefix}-{uuid}
```

The raw slug and session ID remain absent from Durable-visible instance IDs.
This feature increases the ownership prefix from 12 hex characters (48 bits) to
32 hex characters (128 bits). A 48-bit truncated digest is insufficient for a
multi-owner authorization boundary at scale; 128 bits makes accidental or
chosen collision impractical while keeping IDs comfortably within Durable
limits. Ownership is still digest-based rather than literal owner-key storage,
so the guarantee is bounded by the collision resistance of the truncated
SHA-256 digest.

All workflow management paths require both owner-key components:

- workflow management tool closures capture the owner slug and resolved session;
- polling endpoint closures capture their route's owner slug and read the
  request session;
- active count, list, status, cancel, and terminate helpers compare the
  owner-scoped prefix; and
- a mismatched owner or session returns the same not-found/empty result as an
  unknown workflow.

### 4.4 App-wide execution catalogs

The app owns two complete, read-only execution inventories:

1. the existing `AgentCatalog`, used by Workflow Sub Agent Activities; and
2. a workflow handler catalog containing every valid discovered
   `@workflow_tool` handler and its metadata.

These catalogs answer what exists, not what a particular owner may invoke.
Owner A excluding tool X must not unregister X when owner B allows it. An agent
being present in `AgentCatalog` similarly does not grant Workflow Sub Agent
access.

The Durable blueprint closes over the Agent catalog, workflow handler catalog,
and owner-policy catalog and is registered once. The singleton app allowlist
must no longer be an authorization source in production. Compatibility helpers
may remain temporarily for focused tests or external callers, but normal app
construction and execution always pass an explicit owner policy.

### 4.5 Immutable owner-policy catalog

Pass 1 constructs an immutable mapping:

```text
owner slug -> WorkflowPlanPolicy(
    allowed_tools=frozenset(...),
    allowed_subagents=frozenset(...),
    subagent_guidance=((slug, guidance), ...),
)
```

`allowed_tools` is the owner's set of public workflow tools after its existing
`workflows.exclude` filter. `allowed_subagents` and `subagent_guidance` come from
the owner's independent, deny-by-default `workflows.subagents` grants and the
immutable `AgentCatalog`.

The same policy value:

- generates the owner's chat and trigger prompt addenda;
- is captured by the owner's `start_workflow` closure;
- validates every authored `tool` and `sub_agent` node before Durable start; and
- is available to Activity dispatch for defense-in-depth authorization.

### 4.6 One-time Durable registration

After pass 1, `app.py` creates:

- a `df.DFApp` when at least one owner policy exists; or
- a plain `func.FunctionApp` otherwise.

Before individual agent registration, one app-level workflow registration step:

- registers every compatible handler from the unfiltered workflow-tool
  inventory; and
- registers one Durable blueprint containing the orchestrator, tool Activity,
  and Workflow Sub Agent Activity.

Individual agent registration then looks up `owner_policies[resolved.slug]`.
When present, it threads enabled state, explicit policy, owner slug, and the
appropriate addendum into `register_agent()` and
`register_builtin_endpoints()`. When absent, existing non-workflow handler
signatures and bindings remain unchanged.

`build_workflow_integration()` will be split or reshaped so a pure per-owner
integration builder cannot accidentally register app-wide Functions. The
one-time registration function is the only workflow layer that mutates the
`DFApp`.

### 4.7 Plan validation and Activity authorization

`start_workflow` validates the complete authored plan with the captured owner
policy before starting Durable. The Durable input includes `owner_slug` with the
existing owner/session audit metadata and normalized tasks.

Subject to explicit human ratification of Decision #8, each capability-bearing
Activity checks the currently deployed owner policy immediately before
shared-catalog dispatch:

- tool Activity requires `task.tool in policy.allowed_tools`;
- Workflow Sub Agent Activity requires
  `task.agent in policy.allowed_subagents`; and
- a missing owner policy, handler, or Agent catalog entry fails closed with a
  non-sensitive error and correlated owner/workflow/node telemetry.

The orchestrator passes `owner_slug` in each tool and Workflow Sub Agent Activity
payload. It performs no mutable policy lookup during replay. `wait` tasks have no
capability dispatch and retain their existing validated bounds.

Activity checks intentionally use policy from the currently deployed app. If a
deployment removes an owner, disables workflows, or tightens a grant, a pending
node using the removed capability fails closed. Persisting an old policy snapshot
as indefinitely authoritative would make policy revocation ineffective.

This reauthorization and fail-closed revocation behavior is provisional while
the FRD is `Draft`; implementation must not begin until Decision #8 is ratified.

Direct Durable orchestration starts remain privileged control-plane operations.
The application-level owner boundary protects starts and management through
agent surfaces; it is not an authentication boundary against an actor already
authorized to start arbitrary Durable instances.

### 4.8 Trigger ownership

HTTP triggers use the caller-provided `x-ms-session-id` or the existing generated
session behavior. Non-HTTP triggers generate a fresh invocation session ID. In
both cases, the workflow owner is `(resolved.slug, invocation_session_id)`.

The initial trigger Function remains short-lived and never polls for terminal
workflow state. Non-HTTP trigger workflows do not gain a new application-level
owner index or reconnect API. Applications should deliver final output through a
workflow task, while operators use Durable Functions or DTS tooling.

### 4.9 Compatibility and migration

This feature contains one intentional breaking change within the experimental
Dynamic Workflows surface.

Existing workflow IDs use a session-only hash prefix. New IDs use an
owner-plus-session prefix. No application-level legacy fallback is proposed:

- pre-upgrade instances continue running in Durable;
- new agent tools and polling endpoints cannot list, inspect, cancel, or
  terminate those legacy IDs;
- operators can still inspect or control them through Durable/DTS; and
- deployments requiring continued agent-level management should drain or
  terminate active workflows before upgrading.

The rest of the public surface remains compatible:

- `main.agent.md` remains a valid workflow owner with slug `main`;
- `workflows.enabled`, `workflows.exclude`, and `workflows.subagents` do not
  change;
- task schemas and workflow management tool names do not change;
- Durable orchestrator and Activity names do not change;
- built-in route shapes remain `/agents/{slug}/...`; and
- non-workflow session behavior does not change.

### 4.10 Runnable sample

Add `samples/per-agent-workflows/` as a standalone Azure Functions app with no
`main.agent.md`. It contains two descriptively named agents, for example:

- `incident_triage.agent.md`, with chat endpoints and one set of workflow tool
  and Sub Agent grants; and
- `release_readiness.agent.md`, with chat endpoints and a different set of
  grants.

The tools use deterministic synthetic data so verification requires no external
service token. The sample includes:

- clear architecture and workflow-shape diagrams;
- one manual prompt for each agent;
- expected workflow outputs and polling routes;
- Azure Storage and DTS local instructions; and
- `scripts/verify.py`, which defaults to the Azure Storage backend with isolated
  Azurite, supports `--backend dts` for a DTS run, starts the Functions host from
  a temporary app copy, and performs end-to-end assertions.

The verifier deliberately uses the same `x-ms-session-id` for both agents. It
starts one workflow through each agent, verifies both reach a terminal state,
checks that each used only its own capabilities, and verifies that each owner's
status route returns 404 for the other owner's workflow ID. This makes the main
behavioral and security property directly observable for the exercised owner
pair. The README states the prerequisites explicitly: Docker (for isolated
Azurite and optional DTS), Functions Core Tools, and model-provider
authentication.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | FRD number | 0008 from current `main` / include open and draft PR reservations | Use 0009 because open PRs #111 and #121 both reserve 0008 | Agent | 2026-08-10 |
| 2 | Workflow owner identity | Display name / source path / endpoint-specific name / canonical slug | Use app-wide unique `ResolvedAgent.slug` on every channel | Agent | 2026-08-10 |
| 3 | Workflow ownership scope | Session only / owner only / `(owner_slug, session_id)` | Use `(owner_slug, session_id)` so equal session IDs across agents remain isolated | Human | 2026-08-10 |
| 4 | Existing workflow IDs | Dual-format fallback / migration map / no application fallback | Accept the experimental breaking change, preserve Durable/DTS operator access, and document drain guidance | Human | 2026-08-10 |
| 5 | Durable registration lifetime | Once per owner / once per app | Register the Durable engine exactly once per app | Agent | 2026-08-10 |
| 6 | Workflow handler inventory | First owner's filtered tools / union of owner tools / complete discovered catalog | Register the complete compatible handler catalog once and authorize separately per owner | Agent | 2026-08-10 |
| 7 | Owner policy representation | Mutable process global / request-time reconstruction / immutable slug-keyed catalog | Build immutable `WorkflowPlanPolicy` values during side-effect-free composition | Agent | 2026-08-10 |
| 8 | Activity authorization | Trust start-time validation / persist start-time policy / reauthorize deployed policy | Reauthorize tool and Sub Agent Activities against current deployed owner policy; pending nodes fail closed after restrictive changes | Human | 2026-08-10 |
| 9 | Enabled owner without starter | Warn and disable / silently ignore / fail composition | Fail composition because inert workflow configuration is misleading | Human | 2026-08-10 |
| 10 | Non-HTTP trigger management | Add owner index / shared synthetic session / generated non-discoverable invocation session | Use generated sessions with no new application index; use Durable/DTS for operator management | Human | 2026-08-10 |
| 11 | Authoring schema | Add owner/config fields / reuse current workflow config | Reuse existing fields; owner identity is runtime-derived | Agent | 2026-08-10 |
| 12 | Sample proof | Extend a main-agent sample / documentation only / dedicated multi-owner sample | Add a runnable sample with two non-main owners and same-session isolation verification | Human | 2026-08-10 |
| 13 | Ownership digest width | Retain 48-bit prefix / store literal owner data / expand digest | Use a 128-bit truncated SHA-256 prefix over a length-delimited owner/session encoding; avoids exposing raw identity while making collisions impractical | Human | 2026-08-10 |

## 6. Test plan

- [x] Unit: composition and owner-policy catalog
  - any eligible non-main agent can enable workflows;
  - an app with only non-main workflow owners is a `df.DFApp`;
  - `main.agent.md` remains supported;
  - `debug_chat_ui`-only and endpoint-less enabled owners follow the finalized
    eligibility decision;
  - distinct owners receive independent tool excludes, Sub Agent grants, and
    prompt guidance;
  - owner-policy mappings and values are immutable.
- [x] Unit: one-time runtime registration
  - multiple enabled owners register one orchestrator and one copy of each
    Activity;
  - complete workflow handler and Agent catalogs remain available;
  - excluding a handler for one owner does not unregister it for another;
  - production execution does not authorize from the singleton app allowlist.
- [x] Unit: owner-scoped context and management
  - the same session ID under two owner slugs generates different prefixes;
  - active limits, list, status, cancel, and terminate require both owner and
    session;
  - cross-owner operations return empty/not-found without disclosing existence;
  - legacy session-only IDs do not match an owner-scoped prefix, are treated as
    not-found, and their Durable instances are not deleted or mutated.
- [x] Unit: plan and Activity authorization
  - prompt guidance and start-time validation use the same owner policy;
  - tool and Workflow Sub Agent Activities reject capabilities belonging only to
    another owner;
  - missing or disabled owner policy fails closed;
  - restrictive policy changes reject a pending disallowed node;
  - every capability-bearing Activity payload contains `owner_slug`;
  - `wait` tasks retain existing behavior.
- [x] Integration: invocation channels
  - multiple workflow-enabled agents register distinct chat, streaming, MCP,
    HTTP trigger, and non-HTTP trigger surfaces as configured;
  - each enabled surface receives the Durable client binding and correct
    channel addendum;
  - HTTP workflow polling routes cannot observe another owner under the same
    session ID;
  - trigger starters return/end without waiting for terminal workflow state.
- [x] Workflow Sub Agent isolation
  - each owner can schedule only its own `workflows.subagents` grants;
  - one specialist may be granted to multiple owners without duplicate Activity
    registration;
  - workflow leaf specialists retain their current isolated execution role.
- [x] Fixture scenario:
  `tests/fixtures/config_scenarios/18_multi_owner_workflows/`.
- [ ] E2E: Azure Storage and DTS runs demonstrate concurrent owners, overlapping
  session IDs, distinct policies, status/control isolation, and execution after
  starter completion.
- [x] Sample verifier: one command starts dependencies and proves both successful
  workflows plus cross-owner denial. The script and its pure verification tests
  are implemented; model-backed Storage/DTS execution remains covered by the
  unchecked E2E item above.
- [x] Canonical gate:
  - `python -m ruff check src tests`;
  - `python -m mypy src`;
  - `python -m pytest --cache-clear --cov=./src/azure_functions_agents
    --cov-report=xml --cov-branch tests`.

## 7. Docs impact

- [x] `docs/architecture.md` — add the owner-policy catalog, one-time Durable
  registration, owner-scoped execution, and Activity reauthorization.
- [x] `docs/front-matter-spec.md` — remove the `main.agent.md` restriction and
  document eligible starter surfaces.
- [x] `docs/workflows.md` — document multiple owners, identity, isolation,
  migration, trigger ownership, and operator guidance.
- [x] `docs/triggers.md` — clarify that each workflow-enabled declared trigger
  uses its owning agent's policy and Durable client.
- [x] `README.md` — link the per-agent workflow sample.
- [x] `samples/README.md` — list the runnable sample and its one-command verifier.
- [x] `docs/front-matter-reference.md` — no change expected because no schema
  change is planned.

## 8. Status & sign-off

- **Architecture review (phase 2):** Completed by an independent rubber-duck
  reviewer on 2026-08-10 against current `main`, `docs/architecture.md`, FRD
  0004, FRD 0007, issues #1274/#1275, and the existing trigger and Workflow Sub
  Agent implementations. No blocking findings remained. Important findings on
  ownership digest strength, provisional decisions, legacy-ID wording, and
  verifier prerequisites were incorporated.
- **Human sign-off:** Completed by TsuyoshiUshio on 2026-08-10. The human
  approved proceeding with implementation in the same PR, ratifying Decisions
  #8-#10 and #13. Status set to `Finalized`.
