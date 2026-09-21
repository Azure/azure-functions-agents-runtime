---
frd: 0009
title: Public Durable Agent Loop
status: In review
author: larohra
created: 2026-09-16
updated: 2026-09-21
issues: []
pull_requests: []
branch: larohra/durable-agent-loop
---

# FRD 0009 - Public Durable Agent Loop

> Review draft, not an implemented feature or finalized public contract.
> This FRD is published separately from product changes. Human architecture
> sign-off is not yet recorded; opening this review does not authorize
> implementation or waive the open gates below.

## 1. Summary

Introduce an opt-in, public-preview Durable execution mode for the existing
markdown-first Microsoft Agent Framework runtime. Authors configure global
defaults in `agents.config.yaml` and override them in individual `.agent.md`
files, including the shorthand `durable: true`. Every enabled request belongs
to an authorized session; one Durable Entity owns that session's conversation,
active-turn admission, and optional sandbox binding. A native Durable
orchestration schedules each foreground model call and each tool invocation
separately. Remote MCP dispatch stays in the Functions worker with its
credentials. Without a Sandbox Group, local tools dispatch from Functions activities;
an existing Dynamic Sessions interpreter may provide isolated code execution
without a runtime-guaranteed workspace lifetime. With a customer-provided Sandbox Group, tool
activities dispatch local code/workspace tools through the session's one bound
sandbox. The first public release supports HTTP/chat; Timer is a separate
design probe for later non-HTTP support.

Prefer native Entity payload offload, not the spike's custom Blob-reference
history store. Public release requires a qualified native Python/host/provider
combination, lossless model-state round trips, reliable trigger intake, strict
ownership enforcement, and a proven retention/deletion lifecycle. Public
preview does not waive these engineering gates or imply a compliance
certification, a GA dependency contract, exactly-once external effects, or
immortal workers/sandboxes.

### Open review and qualification gates

| Gate | Current evidence | Required resolution |
| --- | --- | --- |
| Native payload lifecycle | Large-state offload worked; native blobs survived Entity deletion/purge. The inspected public surfaces and executable counterexamples do not establish complete reclamation; age-only cleanup is rejected | **Blocking design/release decision:** a supported ownership/reference-lifecycle, native writer-fencing/erasure-completion and coordinated-restore contract, or an explicit revision of the proposed guarantee |
| Shared runtime and human input | The real Python worker indexed exactly seven Functions, but host external configuration startup failed before readiness or any Durable submission | Identify the throwing startup component, then qualify routing/auth, client lifetime, per-call checkpoints, parallel execution and same-run answer/cancel/expiry recovery |
| Sandbox workspaces | A bounded live Python 3.13/Linux synthetic bundle passed activation, overlapping guest execution, same-ID suspend/resume and owned-resource deletion/404 | Qualify integrated Durable dispatch, managed identity/private paths, restricted-identity negatives, Python 3.14 execution, production dependencies and broader service limits |
| Production profiles and limits | S0 native-offload and offline MAF results remain valid; B added 54 offline checks. Its optional native cap/read-transport case was not run | Qualify advertised deployed profiles and independent state/message bounds. Source-wired host tuning is not a tested native-Python mitigation or support commitment |
| Final architecture and delivery approval | Separate FRD review and explicit spike/E2E reuse are requested; implementation slices are proposed in section 4.14 | Resolve the remaining decisions, review the current contracts and record human sign-off before status Finalized or product implementation |

UI/SSE and non-HTTP research are outside the core release dependency chain.
Round 2 is closed: runtime qualification is startup-blocked, payload lifecycle
has a platform-contract gap, and the narrow Sandbox transport has a live pass.
All mutation receipts are consumed/closed. Successful primitive evidence is
not overall production qualification, a reusable access grant or a compliance
certification.

## 2. Motivation / problem

### Current mainline

Source baseline: main/worktree commit `8c8a682`.

- `app.py:create_function_app()` performs two-pass composition and selects
  `DFApp` for Dynamic Workflows. There is no public Durable Agent Loop setting.
- `runner.py:_build_role_agent()` builds a MAF harness; `run_agent()` invokes its
  whole tool loop inside one process. Its session lock is process-local.
- `_blob_history.py:BlobHistoryProvider` and `_file_history.py` persist ordinary
  conversations; cross-worker ordering of whole turns is the caller's
  responsibility. These remain the non-durable path.
- `registration/endpoints.py` runs synchronous/SSE chat directly through that
  runner. `_handlers.py:make_agent_handler()` generates a fresh session ID for
  each non-HTTP invocation.
- `_auth.py:authorize_entra_request()` checks Easy Auth/allowlists but returns
  only an allow/error result, not a trusted principal for session authorization.
- `system_tools/sandbox.py` implements Dynamic Sessions/session pools. It is
  not the Sandbox Groups product requested here.
- `workflows/engine.py` already provides native orchestration, per-activity
  authorization, retry, and deterministic scheduling patterns. Reuse its
  conventions, not its workflow-plan semantics.

### What the spike proves, and what it does not

The source on `larohra/durable-loop-leadership-demo` (`d3ebac5`) supplies useful
one-step model, admission, receipt, sandbox-adapter, and recovery prior art.
It is not a drop-in implementation for current main:

| Area | Spike evidence | Production implication |
| --- | --- | --- |
| Durable SDK | `azure-functions-durable>=1.2.10,<2`; legacy context bindings in `experimental/durable_loop_registration.py` | Port to native 2.x APIs; do not copy decorators or run both SDK generations |
| MAF | Core 1.17.0, OpenAI 1.14.2, Foundry 1.12.0, versus current 1.13.0/1.10.2/1.10.3 | Qualify one compatible package set independently |
| Checkpoints | `_call_model_activity()` and `_schedule_tool_refs()` schedule real Durable activities | Reuse the single-call boundary and deterministic tool-result ordering |
| Entity state | `_entity_admit()` / `_entity_complete()` fence a run and commit a `committed_context_ref` | Useful transition tests, but not proof of inline Entity history |
| History storage | `BlobDurableContentStore` implements custom immutable references and a 32 MiB application cap | Do not port unless native offload is proven insufficient and a revised design is approved |
| Model adapter | `MafOneStepModelProvider` disables function invocation, supplies schema-only tools, and rejects stateful continuation options | Reuse intent and wire tests; adapt against the qualified MAF release |
| Sandbox | Optional `azure-containerapps-sandbox==0.1.0b4`; stable creation labels, group/manifest checks | Reuse narrow transport/reconciliation ideas, not entire controller/session-runtime subsystems |
| Workspace policy | `per_call` default; retained profile is gated and includes `_attach_or_recreate()` | Replace with session affinity and explicit `workspace_lost`; no silent replacement |
| UI | Separate `larohra/durable-loop-chat-ui` branch; optional observation journal and hosted assets | Evaluate separately; journal/browser state must not become execution authority |

The spike also requires private APIM-specific model/control routing and contains
background model polling, demo authentication, fault injection, and broader
hybrid execution machinery. None becomes a public prerequisite by accident.

## 3. Goals / Non-goals

### Required outcomes

| ID | Requirement |
| --- | --- |
| F1 | Global Durable defaults plus per-agent boolean/object overrides, with absent configuration preserving current behavior |
| F2 | Every model invocation and tool invocation runs behind its own Durable scheduling/completion boundary |
| F3 | Foreground inference only; no Responses background submission/poll/cancel control path |
| F4 | Every request has an authorized, transport-neutral session and Entity-backed conversation history |
| F5 | One active HTTP/chat turn per session across workers; no implicit interactive turn queue; fan out tool batches within a turn |
| F6 | Caller-supplied session IDs are supported with ownership/collision checks; missing IDs are generated without breaking retry deduplication |
| F7 | HTTP/chat only in the first public release; Timer design probe and other non-HTTP trigger work are not shipped v1 support |
| F8 | A configured Sandbox Group runs local code/workspace tools; remote MCP remains worker-side. Without a group, worker activities may use the existing isolated Dynamic Sessions interpreter |
| F9 | A live session never silently switches sandbox identity; permanent workspace loss is explicit |
| F10 | Prefer native large-state offload; never silently truncate conversation history to fit a backend limit |
| F11 | Immediate access revocation on deletion, followed by verified content erasure within the configured window |
| F12 | Production identity, isolation, networking, audit, recovery, and operational gates apply despite preview release status |
| F13 | Publish and verify the exact generated function/trigger/route footprint for each supported configuration; keep app-wide runtime registrations shared |
| F14 | Support durable in-flight human input: persist a question, wait without a worker, accept an authorized answer and resume the same turn; no new tool-action approval feature in v1 |

### Non-goals for this release

- Replacing MAF, adding a `runtime:` selector, or changing non-durable execution.
- Exactly-once email/payment/other external side effects from arbitrary tools.
- Pinning a Functions process or guaranteeing indefinite sandbox existence.
- Background provider polling, provider-managed conversation continuity, or
  opaque fallback to a different model/deployment.
- Running an entire MAF agent loop in a single activity or Entity operation.
- Durable loop composition with chat subagents or Dynamic Workflow subagents
  in this first release. Reject affected combinations and references rather
  than invoking them through the ordinary runner.
- Exposing a durable agent as the existing MCP `agent_chat` endpoint before a
  caller-identity and asynchronous-result contract is specified. Outbound MCP
  tools are in scope; inbound MCP agent exposure is a different surface.
- Cross-agent shared workspaces, forks,
  automatic sandbox reconstruction, or migration of private spike sessions.
- Durable catch-up of Timer occurrences missed before Durable acceptance.
  Any later Timer design starts from normal Functions delivery semantics;
  Timer is not shipped in this HTTP-only release.
- Shipping non-HTTP durable triggers, broker settlement/queuing policies, or
  `durable.ingress_policies` in v1. The Timer probe is research, not a public
  compatibility promise.
- A public `durable.tool_policies` delivery/reconciliation DSL in v1. Keep the
  normal Durable at-least-once contract and runtime-owned transport behavior.
- Tool-action approval modes, per-MCP approval settings or an approval-policy
  engine as part of this change. Human input is not advertised as a security
  authorization gate around arbitrary tools.
- Custom conversation segmentation/manifests solely to reproduce native
  payload offload, or an additional execution-order/replay engine.
- Claiming all SDK-recognized trigger types are supported merely because the
  generic serializer recognizes them.

## 4. Proposed design

### 4.1 Pipeline and module ownership

| Stage | Existing surface | Proposed change |
| --- | --- | --- |
| Discover | `discovery/tools.py`, `skills.py`, `mcp.py` | Preserve read-only discovery and filters; expose stable descriptors/package identity without resource creation |
| Translate | `config/schema.py`, `merge.py`, `validation.py`, `loader.py` | Shared typed Durable settings, field-aware inheritance, compatibility validation, useful indexing errors |
| Compose | `app.py`, `registration/catalog.py`, `capabilities.py` | Freeze execution policy/catalog before app mutation; choose one `DFApp` if workflows or Durable agents need it |
| Register | `registration/triggers.py`, `_handlers.py`, `endpoints.py`, `_auth.py` | Route enabled agents to one Durable submission contract; reuse auth and serializers; register native bindings once |
| Execute | New `durable/engine.py`, `activities.py`, `model.py`, `tools.py` | Native turn orchestration; separately checkpointed model/tool activities; DTS fan-out/fan-in for tool batches |
| Session | New `durable/session.py`, `contracts.py`, `intake.py` | One authoritative session Entity, typed versioned state, request admission and transport normalization |
| Sandbox | New `durable/sandbox.py` and a narrow transport adapter | Allocate, bind, resume, dispatch, reconcile and delete within the approved group |
| Lifecycle | New `durable/lifecycle.py`; existing observability conventions | Expiry, deletion, cancellation reconciliation, quotas and content-free audit |
| UI | `public/durable-chat/`, dedicated endpoint adapter | Optional client of the public Durable API; no authority over session persistence |

Names above are proposed module boundaries, not a requirement to introduce empty
scaffolding. Extract shared helpers only when both paths actually need them.
Keep the composition root authoritative, registration Azure-aware, and all
runtime clients lazy. Do not reparse front matter in handlers/activities or
perform network/resource provisioning during discovery.

Validate the complete graph before creating the app. A non-durable coordinator
must not bypass this mode by referencing a durable target through an ordinary
delegate/workflow activity. Malformed or unsupported Durable declarations must
produce actionable indexing/startup failures, not a fallback to `run_agent()`.
Preserve unrelated legacy loader behavior.

### 4.2 Authoring and inheritance

Proposed example. Model-step and tool-call count ceilings are absent by default;
authors add them only when wanted. Shown timeouts are separate proposals:

```yaml
# agents.config.yaml - illustrative explicit settings, not implicit limits
http_auth: entra
durable:
  enabled: false
  history:
    session_ttl: P10D
    erasure_window_seconds: $DURABLE_ERASURE_WINDOW_SECONDS
  # Optional: omit for Functions-only tools and no persistent workspace.
  sandbox_group:
    resource_id: $SANDBOX_GROUP_RESOURCE_ID
    region: $SANDBOX_GROUP_REGION
    # Optional base selection; a compatible Python 3.13/3.14 disk is the default.
    disk: python-3.13
```

```yaml
---
name: Researcher
description: Answers research requests.
builtin_endpoints:
  chat_api: true
durable: true
---
Research the request using the available tools.
```

```yaml
# Optional per-agent call-count limit, authored explicitly.
durable:
  enabled: true
  limits:
    max_model_steps: 24
```

| Per-agent value | Resolution |
| --- | --- |
| Absent | Inherit global Durable configuration |
| `true` | Override `enabled` to true and inherit other settings |
| `false` | Disable the mode, even when globally enabled |
| Object | Recursively override authored fields; omitted fields inherit |

Global `durable` is an object; its absent `enabled` default is false. A global
`enabled: true` enables all inheriting agents, subject to compatibility checks.
Use shared Pydantic models/validators for fields shared across levels. Reject
unknown keys, booleans masquerading as numeric limits, invalid/non-finite
durations, and inconsistent budgets.

`max_model_steps` and `max_tool_calls` are optional count ceilings with no
runtime-imposed default. Omitted values inherit; if absent globally too, there
is no count ceiling. Explicit `null` clears an inherited count ceiling. A
configured ceiling must be a positive integer; `tools: false` remains the
explicit way to disable tools entirely.

Configured count budgets must be included in the **initial effective model
prompt**, using a runtime-owned instruction addendum rather than rewriting
the author's markdown or the original user message. It states the available
model/tool budget and asks the model to finish within it. Subsequent model
requests receive the remaining counts. With no configured count ceiling, add
no count-limit instruction.

For example, an explicitly limited turn may receive:

```text
Runtime call budget for this turn:
- Model steps remaining, including this request: 3.
- Tool invocations remaining: 2.
Finish within these budgets. Your final available model step is answer-only.
```

Only emit lines for configured ceilings; show the configured total as well
when earlier preparation/compaction has already consumed part of it.

The orchestrator enforces the budget even if the model ignores the instruction.
Before the final available model step, request a final answer with tool
selection disabled. When the tool budget is exhausted, allow finalization only
within any remaining model/time budget. A requested tool batch must fit the
remaining tool count; otherwise dispatch none of that batch, record truthful
unexecuted outcomes and request finalization if budget remains. If finalization
cannot complete within the budget,
stop scheduling new work and record `budget_exceeded` with the observed
partial outcome. Preserve its history; do not claim successful completion.
This is not a guarantee that an already-running external command can be killed.

Counts are per accepted turn and refer to logical scheduled model steps
(including model-based compaction) and tool invocations. Durable replay does
not consume them again; carry them through ContinueAsNew. Native redelivery/
transport retries remain a separate at-least-once concern, so these are not
an exactly-once physical network-request or billing ceiling. Timeouts, payload
budgets, deployment capacity and service quotas remain independent controls.

Do not add `ingress_policies` or `tool_policies` to v1 authoring. Deployment
drain/retirement controls remain an operator concern, not additional fields
an agent author must understand to use `durable: true`.

Infrastructure overrides need special care: a Sandbox Group override is a
complete resource-ID/region binding, not a partial mix of two groups. Explicit
`sandbox_group: null` selects no workspace for new sessions; omission inherits.
Neither it nor an agent edit moves an existing session to another execution
profile. A session's captured profile/group is immutable; unavailable or revoked
bindings fail explicitly.

`model_timeout_seconds` and `tool_timeout_seconds` are unset by default.
When explicitly authored, cap them by any explicitly configured turn deadline.
Durable mode must not acquire the ordinary in-process runner's implicit
900-second fallback as a new, hidden orchestration deadline; preserve that
legacy behavior for non-durable agents. Platform/provider timeouts still apply.
Production deployment supplies explicit positive session
TTL and erasure-window values; do not silently choose a retention promise.
`P10D` above is an example, not an implicit TTL default.

**`session_ttl` is an idle TTL**, measured after the most recent accepted turn
finishes, including success, failure, or cancellation. It is not a maximum age
from session creation and does not expire an actively executing turn. Status/
history reads, UI polling, rejected requests and replay of an existing
idempotency receipt do not refresh it.

The user explicitly extends idle expiry to a run waiting for human input.
Start that dormant interval when the question is durably opened and
available through the authorized API, not when a browser first views it.
A timely accepted answer resumes the same run and invalidates that wait's
expiry generation. If unanswered, expire both the waiting run and session;
do not create a fresh full TTL by treating the wait timeout as an ordinary
failed-turn completion. No separate human-wait duration setting is introduced.

Accept **only ISO-8601 duration strings**, using the same fixed-unit
`PnDTnHnMnS` subset as Dynamic Workflows: `PT10S`, `PT30M`, `P10D`, or
`P10DT10S`. No duration object, plain numeric TTL, month/year/week calendar
semantics, or new parallel parser. Reuse/extract the existing
`workflows/schema.py:parse_iso8601_duration` grammar into a shared pure helper;
preserve existing workflow behavior and keep field-specific bounds separate.
In particular, a workflow wait's 24-hour policy cap is not a session TTL cap.
The final duration must be finite and positive; reject booleans, null, zero,
negative, malformed, and overflowing values.

Persist the effective idle interval and lifecycle-policy version when the
session is created. On completion, atomically record `last_turn_completed_at`,
`expires_at = last_turn_completed_at + session_ttl`, and an expiry generation.
An accepted new turn invalidates the previous idle timer while executing; its
terminal transition establishes the next idle interval. An expiry workflow
must check the captured generation, current lifecycle state and deadline in
the Entity before revoking anything. A stale timer cannot expire an active or
newly renewed session. Runtime-owned timestamps, not caller timestamps, drive
these transitions.

A quarantined/uncertain-effect outcome must not gain an indefinite active-turn
exemption from retention: its finite reconciliation/quiescence policy governs
expiry and physical cleanup without allowing unsafe new work. Define and test
that state separately from an actively executing turn.

Later configuration defaults affect new sessions, not existing policy promises.
Normal idle renewal after a real turn intentionally advances `expires_at`.
Changing an already-bound interval requires an explicit policy migration, not
silent reinterpretation of an agent edit. On revocation/deletion, persist the
absolute `erase_by`; no new request, renewal or config edit can extend that
in-progress erasure promise. Validate the erasure window against the qualified
residual-writer and cleanup bound, not merely any positive number. A separate
absolute session-age limit is not introduced by this idle TTL setting.

An effective legacy `system_tools.dynamic_sessions_code_interpreter` is
incompatible only when the same durable agent also has an effective Sandbox
Group while tools are enabled. Reject that combination because it would
introduce a second execution environment outside the bound group. Do not
reinterpret a Dynamic Sessions endpoint as a Sandbox Group. Authors selecting
a group can opt out of the inherited legacy interpreter:

```yaml
system_tools:
  dynamic_sessions_code_interpreter: false
durable: true
```

Without a Sandbox Group, allow the existing interpreter: its driver runs in a
Durable activity on the Functions worker, while the generated Python executes
in the remote isolated Dynamic Sessions environment, not in the worker process.
Checkpoint each interpreter invocation as a tool call. Namespace its pool
identifier with the internal authorized session incarnation, not a raw
caller-chosen ID that could collide across tenants/agents.

The pool owns that interpreter environment's expiry/reset behavior. Do not
promise the retained-workspace/same-physical-sandbox guarantee of Sandbox Groups
or depend on REPL state surviving a replay/restart. Apply the same uncertain
effect and redelivery rules as other code-execution tools. Non-durable agents
retain their existing behavior.

Native backend selection, payload-offload activation, transport settings, task
hub identity, and storage credentials remain `host.json`/app settings. Do not
create a competing front-matter copy of the Durable extension's configuration.
Deployment qualification checks the resolved combination.

### 4.3 HTTP session contract and future transport seam

The public session ID is a **correlation identifier, not an authorization token**.
Use the existing safe alphabet/length (`[A-Za-z0-9._-]{1,128}`), checked as a full
match. Values are case-sensitive; HTTP header names are not. Reject blank,
non-string, malformed, or conflicting identifiers instead of generating a new
session silently.

| Transport | Standard session carrier |
| --- | --- |
| HTTP/custom HTTP/chat | `x-ms-session-id` header; a supplied `x-session-id` JSON control field must agree |

Do not add per-agent JSONPath/correlation mappings or recursively search
business data for a coincidentally named field. Keep the internal invocation
envelope transport-neutral for the Timer research and later non-HTTP design;
that is not shipped support for additional trigger types.

If no session ID is supplied, generate one and persist it with the request
receipt. A lost HTTP acknowledgement resolves to that same generated session,
not another UUID. A Timer probe must investigate how a trigger with no caller
payload selects a session; v1 does not invent a timer-header capability.

The logical namespace is **deployment/app + trusted tenant scope + agent slug**.
Transport/trigger type is not part of session identity. The Entity records the
owner, authorized submitter policy, and an internal immutable session incarnation.
Internal Entity/run/sandbox labels use non-revealing identifiers, never raw user
names, emails, bearer tokens, or untrusted URI/path segments. Hashing identifiers
does not replace authorization.

Caller-supplied creation is allowed: an atomic create/admit operation establishes
ownership or detects an existing session. Another owner cannot claim it by
knowing the same ID. A missing or unauthorized resource is masked consistently.
Deleted/expired IDs cannot implicitly resurrect old content or a sandbox;
retain a minimal non-content tombstone under a documented operational policy.
Any future explicit ID reuse must create a new incarnation and cannot expose
prior run/index data.

### 4.4 Ownership for v1 HTTP/chat

HTTP durable routes require platform-validated Entra identity, including trusted
Easy Auth enforcement and the existing allowlists. Extend `_auth.py` with a
typed principal result while preserving the existing legacy allow/error helper.
Function keys, a client-supplied principal header, or possession of a session ID
are not sufficient production session ownership. Local tests use explicit
synthetic principals; no cloud anonymous/demo fallback is introduced.
Preserve existing auth precedence: built-in endpoints inherit global
`http_auth`; custom HTTP triggers use `trigger.args.http_auth`. Require an
effective Entra policy rather than silently changing an authored
anonymous/function-key policy.

`PrincipalIdentity` is the immutable `(tenant_id, object_id)` pair supplied by
the verified platform identity, for a user or service principal. An application
ID alone does not identify an individual user. Never trust ordinary request
body `owner`/`tenant` fields as authentication.

There is no new customer-authored ingress/grant policy in v1. The HTTP caller
creates or resumes only an authorized session under the existing endpoint
policy. Cross-owner delegation and non-HTTP source identity require a later
explicit design, informed by the Timer probe.

Reauthorize history/status/events/cancel/delete and tool execution against
current policy. Frozen descriptors make replay stable but do not preserve a
revoked permission. Do not return raw DTS management URLs, system keys, Entity
state, storage references, or provider control endpoints to callers.

### 4.5 Admission, idempotency and reliable handoff

The Entity owns a short `try_begin_turn` transition: authorize, check tombstone
and expiry, deduplicate, check `active_turn_id`, then record the accepted request.
Entity operations are already serialized by Durable. Do not run the model or
tools inside the Entity, hold a process lock, or build a separate ordering
service.

External native clients signal/read Entities; neither a signal acknowledgement
nor a read of "idle" is an admission result. Prefer a **short admission
orchestration** that calls the Entity and returns an authoritative decision.
This is a logical operation, not a requirement for another registered
Function. The proposed consolidated orchestrator handles typed `admit`, `run`
and lifecycle inputs in independent instances; one long-running lifecycle
instance cannot block other admissions.
Prototype the native Entity `schedule_new_orchestration` action to record
admission and stage the turn start through the framework's state/action
completion path. Fault-test that handoff on each supported provider.
Do not use "reserve in Entity, reply to HTTP, then start the turn" without a
recoverable launch contract.

| Outcome | Public behavior |
| --- | --- |
| Admitted and launch durably staged | HTTP 202 with session/run/request IDs and authorized relative status links |
| Same logical request/body already accepted | Return the existing receipt/result; no new turn |
| Same idempotency key, different normalized request | HTTP 409 `idempotency_conflict` |
| Another distinct active turn | HTTP 409 `session_busy`; do not queue that rejected attempt |
| Admission acknowledgement times out | Explicit outcome-unknown response and stable retry/status contract; not a false rejection or a new key |
| Quota/capacity exhausted | Bounded, explicit 429/503 with retry guidance before new resource allocation |

Require an HTTP `Idempotency-Key` for turn submission. Session ID and request ID
are different. Normalize transport/retry metadata before comparing request
hashes. Identical business content with different logical request IDs is not
automatically a duplicate.

Admission-attempt receipts and accepted-turn idempotency are distinct: a
request retry must not be permanently pinned to its first busy refusal.
Bound late admission with a trusted deadline and correlate every attempt.
Never infer an outcome from absent/stale state. Accepted-turn deduplication
retention must cover the documented client retry horizon; expiry is an
explicit contract, not indefinite exactly-once processing.

There must be a stable address even when the caller never received a generated
session ID. Define `RequestIdentity = (deployment_namespace, tenant_scope,
agent_slug, submitter_object_id, producer_scope, logical_request_id)`.
`producer_scope` identifies the trusted HTTP producer namespace and leaves room
for later typed ingress adapters without granting them v1 support.
Do not include admission-attempt IDs or volatile delivery metadata.

When no session ID was supplied, derive an opaque, stable session ID from a
length-delimited SHA-256 encoding of `RequestIdentity`, following the existing
workflow identity-helper convention. Keep that derivation and namespace stable
for the supported v1 retry horizon. No secret rotation or secondary lookup
Entity is necessary, and the derived ID is not authentication or encryption.
Retain it in the accepted Entity receipt. Requests supplying an ID repeat it
on retry.

Use a separate admission-attempt ID and native admission-orchestration instance
for each authoritative busy/unknown decision. All attempts consult the same
session Entity and accepted-request receipt. Do not restart a completed
admission instance and replay its old busy answer forever. Concurrent first
requests with one identity resolve to one session/accepted receipt; different
payloads with that same identity conflict rather than generating another ID.

#### Deferred non-HTTP research

Reject `durable: true` on non-HTTP triggered agents in the public v1 surface;
do not silently invoke the ordinary runner. Keep existing non-durable triggers
unchanged. A separate Timer design probe explores session selection, trusted
identity, same-session contention, and the boundary between trigger delivery
and Durable acceptance.

For that probe, normal Timer delivery semantics apply before acceptance; no
missed-tick catch-up is promised. Do not claim a Timer probe proves Queue/
Service Bus acknowledgement, FIFO, poison or dead-letter behavior. Those
contracts, including whether busy non-HTTP work queues or remains source-owned,
are intentionally deferred rather than added to this release's dependency chain.

### 4.6 Turn execution and conversation state

Use one native versioned orchestration per accepted turn. Each turn:

1. Reads its captured immutable definition/model/tool-policy version and the
   authorized session state through Durable operations.
2. Prepares the model context and any tool catalog in bounded activities.
3. Schedules exactly one foreground model request.
4. Records its message/tool-call output in session conversation state through
   an Entity operation before advancing.
5. Schedules each allowed tool invocation as an individual activity.
6. Records the tool results in the model's call order, then schedules the next
   model request or commits the final turn outcome.

Use native Durable time/timer/task APIs. No network, model, credential lookup,
ordinary clock/random calls, file access, or mutable configuration discovery in
the orchestrator.

The session Entity contains versioned ownership/policy references, conversation
messages and their tool-call IDs, active-turn identity, accepted-request
receipts, lifecycle state, and the sandbox binding. Keep operations short.
Separate helper records/indexes are allowed only for a demonstrated need; do
not split metadata and conversation into two authoritative Entities by default.

Conversation history is already ordered. Durable orchestration history owns
execution replay; the Entity owns the conversation across different turns and
orchestration lifetimes. A recorded `call_activity` or `call_entity` result is
reused on replay. Do not implement duplicate checkpoint/replay logic solely to
imitate this guarantee.

"Committed conversation" means an Entity update has been durably acknowledged.
Record observed partial/failed/cancelled turns with explicit state; do not
erase them to make a failed run appear not to have happened. A browser token
delta or an activity's in-memory value is not yet such a commit. Publish a
successful terminal response only after the final conversation/state update.

The stored transcript and the next model's working context are not identical
when a turn is incomplete. Context preparation operates on complete provider
dependency groups: reasoning/assistant items, all referenced tool calls, and
their matching outcomes. Cancellation after committing a model tool request
must not send an unmatched call into the next model request.

Where the qualified provider supports it, append clearly runtime-authored
`not_executed`, `cancelled`, `failed`, or `outcome_unknown` function outcomes
with the original call IDs; they are not fabricated successful tool results.
Keep the original provider items and execution evidence intact. An uncertain
mutating effect still blocks normal continuation until execution is quiescent
and an authorized reconciliation decision is recorded.
If a valid dependency group cannot be reconstructed without inventing content,
fail `context_incomplete` explicitly instead of dropping calls/reasoning or
silently starting a fresh context. Wire tests cover continuation after
cancellation, partial tool completion, result-size failure, and reconciliation.
Use `not_executed` only when dispatch is known not to have occurred; cancellation
of a wait or an unknown activity result is not that evidence.

#### Human input boundary

Ordinary clarification is already supported: the model asks a question,
completes its turn, and the next HTTP message continues the session.
The user wants the second form too: pause an unfinished run for input, then
resume it. The spike genuinely implements this. After clarifying the scope,
the user explicitly deferred tool-action approval modes and enforcement.
An answer supplies information to the agent; it is not a capability grant.

Use the runtime-owned `request_human_input` contract for a question, optional
choices/free-text and response schema. Reserve its name against project-tool
collisions. It is a Durable control operation: question/answer state, waits and
continuation are checkpointed through Entity calls and external events rather
than holding an activity open for a person.

Use the same session Entity for the question, complete accepted answer receipt,
active-turn fence and continuation. A short `input` mode of the shared
orchestrator can atomically accept the answer through `call_entity`, then use
native `send_event` to wake the original run. The receiver revalidates and
acknowledges the Entity receipt. Native event sending is one-way, not proof of
answer consumption; detect accepted-but-unconsumed answers if a run fails.
Do not port the legacy HTTP acceptance-to-outbox-launch gap.

Headless clients read the pending question from the authorized run-status
response and POST an answer to the existing management handler's
`runs/{run_id}/input/{request_id}` action with an idempotency key. Return
accepted only after the Entity commits the complete answer. Exact retries
replay the receipt, conflicting submissions return 409 and expired/closed
questions return 410; session deletion/expiry cannot be bypassed by retry.
UI/SSE remain optional.

Allow one outstanding question per turn. A model step mixing
`request_human_input` with other tools is invalid: dispatch none and report a
clear protocol error rather than performing actions before obtaining the
requested information. Normal tool-only batches still fan out in parallel.
An explicit human-input request consumes one logical tool slot; waiting,
answer delivery and replay do not consume additional model/tool slots.

Human waits use the configured session-TTL interval from question opening,
capped by any explicitly authored turn deadline. Unanswered expiry ends the
waiting run/session without self-renewal. Do not hold an HTTP connection or
worker while waiting. Qualification covers the spike's expiry, reservation,
answer/cancel race and duplicate-reply gaps.

Do not add `approval_mode` authoring, a second MCP approval layer, or a public
approval-policy DSL in this feature. Existing tool/server configuration,
credentials and filtering remain the use-authorization boundary. If an
existing SDK tool already declares a mandatory approval requirement, do not
silently bypass it in the disabled-auto-invocation Durable path: reject that
unsupported combination explicitly rather than advertising v1 approval support.

### 4.7 Foreground model adapter and MAF compatibility

Build a fresh MAF single-step invocation with executable tool dispatch disabled
at both the Agent and client layers. Supply tool schemas/descriptors rather
than live executable wrappers. Disable hidden autonomous tool/harness loops,
provider-hosted tools, and compaction paths that can invoke additional models
outside the orchestration's individual boundaries.

For Responses-compatible providers use foreground execution and `store: false`;
reject background mode/continuation polling. Preserve full supported message
items, including opaque encrypted reasoning, function-call IDs/arguments and
matching outputs. Do not stringify/filter down to assistant text and call it a
lossless checkpoint. `previous_response_id`, provider conversation IDs and
background continuation tokens are not the authoritative session mechanism.

The customer contract remains a supported provider endpoint, model/deployment
and normal credentials. Customers do not write MAF adapters or serialization
workarounds. Reuse the current `ClientManager` integration and qualify the
runtime's dependency combination.

The independent audit reproduced two upstream defects in the older **MAF
OpenAI integration 1.10.2**: encrypted-only reasoning is omitted from the next
request, and summary-bearing reasoning loses its encrypted payload during
parsing. Both are already fixed in integration 1.11.0 through
[microsoft/agent-framework#7233](https://github.com/microsoft/agent-framework/pull/7233).
This is not an unresolved Durable architecture blocker or a reason for a
customer-written adapter/provider fork.

The audit ran 60 offline cases and 111 serialized HTTP requests through actual
SDK `Agent.run` paths, JSON persistence and a fresh-process restore, covering
OpenAI, Azure OpenAI and Foundry. It verified the old failures and preservation
with first-fixed, spike and current release sets; non-reasoning controls passed.
These are mock-transport serialization results, not live provider acceptance,
streaming or Durable recovery qualification.

Select a compatible tested dependency set and keep regression coverage.
Do not blindly upgrade one package: Foundry's later
[microsoft/agent-framework#7536](https://github.com/microsoft/agent-framework/pull/7536)
makes encrypted reasoning explicitly opt-in to avoid sending unsupported
options to non-reasoning deployments. The runtime owns correct supported
provider/model options; upstream MAF owns its generic SDK parsing/serialization.
Carry the synthetic wire fixtures, pinned dependency manifests and
fresh-process regression cases into the corresponding C1/C2 changes.
Reported mock-transport results do not replace deployed qualification.

Capture non-secret model/deployment/API binding and code/schema version for the
turn. Credentials are resolved at execution time and never checkpointed.
Encrypted provider state is not assumed portable across deployments, regions,
providers or upgrades. Incompatible continuation fails explicitly; there is no
silent dropping of encrypted items or provider switching.

Existing MAF context-compaction configuration remains the user-facing concept.
If compaction makes a model call, that call needs its own Durable activity.
Compaction changes the model's working context, not the retained transcript.
Do not silently replace user history-provider behavior or prune customer data.
An unprovable compaction/harness combination is an explicit compatibility gate,
not permission to hide an extra model invocation in a generic activity.

### 4.8 Tool execution and side effects

| Effective session profile | Execution |
| --- | --- |
| No Sandbox Group | Tool code/dispatch runs in a Durable Functions activity; no persistent local workspace or cross-worker file affinity |
| No group, Dynamic Sessions interpreter enabled | The Functions activity calls the existing remote isolated interpreter; pool-managed state, not a runtime-owned durable workspace |
| Sandbox Group, local code/workspace tool | The activity dispatches execution to the bound sandbox; never falls back to local execution on the Functions worker |
| Remote MCP tool | A Durable activity on the Functions worker invokes the remote server, using worker-side permissions/credentials. It does not route through the sandbox |

This applies to project tools, system web requests, skill/resource/script tools,
and outbound MCP calls. Reuse capability filters, validation and SSRF/egress
rules; missing tools must not trigger runtime rediscovery that broadens access.
Tools that cannot execute under the selected packaging/protocol contract are
rejected before model execution with an actionable capability error.

Use DTS **fan-out/fan-in** for the tool calls returned in one model response:
schedule each activity independently, then assemble results by the original
call IDs/order. Result ordering is not an execution dependency. Do not impose
a blanket serial chain or a new default customer parallelism cap.

Calls in a batch are treated as independent. Tell the model to request
genuinely dependent shared-workspace actions in successive steps, not one
parallel batch. Required preparation, such as verifying the sandbox/package
before dispatch, remains a real barrier. The runtime must not pretend it can
infer arbitrary data dependencies from Python code. One active user turn per
session still applies; multiple tool activities within it may run concurrently.

Durable activities are at-least-once, not exactly-once external effects.
Distinguish ordinary replay (recorded results reused) from a crash after a side
effect but before completion was recorded. Expose a stable tool operation ID
derived from the internal session/turn/step/call identity. Reuse existing
workflow task-context/idempotency conventions where possible.

V1 does not expose `durable.tool_policies`, delivery-kind classifications, or
customer reconciliation-handler configuration. Use normal Durable delivery and
make stable operation IDs available to tool authors. External services/tools
remain responsible for idempotency where effects must not repeat; the framework
does not silently promise exactly-once behavior.

Reuse internal sandbox request/result receipts where they can resume an
already-started invocation after a lost response. They do not become a generic
customer policy DSL or a transaction with external services. Do not add another
automatic application retry loop around an unknown effect, or claim a timed-out
command stopped. Surface uncertain outcomes accurately.

### 4.9 Sandbox Group lifecycle and packaging

The customer provisions the group; the runtime creates session sandboxes, not
customer groups or broad cloud roles at request time. Use the supported SDK
behind a narrow adapter, managed identity, least-privilege group-scoped RBAC,
and operator-owned resource/region bindings.

Persist the group binding, sandbox ID, internal session incarnation, package and
policy digests, and lifecycle state. Allocate from a clean approved image/disk;
do not share a mutable customer disk/volume across different sessions.
Package tools/skills and dependencies deterministically with an immutable
manifest. No pickled Python closures, runtime downloads of arbitrary code, or
all-environment-variable forwarding. Validate the manifest before execution.

#### Customer base selection

Expose optional `disk` or `disk_id` on `sandbox_group`, using the actual SDK
source concepts rather than inventing an arbitrary OCI-image field. They are
mutually exclusive and override the inherited source as a unit. The normal
default is a public Python 3.13/3.14 disk matching the qualified Functions/
package ABI, with Python 3.13 as the default supported choice. The spike's
`resolve_sandbox_create_source()` already selects `disk="python-3.<minor>"`;
pass the chosen base explicitly and qualify the applicable profile. Round 2
executed public `python-3.13`; Python 3.14 has catalog evidence only, not guest
or native-dependency compatibility proof.

```yaml
durable:
  enabled: true
  sandbox_group:
    resource_id: $SANDBOX_GROUP_RESOURCE_ID
    region: $SANDBOX_GROUP_REGION
    disk: python-3.13   # optional public base override
    # Alternatively: disk_id: $APPROVED_CUSTOM_DISK_ID
```

A custom base must satisfy the same Python/OS/native-wheel and bootstrap
contract. Fail setup explicitly on an incompatible ABI. A source disk is a
clean base for a new session sandbox, not permission to attach one mutable
customer data disk to multiple sessions.

#### Concrete package and invocation transport

1. Produce one deterministic, versioned tool bundle from the approved
   deployment artifact: tools, skills, required application modules/assets,
   and compatible Python dependencies. Keep environment files, local settings,
   credentials, caches and repository metadata out. Do not zip an arbitrary
   writable project tree or copy Windows native wheels into a Linux sandbox.
2. A checkpointed sandbox-preparation activity obtains that immutable artifact.
   Keep only digest/version/manifest references in orchestration state, not a
   code archive or credential-bearing URL.
3. The Functions worker uses the authenticated Sandbox SDK file transport to
   upload the archive, digest and manifest seed to the already-bound sandbox.
   This is code delivery through `write_file`, not a public HTTP upload endpoint
   in the guest and not a new customer-deployed MCP server.
4. Deliver the small runtime bootstrap and release readiness only after the
   required artifacts are present. The bootstrap verifies SHA-256, archive
   paths/size, Python/OS/wheel ABI and the session/group binding, extracts into a
   staging directory, atomically activates it, and publishes its ready manifest.
5. The controller verifies that handshake before tool execution. Reuse the
   installed matching bundle on later turns. Partial upload, missing artifacts,
   or mismatched content cannot be treated as ready.
6. Each tool activity uploads a structured request containing operation ID,
   tool name, normalized arguments and request hash to a per-call path. Invoke
   a fixed one-shot runner through the SDK process API with only validated
   runtime-generated paths/IDs in its command. The runner reads arguments from
   JSON, dispatches an allowed catalog tool, and atomically publishes that
   call's result file. Never interpolate model arguments into a shell command
   or module import.
7. Correlate the structured result with that request, validate it, and return
   it as the Durable activity result. The sandbox remains session-bound.
   Reattachment reuses its verified package; loss does not trigger recreation.

Existing concrete prior art is `controller/package.py:deliver_content_package`,
`controller/bootstrap_delivery.py:deliver_content_and_bootstrap`,
`harness/bootstrap.py:prepare_sandbox`, and the
`transport/aca_sdk.py` file/process adapter. The current spike uses a byte-array
archive, size verification followed by guest digest verification, safe staged
extraction, `.python_packages` import paths, and an invocation result lookup.
Port those bounded responsibilities, not the entire hybrid controller or an
unreviewed whole-script-root capture policy.

The spike's `experimental/hybrid_tools.py:InvocationSandboxLease.invoke`
currently holds `_queue_lock` while writing a request JSON file and polling a
result JSON file, and clamps execution to `_MAX_TOOL_SECONDS`. Those are
precisely the serial transport/fixed-timeout assumptions not to port blindly.
Use independent call paths and independently running executor processes for
parallel batches; preserve the request-hash/result correlation. Qualify actual
execution overlap inside the sandbox, not merely parallel scheduling in DTS.
If the service cannot support that contract, return to design review rather
than advertising parallel sandbox execution backed by a hidden serial queue.

#### Observed bounded transport result

A direct-SDK synthetic probe using `azure-containerapps-sandbox==0.1.0b4` and
`azure-core==1.35.1` passed on one explicit public Python 3.13 base. The guest
reported CPython 3.13, Linux x86_64, 64-bit, GIL enabled and SOABI
`cpython-313-x86_64-linux-gnu`. Four execs and 20,391 uploaded bytes covered
ABI probing, verified bundle/manifest activation and two independent guest
processes with 3,392,168 ns of measured interval overlap.

The same sandbox ID retained its marker/content across Memory suspend/resume.
The driver then deleted only that owned resource and verified a subsequent
404; the create count remained one, with no replacement. Total measured time
including cleanup was 21.565105 seconds. This proves the recorded resource
lookup absence, not physical-media or service-backup erasure.

The caller used Azure CLI user identity, not managed identity. The case used
one synthetic stdlib bundle, not the integrated Functions/Durable path or a
representative native-dependency package. Restricted-identity negatives were
not authorized/executed; private paths, Python 3.14 execution, long-running
service limits and actual billing remain unproved. Do not repeat the primitive
probe as a substitute for those specific production-profile gates.

#### Remaining lifecycle and isolation requirements

Carry the successful cases into product integration and add the remaining
interrupted/ambiguous upload, digest mismatch, archive traversal,
native ABI mismatch, lost invoke response, parallel invocations, worker
reattachment, and attempts by untrusted tool code to tamper with runtime
control records. A ready manifest is a compatibility check, not an attestation
that arbitrary guest code is trustworthy. Control-plane credentials and remote
MCP credentials stay in the Functions worker.

Use a stable provisioning operation label and reconciliation for a lost create
response. Labels aid lookup, not authorization or proof of service-side
idempotency. Multiple matching sandboxes or an unknown create outcome require
reconciliation, not another blind allocation.

Proposed states:

```text
unallocated -> allocating -> ready <-> suspended
                    |          |           |
                    +------ unavailable ---+
                               |
                       workspace_lost

ready/suspended/unavailable -> deleting -> deleted
```

Transient unavailability does not change the binding. Resume/attach only the
same sandbox ID after owner/group/manifest checks. Permanent deletion, expired
workspace, or unrecoverable storage loss produces `workspace_lost`; require an
explicit new session. No automatic `_attach_or_recreate()` path.

Promise persistent workspace files only within the qualified service lifecycle.
Do not promise an immortal process, REPL variables, or a particular Functions
worker. Sandbox SDK auto-suspend is not an absolute sandbox lifetime. Service
quotas, stop/resume behavior, private control-plane reachability and maximum
lifetime must be confirmed for the target profile.
Align the sandbox's supported retention policy with the session's idle TTL;
provider auto-delete settings are not assumed to renew themselves. Qualify
renewal and failure handling without weakening same-sandbox affinity.

No host/control-plane credentials are injected into untrusted tool code.
Outbound auth uses narrowly scoped mechanisms appropriate to the tool. Keep
model traffic and the Functions control plane out of sandbox credentials.
Enforce allowed egress and path roots, bound exec/output/package sizes, and
preserve exact per-session isolation for files, disks, snapshots and indexes.

### 4.10 Native Entity offload, size and context budgets

Preferred representation: ordinary versioned Entity state including conversation
messages. Native host/provider offload determines physical storage. The runtime
does not maintain a competing Blob-reference transcript by default.

Source findings:

| Layer | Finding |
| --- | --- |
| Native Python b3 | Entity state/input/output travel through host-delivered Base64/protobuf and JSON conversion; no standalone Python Blob store is registered |
| AzureManaged host/backend 1.10.0 | Published implementation externalizes/hydrates Entity state, operation input/output, and query results |
| Azure Storage provider | Has a separate preexisting large-message/property offload mechanism |
| Public support docs | Large-payload support remains Preview; Functions-specific documented language support does not by itself certify native Python |

Thus native offload is a credible path, not a claim that installing b3 alone
activates or qualifies it. Record exact transitive SDK, Functions host, resolved
bundle/extension, provider, and payload-library versions. Do not mix standalone
Python `payload_store` examples into the Functions app and assume activation.

#### Empirical result before design finalization

The requested native experiment now **passed** on a protected local Windows
Functions host using live DTS/Blob Storage: 1,573,127 bytes of Entity state
were externally stored as a native 1,184,890-byte gzip Blob and hydrated with
matching checksums through warm/cold typed reads, queries and an Entity
operation after a real worker restart. Large Entity operation input/output
and activity input/output passed as separate cases.

The actual loaded stack was Core Tools 4.13.0, host 4.1051.300.26316, bundle
4.38.1, Durable extension 3.14.0, AzureManaged host/backend/adapter 1.10.0,
payload library 1.24.2, Python 3.13.15, native Durable b3 and durabletask
1.10.0. No application-managed conversation Blob references or standalone
Python payload store were used.

This resolves the primitive viability question, not every production gate.
Scale, thresholds/caps/batches, failure windows, deployed-host parity,
managed identity/private networking, support commitment and lifecycle still
need qualification. All nine native payload blobs survived Entity deletion
and orchestration purge until explicit probe cleanup. A private synthetic
container and fixture hashes made that cleanup provable; they are not a
production shared-container/session-erasure design.

For the inspected AzureManaged host, relevant settings include
`payloadStorageEnabled`, `payloadStorageThresholdBytes`, and
`payloadStorageMaxSizeBytes` under `extensions.durableTask.storageProvider`.
Its payload account is resolved from `AzureWebJobsStorage`; the inspected
identity path uses `__accountName`, not an assumed `__blobServiceUri`-only
configuration. These settings must be validated against the actual deployed
host, not duplicated into agent YAML.

| Boundary | Evidence and interpretation |
| --- | --- |
| DTS individual logical state/payload | Approximately 1 MiB without offload; not a universal Entity limit across providers |
| AzureManaged common library 1.24.2 | Default threshold 900,000 serialized UTF-8 bytes; default offloaded payload cap 10 MiB |
| Standalone Python Blob store | Different defaults, including 256 KiB threshold; not a Functions configuration contract |
| Azure Storage implementation | About 45 KiB queue-message and 60 KiB selected table-property offload thresholds; different encoding measurements |
| Native Python/host transport | Aggregate/protobuf/Base64/gRPC limits still apply; a 4 MiB client setting is not raised by Blob offload |
| Application/model | Bound serialized state, receipts, requests/results, token context and replay memory independently; do not introduce a hidden default call-count ceiling |

#### Library bounds versus native management transport

Round 2 reproduced 54 offline checks: 11 Python checks, 16 against the actual
pinned common C# library, and 27 native-probe preparation checks. Nine
exact-size fixtures exercise UTF-8 serialization and envelope overhead.
These are not native-host commit, failure-window or garbage-collection proof;
the optional new native boundary case was not run.

Under the explicit tested settings, the common library offloads at **greater
than or equal to 262,144 serialized UTF-8 bytes** and rejects **greater than
10,485,760 bytes**, before compression/upload. The installed common default
threshold is separately 900,000 bytes. Standalone Python uses a different
threshold-equality comparison and is not a substitute for this native path.

S0's native management gRPC profile was **4,194,304 bytes**. A minimal real
`GetEntityResponse` containing an exact-cap state serialized to **10,485,793
bytes** offline, before additional real-world metadata. A provider's per-field
offload cap therefore does not guarantee a hydrated full-state management
read fits its independent client channel.

The existing deployment option
`extensions.durableTask.maxGrpcMessageSizeInBytes` is documented outside
`storageProvider`; native b3 source maps a positive value to its client send
and receive limits. The published support table explicitly names .NET-isolated
and Java, so native Python support still needs owner confirmation and a tested
profile. No value was raised or selected by this investigation. Do not add a
parallel agent-author knob or treat zero as a qualified unlimited setting.

An Entity operation can return a bounded digest/status/selected view when
that is what the caller requires. It still materializes the full state and may
produce another native snapshot. Client-side slicing after a full read cannot
avoid the transport limit, and a digest/projection cannot silently replace
required conversation history. Paging needs explicit consistency and response
bounds. Neither projection nor tuning resolves native payload lifecycle.

Measure actual serialized UTF-8 state and complete envelopes, including metadata
and Base64 expansion where applicable. Set a qualified application state cap
with room for admission/cancellation/deletion/error metadata. Do not adopt the
spike's 32 MiB cap or a nominal 1 MiB threshold as the public limit.

Test >1 MiB low-compressibility state on a cold worker, large operation input
and output independently, query hydration, activity input/output, and aggregate
batches. The configured application cap is finalized from these results before
release; missing qualification is a failed production readiness check.

Offload does not compact model context, make state paginated, or remove
full-state serialization/I/O cost. `ContinueAsNew` resets orchestration history,
not Entity conversation history. Use it only at an acknowledged safe boundary
with no unfinished tools and an explicit pending-event policy. Native b3
versioned continuation is useful, but not permission to make incompatible
replay edits.

No offload failure, capacity limit, or compaction path silently deletes the
oldest conversation. Reject growth explicitly while preserving existing state,
or expire/delete under the previously configured lifecycle policy.

### 4.11 Deadlines, cancellation and recovery

| Concept | Contract |
| --- | --- |
| Orchestration lifetime | Logical durable execution may outlive a worker; no requirement for a worker to remain alive |
| Turn deadline | An explicitly configured turn `timeout`, if any; do not introduce the ordinary runner's implicit 900-second fallback into Durable mode |
| Model/tool deadline | Unset by default; apply only when authored, and respect any configured turn deadline plus platform/provider constraints |
| Function timeout | Host setting, not replaced by the above; Flex/Premium/Dedicated default 30 minutes and can allow unbounded execution subject to interruption |
| HTTP wait | Acceptance/status architecture; never depend on an initiating request surviving the documented 230-second response limit |
| Session/workspace lifetime | Separate configured expiry and provider availability, not `functionTimeout` |

Legacy Linux Consumption is not an advertised Python >=3.13 target. Qualify
Flex Consumption and/or Premium/Dedicated separately; Always On is required
where applicable. "Unbounded timeout" is not a guarantee against scale-in,
platform updates, crashes or deployment interruption. Entity operations also
consume invocation/batch budgets; keep them free of long work.

Cancellation is an acknowledged intent, not rollback or proof of remote
termination. Stop scheduling new model/tool steps; cancel/stop remote work
where the adapter supports a confirmed operation. Preserve late results and
uncertain-effect evidence. Do not release the session for new mutating work
until old effects are complete, fenced by a supported target, or explicitly
quarantined/reconciled.

Use native Durable retries for the approved retry policy; account for SDK
transport retries too. Retry state and policy version are captured in the turn.
Entity fences protect admission/late completion, not external transactions.
An independent lifecycle reconciler handles hard termination/crashes that
bypass an orchestration's cleanup code.

Disabling/removing the last durable agent must not strand idle conversations
or cleanup work. Drain through deployment controls rather than a new customer
front-matter field: disable the generated admission functions using the
platform's per-function disable/routing controls, while leaving native
orchestrations/activities/Entities and management handlers available. Publish
the generated admission-function names for this operational step. Preserve
retiring agent definitions and lifecycle work until drainage is complete.

Deployment readiness must reject switching/removing the runtime or retiring
those definitions until active turns, idle retained sessions, sandbox bindings,
deferred intake and pending erasure jobs are drained under policy. `durable:
false` is not a cleanup command. After complete drainage it can return the
agent to ordinary behavior; a fresh opt-out with no durable state remains
valid. Add removal/disablement tests, not just rolling-upgrade tests.

Version instance/state/tool/package contracts. Preserve runnable old versions
while draining active turns; do not reuse task hubs across incompatible
deployments. A new policy may revoke execution; a new deployment must not
silently replay old work against different tools, instructions or models.
Private spike histories are not automatically migrated or resumed.

### 4.12 Retention, deletion, privacy and operations

The approved deletion contract is:

1. Atomically revoke session access and new admissions.
2. Stop/quarantine active work so late operations cannot recreate deleted data.
3. Reconcile/delete the sandbox and associated session-owned workspace resources.
4. Delete Entity conversation content and relevant run/history/index/UI records.
5. Erase native offloaded content, superseded copies and orphans within the
   configured window, accounting for soft deletion, versions and backups.
6. Retain only policy-approved non-content audit/tombstone evidence; expose
   deletion progress without exposing another owner's data.

Deleting an Entity or purging an orchestration does not automatically erase DTS
payload blobs. The inspected store uses generated Blob references and exposes
no automatic per-session deletion/GC API. This is a **release-blocking platform
lifecycle requirement**. Choose a supported upstream lifecycle hook or a
platform-approved, demonstrated storage lifecycle strategy before claiming the
window. Do not parse private backend internals or apply age-only deletion that
can remove live/replayable references. If no strategy meets the window, stop
the release and revise the FRD with the user; do not silently substitute custom
storage or weaken the promise.

The follow-up public-API and library investigation evaluated one minimal
candidate: application revocation/native state cleanup followed by an age-based
Blob lifecycle rule. Executable counterexamples showed that this cannot
distinguish live references from orphans, enumerate superseded/uncommitted
uploads, fence native late writers, protect restore references or guarantee a
bounded collection receipt. The candidate is rejected; no such rule is deployed.

For the inspected versions, the missing platform contract must cover complete
session-to-payload ownership, authoritative commit/reachability lifetime,
native-writer fencing plus erasure completion, and coordinated restore/retention.
This is a bounded finding about the inspected public surfaces, not a claim
about every private platform capability or future release. Obtain the supported
owner contract rather than inventing an API or guessing from Blob listings.

The lifecycle gate includes **late native writers**, not just Entity updates.
A running activity can return after revocation and cause host-managed history/
payload writes. The runtime lifecycle owner and Functions/provider owners must
establish a supported quiescence barrier or bounded residual-write horizon
covering activities, SDK retries, queued completions and remote commands.
A terminal orchestration status alone is not assumed to prove all writers
stopped.

Validate `erasure_window_seconds` against that bound plus the qualified cleanup
budget. If no finite bound/barrier is supported, the production profile is not
ready. Azure's ability to configure an unbounded `functionTimeout` does not
override this requirement. Report physical deletion complete only after the
barrier/bound, final payload sweep and storage-retention checks; a successful
early sweep or Entity deletion is not enough.

Align session expiry, retry/idempotency horizons, orchestration retention,
payload retention, Blob soft-delete/version retention, sandbox snapshots and
audit retention. Restore procedures cover both durable state and external
payload storage/configuration. A task-hub-only export is not a proven full
session backup.

Use separate least-privilege managed identities/scopes where needed. Qualify
private connectivity independently for DTS, Blob, models, MCP services and
Sandbox control/data paths; a single private endpoint is not an end-to-end
private network guarantee. Keep customer content out of default logs, traces,
custom status, labels, exception messages and metric dimensions. Never
checkpoint credentials or a whole environment/configuration object.

`store: false` and encrypted reasoning are not blanket zero-retention or
compliance claims. Document all storage/processors, region boundaries, access
roles and retention behavior. Treat prompts/tool results as untrusted content;
tool authorization and egress restrictions are not delegated to the model.

Operational readiness includes content-free counts/latency/outcomes for
admission, retries, activity failures, throttling, native offload failures,
state-size pressure, stuck turns, sandbox loss, deletion backlog and erasure
deadline breaches. Avoid unbounded per-session metric cardinality. Capture
actual model attempts/usage without double-counting replay.

### 4.13 Public API and Durable ChatUI

**Optional, not a core v1 release gate:** the user explicitly classified UI
and SSE as good-to-have. Preserve an extensible API boundary without making
core durability depend on an observation journal. The source review found
foreground streaming feasible and existing spike code reusable; live
streaming/reconnect/retention behavior remains to be qualified.

Preserve ordinary routes for non-durable agents. For enabled agents, existing
`POST /agents/{slug}/chat` and custom HTTP triggers submit a durable turn and
return its acceptance/receipt contract, not a long-held synchronous model
response. Document that opt-in response-shape change.

Proposed additional authenticated surfaces:

| Surface | Purpose |
| --- | --- |
| `GET /agents/{slug}/runs/{run_id}` | Bounded authorized run status/final result |
| `POST /agents/{slug}/runs/{run_id}/cancel` | Idempotent cooperative cancellation |
| `POST /agents/{slug}/runs/{run_id}/input/{request_id}` | Owner-authorized, idempotent answer to the pending question in the existing run |
| `GET /agents/{slug}/sessions` | Authorized paginated session discovery for clients without a browser-local index |
| `GET /agents/{slug}/sessions/{session_id}/history` | Authorized paginated conversation, not raw orchestration history |
| `DELETE /agents/{slug}/sessions/{session_id}` | Immediate access revocation plus asynchronous deletion receipt |
| `GET /agents/{slug}/runs/{run_id}/events` | Authenticated run-scoped SSE stream: progress, provisional foreground deltas when supported, and committed final outcome |

Return `session_id` and `x-ms-session-id` consistently. Keep status links
relative and runtime-owned. Do not register the old direct-run `chatstream` or
MCP handler for a durable agent accidentally. A Durable-compatible chatstream
adapter can submit then follow the run; otherwise expose a clear unsupported
surface until that adapter ships.

The runtime generates these static HTTP-trigger handlers in the same
Function App. Customers do not implement them or deploy another app. A run ID
is a route parameter, not a new function registration. Reuse common auth,
request parsing and routing helpers rather than duplicating them per action.

The event endpoint subscribes to an already-started run. A model activity
publishes foreground-stream observations; Durable does not automatically
turn an activity into token streaming. Progress/final-result streaming still
works when a provider does not produce token deltas. Disconnect/reconnect
does not cancel or restart execution, and reading a stream does not renew
the idle TTL.

#### Polling-only ChatUI: no streaming journal

SSE is not required for chat. In the polling-only path:

1. The UI submits to the existing chat/custom HTTP entry point and receives
   202 plus a run ID/status link.
2. It polls the authorized run-status API with bounded backoff. This reads
   Durable state; it does not poll the model provider or issue another model
   call.
3. It renders coarse run/step progress and committed responses/history.
   A `waiting_for_input` response includes the saved question; the UI submits
   the answer through the management API and continues polling.
4. Reload/another client uses session discovery and Entity-backed history,
   rather than requiring browser-local transcripts.

No token-observation journal/container/publisher is needed in this mode.
There is no token-by-token typing display or promise to retain every
intermediate progress event. Adapting the spike UI requires a status/history
transport adapter; it is not merely disabling its current SSE reader while
leaving a journal-dependent implementation unchanged.

Native large-Entity offload and normal Functions host storage can still use
Blob Storage. Those are independent of the optional streaming journal, so
"no SSE journal" does not mean "no Blob Storage anywhere".

The user confirmed that "Durable ChatUI" means the UI on
`larohra/durable-loop-chat-ui`, not the DTS management dashboard.

| Option | Advantages | Costs/risks | Recommendation |
| --- | --- | --- | --- |
| Reuse spike UI unchanged | Existing hosted UI, authenticated server replay and visible history | Private contract/native-version coupling; browser-owned session/request index; new lifecycle/API alignment needed | Not the final public contract |
| Adapt the existing durable UI | Retains working server replay, async run/cancel/reconnect and rendering safeguards | Wire the new Entity/session APIs and verify cross-client restore, auth, retention and failure states | Preferred optional UI slice |
| Extend classic synchronous chat UI in place | Fewer visible assets | Conflates synchronous and durable lifecycle; risks regressing ordinary chat | Do not make it the core delivery dependency |

Keep the UI optional via existing `builtin_endpoints.debug_chat_ui`. The
backend must work for HTTP/API customers without it. Use the server's
Entity history as authority; browser storage is an optional bounded cache, not
the transcript source. Do not persist keys, credentials or sensitive prompts
by default in IndexedDB/localStorage.

This does not mean the current UI lacks server-provided history. Source
inspection shows that `durable_chat_http.py:get_events` serves authenticated
server journal replay/final outcomes. Separately,
`history.js:openDurableChatHistory` opens IndexedDB and `app.js:_loadHistory`
loads the saved session/request index and transcripts from it.

The incremental requirement is recovery of authorized retained sessions and
conversation from another client or after browser storage is cleared. Reuse
the existing UI and server replay; add the Entity-backed session discovery/
history adapter where needed, not a second transcript store or a wholesale
UI rewrite. A small owner-scoped index may support discovery; it is not a
second authority for conversation content.

Runtime status polling or SSE reconnect is allowed; **provider background
model polling is not**. If token deltas are included, mark them provisional
and identify attempt/epoch so a retry can replace a draft rather than duplicate
text. Loss of the observation stream must not retry a model/tool or block
cleanup. A UI journal is not a Durable execution checkpoint. Poll/status-first
UI may ship before optional token streaming, with its supported behavior clear.

#### Observation publication and cleanup

Only enabled observation paths publish streaming data. The model activity
batches user-visible provisional text; tool activities publish bounded
progress summaries rather than dumping credentials/arguments. The SSE reader
can run on another worker. The deterministic orchestrator does not perform
Blob I/O, and committed final status is reconciled independently of journal
publication. A publisher failure degrades observation delivery, not execution.

Use an application-owned private namespace by run/attempt, with correlated
chunk IDs, a bounded manifest/cursor and replacement snapshots. Do not create
a Blob per token. On termination, expiry or deletion, close/fence producers,
retain the permitted reconnect window, then remove only that run's batches,
snapshots and manifests. Account for orphaned/late writes and storage
versions/soft deletion. Stream reads never renew session idle TTL, and deleting
observations must not delete the committed conversation.

This cleanup has application-owned names/ownership evidence. It does not solve
the separate native-offload GUID-blob attribution/erasure problem. The two
stores must not be conflated.

Entities are a viable alternative for coarse observations. A separate
run-observation Entity can store a bounded projection, but token-frequency
updates serialize operations and add whole-state/read-write overhead; readers
still need HTTP delivery. Do not append streaming tokens to the authoritative
conversation Entity and contend with admission/history. Prefer the existing
batched Blob journal for optional text streaming; progress/final-only delivery
can use existing durable status and Entity results without a token journal.

#### Proposed exact Function App footprint

The recommended simplification has **five feature-owned registrations**, shared
across durable agents. Fold control responsibilities into the registered
orchestrator and model/tool/maintenance operations into one typed execution
activity; keep the implementations in clear internal modules. Native b3 adds **two SDK
registrations** unconditionally, for a **seven-function baseline** in a new
durable-only app with built-in chat. This consolidation remains a design
proposal awaiting human confirmation, not a shipped implementation.
The real Python worker indexed a seven-definition prototype in Round 2, but
host external configuration startup failed before readiness or
any Durable submission. Indexing does not qualify routing, authentication,
human input, drain behavior or client lifetime. The two SDK entries were also
observed independently and verified against the pinned constructor source.

| Registration | Trigger | Responsibility |
| --- | --- | --- |
| `agents_durable_submit_v1` | HTTP | Shared built-in chat admission; independently disableable while draining |
| `agents_durable_management_v1` | HTTP | Typed management, SSE, UI shell and allowlisted asset handlers |
| `agents_durable_orchestrator_v1` | Orchestration | Typed admission, turn and lifecycle workflows in independent instances |
| `agents_durable_state_v1` | Entity | Disjoint session and owner-index state kinds/key namespaces |
| `agents_durable_execute_v1` | Activity | One typed model, tool, resource/package, or storage-maintenance operation |
| `BuiltIn__HttpActivity` | Activity | Native b3 SDK registration |
| `BuiltIn__HttpPollOrchestrator` | Orchestration | Native b3 SDK registration |

The two SDK helpers are **outbound HTTP client machinery**, not HTTP-trigger
API handlers. `BuiltIn__HttpActivity` makes a request for `context.call_http`;
`BuiltIn__HttpPollOrchestrator` handles a 202/Location response by waiting and
polling durably. They cannot serve incoming chat/status/history requests.
Their presence does not expose another pair of public HTTP routes.

Reuse normal Durable client operations inside the thin authenticated
management facade rather than reimplementing status/event/purge behavior.
The extension's own management HTTP endpoints are a separate surface; raw
management/system-key URLs do not implement this runtime's tenant/session
ownership contract and must not be substituted as public agent APIs.
Where an authored HTTP/chat entry already exists, adapt it rather than add
another duplicate submission endpoint.

Totals including SDK entries: **2 HTTP + 2 orchestration + 2 activity + 1 Entity = 7**.
The SDK polling helper is not our provider-background-polling path; model
inference remains foreground. Do not strip SDK registrations to make the
inventory look smaller. The SDK deduplicates reserved built-ins when merging
blueprints, so they are counted once, not per agent or blueprint.

The orchestrator explicitly dispatches short admission, turn and lifecycle
instances; an admission does not wait behind an idle-TTL instance. The
execution activity dispatches typed internal operations, not an entire agent
loop. The orchestrator calls it separately for each model step, each parallel
tool call, and each maintenance operation. Sharing a registered function does
not combine checkpoints or serialize those calls. Model arguments cannot
select an internal model/maintenance operation.
Every actual tool/model call remains individually scheduled/checkpointed.
Separate model/tool modules and operation-specific tracing/display tags retain
clarity; separate activity registrations were a diagnostic/versioning
convenience, not a durability requirement.
Native Functions Python display-tag support still needs platform clarification
and live dashboard evidence; source wiring alone is not that support claim.
The state registration has strict disjoint state kinds; its index does not
own transcript content. Separate registrations would need a revised count.

| Configuration | Total feature + SDK registrations |
| --- | ---: |
| One or many durable agents, built-in chat | 7 |
| Add qualified SSE, optional UI or Sandbox Group | +0 each |
| Add C custom HTTP ingress declarations | +C |
| Custom-ingress-only, no built-in chat | 6 + C |
| Earlier separate model-activity alternative | 8 with built-in chat |
| Earlier separate Control/Maintenance alternative | 10 with built-in chat |

An existing custom HTTP function is reused, not duplicated; `+C` describes
the total inventory rather than necessarily an incremental addition.
Existing native workflow apps already include the SDK's two built-ins.
Their shared workflow engine contributes three registrations separately.
Retained old function versions during upgrades must also be counted.

For the consolidated proposal: `F = O + 2I + 3W + D(4 + B) + C`, where O is unchanged
ordinary registrations excluding these SDK/shared engines, I means the app
uses native DFApp, W means an existing Dynamic Workflow engine is registered,
D means the Durable Agent Loop engine is present, B means built-in durable
chat is exposed, and C is custom durable HTTP ingress declarations.

There are **two registered HTTP route patterns**:
`agents/{slug}/chat` (POST) and `agents/{slug}/{*path}` (GET/POST/DELETE).
They implement seven core logical method/path families, eight with SSE,
nine with a polling-only UI, or ten with both SSE and UI. The UI additions
are its shell and `ui/assets/{asset}`. The human-input POST reuses
management; its question is included in authorized run status. Routes are parameterized, not
registered separately for every run/session. Management must reject admission
paths, including when the submit function is disabled.

A single HTTP router is a viable alternative, reducing the built-in-chat
baseline from seven to six registrations. It would require explicit route-level
admission gating so a drain does not disable status, input, cancellation or
deletion. The two-handler layout is recommended for independently disabling
admission through platform controls, not because Durable requires separate
HTTP functions or because their count establishes a security boundary.

JSON handlers use request-scoped clients, SSE creates/disposes its own client
inside the response generator, and static asset handling creates no Durable
client. All durable v1 data routes use enforced Entra authentication, with
agent-specific allowlists/ownership checked inside the shared router. Preserve
ordinary/custom route precedence and fail startup on conflicting reserved paths.

`durable_client` inputs and HTTP outputs are bindings, not new functions.
Durable timers are not Timer-trigger functions. Extension-owned management
routes are distinct from these user-app registrations.

Add `app.get_functions()` assertions for exact names, trigger kinds, methods,
route patterns, auth and once-only counts across zero/one/many agents and all
optional combinations. Real-host tests must cover mixed JSON/SSE/static
responses, generator lifetime, wildcard/empty/trailing/encoded paths,
authorization, route precedence and disabling admission without losing cleanup.

SDK evidence:
[`Blueprint.__init__` and built-in registration](https://github.com/microsoft/durabletask-python/blob/46602d5221591b6aaeff1238cd0ec952419e2e29/azure-functions-durable/azure/durable_functions/decorators/durable_app.py#L37-L70).

### 4.14 Delivery sequence

**Use a separate FRD review PR and a short implementation stack into
`feature/durable-agent-loop`.** The integration branch starts from `main` and
collects reviewed feature increments without exposing unfinished behavior on
`main`. Speed comes from overlapping implementation, review and qualification,
not from omitting the architecture gate or merging incomplete behavior.

| PR / lane | Outcome and base | Review boundary |
| --- | --- | --- |
| **D0 - FRD only** | This document and its index entry, targeting `feature/durable-agent-loop`; no runtime/dependency changes | Open for architecture review with status In review; resolve decisions and record human sign-off before product implementation |
| **C1 - Runtime baseline** | Native b3/compatible MAF dependency integration and existing-behavior regression coverage, based on `feature/durable-agent-loop` | Useful independently; no dead public Durable flag or provider fork |
| **C2 - Durable HTTP core** | Adapt the spike's model/turn/session/HTTP primitives into the complete native config-to-execution slice, with a working Durable qualification fixture; based on C1 | Shared registrations, ownership/admission, foreground calls, parallel worker/MCP tools, paused input, TTL and approved lifecycle all carry their own tests/docs; no false-success deletion stub |
| **C3 - Sandbox workspaces** | Reuse selected packaging/bootstrap/SDK transport code and tests; deliver parallel local execution, same-ID resume/loss/cleanup and current-checkout smoke adaptation; based on C2 | Optional workspace capability is complete; worker-side MCP and existing interpreter behavior stay explicit |
| **C4 - Integrated release qualification** | Adapt the existing ACA deployed pipeline around the C2/C3 fixtures; integrate cross-layer qualification, operator runbooks and support matrix; based on C3 | Not a parking place for missing safety or tests that belong to C1-C3; required release checks cannot silently remain advisory |
| **R - Promotion to main** | Separate PR from the reviewed and qualified `feature/durable-agent-loop` branch into `main` | Final integration/support/release gates and explicit approval; not opened or merged merely because the integration branch exists |
| **Q - Qualification/decision lane** | Carry forward the closed bounded A/B/C evidence, source-reuse inventory, pipeline adaptation preparation and remaining platform-owner questions | Runs alongside FRD review and, after approval, implementation; any new live experiment needs fresh scoped approval |

The unmerged implementation chain is
`feature/durable-agent-loop <- C1 <- C2 <- C3 <- C4`; D0 remains a separate
documentation review into the integration branch. Each code PR targets the
branch immediately below it so its diff shows only that layer. Reviewed
increments can merge into the integration branch before the whole feature is
ready for `main`; this does not waive any layer's tests, docs or safety contract.

Start C1 explicitly from a current integration-branch snapshot, not the
project's default `main`. If a lower dependency has already landed, use the
fresh integration tip containing it. Keep the integration branch synchronized
with `main` through deliberate reviewed updates and propagate necessary changes
through owning layer sessions; do not rewrite another session's branch.

The repository's public-build and E2E PR filters include `feature/*`, but
retargeting does not itself prove checks ran or make them required. Branch
protections and required checks are a separate repository-owner decision,
not inherited automatically from `main`. Do not broaden cloud credentials,
privileged pipeline permissions or release/publishing triggers as a shortcut.
Only promote the integration branch to `main` after the complete feature meets
the agreed gates; status Implemented follows that final promotion, not an
intermediate merge into the feature branch.

Do not wait for a code PR to merge before starting the next layer. Once a lower
layer has a coherent buildable commit pushed, create the next layer from that
recorded commit/branch. Lower-layer review and CI continue while upper-layer
implementation proceeds. Do not create a dependent worktree from uncommitted
or unpushed work, and do not create every stack layer from `main`.

One session/worktree owns each PR, its commits, pushes, rebases and conflict
resolution. Open each PR through the app-native flow. Register native GitHub
stack metadata after the PRs exist and their bases/heads are revalidated; if
native stacks cannot be used (for example, cross-fork heads), preserve the
ordinary dependent-PR chain. Synchronize changes bottom-up in owning sessions,
without coordinator force-pushes or recreating partially merged stacks.

Parallelize independent qualification fixtures, reviews, documentation/example
preparation and SDK/Sandbox research against the agreed contracts. Keep
`config/schema.py`, `app.py`, persisted contracts and shared registration edits
under their owning layer to avoid multiple agents independently inventing
incompatible APIs. Parallel preparation is not permission to author a higher
stack layer before its actual parent is committed and pushed.

Keep tests and directly affected/generated docs with each behavior, not in a
later catch-all PR. Every layer stays buildable and safe on its own. If a
proposed cut creates a broken or misleading intermediate API, keep those
changes together rather than split mechanically. Freeze the cross-layer
contracts early; review fixes rebase through the stack before final integration.

UI/SSE and Timer/non-HTTP research are outside the critical delivery chain.
There are no new public approval-policy or ingress-policy DSLs. Introduce
Sandbox fields only with C3's implementation.

#### Required reuse intake

The implementation is a deliberate port of proven pieces, not a greenfield
rewrite or a branch-wide merge. The intake baselines are:

| Source | Candidates | Target / required adaptation |
| --- | --- | --- |
| `larohra/durable-loop-leadership-demo@d3ebac5afadeb8cb6d59f54a53177a39de4275f5` | `experimental/durable_loop_activities.py`, registration/state/protocol/receipts/HTTP/tool modules and `tests/test_durable_loop_*` | C1 compatibility regressions where applicable; C2 native 2.x activities/orchestrator, Entity state and approved identity/input APIs |
| Leadership spike and `feature/aca-sandboxes@88f553ed6a67e399a8fb660cf7aaef15905f0590` | `controller/package.py`, `controller/bootstrap_delivery.py`, `controller/sandbox_config.py`, `harness/bootstrap.py`, `transport/aca_sdk.py`, manifest models and associated tests | C3; compare overlapping implementations before selecting. Keep deterministic package/bootstrap and SDK transport, but remove conflicting serial/fixed-timeout/recreate assumptions |
| `feature/aca-sandboxes@88f553ed6a67e399a8fb660cf7aaef15905f0590` | Current-checkout smoke, deployed fixture/assembly/provenance/suite tooling, Python matrix, dependency export and pipeline regression tests, including #196 and #197 | Q prepares; C2/C3 adapt usable fixtures and tests with behavior; C4 integrates the deployed stage and required release gates |
| `larohra/durable-loop-chat-ui@9fe3edb8df1292533508e3742df13b42ad8c3e33` | `experimental/durable_chat_*`, `public/durable-chat/`, frontend/history/journal tests and browser E2E fixtures | Optional follow-up; use the public API and server-backed session/history recovery. IndexedDB remains an optional cache, not conversation authority |

Source-module paths above are relative to `src/azure_functions_agents/` unless
they begin with `tests/` or `eng/`. Revalidate each source SHA and the current
target base before implementation; branch names are discovery hints, not
immutable evidence.

Each PR must record source commit/files, destination, disposition (`port`,
`adapt`, `omit`), rationale and existing/new tests. Cherry-pick only a
self-contained compatible commit; otherwise transplant a coherent piece with
its tests and provenance. Avoid carrying unrelated ACA runtime/config/FRD
changes or duplicating helpers already present on the target branch.

Do not port legacy Durable bindings, provider background polling, private
APIM/demo auth, the custom Blob transcript, Table-backed ACA session authority,
silent sandbox replacement, `_queue_lock` serialization or hidden fixed call
timeouts. These conflict with the approved direction. Reusing a test does not
establish native replay, current ownership or parallelism without adapting
and executing its assertions against the new contracts.

#### Existing E2E pipeline integration

The reusable pipeline branch is `feature/aca-sandboxes`. In particular,
[qualification assets #196](https://github.com/Azure/azure-functions-agents-runtime/pull/196)
and [deployed CI wiring #197](https://github.com/Azure/azure-functions-agents-runtime/pull/197)
already provide substantial delivery infrastructure:

- `eng/ci/e2e-tests.yml` and
  `eng/templates/official/jobs/{e2e-tests,aca-qualify}.yml` define
  current-checkout smoke and deployed Python 3.13/3.14 Linux jobs.
- `eng/scripts/aca_qualification_pipeline.py` assembles one runtime wheel
  with a fixture, a checked-in requirements export and an embedded build
  marker; its deployment/preflight helpers are reuse candidates.
  `aca_deployed_qualification.py` authenticates and gates subsequent
  turn/lifecycle/loss/load cases on fail-fast cold-start/provenance checks.
- `tests/live/apps/aca-qualification/`, deployed live cases/support helpers,
  `tests/test_aca_qualification_pipeline.py`,
  `eng/constraints/aca-fixture-requirements.txt` and the CI guides carry
  existing fixture, dependency and pipeline regression work.
- `aca_pr_smoke.py` and `reap_aca_smoke_sandboxes.py` supply current-run smoke
  preflight/cleanup patterns; they are not proof of Durable payload erasure.

Adapt these assets to the asynchronous Durable API, Entity-backed sessions,
DTS/native offload and the approved foreground model/tool and human-input
contracts. Do not import the source fixture's Table session store, old API
expectations or broader ACA runtime to make its tests pass. Reuse or narrowly
extract tooling with a real consumer rather than create a parallel deployment
framework. C2/C3 include their usable fixtures, assertions and tooling tests;
C4 completes cross-layer wiring and qualification, not all testing from zero.

Preserve the Python matrix, authored region/base, authenticated preflight and
fail-fast suite ordering. Record the exact tested wheel digest and effective
dependency/host versions with deployment evidence: the source's embedded
build/commit/runtime marker is useful provenance, not exact-wheel attestation.
Its N=5 bounded smoke does not establish production scale limits.

Preserve credential-free versus privileged execution boundaries. The source
current-checkout smoke excludes fork PRs; its deployed stage accepts trusted
manual runs and main-branch CI, not PR or scheduled runs. Final permissions
and release-check promotion require pipeline-owner approval. Do not carry
personal service-connection/resource values into the public design, and do
not treat code reuse as permission to queue or deploy a cloud pipeline.

The source cloud jobs are advisory (`continueOnError: true`); passing the
overall pipeline therefore is not proof that those checks passed. During
bring-up, label advisory and not-run outcomes. Before public release, make
the FRD's required checks explicitly release-blocking and retain evidence for
each supported profile. Add exact-owned cleanup and failure reporting for
test hubs/Entities/native blobs/sandboxes; the deployed source stage does not
already close the payload ownership, late-writer or erasure gap.

**Critical decision lane:** the native payload ownership/reclamation contract
must be resolved with the platform owner or an explicit user-approved scope
decision. Stacking does not supply a missing lifecycle guarantee. Resolve that
decision before implementing/claiming the affected erasure contract; do not
silently weaken it to satisfy a delivery target. Normal platform/security
release sign-off remains separate from passing a bounded spike.

## 5. Decisions log

Human decisions below record the conversation, not final approval of every
proposed field/default. Agent proposals remain reviewable until sign-off.

| # | Decision | Options considered | Choice | Decided by | Date |
| --- | --- | --- | --- | --- | --- |
| 1 | Scope/lane | Small patch / full feature lifecycle | New medium+ FRD; deliberate spike reuse, no blind port | Human | 2026-09-16 |
| 2 | Security baseline | Platform controls / named certification | Tenant isolation, MI, private networking, retention/deletion, audit; no certification claim | Human | 2026-09-16 |
| 3 | Concurrent requests | Reject / bounded queue / forks | One active turn; reject competing HTTP requests with 409 | Human | 2026-09-16 |
| 4 | First-release invocation scope | HTTP only / non-HTTP too / full delegation composition | HTTP/chat plus non-HTTP triggers | Human | 2026-09-16 |
| 5 | Session correlation | Front-matter mappings / standard transport contract | HTTP header and payload session-ID contract; same authorized session across transports | Human | 2026-09-16 |
| 6 | Session creation IDs | Runtime-only / caller-supplied | Caller IDs allowed with ownership/collision checks | Human | 2026-09-16 |
| 7 | Large-state direction | Custom references / investigate native offload | Deep-dive native b3 first; custom Blob recommendation withdrawn | Human request; Agent recommendation | 2026-09-16 |
| 8 | Entity layout | Metadata + transcript Entities / one authoritative Entity | Start with one; use Durable replay rather than a parallel ordering system | Agent proposal clarified with Human | 2026-09-16 |
| 9 | Deletion semantics | Immediate physical purge / bounded purge / logical only | Immediate revocation, content erasure within configured documented window | Human | 2026-09-16 |
| 10 | Sandbox loss | Silent replacement / checkpoint reconstruction / fail closed | Same sandbox; `workspace_lost` on permanent loss | Human | 2026-09-16 |
| 11 | Config surface | Per-agent bool only / global + per-agent overrides | Global defaults, per-agent bool/object overrides | Human | 2026-09-16 |
| 12 | UI identity | Own spike UI / external offering | Own `larohra/durable-loop-chat-ui` branch | Human | 2026-09-16 |
| 13 | Release support target | Wait for GA dependencies / qualified public preview | Public preview with production security/reliability/lifecycle gates | Human | 2026-09-16 |
| 14 | Provider execution | Foreground / background polling | Foreground only; individually checkpoint model/tool calls | Human | 2026-09-16 |
| 15 | Native storage adoption | Default custom store / qualified host offload | Prefer one Entity plus native offload; exact stack and lifecycle proof are release gates | Agent proposal | 2026-09-16 |
| 16 | Turn execution | Agent in Entity / whole-loop activity / per-call orchestration | Per-turn orchestration, short Entity operations, one activity per model/tool call | Agent proposal | 2026-09-16 |
| 17 | Tool concurrency | Default fanout / serial mutable session | Serial tools in v1; different sessions may run concurrently within quotas | Agent proposal | 2026-09-16 |
| 18 | UI reuse | As-is / adapted optional UI / classic replacement | Adapt UX as optional API client; server history authoritative | Agent proposal | 2026-09-16 |
| 19 | Delivery | Large blind port / reviewable vertical slices | P0-P6 with P1 and P5 hard technical/publication gates | Agent proposal | 2026-09-16 |
| 20 | Generated-session retry address | Lookup Entity / stable scoped derivation | Stable SHA-256-derived request/session identity and accepted receipt in the session Entity | Agent proposal after review | 2026-09-16 |
| 21 | Trusted trigger authorization | Payload ownership / implicit access / explicit operator grants | Typed binding service actor and exact owner/action grants; queue-created sessions owned by service actor | Agent proposal after review | 2026-09-16 |
| 22 | Timer recovery boundary | Durable catch-up / normal source delivery | Normal Timer semantics before acceptance; Durable recovery after acceptance | Human | 2026-09-16 |
| 23 | Ambiguous unsafe tools | Blind redelivery / conservative effect claim | Unsafe by default; acknowledge a per-delivery effect claim and refuse ambiguous redispatch | Agent proposal after review | 2026-09-16 |
| 24 | Partial-turn context | Drop history / reuse invalid calls / explicit projection | Preserve transcript and complete dependency groups with truthful runtime outcomes or fail explicitly | Agent proposal after review | 2026-09-16 |
| 25 | Config changes and retirement | Recalculate promises / drain retained state | Capture lifecycle policy/erasure deadline; maintenance-only runtime and full drainage before removal; absolute TTL proposal superseded by Decision 30 | Agent proposal after review | 2026-09-16 |
| 26 | Physical erasure | Entity deletion / early sweep / proven writer boundary | Require quiescence or bounded residual writers before final completion | Agent proposal after review | 2026-09-16 |
| 27 | Legacy Dynamic Sessions | Reinterpret / keep second workspace / reject | Initially proposed a blanket conflict; superseded by Decision 29 | Agent proposal after review | 2026-09-16 |
| 28 | Default call-count budgets | Implicit 48/128 ceilings / opt-in only | No model-step/tool-call count ceilings unless authored; notify the model in its initial prompt and enforce configured budgets | Human | 2026-09-16 |
| 29 | Dynamic Sessions compatibility | Blanket rejection / conditional coexistence | Allow worker-activity dispatch to the isolated interpreter without a Sandbox Group; reject only the effective combined configuration | Human proposal, Agent concurrence | 2026-09-16 |
| 30 | TTL syntax and reference event | Additive object / ISO string; absolute / idle | ISO-8601 strings only, consistent with Dynamic Workflows; idle TTL after the last completed turn, not age from first request | Human | 2026-09-16 |
| 31 | First-release trigger scope | All triggers / HTTP + Timer / HTTP only | HTTP/chat only; Timer is a design probe. Supersedes Decision 4's broader release scope | Human | 2026-09-17 |
| 32 | Parallel tool calls | Serial default / DTS fan-out/fan-in | Parallelize a model's tool batch; preserve order only in result assembly. Supersedes Decision 17 | Human | 2026-09-17 |
| 33 | Remote MCP placement | Sandbox client / Functions worker | Worker activity with worker credentials; the remote server executes its tool | Human | 2026-09-17 |
| 34 | V1 tool delivery configuration | Policy/reconciler DSL / platform delivery contract | Remove public tool_policies and generic effect-policy framework from v1; keep at-least-once/idempotency responsibilities clear | Human | 2026-09-17 |
| 35 | Sandbox base | Implicit fixed base / authorable source | Authorable disk/disk_id; default qualified Python 3.13/3.14 base and validate ABI | Human | 2026-09-17 |
| 36 | Per-call deadlines | Implicit 300 seconds / opt-in | Model/tool timeouts unset unless authored; no new hidden Durable-mode application deadline | Human request; Agent clarification | 2026-09-17 |
| 37 | Evidence before sign-off | Source-only assumptions / empirical probe | Run native Entity offload spike before finalizing; independently validate MAF claim and upstream ownership | Human | 2026-09-17 |
| 38 | UI/SSE release scope | Core requirement / optional extension | UI and SSE are optional good-to-have, not core v1 release gates; Blob observations are acceptable for the optional path | Human | 2026-09-18 |
| 39 | Registration consolidation | Separate Control/Maintenance / shared typed handlers | Recommend one orchestrator and model/execution activities: six feature-owned plus two SDK registrations, eight total | Agent proposal answering Human feedback | 2026-09-18 |
| 40 | Human input scope | Paused clarification only / enforced action approval too | User selected paused input plus runtime-enforced approval of an exact proposed tool action; trust and dormant-TTL details remain under review | Human | 2026-09-18 |
| 41 | Dormant human-wait lifetime | Reuse session TTL / separate wait setting | Reuse session_ttl from question opening; timely answer resumes, unanswered expiry ends run/session without a fresh TTL interval | Human | 2026-09-18 |
| 42 | Human input versus action approval | Input plus enforced action approval / input only | Support paused input only in this change; no tool approval_mode or extra MCP approval setting. Supersedes Decision 40's enforcement scope | Human | 2026-09-18 |
| 43 | HTTP helper/API distinction | Reuse outbound SDK helpers as server APIs / thin authenticated facade | SDK HTTP built-ins send requests; reuse Durable client operations behind shared incoming handlers, not raw system-key APIs | Agent clarification from verified SDK source | 2026-09-18 |
| 44 | Chat without SSE | Require observation journal / poll committed state | Polling-only UI uses status, pending questions and Entity history; no streaming journal, independent of native history offload | Agent clarification for review | 2026-09-18 |
| 45 | Model activity registration | Separate model function / shared typed execution | Recommend shared execution for model, tool and maintenance: five feature handlers plus two SDK built-ins, seven total; separate invocations/checkpoints remain | Agent recommendation answering Human feedback | 2026-09-18 |
| 46 | Remaining qualification | Keep resolved items open / targeted pre-design probes | Archive resolved offload/MAF evidence; existing nested session coordinates focused subagents for remaining runtime, lifecycle/boundary and Sandbox probes before finalization | Human | 2026-09-18 |
| 47 | Delivery/review topology | Merge-serial implementation / overlapping dependent PRs | Separate FRD-only review; propose a short C1-C4 code stack with buildable pushed handoffs, overlapping reviews/qualification and owner-scoped rebases | Human request; Agent delivery proposal | 2026-09-21 |
| 48 | Existing spike implementation | Implicit reuse / explicit per-layer intake | Require source-SHA/file-to-target mapping, port/adapt/omit rationale and carried regression tests; no whole-branch merge or unexamined rewrite | Human request; Agent reuse mapping | 2026-09-21 |
| 49 | E2E qualification infrastructure | Rebuild pipeline / adapt existing ACA work | Reuse feature/aca-sandboxes fixture, deployment, provenance, matrix and suite assets; adapt Entity/DTS assertions and make required release checks explicitly blocking rather than inheriting advisory success | Human request; Agent source-verified proposal | 2026-09-21 |
| 50 | Review publication boundary | Wait for all qualification / publish an explicitly incomplete review draft | Open the standalone FRD-only PR now with unresolved gates visible; no product implementation, architecture sign-off, pipeline execution or expanded cloud authorization implied | Human | 2026-09-21 |
| 51 | Integration and final merge target | Land feature increments directly on main / dedicated integration branch | Create feature/durable-agent-loop from main, target D0 and the bottom code layer there, and promote the reviewed/qualified feature to main through a separate final PR. Preserve small reviews, stacked handoffs and all architecture/release gates | Human | 2026-09-21 |

## 6. Test plan

Tests mirror modules under `tests/`; configuration scenarios go under
`tests/fixtures/config_scenarios/`. Exact fixture sequence numbers are selected
when implementation starts. Use synthetic content and isolated task hubs/groups
for all cloud fault tests. No production resources or customer content are used
for qualification.

| Area | Required cases / measurable acceptance |
| --- | --- |
| Schema/merge | Absent/true/false/object; global enabled inheritance; nested override; explicit clear; invalid types/limits/group bindings; no public dead flag |
| Idle TTL | ISO-only syntax; shared parser compatibility; no inherited workflow wait cap; success/failure/cancel renewal; active work protected; stale timers rejected; reads/rejections/replays do not renew; policy edits and quarantine |
| Composition | Exactly one `DFApp`/native registration; mixed normal/workflow/durable agents; illegal references/MCP exposure rejected before app mutation |
| Session parsing | HTTP header/body agreement; conflicting, Unicode, empty, oversized and non-string IDs; no silent fallback; unsupported trigger combinations rejected |
| Ownership | Cross-tenant/owner collisions; spoofed Easy Auth/body claims; status/history/events/cancel/delete authorization |
| Admission | Simultaneous requests on different workers produce one accepted active turn; 409 for a confirmed competitor; stable receipt after lost acknowledgement |
| Request identity | Same key/body deduplicates; changed body conflicts; generated session survives lost first acknowledgement/restarts; volatile metadata excluded; new busy attempts can later admit |
| Handoff | Crash before/after Entity admission and staged turn start; no orphaned reservation or duplicate accepted turn |
| Deferred trigger research | Timer session/identity/contention lessons and pre-acceptance limitation; broker semantics explicitly remain unqualified future work |
| Model boundary | One foreground logical model operation per activity; no hidden semantic tool/compaction/model loop or background polling; account for SDK transport retries separately |
| Optional call budgets | No count ceilings or budget prompt when absent; explicit inheritance/clear; initial and remaining-budget notices; exact count boundaries, final-answer reservation, batch overflow and ContinueAsNew |
| Model wire fidelity | Actual pinned MAF serializer preserves full encrypted reasoning/tool exchange after JSON persistence and cold worker restore; incompatible provider context fails explicitly |
| Replay | Worker loss after recorded model/tool/Entity results does not issue those calls again; separately exercise loss before activity acknowledgement |
| Tool effects | Stable operation IDs; at-least-once failure window; downstream idempotency; internal sandbox receipt recovery where supported; no false exactly-once or termination claim |
| Parallel tools | Activity intervals overlap for an independent batch; every call is separately checkpointed; stable call-ID aggregation; no default serial chain/parallelism knob |
| Conversation | Failed/partial turns retained; truthful complete tool dependency groups for next-turn input; context-incomplete path; no replay append duplication; no success before Entity commit |
| Human input | Same-run/worker-independent resume; durable complete-answer receipt; early/duplicate/lost events; first-answer/conflict/expiry/cancel races; headless status/reply with streams disabled; mixed batches dispatch none; no implied action approval |
| Native offload | Low-compressibility state >1 MiB; state/input/output/query/activity paths separately; cold restore; observed actual blobs and fully hydrated content |
| Size boundaries | Actual threshold-1/threshold/threshold+1 and cap boundaries; UTF-8 multibyte; Base64/protobuf/batch amplification; metadata headroom |
| Storage failures | Upload/download 403, private DNS failure, crash after upload/before backend commit; complete old-or-new state, explicit errors and orphan accounting |
| Sandbox | Same ID across HTTP turns/workers; one prepare/create before parallel local tools; worker-side MCP; wrong group/owner/manifest denied; no cross-session disk reuse |
| Package transport | Authorable/default base; compatible wheels; allowed artifact contents; interrupted upload; guest digest/ABI handshake; atomic staging; fixed structured invocation; control-record tampering and lost response |
| Sandbox loss | Suspend/resume same ID; permanent deletion produces `workspace_lost`; no hidden new sandbox or worker fallback |
| Lifecycle | Captured expiry/policy, no implicit deadline extension, invalid clears, immediate revocation, late writes after early sweep, confirmed writer barrier, erasure deadline proof, restore |
| Versioning | Replay and ContinueAsNew across deployment versions; pending events; no unfinished activity loss; incompatible hubs/models/packages rejected |
| Quotas/operations | Authored call/time limits only; platform payload/capacity constraints remain distinct; overload does not allocate extra resources; content-free diagnostics and no replay usage duplication |
| UI | Auth, escaped untrusted content/CSP, server history, lost acknowledgement, explicit cancel/delete, reconnect/draft replacement, no secret browser persistence |
| Compatibility | Ordinary behavior preserved when off; legacy interpreter allowed without a group, conflict with a group, isolated pool IDs; maintenance-only registration; refuse removal with retained state |
| Reuse parity | Each selected spike primitive/helper has a source-to-target record and relevant ported tests; explicitly replace assertions coupled to legacy Durable, Table sessions, serial tools or demo ownership |
| Qualification pipeline | Reused fixture/wheel assembly and exported dependencies; Python 3.13/3.14 Linux matrix; provenance mismatch stops later suites; async Entity/DTS-specific assertions; credential/trust boundaries; exact-owned cleanup; advisory/skipped results cannot satisfy required release checks |

The native probe must initially exclude MAF so storage/Entity correctness can
be isolated. Then run the integrated model/tool/session recovery suite. Do not
use a 1 KiB offload threshold with small compressible data as a substitute for
crossing the real >1 MiB boundary.

Qualify Python 3.13 and 3.14 using the repository gate:

```text
python -m ruff check src tests
python -m mypy src
python -m pytest --cache-clear --cov=./src/azure_functions_agents --cov-report=xml --cov-branch tests
```

Start with targeted tests per slice, then the full gate before that slice is
ready. Real-host and Azure integration suites are separate from default unit
tests and require explicit environment approval. Keep a separate testing-review
checkpoint; record exact dependency/host/provider versions with qualification.

## 7. Docs impact

| Document | Change |
| --- | --- |
| `docs/architecture.md` | Execution-mode selection, module map, one Entity/session, native orchestration/activity boundary, optional sandbox and lifecycle ownership |
| `docs/front-matter-spec.md` | Global/per-agent inheritance, `durable: true`, advanced options, supported/invalid combinations |
| `docs/front-matter-reference.md` | Regenerate from schema; never hand-edit |
| `docs/triggers.md` | HTTP/chat v1 contract and explicit non-HTTP restriction; separate Timer research, not shipped broker guarantees |
| New `docs/durable-agent-loop.md` | API, session/workspace guarantees, native offload setup and actual support/limit matrix, retries/cancellation/deletion/upgrade runbooks |
| `docs/workflows.md` | Distinguish a Durable Agent Loop from Dynamic Workflows and document composition restrictions |
| `README.md`, `docs/index.md`, `docs/getting-started.md` | Public-preview capability and secure minimal setup; no premature production/GA claims |
| `docs/frds/README.md`, `mkdocs.yml` | FRD index and published guide navigation |
| Samples/deployment guidance | Minimal HTTP durable agent, optional Sandbox Group/base/package transport, worker-side MCP, MI/private network and retention prerequisites |

For each schema slice, run `eng/scripts/generate_config_reference.py`, then use
the `update-schema-docs` skill to update examples and onboarding surfaces. Keep
docs/tests with the behavior they describe. Do not publish private demo IDs,
anonymous auth instructions, production secrets, or unsupported API promises.

## 8. Status & sign-off

- **Status:** In review; HTTP-only v1 scope, explicit spike/pipeline reuse and
  unresolved qualification/platform decisions are documented.
- **Review publication:** Approved as a documentation-only PR, separate from
  implementation and final architecture approval.
- **Human sign-off:** Not yet given. Do not mark Finalized or implement product
  changes before explicit approval.
- **Architecture review:** A separate review found eight major contract gaps:
  generated-session retry lookup, trigger ownership configuration, pre-intake
  source recovery, unsafe redelivery fencing, incomplete model dependency
  groups, lifecycle changes/runtime retirement, late native payload writers,
  and legacy Dynamic Sessions compatibility. The draft addresses each;
  the Timer delivery boundary was explicitly resolved with the user.
  These are design resolutions, not claims that implementation probes passed.
  Subsequent human decisions removed default call-count ceilings, required
  initial-prompt budget guidance, narrowed the Dynamic Sessions conflict to
  Sandbox Group coexistence, and selected ISO-only idle TTL semantics.
  A narrow follow-up found no major inconsistency in that earlier revision.
  Newer human feedback subsequently narrowed v1 to HTTP, selected parallel
  tools/worker-side MCP, removed the public tool-policy DSL/default per-call
  timeouts, and requested pre-finalization experiments. The earlier review
  is not a claim that these latest changes or empirical gates are complete.
- **Testing review:** Required during each implementation slice.
- **Qualification evidence:** Native b3 large-state/IO/hydration/cold-restore
  primitive checks and exact probe-owned cleanup passed on the recorded local
  host/live-backend combination. The MAF replay defects were reproduced and
  already-fixed upstream releases validated with synthetic SDK requests.
  These do not establish deployed production support, scale/failure-window
  behavior, private Sandbox paths, full erasure/restore or human-input safety.
- **Round 2 closure:** A reproduced 14 offline checks and one source check,
  then indexed seven Functions in the real worker; host startup remained
  blocked before any test submission. The null-input/provider messages do not
  identify the throwing component. An explicit five-minute `functionTimeout`
  trial did not fix it and establishes no product default. B reproduced 54
  offline checks, retained the lifecycle platform gap and did not execute its
  optional native boundary case. C passed the bounded Python 3.13 synthetic
  transport/resume/owned-cleanup case described in section 4.9.
- **Operational closure:** Original access policy and exact-owned cleanup were
  verified; prior S0 evidence was preserved. All live mutation receipts are
  closed. Further live experiments require fresh scoped approval, and none of
  these outcomes constitutes final architecture sign-off.

### Evidence and qualification references

Sections 4.7 and 4.10 summarize the reported synthetic qualification evidence;
section 4.14 identifies reusable source and test assets. Reproducible fixtures
and deployment evidence must accompany the relevant implementation slices.
The following pinned public sources support the design without requiring
access to local session artifacts:

1. [Native b3 release](https://github.com/microsoft/durabletask-python/releases/tag/azurefunctions-v2.0.0b3) and [pinned dependencies](https://github.com/microsoft/durabletask-python/blob/46602d5221591b6aaeff1238cd0ec952419e2e29/azure-functions-durable/pyproject.toml#L26-L33).
2. [Native Functions worker](https://github.com/microsoft/durabletask-python/blob/46602d5221591b6aaeff1238cd0ec952419e2e29/azure-functions-durable/azure/durable_functions/worker.py#L26-L51) and [Entity handoff](https://github.com/microsoft/durabletask-python/blob/46602d5221591b6aaeff1238cd0ec952419e2e29/azure-functions-durable/azure/durable_functions/worker.py#L120-L142).
3. [Native Entity context](https://github.com/microsoft/durabletask-python/blob/46602d5221591b6aaeff1238cd0ec952419e2e29/durabletask/entities/entity_context.py) and [real-host Entity tests](https://github.com/microsoft/durabletask-python/blob/46602d5221591b6aaeff1238cd0ec952419e2e29/tests/azure-functions-durable/e2e/test_dtask_entities_e2e.py).
4. [Published AzureManaged host 1.10.0](https://www.nuget.org/packages/Microsoft.Azure.WebJobs.Extensions.DurableTask.AzureManaged/1.10.0) and [payload adapter 1.10.0](https://www.nuget.org/packages/Microsoft.DurableTask.Extensions.AzureBlobPayloads.AzureManaged/1.10.0). Entity coverage was inspected in published metadata/IL; private source was not accessed.
5. [Official large-payload guide](https://learn.microsoft.com/en-us/azure/durable-task/scheduler/durable-task-scheduler-large-payloads), [common options at the inspected package commit](https://github.com/microsoft/durabletask-dotnet/blob/29d53ff5091ba1edeefb6b7798716a259d138ae6/src/Extensions/AzureBlobPayloads/Options/LargePayloadStorageOptions.cs), and [native common byte comparisons](https://github.com/microsoft/durabletask-dotnet/blob/29d53ff5091ba1edeefb6b7798716a259d138ae6/src/Extensions/AzureBlobPayloads/Interceptors/PayloadInterceptor.cs#L169-L190).
6. [Durable programming guarantees](https://learn.microsoft.com/en-us/azure/durable-task/common/programming-model-overview), [Entity access](https://learn.microsoft.com/en-us/azure/durable-task/common/durable-task-entities#access-entities), and [Functions timeouts](https://learn.microsoft.com/en-us/azure/azure-functions/functions-scale#function-app-timeout-duration).
7. [Trigger retry limitations](https://learn.microsoft.com/en-us/azure/azure-functions/functions-bindings-error-pages) and [timer retry behavior](https://learn.microsoft.com/en-us/azure/azure-functions/functions-bindings-timer#retry-behavior).
8. [Sandbox overview](https://learn.microsoft.com/en-us/azure/container-apps/sandboxes-overview), [Python SDK reference](https://sandboxes.azure.com/docs/sandboxes/sdk-reference/python-sdk), [limits](https://sandboxes.azure.com/docs/sandboxes/limits), and [private endpoints](https://sandboxes.azure.com/docs/sandboxes/private-endpoints).
9. [Azure encrypted reasoning guidance](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/responses#encrypted-reasoning-items) and [OpenAI 1.10.2 request serialization source](https://github.com/microsoft/agent-framework/blob/python-1.12.0/python/packages/openai/agent_framework_openai/_chat_client.py#L1530-L1675).
10. [microsoft/agent-framework-durable-extension#59](https://github.com/microsoft/agent-framework-durable-extension/pull/59) and accepted ADR 0032 are history/compaction prior art, not a native-2-compatible replacement: the inspected prototype pins Durable <2 and does not checkpoint between individual tool calls.
11. [Documented Durable host settings and language caveat](https://github.com/MicrosoftDocs/azure-docs/blob/0218ddd6708cbe75bdb874eac3b38528539870a0/includes/functions-host-json-durabletask.md#L154), [native host binding assignment](https://github.com/Azure/azure-functions-durable-extension/blob/15d277c7a220b8d752e47605d4208e6695cee8ca/src/WebJobs.Extensions.DurableTask/Bindings/BindingHelper.cs#L29-L57), and [b3 positive-value client limits](https://github.com/microsoft/durabletask-python/blob/46602d5221591b6aaeff1238cd0ec952419e2e29/azure-functions-durable/azure/durable_functions/client.py#L148-L182). These are source references, not a tuned-profile live pass.
12. [Common payload-store public surface](https://github.com/microsoft/durabletask-dotnet/blob/29d53ff5091ba1edeefb6b7798716a259d138ae6/src/Extensions/AzureBlobPayloads/PayloadStore/PayloadStore.cs#L9-L33) and [native Blob upload/token handling](https://github.com/microsoft/durabletask-dotnet/blob/29d53ff5091ba1edeefb6b7798716a259d138ae6/src/Extensions/AzureBlobPayloads/PayloadStore/BlobPayloadStore.cs#L71-L196) support the bounded lifecycle-gap finding, not an application-owned reclamation API.
