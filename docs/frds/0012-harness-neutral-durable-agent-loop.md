---
frd: 0012
title: Harness-neutral Durable Agent Loop
status: In review
author: harshivcodes
created: 2026-10-14
updated: 2026-10-19
issues: []
pull_requests: [#266]
branch: harshivcodes/durable-agent-loop-changes
---

# FRD 0012 - Harness-neutral Durable Agent Loop

## 1. Summary

Add opt-in Durable execution for HTTP and chat agents without creating separate
MAF/Copilot and direct/Durable runner hierarchies. Each selected harness exposes
a narrow, versioned checkpoint protocol that can run until externally
schedulable work is reached and later resume from an opaque continuation.
Azure Durable Task Scheduler (DTS) owns replay, activity scheduling, timers and
recovery. The runtime owns agent semantics, session authority, tool policy and
terminal outcomes. Existing direct execution remains unchanged.

This FRD is the proposed harness-neutral replacement for the MAF-specific
execution architecture in FRD 0011. It retains the established product contract
where compatible: explicit opt-in, HTTP/chat ingress, owner-scoped sessions,
idempotent admission, one active turn per session, Entity-backed authority,
management APIs, durable human input, cancellation fencing, TTL and explicit
failure instead of silent fallback. FRD 0011 remains the historical record until
this FRD is finalized.

## 2. Motivation / problem

The runtime on `main` has one app-bound `AgentRunner` facade and two concrete
whole-turn backends:

- `_MAFHarnessRunner` executes the MAF-owned model/tool loop.
- `_CopilotHarnessRunner` executes the Copilot SDK-owned loop.

Both consume `HarnessRequest` and `ToolDescriptor`, but neither exposes every
model/tool boundary as a durable protocol. FRD 0011 instead designed a separate
MAF-specific Durable loop with one model or tool invocation per activity.
Adding Copilot to that design would produce four execution paths:

1. direct MAF;
2. direct Copilot;
3. Durable MAF; and
4. Durable Copilot.

Four independent loops would duplicate policy, session behavior, tool
accounting, cancellation, telemetry and failure semantics. Making direct
execution use a new shared loop immediately would create a different risk: it
would rewrite working MAF and Copilot behavior before Durable equivalence is
proven.

The required common boundary is smaller than a shared agent loop. A Durable
executor needs to:

1. ask the selected harness to run until a safe checkpoint;
2. persist a versioned continuation reference and the requested external work;
3. schedule that work through DTS;
4. supply the completed results to the same harness; and
5. repeat until the turn completes or fails.

This boundary must preserve SDK-native continuation rather than defining a
runtime-owned common transcript.

## 3. Goals / Non-goals

**Goals**

- Keep existing direct MAF and Copilot execution behavior unchanged.
- Add a private, versioned checkpoint protocol implemented by each selected
  harness.
- Use one MAF checkpoint connector and one Copilot checkpoint connector for
  Durable execution.
- Keep DTS replay and scheduling mechanics outside harness-neutral contracts.
- Preserve opaque MAF and Copilot continuation without serializing SDK objects
  into orchestration or Entity payloads.
- Store user, conversation and external-work content by reference so DTS and
  Entity state contain control metadata only.
- Reuse `AppHarness`, `HarnessRequest`, `ToolDescriptor`, provider resolution,
  capability catalogs, auth and observability.
- Support opt-in HTTP/chat Durable turns with owner-scoped sessions,
  idempotent admission, management APIs and durable human input.
- Fail closed before registration when an effective capability cannot expose a
  verified pre-side-effect checkpoint.
- Preserve truthful at-least-once and unknown-outcome semantics for external
  effects.
- Permit ordinary, workflow-enabled and Durable agents to coexist in one app
  when they do not compose with each other.

**Non-goals**

- Replacing `AgentRunner.run_agent()` or `run_agent_stream()` with the
  checkpoint protocol.
- Defining a common MAF/Copilot transcript format.
- Persisting arbitrary SDK objects, callbacks or Python callables.
- Exactly-once external side effects.
- Treating telemetry or post-execution events as checkpoints.
- Durable execution of unqualified native MCP tools, built-in shell/file tools,
  executable skills or native subagents.
- Durable composition with Dynamic Workflows, `subagents:` or
  `workflows.subagents` in the first supported contract.
- Inbound agent-as-MCP-tool Durable invocation in the first supported contract.
- Provider background polling, a new tool-policy DSL or a tool-action approval
  engine.
- Changing direct streaming behavior.
- Automatic migration of incompatible persisted continuations or sessions.
- GA, compliance or universal hosting-profile claims from preview evidence.

## 4. Proposed design

### 4.1 Pipeline and module ownership

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| discover | `discovery/{tools,skills,mcp}.py` | No Durable behavior. Continue producing read-only SDK-neutral inventories. |
| translate | `config/{schema,merge,validation,loader}.py` | Add Durable settings, inheritance and capability compatibility validation. |
| compose | `app.py`, `registration/{catalog,capabilities}.py`, new `durable/policy.py` | Freeze a slug-keyed Durable policy catalog before app mutation; select one `DFApp` when workflow or Durable policies exist. |
| register | `registration/{triggers,endpoints,_handlers,_auth}.py`, new `durable/integration.py` | Register per-entry admission/management dispatchers and the shared Durable Entity, orchestrator and activity bindings once. |
| execute: direct | Existing `runner.py` and selected `harness/*` runners | No behavior change. Continue whole-turn MAF or Copilot execution. |
| execute: checkpoint | New `harness/_checkpoint.py`; selected MAF/Copilot execution modules | Define and implement the private checkpoint protocol without importing DTS. |
| execute: Durable | New `durable/{engine,activities,contracts}.py` | Interpret checkpoint outcomes through replay-safe orchestration and side-effecting activities. |
| session/lifecycle | New `durable/{session,intake,lifecycle}.py` | Own Entity authority, admission, receipts, cancellation fencing, TTL and deletion. |
| UI | Existing `public/index.html` and endpoint adapter | Submit Durable work and poll authorized status/history when the selected agent is Durable-enabled. |

Discovery remains read-only. Registration remains the only Azure-aware startup
stage. Harness clients, credentials, native processes, storage adapters and
sessions remain lazy execution concerns.

`app.py:create_function_app()` builds the complete `AgentCatalog` and a
side-effect-free `DurableAgentPolicyCatalog` before creating or mutating the
Function App. Activities resolve an agent by slug from those immutable catalogs;
they never reparse YAML/front matter or rediscover tools.

### 4.2 Authoring and inheritance

The public authoring surface remains the Durable surface proposed by FRD 0011:

```yaml
# agents.config.yaml
http_auth: entra
durable:
  enabled: false
  history:
    session_ttl: P10D
```

```yaml
---
name: Researcher
builtin_endpoints:
  chat_api: true
durable: true
---
Research the request using the available tools.
```

```yaml
# Optional per-agent settings.
durable:
  enabled: true
  limits:
    max_model_steps: 24
    max_tool_calls: 48
  sandbox_group:
    resource_id: $SANDBOX_GROUP_RESOURCE_ID
    region: $SANDBOX_GROUP_REGION
    disk: python-3.13
```

| Setting | Resolution |
| --- | --- |
| Global `durable` | Object only; absent `enabled` defaults to false. |
| Per-agent absent | Inherit the global Durable settings. |
| Per-agent `true` | Enable with inherited settings. |
| Per-agent `false` | Disable for that agent. |
| Per-agent object | Recursively override authored settings. |
| Omitted count limit | Inherit. |
| Explicit `null` count limit | Clear the inherited limit. |
| `sandbox_group: null` | No session-bound workspace for new sessions. |

The effective existing `ResolvedAgent.timeout` remains the deadline for one
logical turn, including connector checkpoints, tool activities, retries and
human-input waits. Durable settings do not add a second timeout field.

Validation rejects effective `durable.enabled: true` with:

- `workflows.enabled: true`;
- nonempty `subagents` or `workflows.subagents`;
- workflow/delegation runtime tool references;
- inbound Durable MCP exposure;
- anonymous HTTP auth;
- capabilities not qualified for the selected harness checkpoint protocol; or
- incompatible sandbox/interpreter combinations.

Independent ordinary, workflow-enabled and Durable agents may coexist in one
app. Unsupported Durable configuration never falls back to ordinary execution.

### 4.3 Checkpoint protocol

The checkpoint protocol is private, SDK-neutral and versioned. It is not a new
public runner API and it does not replace the whole-turn backend protocol used
by `AgentRunner`.

Conceptually, each selected harness implements:

```python
class HarnessCheckpointConnector(Protocol):
    async def run_until_checkpoint(
        self,
        request: CheckpointRequest,
    ) -> CheckpointOutcome: ...

    async def resume_from_checkpoint(
        self,
        continuation: ContinuationRef,
        results: tuple[ExternalWorkResult, ...],
    ) -> CheckpointOutcome: ...
```

Names are illustrative; the contract requirements are normative.

`CheckpointOutcome` is a closed, versioned union:

| Outcome | Meaning |
| --- | --- |
| `TurnCompleted` | The harness produced the terminal assistant result and a terminal session continuation for the next turn. |
| `ExternalWorkRequested` | The harness suspended before one or more side effects and returned serializable work requests plus a continuation reference. |
| `HumanInputRequested` | The logical turn is waiting for authenticated user input and can resume from the returned continuation. |
| `RetryRequested` | The connector classified a sanitized transient failure and supplied bounded retry guidance. |
| `TurnFailed` | The turn cannot continue; the outcome includes a stable error category and no success-shaped fallback. |

Every outcome includes:

- protocol schema version;
- harness kind;
- agent slug, session ID, run ID and turn generation;
- absolute logical deadline;
- connector compatibility version;
- non-secret model/provider identity needed for diagnosis;
- bounded usage and tool-call evidence available at that boundary; and
- sanitized failure or retry metadata when applicable.

Any prompt, question, answer, tool payload or terminal result associated with an
outcome is represented by `ContentRef`, not embedded content.

The checkpoint protocol never includes live SDK clients, credentials, callbacks,
Python callables or raw transcript objects.

### 4.4 Continuation references

`ContinuationRef` is a bounded, versioned envelope. It identifies harness-owned
state without exposing or copying that state into DTS:

```text
schema_version
harness_kind
connector_version
connector_profile_id
backend_namespace
agent_slug
owner_scope_digest
entry_id
session_id
session_incarnation_id
run_id
turn_generation
storage_kind
opaque_state_key
state_format_version
continuation_role
created_at
expires_at
integrity_metadata
```

- `opaque_state_key` is a reference, not arbitrary SDK state embedded in Entity
  or orchestration payloads.
- `continuation_role` distinguishes a resumable mid-turn checkpoint from the
  terminal session seed produced by `TurnCompleted`.
- The reference contains no credential, prompt, tool result or reversible owner
  identifier.
- Durable continuation storage is separate from ordinary MAF history and
  ordinary Copilot session paths. Its runtime-minted namespace is derived from
  the bound backend/task hub, agent, a non-reversible owner-scope digest,
  registration-assigned entry ID, immutable session incarnation, run and turn
  generation. No client controls the storage key.
- Qualified Azure hosting profiles persist both MAF and Copilot native
  continuation in Azure Blob Storage under that dedicated Durable namespace.
  Worker-local ephemeral storage is never authoritative.
- Before returning a `ContinuationRef`, the connector fully writes, flushes and
  seals the native snapshot and records its digest/integrity metadata. A
  committed snapshot key is immutable; later checkpoints use new keys.
- The Entity authorizes the exact continuation identity before a connector may
  read it. Harness-owned storage encrypts the referenced state and applies
  integrity protection and least-privilege access.
- MAF and Copilot may use different storage representations.
- DTS and the Entity compare versions and identity fields but never parse native
  harness state.
- A continuation is valid only when backend namespace, harness, connector
  profile, agent, owner scope, entry, session/incarnation, run, turn generation
  and every declared protocol/state version match the authoritative Entity
  record.
- Resuming through a different harness is rejected.
- Incompatible protocol, connector or state-format versions fail explicitly as
  `continuation_incompatible`; the runtime does not reset history or silently
  start a replacement turn.
- A code deployment does not by itself invalidate a continuation. Deployed
  versions must retain declared compatibility with persisted protocol/state
  versions. Operators own breaking migrations and drain strategy.
- `TurnCompleted` returns a terminal session continuation tied to the completed
  run and turn generation. The Entity commits it atomically with the terminal
  result as the authoritative seed for the next turn.
- Turn `N+1` receives that prior terminal reference as `session_seed` in its
  initial `CheckpointRequest`. The connector consumes it to construct the new
  turn but does not resume or mutate turn `N`. Its first new checkpoint is
  minted with turn `N+1`'s run ID and generation.
- Continuation envelopes, excluding the opaque native state, are limited to
  16 KiB serialized. Native state is always stored by reference.

All non-control content uses the same bounded `ContentRef` rule:

```text
schema_version
storage_kind
opaque_content_key
content_digest
content_length
content_type
created_at
expires_at
integrity_metadata
```

- User prompts, committed public conversation entries, human-input questions
  and answers, external-work arguments and external-work results are always
  written to the qualified content store. They are never embedded in
  orchestration history or Entity state, regardless of size.
- Azure hosting profiles use Azure Blob Storage under the dedicated Durable
  namespace. A reference contains an opaque runtime-minted key, never a raw
  service URL, credential or client-controlled path.
- The writer fully persists the content and its integrity metadata before a
  `ContentRef` may be committed. Referenced content is immutable; updates use a
  new key.
- Activities and authorized management endpoints resolve content only when
  needed. They verify the declared length, digest and scope before use.
- DTS and the Entity store only bounded references, identity, versions,
  lifecycle state, deadlines and receipts.
- A `ContentRef` is limited to 16 KiB serialized. Capacity, integrity or lookup
  failure is explicit and never produces truncated or success-shaped content.

### 4.5 Harness connectors

#### MAF connector

The MAF checkpoint connector:

- constructs the same provider, model, instructions, role, capability and
  observability inputs as direct MAF execution;
- performs one foreground model step with automatic executable tool loops
  disabled;
- supplies tool schemas without executable wrappers at the model boundary;
- translates complete compatible model output, reasoning items and tool calls
  into a checkpoint outcome;
- persists the complete native continuation required for a later step;
- reconstructs the next MAF step from the persisted continuation and ordered
  external results; and
- never enters the existing whole-turn `run_agent()` path.

MAF Durable support is qualified through an exact connector profile keyed by
runtime package, MAF SDK version, provider/API, model family, hosting profile
and enabled capability set. The profile states the compatible checkpoint
protocol and native-state versions and is persisted in every continuation.
Text-only reconstruction is insufficient. Qualification must preserve
reasoning/encrypted items, call IDs and ordering, compaction state, usage and
parallel calls through a cold-process resume.

#### Copilot connector

The Copilot checkpoint connector:

- creates custom tools as declaration-only tools with no executable handler;
- observes pending external tool requests before execution;
- persists SDK-native SessionFs state and a stable session/resume reference;
- resumes with pending-work continuation enabled;
- injects externally completed tool results through the SDK pending-tool API;
- preserves SDK-owned session and transcript behavior; and
- translates terminal SDK output and usage into the common `AgentResult`.

Only capabilities with a verified pending-work boundary are allowed in Durable
Copilot mode. Custom runtime tools are the initial required capability.
Copilot-native MCP execution, shell/file tools, executable skills, native
subagents and other SDK-owned effects remain direct-only until independently
qualified.

Neither connector may interpret a post-execution telemetry event as a safe
checkpoint.

#### Connector profiles and capability matrix

Startup selects one exact versioned connector profile. If no profile matches the
selected harness, runtime/SDK version, provider/model family, hosting profile
and effective capability set, registration fails. Compatible rollout versions
must be able to read every protocol/state version they may encounter; a mixed
deployment that cannot do so is unsupported.

| Capability | MAF Durable | Copilot Durable | Requirement |
| --- | --- | --- | --- |
| Runtime custom tools | Qualification-dependent | Initial supported boundary | Must suspend before `ToolDescriptor.invoke()`. |
| `web_request` | Qualification-dependent | Qualification-dependent | Must use the runtime external-work contract, not hidden SDK execution. |
| Remote MCP | Qualification-dependent | Rejected initially | Must expose each request before network side effects. |
| Session sandbox tools | Qualification-dependent | Qualification-dependent | Must dispatch through sandbox activities and preserve workspace identity. |
| `request_human_input` | Qualification-dependent | Qualification-dependent | Must return `HumanInputRequested` before continuation. |
| Executable skills | Rejected initially | Rejected initially | Prompt-only content may be qualified separately; executable effects may not be hidden in the harness. |
| Harness-native shell/file/MCP tools | Rejected initially | Rejected initially | No verified universal pre-effect boundary. |
| Native/runtime subagents and workflows | Rejected initially | Rejected initially | Composition is outside the first contract. |

Any capability capable of executing an effect before the connector returns a
checkpoint is rejected, even if the SDK later emits telemetry for that effect.

### 4.6 External work contract

`ExternalWorkRequest` is serializable data, not a `ToolDescriptor`:

```text
schema_version
operation_id
tool_call_id
agent_slug
owner_scope_digest
entry_id
session_id
session_incarnation_id
run_id
turn_generation
tool_name
arguments_ref
adapter_kind
adapter_version
canonical_schema_digest
policy_digest
execution_placement
implementation_compatibility_version
policy_snapshot
idempotency_key
deadline
```

`ExternalWorkResult` carries the same operation, call, session, run and
generation identity plus terminal status, bounded diagnostic metadata and a
`result_ref`. Tool arguments and results are never inline.

The command identity is the tuple of adapter kind/version, canonical tool name,
canonical input schema digest, policy digest, execution placement and declared
implementation compatibility version. A whole-catalog or deployment digest may
be recorded for diagnosis but is never an equality gate: unrelated catalog
changes do not invalidate an accepted operation. Tool authors must change the
implementation compatibility version when behavior changes incompatibly while
the name/schema remain stable.

The activity:

1. reauthorizes the current owner and agent policy;
2. resolves `tool_name` from the immutable capability catalog;
3. verifies the persisted command identity against the resolved adapter;
4. resolves and integrity-checks `arguments_ref`, then validates the content
   through `ToolDescriptor`;
5. passes the stable operation/idempotency identity to a qualified adapter and
   invokes `ToolDescriptor.invoke()` or the qualified remote/sandbox adapter;
6. records a bounded structured result or truthful unknown outcome; and
7. returns no callable or SDK-native object to the orchestrator.

Parallel requests may execute concurrently. Results are assembled in original
tool-call order before connector resume.

Durable delivery is at-least-once. The Entity serializes this operation state:

```text
created -> dispatch_claimed -> dispatched -> terminal
                                      \----> outcome_unknown
```

- `dispatch_claimed` generation-fences which activity attempt may dispatch.
- An activity must commit `dispatched` before, or atomically with, using a
  downstream idempotency/receipt primitive when that adapter provides one.
- Only a failure proven to occur before dispatch may be surfaced as a native
  retryable failure.
- After dispatch may have occurred, the activity may retry automatically only
  when the adapter proves idempotency or retrieves the matching downstream
  receipt.
- Loss of an arbitrary local Python callable after invocation is
  `outcome_unknown`; it is never automatically invoked again.

Every operation uses a stable idempotency key and records dispatch/completion
receipts where the downstream system supports them. The runtime distinguishes:

- known not dispatched;
- dispatched with confirmed result;
- dispatched with confirmed failure;
- cancelled with confirmed stop;
- cancellation requested but stop unconfirmed; and
- effect outcome unknown.

Unknown-effect work is not blindly retried. A matching downstream receipt may
allow safe result reuse. The runtime does not claim exactly-once side effects.

### 4.7 Durable orchestration

The turn orchestrator uses deterministic DTS APIs only:

1. obtain the accepted turn generation and optional authoritative session seed
   from the Entity;
2. schedule a harness activity to start from that seed or resume the selected
   connector from a mid-turn checkpoint;
3. commit the returned checkpoint outcome through the Entity;
4. for external work, schedule one activity per work request;
5. commit the completed ordered result batch;
6. schedule another harness activity with the recorded continuation and results;
7. wait through Durable events/timers for human input when requested; or
8. commit the terminal result or failure.

The orchestrator performs no network, filesystem, credential, SDK-client,
configuration discovery, tool or model work. It does not import concrete MAF or
Copilot connectors. It schedules a typed harness/tool operation through the
shared activity registration.

DTS history is the execution record. It is not the public conversation
transcript and is never returned directly to clients.

### 4.8 Entity and session authority

One Entity is authoritative for each owner-scoped session. It stores bounded,
versioned business state:

- owner and immutable session incarnation;
- accepted request receipts;
- one active turn and monotonically increasing generation;
- run lifecycle and absolute deadline;
- ordered `ContentRef` values for committed public conversation entries;
- current continuation metadata;
- authoritative terminal session continuation for the next accepted turn;
- external-work dispatch/result receipts;
- pending human question and accepted answer receipt;
- cancellation and deletion intent;
- TTL generation and cleanup progress; and
- terminal result/error metadata.

Harness-native continuation remains in harness-owned storage. The Entity stores
only the `ContinuationRef`, ordered conversation `ContentRef` values and bounded
control metadata required by the runtime. Ordinary MAF Blob/file history is not
attached to Durable MAF turns. Copilot SessionFs remains native continuation
storage, not owner/session authority.

Entity generation fencing rejects late connector, tool, answer and terminal
commits from an older turn. Late external effects may still occur; fencing
prevents them from mutating the authoritative conversation.

The Entity also owns the dispatch-claim transition. A replayed or duplicate
activity cannot claim an operation already claimed by another generation or
already terminal.

Continuation cleanup follows authoritative state:

- During an active turn, the prior terminal session seed and every checkpoint
  still referenced by an in-flight generation remain available.
- After a newer checkpoint is committed and generation fencing proves that no
  in-flight operation may use an older mid-turn snapshot, that snapshot is
  eligible for asynchronous garbage collection.
- When a turn completes, its terminal session continuation is retained as the
  next-turn seed. The previous turn's terminal seed remains until the first
  authoritative checkpoint of the new turn is committed, then becomes eligible
  for cleanup.
- The current terminal session continuation and committed conversation content
  remain until session TTL expiry or authorized deletion.
- Cleanup is receipt-backed and retryable. A cleanup failure does not remove
  authoritative metadata or make a session appear deleted.

### 4.9 Admission, identity and idempotency

V1 supports built-in chat and one authored HTTP entry per agent. Other triggers
and inbound Durable MCP are outside this contract.

- `x-ms-session-id` and an optional root `session_id` use the shared session ID
  validation and must agree.
- `Idempotency-Key` is required and is the sole client retry key.
- The runtime hashes the normalized complete request body plus originating entry
  identity into the request fingerprint. The idempotency key alone is not the
  fingerprint.
- After validation and fingerprinting, request content is persisted before
  durable admission and represented by `ContentRef`; raw prompt content is not
  stored in the Entity or orchestration input.
- Identity is scoped within the bound backend/task hub by
  `(agent_slug, normalized_owner_id, entry_id)`.
- `entry_id` is assigned by registration and cannot be supplied by the request.
- Entra owner identity uses the verified tenant/object pair.
- Function/admin key ownership uses a domain-separated fingerprint of the
  validated presented credential. Raw keys are never persisted or logged.
- Anonymous Durable admission is unsupported.
- Multiple credential carriers or ambiguous key sources are rejected rather
  than choosing one. Easy Auth owner extraction is supported only for qualified
  platform profiles with verified headers/principal data.
- One active turn is allowed per session. A different request while active
  returns `409 session_busy`; there is no implicit queue.
- Session incarnation, request ID and run ID are deterministically derived from
  the scoped owner/session/entry identity and idempotency key with
  domain-separated hashes; raw credentials are never inputs.
- The normative derivations are versioned and collision-resistant:
  `session_incarnation_id = H("session-v1", backend, hub, agent, owner_scope,
  entry, session_id, creation_nonce)`, `request_id = H("request-v1",
  session_incarnation_id, idempotency_key)`, and `run_id = H("run-v1",
  request_id)`. The Entity persists `creation_nonce`; clients cannot select it.
- Request acceptance, stable run creation and start acknowledgement are one
  reliable durable handoff. Start acknowledgement atomically records the
  unchanged absolute deadline and schedules its generation-tagged timeout
  signal.
- Same key and same fingerprint returns the original receipt.
- Same key and different fingerprint returns `409 idempotency_conflict`.
- Unknown admission/start outcome is reconciled against the Entity and stable
  run using the same key; it never creates a replacement execution.
- Tombstones prevent implicit resurrection after deletion.
- Missing and unauthorized owner-scoped resources use the same non-revealing
  response shape.

Session ID is correlation, not authentication. Every management, input,
history, cancellation and deletion request reauthorizes the current caller.

### 4.10 HTTP and registration surface

Each Durable-enabled HTTP entry owns submission and management paths in one
Functions registration so its effective Entra or function-key policy applies
consistently. Built-in entries use `agents/<literal-slug>` as their prefix.
Authored HTTP entries preserve their normalized authored route as the submission
prefix. Their management routes are placed beneath the reserved `manage`
subpath of that same prefix. Path normalization, literal-slug validation and
route-collision checks reject ambiguous or overlapping registrations before app
mutation.

| Method/path under an entry prefix | Contract |
| --- | --- |
| `POST /chat` or authored HTTP entry | Admit Durable work and return `202`, stable IDs and relative management links. |
| `GET /manage/runs/{run_id}` | Return bounded authorized status, result or pending question. |
| `POST /manage/runs/{run_id}/cancel` | Record idempotent cooperative cancellation intent. |
| `POST /manage/runs/{run_id}/input/{question_id}` | Record an authenticated idempotent human answer. |
| `GET /manage/sessions` | Return owner-scoped paginated session discovery. |
| `GET /manage/sessions/{session_id}/history` | Return the authorized logical conversation projection. |
| `DELETE /manage/sessions/{session_id}` | Revoke access and return stable cleanup progress. |

Durable agents do not register ordinary chat streaming, ordinary Blob/file
history or workflow-status endpoints. The existing debug UI remains opt-in. For
a Durable agent it submits once with a stable idempotency key and polls the
returned authorized status link. A polling failure never resubmits or cancels
the run.

The app registers these shared execution functions at most once:

| Registration | Purpose |
| --- | --- |
| `agents_durable_orchestrator_v1` | Typed admission, turn, input and lifecycle orchestration instances. |
| `agents_durable_state_v1` | Session and owner-index Entities. |
| `agents_durable_execute_v1` | Typed harness-checkpoint, external-work and maintenance activities. |

Registration validates the complete graph and exact function/route inventory
before app mutation.

### 4.11 Human input

`request_human_input` is a reserved runtime capability, not a general
tool-action approval system.

- The connector returns `HumanInputRequested` before continuing the turn.
- The Entity stores one pending question reference, its schema and a stable
  question ID.
- The wait is bounded by the existing whole-turn deadline.
- Answers are owner-authorized, persisted as `ContentRef`, idempotent and
  ordered through the same Entity.
- Each answer requires a question-scoped `Idempotency-Key`; its complete answer
  body is fingerprinted. The same key/body returns the original receipt and the
  same key with a different body returns `409 idempotency_conflict`.
- An accepted answer receipt does not claim that the turn already resumed.
- If wake-event delivery is lost after answer acceptance, reconciliation
  redelivers the same answer to the same stable run without accepting a second
  logical answer.
- Cancellation after answer acceptance remains valid; the answer receipt is
  retained without claiming execution or rollback.
- Mixed human-input and ordinary external-work batches are rejected before
  dispatch.
- Terminal, expired, cancelled or deleted questions reject new answers.
- Resume uses the same run, session, turn generation, continuation and sandbox.

### 4.12 Cancellation, deadlines and recovery

Cancellation is acknowledged intent, not rollback.

- The absolute deadline starts when start acknowledgement is committed, not
  during admission. Replay, retries, connector resumes and `ContinueAsNew`
  preserve it unchanged.
- A generation-tagged Entity timeout signal independently enforces the deadline
  if the turn orchestration is lost. Duplicate acknowledgement and human input
  never extend it.
- The Entity records terminal intent and fences later state commits.
- The orchestrator stops scheduling new checkpoints or external work.
- Activities receive the absolute deadline and enforce it locally.
- The worker owning an active operation observes cancellation through a
  qualified bounded durable check and requests connector/downstream cancellation
  where supported. After observation it spends at most five seconds total, in
  parallel rather than per operation, on local/downstream cleanup; this does not
  extend the logical deadline.
- Local request cancellation does not claim that provider compute or an
  external effect stopped.
- Arbitrary synchronous Python tools cannot be force-killed safely in-process.
- Unconfirmed stop is recorded as `outcome_unknown`.
- Late outcomes cannot reopen the run or modify a later turn.
- A later turn may start after terminal commit; the runtime does not promise
  effect isolation from an earlier unknown operation.
- Deadline processing stops new scheduling, marks unfinished dispatched work
  `outcome_unknown`, commits terminal `timed_out`, releases the active slot and
  begins idle TTL.

Persisted protocol and continuation versions support cold restore after worker
loss. Incompatible continuation fails explicitly. No recovery path silently
starts a new turn, resets history or changes harness.

### 4.13 TTL, deletion and storage bounds

- `history.session_ttl` is an explicit positive fixed-unit ISO-8601 duration.
- Idle TTL begins after terminal turn commit.
- Active turns and human-input waits remain bounded by the logical turn
  deadline, not an unlimited active exemption.
- Reads, polling, rejected requests and duplicate receipts do not renew TTL.
- Generation-tagged Durable signals handle deadline and idle expiry.
- Owner-authorized deletion immediately revokes admission and data access,
  fences stale work and begins cleanup of runtime-owned continuation snapshots,
  referenced content and workspace.
- Repeated deletion returns the same stable deletion receipt and progress.
- Non-content request/deletion tombstones remain through the documented retry
  horizon.

The implementation defines lower limits when required by a qualified hosting
profile, but never higher limits than this contract:

| Boundary | Maximum serialized content |
| --- | --- |
| Continuation envelope | 16 KiB |
| Content reference envelope | 16 KiB |
| External-work request envelope, including references | 64 KiB |
| External-work result envelope, including references | 64 KiB |
| One ordered result-batch metadata envelope | 128 KiB |
| Entity hydrated control state | 512 KiB |
| One management response page | 256 KiB and 100 items |

Metadata headroom is reserved inside each limit. Size is checked before
scheduling and before Entity commit. Native state, prompts, conversation
content, human input, arguments and results always use qualified
integrity-checked storage references whose envelopes fit the applicable limit.
History is server-side paginated and hydrates only the authorized page.
Capacity or reference failure is explicit and cannot partially replace
authoritative state or silently truncate content.

### 4.14 Sandbox and remote execution

The optional session-bound sandbox contract remains independent of the harness:

- the customer provisions an approved Sandbox Group;
- the runtime allocates and binds one session workspace;
- local tool activities execute in fresh guest processes while files persist;
- package and policy digests are captured and verified;
- credentials remain worker-side;
- remote MCP executes from worker activities with existing auth/egress policy;
- permanent workspace loss returns `workspace_lost`; there is no automatic
  replacement or worker fallback; and
- deletion/expiry performs actual owned cleanup with visible progress.

Sandbox execution still uses the same `ExternalWorkRequest` and result receipt
contract. It does not introduce a third harness connector.

### 4.15 Compatibility

- Existing ordinary MAF and Copilot direct execution is unchanged.
- Existing direct streaming is unchanged.
- Existing Dynamic Workflows behavior and persisted retry envelopes are
  unchanged.
- Existing `AgentResult`, `ToolCallEvidence`, provider selection, auth and
  capability discovery remain the common public/runtime contracts.
- Durable authoring is opt-in; absent or disabled configuration preserves the
  ordinary registration and execution path.
- The app-level selected harness remains immutable for the app instance.
- A persisted Durable run records its harness kind and cannot resume through a
  different harness.
- MAF remains the default production runtime. Copilot Durable support remains
  within the bounded internal app-level preview selection.
- Unsupported Durable capability sets fail during startup validation without
  ordinary-runner fallback.
- Sessions survive compatible deployments in the same backend/task-hub
  namespace. Users own incompatible code/model/tool migrations.

### 4.16 Observability and security

- Preserve one logical `agent.run` trace per turn across checkpoints and
  activities.
- Replay does not double-count model attempts, tool calls or usage.
- Spans identify checkpoint kind, operation ID, retry epoch and terminal
  category without prompt, result, owner or credential content by default.
- Content attributes remain behind `ENABLE_SENSITIVE_DATA`.
- Logs and status never expose raw keys, tokens, native session files, storage
  references, raw DTS URLs or Entity state.
- Continuation storage is least-privilege and scoped to the bound app/harness
  namespace.
- Tool activities reauthorize current policy before every effect.
- Default diagnostics distinguish retryable transport failure, terminal
  connector failure, continuation incompatibility, capacity failure,
  cancellation and unknown external outcome.

### 4.17 Acceptance criteria

The feature contract is satisfied when:

- direct MAF and Copilot characterization tests remain unchanged and green;
- no direct request uses the checkpoint protocol unless separately approved;
- each Durable harness has exactly one checkpoint connector;
- both connectors pass cold-worker start/resume tests;
- Azure profiles persist immutable native snapshots in Blob Storage and never
  depend on worker-local continuation state;
- a completed turn's terminal continuation seeds the next turn, and superseded
  snapshots are removed only after generation-safe replacement;
- the orchestrator performs no side-effecting work;
- the same external-work/result contract is used for MAF and Copilot;
- tool identity, generation, deadline and idempotency survive replay;
- incompatible continuation and harness mismatch fail explicitly;
- duplicate delivery cannot create a second authoritative result;
- prompts, conversation entries, human input and external-work payloads never
  appear inline in DTS history or Entity state;
- unknown external effects remain unknown rather than being reported as
  success or blindly retried;
- unsupported Copilot/MAF capabilities fail before registration;
- one active turn, owner isolation and idempotent admission hold across workers;
- management, input, cancellation, TTL and deletion remain authorized and
  generation-fenced; and
- the documented hosting/provider profiles pass real deployed qualification.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Feature relationship to FRD 0011 | Edit historical finalized design / new superseding FRD | Preserve FRD 0011 and draft a new harness-neutral replacement | Agent process requirement | 2026-10-14 |
| 2 | Shared abstraction | Shared replacement agent loop / narrow checkpoint protocol | Narrow, versioned checkpoint protocol | Human; Agent recommendation | 2026-10-14 |
| 3 | Existing direct execution | Rewrite through checkpoint protocol / preserve | Preserve direct MAF and Copilot whole-turn behavior | Agent recommendation | 2026-10-14 |
| 4 | Harness structure | Direct and Durable connector variants / one checkpoint connector per harness | One MAF and one Copilot checkpoint connector | Agent recommendation | 2026-10-14 |
| 5 | DTS relationship | Runtime-owned state machine / DTS execution with runtime semantics | DTS owns replay and scheduling; runtime owns agent semantics | Human; Agent recommendation | 2026-10-14 |
| 6 | Native continuation | Common transcript / opaque harness-owned continuation | Opaque versioned reference; no common transcript | Human; Agent recommendation | 2026-10-14 |
| 7 | Resume compatibility | Best-effort fallback / explicit version and harness checks | Fail explicitly on incompatible protocol, state or harness | Agent recommendation | 2026-10-14 |
| 8 | Tool representation | Serialize `ToolDescriptor` / stable external-work data | Serialize stable IDs, arguments, policy metadata and idempotency; resolve descriptor in activity | Agent recommendation | 2026-10-14 |
| 9 | External effects | Exactly-once claim / at-least-once with receipts | At-least-once with truthful `outcome_unknown` handling | Human; prior FRD contract | 2026-10-14 |
| 10 | Durable capability support | Assume SDK parity / qualify pre-side-effect boundaries | Fail closed for unqualified capabilities | Human; Agent recommendation | 2026-10-14 |
| 11 | Session authority | Harness transcript / DTS history / Entity | Entity owns product authority; harness owns native continuation; DTS owns execution history | Agent recommendation | 2026-10-14 |
| 12 | Public enablement | Default / opt-in | Global/per-agent opt-in; disabled or absent preserves ordinary path | Human; prior FRD contract | 2026-10-14 |
| 13 | Initial ingress | All triggers / HTTP and chat | HTTP/chat only | Human; prior FRD contract | 2026-10-14 |
| 14 | Composition | Workflows and subagents in first contract / defer | Defer composition; allow independent same-app coexistence | Human; prior FRD contract | 2026-10-14 |
| 15 | Copilot initial capability | All native capabilities / verified external custom-tool boundary | Require custom-tool checkpoint support; gate native capabilities independently | Agent recommendation | 2026-10-14 |
| 16 | Human interaction | General action approval / durable input | Durable human input only; no action-approval engine | Human; prior FRD contract | 2026-10-14 |
| 17 | Session concurrency | Queue / reject concurrent turn | One active turn; return `409 session_busy` | Human; prior FRD contract | 2026-10-14 |
| 18 | Deployment continuity | Pin old deployment / declared compatibility | Preserve sessions across compatible deployments; fail explicit incompatibility | Human; prior FRD contract refined by Agent | 2026-10-14 |
| 19 | Continuation isolation | Reuse direct history paths / dedicated Durable namespace | Runtime-minted owner-, entry-, incarnation-, run- and generation-scoped Durable namespace | Architecture review | 2026-10-14 |
| 20 | Dispatch recovery | Retry activities generically / explicit dispatch state | Entity-fenced dispatch state; retry only proven pre-dispatch or adapter-proven idempotent work | Architecture review | 2026-10-14 |
| 21 | Stable command identity | Tool name / whole-catalog digest / operation contract | Versioned adapter, schema, policy, placement and implementation identity | Architecture review | 2026-10-14 |
| 22 | Connector qualification | Informal provider support / exact profile | Persist and require an exact runtime/SDK/provider/model/hosting/capability profile | Architecture review | 2026-10-14 |
| 23 | Catalog changes | Require whole-catalog equality / operation-local compatibility | Catalog digests are diagnostic; validate only the persisted operation contract | Architecture review | 2026-10-14 |
| 24 | Deadline authority | Orchestrator-only timer / Entity signal | Start at acknowledged turn, preserve unchanged and enforce with generation-tagged Entity signal | Architecture review | 2026-10-14 |
| 25 | State limits | Unspecified bounded payloads / explicit upper bounds | Contractual envelope, Entity, batch and response limits with integrity-checked offload | Architecture review | 2026-10-14 |
| 26 | Cancellation cleanup | Unbounded best effort / fixed budget | Five-second total cleanup budget after worker observation; unconfirmed effects remain unknown | Architecture review | 2026-10-14 |
| 27 | Content placement | Inline below size thresholds / always by reference | Store prompts, conversation entries, human input and external-work payloads in qualified content storage; keep only bounded references and control metadata in DTS and Entities | Human; review recommendation | 2026-10-19 |
| 28 | Continuation lifecycle | Mutable/local checkpoints / immutable durable snapshots with a terminal cross-turn seed | Seal Blob-backed snapshots before commit, retain the terminal continuation as the next-turn seed and garbage-collect superseded snapshots only after generation-safe replacement | Human; review recommendation | 2026-10-19 |

## 6. Test plan

Tests mirror source modules. Config behavior adds a scenario fixture under
`tests/fixtures/config_scenarios/`.

- [ ] Config schema, merge and validation: absent/false/true/object inheritance,
  limits, TTL, auth and incompatible composition.
- [ ] Fixture scenario:
  `tests/fixtures/config_scenarios/<nn_harness_neutral_durable_agent>/`.
- [ ] Registration characterization: ordinary MAF/Copilot and Dynamic Workflow
  inventories remain unchanged when Durable is absent.
- [ ] Direct execution characterization: MAF and Copilot whole-turn,
  streaming, history, tool evidence, deadlines and cleanup remain unchanged.
- [ ] Checkpoint contract codec: every outcome and continuation version,
  bounded size, unknown fields, incompatible versions and sanitized errors.
- [ ] MAF connector: tool-free completion, one/many tool calls, ordered resume,
  reasoning fidelity, compaction, terminal cross-turn seeding, provider failures
  and cold restore.
- [ ] Copilot connector: declaration-only tools, pending request persistence,
  process/client restart, external result injection, pending-work continuation,
  terminal cross-turn seeding, denied permissions and cold restore.
- [ ] Capability validation: native MCP, built-ins, executable skills and
  subagents fail closed until individually qualified; prompt-only content is
  distinguished from executable skills.
- [ ] Harness mismatch: MAF continuation cannot resume through Copilot and vice
  versa.
- [ ] Deployment compatibility: compatible connector/state versions resume;
  incompatible versions fail without replacement execution.
- [ ] Orchestrator determinism: replay produces the same activity schedule and
  performs no SDK/network/filesystem/credential/tool work.
- [ ] External work: descriptor re-resolution, operation-identity
  compatibility, unrelated catalog changes, argument validation, parallel
  ordering and policy reauthorization.
- [ ] Effect safety: lost activity response, duplicate delivery, downstream
  receipt reuse, known-not-dispatched and `outcome_unknown`.
- [ ] Entity fencing: stale connector/tool/input/terminal commits cannot mutate
  a newer generation.
- [ ] Admission: owner modes, anonymous rejection, same-key retry, fingerprint
  conflict, busy rejection, reliable handoff and lost acknowledgement.
- [ ] Human input: duplicate/conflicting answers, accepted-but-unconsumed
  cancellation, deadline expiry and mixed-batch rejection.
- [ ] Cancellation: cross-worker observation, bounded local cleanup, ignored
  cancellation and late-result fencing.
- [ ] TTL/deletion: stale signals, no read renewal, revocation, stable deletion
  receipt, cleanup failure and tombstone retention.
- [ ] Content references: prompts, conversation entries, human input and
  external-work payloads are never inline; verify write-before-commit,
  immutability, scope/digest/length checks and missing/corrupt content failure.
- [ ] Continuation lifecycle: Azure Blob backing, seal-before-return,
  immutable snapshot keys, session-seed handoff, generation-safe superseded
  snapshot cleanup, TTL/deletion retention and cleanup retry receipts.
- [ ] State bounds: large low-compressibility outputs, continuation and content
  indirection, every contractual envelope limit, metadata-only Entity growth,
  pagination, storage-reference failure and no silent truncation.
- [ ] Debug UI: one submission, stable idempotency key, polling without
  resubmission, pending question, terminal result and authorized history.
- [ ] Deployed qualification: supported Python/hosting/provider matrix, process
  loss, scale-out, backend/task-hub isolation and storage/network failures.
- [ ] Canonical gate: `ruff`, strict `mypy`, and full CI-equivalent `pytest` on
  Python 3.13 and 3.14.

## 7. Docs impact

- [ ] `docs/architecture.md` - add checkpoint connectors, Durable policy catalog,
  Entity authority and DTS activity boundaries to the module map and data flow.
- [ ] `docs/front-matter-reference.md` - regenerate from `schema.py`.
- [ ] `docs/front-matter-spec.md` - document Durable inheritance, examples,
  limits, TTL and capability restrictions.
- [ ] `docs/triggers.md` - document HTTP/chat Durable admission and unsupported
  trigger/MCP surfaces.
- [ ] `docs/index.md` and `docs/getting-started.md` - describe the opt-in public
  capability when implemented.
- [ ] `README.md` - add a concise Durable quickstart and support-boundary note.
- [ ] Durable API/auth/operations documentation - document admission,
  management, identity, retries, cancellation, human input, TTL and deletion.
- [ ] Copilot preview documentation - document the narrower Durable capability
  matrix separately from direct Copilot support.

## 8. Status & sign-off

- **Architecture review (phase 2):** Independent review completed on
  2026-10-14. The draft preserved the required pipeline boundaries and avoided
  a second direct runner hierarchy. Blocking findings required dedicated
  owner/incarnation continuation isolation, explicit dispatch uncertainty,
  stable operation identity, exact connector profiles, restored admission/API
  guarantees, deterministic deadlines, payload limits and bounded
  cancellation. Those corrections are incorporated in this revision.
- **Human sign-off:** Pending. Status is `In review`; set `status: Finalized`
  only after explicit human approval.
