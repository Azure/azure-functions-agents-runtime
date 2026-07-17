---
frd: 0006
title: Per-agent Dynamic Workflows
status: In review
author: TsuyoshiUshio
created: 2026-07-17
updated: 2026-07-17
issues: [#109]
pull_requests: []
branch: tsuyoshiushio-dynamic-workflow-support
---

# FRD 0006 — Per-agent Dynamic Workflows

## 1. Summary

Allow any session-backed agent, not only `main.agent.md`, to opt into Dynamic
Workflows independently. The Durable engine and discovered workflow handlers
remain app-wide, while enablement, `workflows.exclude`, prompt guidance,
workflow-management tools, and workflow ownership become agent-scoped.

## 2. Motivation / problem

The current implementation treats the filename `main.agent.md` as an
authorization boundary for Dynamic Workflows. `app.py` creates a `DFApp` and
calls `build_workflow_integration()` only when the main agent requests
workflows, while `registration/capabilities.py` also discards workflow tools
for every non-main agent. Applications must therefore rename an otherwise
appropriately named interactive agent to `main.agent.md`, and cannot host two
workflow-enabled agents with different tool inventories in one Function App.

The workflow registry also stores one app-global effective allowlist. Removing
only the filename check would make the last configured agent's
`workflows.exclude` policy apply to every agent, violating the per-agent
authoring contract. Workflow ownership currently hashes only `session_id`, so
two agents receiving the same client-provided session ID can also see and
control each other's workflows.

## 3. Goals / Non-goals

**Goals**

- Honor `workflows.enabled: true` for any agent with a session-backed built-in
  chat API or MCP endpoint, regardless of its filename.
- Create a `DFApp` when at least one eligible agent enables workflows.
- Register the Durable orchestrator, activity, and discovered workflow handlers
  exactly once per Function App.
- Apply `workflows.exclude` independently for each enabled agent.
- Give each enabled agent its own workflow system-prompt addendum and effective
  workflow-tool allowlist.
- Scope Dynamic Workflow IDs and management operations to the owning
  `(agent identity, session_id)` pair.
- Preserve existing `main.agent.md` behavior for workflows started after the
  upgrade.
- Keep malformed `workflows` configuration validation deterministic for all
  discovered agents.

**Non-goals**

- Enabling workflow control tools for timer, queue, Service Bus, Event Hubs, or
  other non-interactive trigger-only agents.
- Changing chat history, MAF `AgentSession`, runner locking, sandbox sessions,
  or the `x-ms-session-id` request contract.
- Creating multiple Durable orchestrators or activities per agent.
- Per-agent workflow handler implementations; discovered handler callables
  remain app-wide and authorization remains per-agent.
- Adding or changing frontmatter keys.
- Adding a legacy ownership fallback for workflows started before this change.
- Sub-agent workflow tasks or cross-app workflow invocation.

## 4. Proposed design

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| discover | `discovery/tools.py` | No behavioral change. Continue discovering one app-wide inventory of `@workflow_tool` handlers. |
| translate | `config/loader.py`, `config/merge.py`, `config/schema.py` | No schema or filename-classification change. Existing `workflows` metadata continues to flow into each `ResolvedAgent`. |
| validate | `app.py`, `workflows/integration.py` | Validate every agent's `workflows` block. Treat an enabled agent as eligible only when it exposes a session-backed built-in `chat_api` or `mcp` endpoint; warn and disable workflows for trigger-only agents. |
| capabilities | `registration/capabilities.py` | Remove the `is_main` gate and resolve each enabled agent's concrete workflow-tool inventory using its own `workflows.exclude`. |
| register | `app.py`, `workflows/integration.py`, `workflows/registry.py`, `registration/endpoints.py` | Create `DFApp` if any eligible agent enables workflows. Register the Durable blueprint and handler inventory once, then construct immutable per-agent workflow integration state. Thread the allocated built-in slug as workflow owner identity. |
| execute | `runner.py`, `workflows/tools.py`, `workflows/context.py` | Capture the invoking agent's allowlist and identity in workflow management-tool closures. Validate plans against that allowlist and derive workflow ownership from both agent identity and session ID. |

### App-wide runtime and per-agent integration

The workflow implementation has two different lifetimes and must model them
separately:

1. **App-wide runtime setup**
   - register the Durable blueprint once;
   - register the complete, unfiltered
     `tool_result.workflow_tools` inventory once in the global name-to-handler
     registry;
   - retain existing reserved-name, duplicate, and handler-shape checks.
2. **Per-agent integration**
   - determine whether the agent is eligible and enabled;
   - consume its already-filtered `AgentCapabilities.filtered_workflow_tools`;
   - compute an immutable `frozenset[str]` allowlist;
   - build its system addendum from that allowlist;
   - pass enabled state, allowlist, addendum, and owner identity into endpoint
     registration and later execution.

`app.py` performs app-wide setup once before registering agents. It must not
call `register_workflows()` once per enabled agent because that would
double-register Azure Functions names. The global workflow handler registry is
safe to share because every enabled agent draws from the same discovered
handler inventory; plan authorization is enforced separately with the
captured per-agent allowlist.

App-wide handler registration must never consume one agent's
`filtered_workflow_tools`. For example, if agent A excludes tool X, X remains
registered as a handler so agent B can execute it when B's independent
allowlist includes X.

### Eligibility

An agent is session-backed for this feature when either
`builtin_endpoints.chat_api` or `builtin_endpoints.mcp` is enabled.
`debug_chat_ui` alone is only a page surface and is not sufficient.

If an ineligible agent sets `workflows.enabled: true`, startup logs a warning
that Dynamic Workflows require a session-backed built-in chat or MCP endpoint.
The agent's normal trigger registration continues without workflow tools or
prompt guidance. An app containing only ineligible workflow requests remains a
plain `FunctionApp`.

The `workflows` block is still shape-validated even when the agent is disabled
or ineligible so unsupported keys and invalid values do not become silent
configuration.

If an eligible agent also has a normal trigger, workflows are enabled only for
its built-in chat/MCP invocation paths. The trigger invocation continues to run
without workflow-management tools because trigger-only workflow execution is a
non-goal.

An MCP-only agent receives workflow-management tools, including
`get_workflow_status` and `list_workflows`, but does not register the
chat-UI-specific `agents/{slug}/workflows` and
`agents/{slug}/workflow-status` HTTP polling routes. Those routes remain tied
to `chat_api`; an MCP client manages workflow state through the management
tools.

### Per-agent allowlists and prompt addenda

`registration/capabilities.py` applies `workflows.exclude` whenever that
agent's workflows block is enabled, without consulting `is_main`. The resulting
workflow tool objects are converted into an immutable name allowlist during
per-agent integration.

The process-global `_APP_ALLOWLIST` and its setters/getters are removed from
`workflows/registry.py`. The effective allowlist instead travels through:

`app.py` → `registration/endpoints.py` → `runner.py` →
`workflows.tools.build_workflow_tools()` → `WorkflowSessionContext`.

`start_workflow()` validates the submitted plan against the allowlist captured
in that request's workflow tool closures. System addenda are built from the
same allowlist, ensuring prompt guidance and enforcement cannot drift.

### Workflow ownership

The authoritative workflow owner identity is the **final built-in endpoint
slug used in that agent's routes**. `register_builtin_endpoints()` resolves
this value once, after its built-in slug collision handling, and passes that
exact value to both:

- chat/MCP runner closures and then `build_workflow_tools()`; and
- the workflow list/status HTTP endpoint closures.

The implementation must not independently re-derive the owner identity in
`app.py`, the runner, or the status endpoint helpers. If app-level function
name allocation and built-in slug allocation remain separate, the final
built-in slug wins for workflow ownership. A missing owner identity on an
enabled workflow path is a startup/programming error and must not fall back to
a shared value such as `"main"`.

The final slug is deterministic for normal single-agent names and already
handles sanitized-name collisions within an app. The configured agent name
remains available as human-readable audit metadata but is not used as the
authorization key because names need not be unique. The Durable orchestration
input's `owner` audit object records both the unique slug identity and the
display name.

New workflow instance IDs derive their ownership prefix from an unambiguous
encoding of `(agent identity, session_id)` and retain the random UUID suffix.
All workflow ownership helpers accept both values. The same owner identity is
captured by workflow management tools and by the agent-specific workflow
list/status HTTP endpoint closures.

The backwards-compatible in-process workflow context registry is also keyed
by `(agent identity, session_id)` rather than `session_id` alone, even though
the live path currently constructs `WorkflowSessionContext` directly. This
prevents the helper API from retaining a stale cross-agent ownership model.

This owner key affects only Dynamic Workflow ID generation and
list/status/cancel/terminate authorization. The raw session ID passed to the
runner, chat history provider, MAF `AgentSession`, runner lock, sandbox tools,
and request/response contracts remains unchanged.

### Authoring / API surface

The frontmatter shape is unchanged, but its valid location broadens from
`main.agent.md` to any eligible agent file:

```yaml
---
name: Report Agent
builtin_endpoints:
  chat_api: true
workflows:
  enabled: true
  exclude:
    - destructive_report_action
---
```

Multiple agents may enable workflows with different excludes:

```text
agents/
├── report.agent.md       # excludes destructive_report_action
└── incident.agent.md     # allows every discovered workflow tool
```

Each agent receives the same five workflow-management tool names and existing
route shapes under its own allocated slug. No new config key, endpoint shape,
or management tool name is introduced.

### Compatibility

- Existing workflow-enabled `main.agent.md` files remain valid.
- Existing `workflows.enabled` and `workflows.exclude` shapes are unchanged.
- Durable orchestrator/activity names and built-in workflow route shapes are
  unchanged.
- A non-main configuration that was previously warned and ignored now enables
  workflows when the agent is eligible. This is the intended feature expansion.
- This FRD intentionally refines issue #90's conceptual
  `(session_id, agent_name)` owner key to `(final built-in slug, session_id)`
  because configured agent names are not guaranteed unique.
- **Intentional experimental-v1 breaking change:** workflow IDs created before
  this version carry a session-only ownership prefix. They cannot be listed,
  queried, canceled, or terminated through the new agent-scoped management
  surfaces after upgrade. No legacy fallback is provided.
- The ownership migration does not change any non-workflow session behavior.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Eligible non-main agents | All triggers / session-backed built-in endpoints | Limit #109 to agents with built-in chat API or MCP endpoints; defer trigger-only workflows | Human | 2026-07-17 |
| 2 | Durable runtime registration | Once per enabled agent / once per app | Register orchestrator, activity, and handler inventory once per app | Agent | 2026-07-17 |
| 3 | Workflow allowlist storage | Last-writer app global / map keyed by agent / capture in request tool closures | Capture each immutable allowlist in the agent's workflow management-tool closures | Agent | 2026-07-17 |
| 4 | Agent ownership identity | Configured name / source path / allocated endpoint slug | Use the unique allocated built-in endpoint slug; retain name only as display metadata | Agent | 2026-07-17 |
| 5 | Workflow ownership scope | Session only / agent only / `(agent, session)` | Scope workflow IDs and management operations to `(agent identity, session_id)` | Human | 2026-07-17 |
| 6 | Existing workflow IDs | Legacy fallback for main / migration map / no fallback | Accept that pre-upgrade workflow IDs become inaccessible because the feature is experimental v1 | Human | 2026-07-17 |
| 7 | Non-workflow sessions | Namespace all session state by agent / change only workflows | Keep chat history, MAF sessions, locks, sandbox sessions, and request semantics unchanged | Human | 2026-07-17 |
| 8 | Ineligible workflow request | Fail startup / silently ignore / warn and continue | Warn clearly, disable workflows for that agent, and preserve normal trigger registration | Agent | 2026-07-17 |
| 9 | Authoritative agent identity | App-level allocated function name / independently derived name / final built-in endpoint slug | Resolve once inside built-in endpoint registration after collision handling and thread that exact slug to runner and polling closures | Agent | 2026-07-17 |
| 10 | App-wide handler registration input | First enabled agent's filtered tools / union of filtered tools / full discovered inventory | Register the full unfiltered discovered workflow-tool inventory once; authorize from per-agent filtered subsets | Agent | 2026-07-17 |
| 11 | MCP-only status access | Add HTTP polling routes / management tools only | Keep HTTP polling routes tied to chat API; MCP-only agents use workflow management tools | Agent | 2026-07-17 |

## 6. Test plan

- [ ] Unit: `tests/test_registration_capabilities.py`
  - a non-main enabled agent receives workflow tools;
  - two agents apply distinct `workflows.exclude` lists;
  - disabled agents receive no workflow tools.
- [ ] Unit: `tests/test_workflow_integration_validation.py`
  - malformed workflow metadata is validated for every agent;
  - per-agent allowlists and addenda are independent;
  - app-wide runtime registration is not repeated.
- [ ] Unit: `tests/test_workflow_registry.py`
  - the handler registry remains app-wide;
  - `start_workflow` validates against its captured agent allowlist;
  - no process-global effective allowlist remains.
- [ ] Unit: `tests/test_workflow_context.py` and workflow tool tests
  - workflow IDs differ for the same session under different agent identities;
  - ownership succeeds only for the matching `(agent, session)` pair;
  - same-session cross-agent status/cancel/terminate attempts return not found;
  - the compatibility session-context registry is keyed by agent and session;
  - orchestration owner audit metadata includes unique identity and display name;
  - non-workflow session IDs remain unchanged.
- [ ] Integration: `tests/test_app_routes.py`
  - a non-main eligible agent causes `DFApp` creation and receives Durable
    client bindings;
  - multiple enabled agents register one Durable engine and independent routes;
  - distinct excludes produce distinct prompt addenda;
  - agent A excluding a tool does not prevent agent B from executing it;
  - each chat API workflow list/status endpoint returns only its own agent's
    workflows for a shared `x-ms-session-id`, with empty/404 semantics for the
    other agent's workflow;
  - MCP-only agents receive workflow management tools but no chat workflow
    polling routes;
  - a trigger-only workflow request warns and leaves the app non-Durable when
    no eligible agent is enabled;
  - main-agent behavior remains covered.
- [ ] Fixture scenario:
  `tests/fixtures/config_scenarios/<next>_multi_agent_workflows/`
  - two eligible agents enable workflows with different excludes;
  - one trigger-only agent demonstrates ineligible behavior.
- [ ] Full gate:
  - `python -m ruff check src tests`;
  - `python -m mypy src`;
  - `python -m pytest --cache-clear --cov=./src/azure_functions_agents
    --cov-report=xml --cov-branch tests`.

## 7. Docs impact

- [ ] `docs/architecture.md` — distinguish app-wide Durable runtime setup from
  per-agent capability, integration, and ownership state.
- [ ] `docs/front-matter-spec.md` — replace the `main.agent.md` restriction with
  session-backed endpoint eligibility and independent excludes.
- [ ] `docs/workflows.md` — document multi-agent enablement, isolation,
  trigger-only exclusion, and ownership migration.
- [ ] `README.md` — update experimental Dynamic Workflow wording if it mentions
  the main-agent restriction.
- [ ] `docs/frds/README.md` — add FRD 0006 to the index.
- [ ] `docs/triggers.md` — no change; trigger-only workflow support is a
  non-goal.
- [ ] `docs/front-matter-reference.md` — no change; `config/schema.py` is not
  expected to change.

## 8. Status & sign-off

- **Architecture review (phase 2):** Initial review completed by `frd-reviewer`
  (rubber-duck), 2026-07-17. No blocking findings. The revision clarifies the
  authoritative post-collision built-in slug, full-inventory app-wide handler
  registration, MCP-only status behavior, HTTP polling ownership coverage,
  issue #90 owner-key refinement, hard failure for missing identities, context
  registry scoping, and audit metadata. Re-review completed by `frd-reviewer`
  on 2026-07-17 with no remaining blocking or important findings; the FRD was
  judged ready for human sign-off.
- **Human sign-off:** Pending after architecture review; set `status: Finalized`
  before implementation.
