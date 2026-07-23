---
frd: 0008
title: Dynamic workflows from Markdown-declared triggers
status: Finalized
author: TsuyoshiUshio
created: 2026-07-17
updated: 2026-07-22
issues: [#108]
pull_requests: [#112]
branch: tsuyoshiushio-dynamic-workflow-design
---

# FRD 0008 — Dynamic Workflows from Markdown-Declared Triggers

## 1. Summary

Enable a workflow-enabled `main.agent.md` to start Dynamic Workflows when it is
invoked by any supported Markdown-declared trigger. Generated trigger handlers
will receive a Durable orchestration client, pass workflow context to the
existing agent runner, and use trigger-specific system guidance that makes the
initial agent invocation a short-lived workflow starter rather than a process
that waits for workflow completion.

## 2. Motivation / problem

Dynamic Workflows v1 already registers Durable orchestration and Activity
functions for `main.agent.md`, and built-in chat/MCP endpoints pass a Durable
client into the runner. Markdown-declared triggers use a separate registration
path. Their generated handlers currently have no Durable client input and call
the runner without workflow enablement, the workflow system addendum, the
Durable client, or the agent name. An agent invoked by an HTTP, timer, queue,
blob, Event Grid, Service Bus, connector, or other supported trigger therefore
cannot call `start_workflow`.

The declared-trigger path should use the same existing workflow integration as
built-in endpoints: one agent turn starts the orchestration and then returns
without polling for completion.

## 3. Goals / Non-goals

**Goals**

- Add a Durable client input to every supported Markdown-declared trigger for
  workflow-enabled `main.agent.md`.
- Pass workflow enablement, the rich Durable client, channel-appropriate system
  guidance, and the agent name to `runner.run_agent`.
- Keep the trigger Function short-lived: after `start_workflow` returns a
  workflow ID, the initial agent turn ends without polling.
- Preserve all trigger args, schedules, routes, auth levels, function names,
  serialization, validation, logging, and error behavior.
- Keep workflow-disabled and non-main agents free of Durable client bindings and
  workflow tools.
- Separate chat completion guidance from non-interactive trigger guidance while
  sharing workflow selection heuristics and the effective workflow-tool list.
- Add a minimal timer-triggered workflow sample that demonstrates the starter,
  a durable wait, and a terminal result tool.
- Develop the change test-first and validate both registration and a live local
  timer-triggered workflow when required external prerequisites are available.

**Non-goals**

- Waiting for workflow completion inside the initial trigger Function.
- Enabling workflows for non-main agents.
- Changing the frontmatter schema or adding trigger-specific workflow config.
- Changing existing workflow execution, management, or completion semantics.

## 4. Proposed design

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| discover | No change | Existing tool discovery continues to return the project workflow-tool inventory without Azure policy. |
| translate | No change | Existing `workflows.enabled` and trigger configuration remain the source of truth; there is no schema change. |
| register | `app.py`, `registration/triggers.py`, `workflows/integration.py` | Thread the already-computed per-agent workflow state into trigger registration. Workflow-enabled handlers receive `durable_client_input(client_name="client")` before their trigger binding. Build chat-specific and trigger-specific workflow addenda from shared content. |
| execute | `registration/_handlers.py`, existing `runner.py` contract | Generated HTTP and non-HTTP handlers pass the system addendum, workflow-enabled state, rich Durable client, and agent name into `run_agent`. The runner injects per-session workflow tools as it already does for built-in endpoints. |

### Trigger registration

`create_function_app()` already computes `workflows_enabled` only for the main
agent and creates a `df.DFApp` whenever that agent requests workflows. It will
pass the same local flag and the trigger-specific addendum to `register_agent`.
Registration will not inspect frontmatter metadata.

`build_workflow_integration()` will return a typed
`WorkflowIntegrationResult` instead of the current two-item tuple. The result
contains:

- `workflow_tools`: the runtime management tools currently returned as the
  tuple's first item;
- `chat_system_addendum`: the existing chat/MCP addendum, or `None` when
  disabled;
- `trigger_system_addendum`: the declared-trigger addendum, or `None` when
  disabled;
- an `enabled` property derived from the addenda.

`app.py` will use this one result for both registration paths rather than
deriving workflow enablement from one channel's prompt string. This result is an
internal package contract; the package-level
`WORKFLOW_SYSTEM_ADDENDUM` compatibility constant remains the static chat
prefix for existing imports.

Both channel addenda are always present together when enabled and both are
`None` when disabled; `enabled` asserts that invariant rather than treating one
channel as authoritative.

`register_agent()` and its HTTP/non-HTTP helpers will accept workflow arguments
as keyword-only parameters. For workflow-enabled handlers, registration applies
decorators in this imperative order:

1. `app.durable_client_input(client_name="client")`
2. `app.route(...)` or the selected non-HTTP trigger decorator
3. `app.function_name(...)`

Disabled handlers keep their current signatures and do not receive a Durable
binding. The generic non-HTTP path means all current and future supported
trigger decorators inherit the behavior without a per-trigger allowlist.
Durable orchestration/activity/entity triggers remain unsupported authoring
surfaces as documented in `docs/triggers.md`.

This order follows the existing built-in chat registration path and makes the
Durable middleware available before the trigger binding is added. Azure
Functions ultimately indexes accumulated builder metadata rather than exposing
decorator order as an authoring contract; tests assert the resulting binding set
and callable behavior, not incidental decorator bookkeeping.

The Azure Durable Functions Python v2 decorator intentionally rewrites the
client parameter annotation to `str` for worker binding validation, then wraps
the function with middleware that converts the starter payload to a rich
`DurableOrchestrationClient` before user code executes. Generated handler tests
will preserve that SDK contract rather than constructing a second client.

### Handler execution

The HTTP and non-HTTP handler factories will use a shared inner execution
closure plus two natural outer signatures:

- workflow disabled: `(req)` or `(trigger_data)`
- workflow enabled: `(req, client)` or `(trigger_data, client)`

Only the enabled closure passes:

- `workflow_enabled=True`
- `workflow_durable_client=client`
- the trigger workflow system addendum
- `agent_name=resolved.slug`

All existing prompt construction, trigger serialization, request/response
schema validation, sessions, observability, logging, and error handling remain
unchanged.

For declared HTTP triggers, `response_schema` and `response_example` continue to
take precedence over conversational workflow guidance. The trigger addendum
will tell the agent to end promptly while honoring the configured response
format. It will include the workflow ID in the immediate HTTP response only when
that format permits it. Authors who need the caller to receive the ID in the
immediate HTTP response should add `workflow_id` to their response
schema/example. The runtime will not bypass or silently weaken response
validation.

### Asynchronous workflow start

This feature changes only how a declared trigger starts an existing Dynamic
Workflow:

1. The trigger invokes the main agent.
2. The agent authors a validated plan and calls `start_workflow`.
3. `start_workflow` calls Durable `start_new`, receives the instance ID, and
   returns `{"workflow_id": ...}` immediately.
4. The agent ends its turn and the trigger Function exits without polling.

The existing Durable orchestrator, workflow tools, ownership rules, and
completion behavior are unchanged.

### Channel-specific system guidance

`workflows/integration.py` will render two addenda from common workflow
selection rules and the same effective workflow-tool inventory:

- **Chat/MCP:** retain the existing chat poller, synthetic notification, and
  follow-up status guidance.
- **Declared trigger:** direct the agent to start the workflow once, retain the
  workflow ID, end promptly, never poll, honor any configured HTTP response
  schema, and use an available result-delivery tool as the final step for
  non-HTTP triggers.

Registration receives the finished addendum; it does not own prompt policy.

### Timer sample

Add `samples/workflow-timer-trigger/` with:

- a minimal `function_app.py`;
- `main.agent.md` declaring `workflows.enabled: true` and a timer schedule;
- small `@workflow_tool` handlers for capturing trigger information and
  publishing a distinctive structured completion log;
- a short workflow with a terminal result tool;
- local settings, host configuration, requirements, and instructions for
  running against the Durable Task Scheduler emulator and a configured model
  provider.

The sample README will explain how to temporarily add `run_on_startup` for an
immediate local demonstration and warn against committing it for production.

### Authoring / API surface

There is no new frontmatter key. Existing authoring becomes effective:

```yaml
---
name: Scheduled Workflow Starter
workflows:
  enabled: true
trigger:
  type: timer_trigger
  args:
    schedule: "0 */5 * * * *"
---
```

The behavior applies only when this file is `main.agent.md`. Any supported
declared trigger can replace `timer_trigger`.

### Compatibility

- Existing workflow-enabled built-in chat and MCP endpoints keep their current
  bindings and completion guidance.
- Workflow-disabled and non-main trigger handlers retain their one-binding
  signatures and behavior.
- Trigger routes, methods, schedules, connector fallback, `arg_name`, and
  generated function names are unchanged.
- The feature adds a Durable client input only under the existing
  workflow-enabled-main-agent condition, where the app is already a `DFApp`.
- `build_workflow_integration()` returns the new typed channel result while
  preserving legacy two-value unpacking as `(workflow_tools, chat_addendum)`.
- No config migration or schema documentation regeneration is required.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Design record | Amend FRD 0004 / create a focused follow-up FRD | Create a focused follow-up FRD and reference FRD 0004 | Human | 2026-07-17 |
| 2 | Trigger scope | Eight types named in #108 / every supported declared trigger | Apply generically to every supported Markdown trigger | Human | 2026-07-17 |
| 3 | Workflow lifetime | Keep starter open / fire-and-forget Durable start / framework callback | End the starter after the workflow ID; Durable execution continues independently | Human + Agent | 2026-07-17 |
| 5 | Prompt guidance | Reuse chat addendum unchanged / channel-specific completion sections | Share workflow/tool guidance but render chat and trigger completion behavior separately | Human + Agent | 2026-07-17 |
| 6 | Registration policy | Re-read metadata in registration / pass resolved state explicitly | Pass `workflows_enabled` and addendum from `app.py` | Agent | 2026-07-17 |
| 7 | Durable client conversion | Construct in runtime / trust Durable v2 middleware | Use the rich client supplied by `durable_client_input` middleware and retain its worker-facing `str` annotation | Agent | 2026-07-17 |
| 8 | Development method | Implementation-first / TDD | Add and run focused failing tests before product changes | Human | 2026-07-17 |
| 9 | Demonstration | Modify interactive sample / add dedicated timer sample | Add `samples/workflow-timer-trigger/` | Human | 2026-07-17 |
| 10 | Integration return contract | Extend tuple / second global render call / typed result | Return `WorkflowIntegrationResult` with channel addenda and derived enablement | Agent | 2026-07-17 |
| 11 | HTTP response schema | Bypass validation / reject combination / preserve validation | Preserve strict validation; include workflow ID only when the authored response format permits it | Agent | 2026-07-17 |
| 14 | Existing integration helper compatibility | Break tuple unpacking / separate legacy wrapper / make typed result iterable | Preserve `tools, addendum = build_workflow_integration(...)`; iteration yields tools and the chat addendum while channel fields remain available by name | Agent | 2026-07-17 |
| 15 | FRD number after rebasing | Keep 0006 / use the next available number on `main` | Renumber to FRD 0008 because 0006 and 0007 are already allocated | Human + Agent | 2026-07-22 |

## 6. Test plan

- [x] TDD red phase:
  - workflow-enabled HTTP and non-HTTP handlers expect Durable context in
    `_run_agent`;
  - registration expects `durableClient` only for enabled main-agent triggers;
  - integration expects distinct chat and trigger addenda;
  - sample contract expects the dedicated timer app and its workflow tools.
- [x] Unit: `tests/test_registration_handlers.py`
  - enabled handler signatures accept a required client;
  - runner receives client, enabled state, trigger addendum, and agent identity
    slug;
  - disabled handlers preserve their old signatures and runner args.
- [x] Unit: `tests/test_registration_triggers.py`
  - HTTP and non-HTTP trigger decorators preserve all args;
  - Durable binding is applied in the correct order only when enabled;
  - generic connector fallback remains unchanged.
- [x] Integration: `tests/test_app_routes.py`
  - real workflow-enabled HTTP and timer functions include `durableClient`;
  - a representative event trigger includes the same binding;
  - route, schedule, `arg_name`, function name, and worker annotation remain
    correct;
  - non-workflow functions have no Durable binding.
- [x] Unit: workflow integration guidance
  - chat guidance retains polling/notification behavior;
  - trigger guidance forbids waiting/polling and explains terminal sinks;
  - both list the same effective workflow tools.
- [x] HTTP response contract:
  - workflow guidance does not override `response_schema`/`response_example`;
  - a schema that includes `workflow_id` can return it;
  - invalid starter responses still fail existing validation.
- [x] Sample contract:
  - `samples/workflow-timer-trigger` has valid workflow-enabled timer
    frontmatter;
  - sample workflow tools are discoverable;
  - the generated app indexes timer, Durable client, orchestrator, and Activity
    bindings.
- [x] E2E startup: existing sample discovery boots the new sample with `func
  start` and Azurite.
- [x] Live local sample: with Foundry `gpt-5.4-mini`, observed timer firing,
  a logged workflow ID, prompt starter completion, terminal sink marker, and a
  terminal Durable instance using the Durable Task Scheduler emulator.
- [x] Full gate: `ruff`, strict `mypy`, and coverage-enabled `pytest`.

## 7. Docs impact

- [x] `docs/architecture.md` - document Durable trigger binding and
  asynchronous workflow start.
- [x] `docs/triggers.md` - document workflow-enabled main-agent triggers and
  link to the consolidated workflow behavior.
- [x] `docs/workflows.md` - document HTTP and non-HTTP trigger-started workflow
  behavior in one place.
- [x] `samples/workflow-timer-trigger/README.md` - local run and verification
  guide.
- [x] `samples/README.md` and `README.md` - add the timer workflow sample.
- [x] `docs/frds/README.md` - add FRD 0008.
- [x] Frontmatter reference/spec - reviewed; no schema or authoring-shape change.

## 8. Status & sign-off

- **Architecture review (phase 2):** Completed by two independent
  `rubber-duck` passes on 2026-07-17. The initial review requested a concrete
  integration return contract, explicit HTTP response-schema behavior,
  and reconciliation of the interactive-only workflow docs. All findings were
  incorporated; re-review found no blocking inconsistencies and declared the
  FRD ready for sign-off.
- **Human sign-off:** TsuyoshiUshio approved the implementation plan, TDD
  method, asynchronous start behavior, and dedicated timer sample on
  2026-07-17. Status set to `Finalized`.
