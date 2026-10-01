---
frd: 0013
title: Durable skills and public run contracts
status: Finalized
author: harshivcodes
created: 2026-09-29
updated: 2026-09-29
issues: []
pull_requests: []
branch: harshivcodes/durable-skills-and-events
---

# FRD 0013 - Durable skills and public run contracts

## 1. Summary

Extend the private Durable Agent Loop with progressively disclosed,
instruction-only skills and a runtime-generic public run contract. A run is
bound at admission to an immutable skill catalog, exposes bounded skill
metadata to the model, and loads exact skill content only through replay-safe
activities. Chat, HTTP, timer, and connector entry points may admit the same
durable execution without running the model inline.

The public contract formalizes the authentication, ownership, observation, and
retention behavior already present in the durable loop and hosted chat. It
reuses the existing endpoint authentication policy, owner resolver, Durable
Entity admission fence, content store, receipt store, and Blob-backed
observation journal. It does not introduce a second identity system, event
store, or result store. New work is limited to missing public contracts,
configurable retention and cleanup, generic event names, and durable skill
loading.

## 2. Motivation / problem

The Durable Agent Loop already provides step-level model/tool durability,
session fencing, idempotent admission, human waits, cancellation, content
references, effect receipts, owner-authorized routes, and reconnectable
Blob-backed observations. Its startup guard deliberately rejects every skill
and declared trigger, and its HTTP, event, authentication, and expiry behavior
is still described as a private chat-specific surface.

Applications with many bodies of expertise therefore have two poor choices:
put all instructions in the root prompt, increasing cost and reducing model
focus, or split work across separately deployed agents even when one durable
agent would be sufficient. Custom portals and non-chat triggers also cannot
rely on a documented, application-neutral contract for run ownership,
reconnection, expiry, and retained effect evidence.

This feature closes those gaps without replacing working mechanisms. It
separates three concerns:

1. **Execution state:** Durable orchestration and entities remain authoritative
   for admission, lifecycle, waits, cancellation, and final commit.
2. **Public observation:** the existing external journal remains authoritative
   for replayable client events and may degrade without changing execution.
3. **Retention and access:** one explicit policy governs when each public or
   recovery artifact expires and which authenticated owner may access it.

## 3. Goals / Non-goals

**Goals**

- Admit instruction-only Markdown skills in durable-loop mode while continuing
  to reject executable skill assets.
- Bind every run to an immutable agent definition, tool catalog, skill catalog
  revision, and retention policy before the first model step.
- Provide bounded initial skill metadata and model-callable, replay-safe
  `load_skill` and paginated `search_skills` runtime operations.
- Reuse exact recorded skill versions and content hashes after replay, restart,
  human-input resume, or catalog activation changes.
- Allow chat, generic HTTP, timer, and connector handlers to normalize input
  into the existing idempotent durable admission path and return without inline
  model execution.
- Promote the existing durable status/result/cancel/human-input/event behavior
  into a versioned, application-neutral public run API.
- Define a public authentication and ownership contract by reusing
  `EndpointAuthConfig` and `resolve_owner_principal()`.
- Define configurable, artifact-specific retention and safe cleanup, including
  stable `expired` versus `not found` behavior.
- Keep secrets, raw skill instructions, private tool payloads, and provider
  envelopes out of public status and event data.

**Non-goals**

- A separate identity provider, authorization database, event store, result
  store, or browser session service.
- Replacing `EndpointAuthConfig`, App Service Authentication, or the current
  owner-principal derivation.
- Replacing the Durable Entity admission fence or storing public event history
  in Durable orchestration history.
- Executing Python or other code packaged in a skill.
- Allowing skill content to add tools, identities, connector grants, resource
  targets, or effect authority to an admitted run.
- Sub-agents, peer-agent networks, Dynamic Workflows, or agent-as-MCP durable
  admission in this prototype.
- Exactly-once external effects. Existing at-least-once and ambiguous-effect
  semantics remain explicit.
- Provisioning Easy Auth, storage lifecycle policies, or a portal
  backend-for-frontend.
- Cross-device conversation enumeration or indefinite transcript retention.
- Product-specific catalog ranking, approval, connector lifecycle, or UI
  behavior.

## 4. Proposed design

### 4.1 Pipeline and module ownership

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| discover | `discovery/skills.py`, new `experimental/durable_skill_providers.py` | Reuse packaged skill parsing to produce immutable instruction-only metadata/content. Define the provider contract in runtime core. Keep a Blob implementation in a first-party optional package or extra. |
| translate | `registration/capabilities.py`, `experimental/durable_loop_config.py`, `experimental/durable_loop_protocol.py` | Validate that durable skills are instruction-only, enforce metadata budgets, resolve retention defaults, and define strict versioned catalog/load/public-resource models. |
| register | `app.py`, `registration/triggers.py`, `experimental/durable_loop_catalog.py`, `experimental/durable_loop_http.py` | Freeze the admitted skill catalog beside the existing tool catalog; register public run routes and supported trigger admission against the existing auth policy. |
| execute | `experimental/durable_loop.py`, `durable_loop_registration.py`, `durable_loop_activities.py` | Add replay-safe skill search/load steps and rebuild each one-step agent with the exact accumulated instruction set. Preserve the existing model/tool loop and authority boundary. |
| observe | new ungated `experimental/durable_run_observations.py`, existing `durable_chat_protocol.py`, `durable_chat_journal.py`, `durable_chat_http.py` | Move/generalize the existing journal contract from chat presentation to public run observations; retain snapshot, sequence, replay, and degradation semantics, with chat as a compatibility adapter. |
| retain | new `experimental/durable_retention.py`, new `experimental/durable_trigger_admission.py`, existing content/journal/receipt stores | Persist resource expiry metadata, own the trigger pending ledger/outbox/sweeper, expose resource state, and run reference-aware cleanup without broad prefix deletion. |

The implementation may rename chat-specific journal types only when doing so
does not create parallel storage or break the existing hosted UI. Compatibility
aliases or adapters are preferred over a second implementation.

### 4.2 Durable skill provider contract

The runtime core defines an asynchronous provider boundary:

```python
class DurableSkillProvider(Protocol):
    async def open_snapshot(
        self,
        *,
        agent_slug: str,
        requested_revision: str | None = None,
        retain_until: datetime,
    ) -> DurableSkillCatalogSnapshotV1: ...

    async def search_metadata(
        self,
        *,
        snapshot: DurableSkillCatalogSnapshotV1,
        cursor: str | None = None,
        query: str | None = None,
    ) -> DurableSkillMetadataPageV1: ...

    async def get_content(
        self,
        *,
        snapshot: DurableSkillCatalogSnapshotV1,
        skill_id: str,
        version: str,
        expected_hash: str,
    ) -> DurableSkillContentV1: ...
```

`DurableSkillMetadataV1` contains only:

- stable skill ID;
- display name;
- selection description;
- immutable version;
- content hash;
- non-authorizing tags;
- `executable=false`.

Provider ID is retained in the internal snapshot and load receipt, not exposed
in model-visible metadata or public events. The snapshot freezes provider ID, catalog revision, catalog hash, ordered
metadata, an opaque non-authorizing snapshot token, and `retain_until`.
Pagination cursors are bound to the snapshot, query, ordering, and page size.
The provider must either guarantee exact-version reads through `retain_until`
or return immutable content references during snapshot creation. For providers
that cannot retain old versions, including packaged files across deployment
replacement, admission materializes canonical content into the existing
durable content store before accepting the run. Materialization does not put
full content in model context; it only makes later progressive disclosure
durable.

`retain_until` is never earlier than the run's absolute deadline plus result
retention plus the 30-day skill-reference grace. Terminalization extends the
lease when a later terminal-relative deadline requires it. A provider that
cannot acknowledge that lease must return immutable content references or let
the runtime materialize the catalog before admission.

One admitted catalog contains at most 10,000 records and 8 MiB of canonical
metadata. One instruction-only skill contains `SKILL.md` plus at most 64
regular UTF-8 `.md`, `.txt`, `.json`, `.yaml`, or `.yml` files under
`references/`, totaling at most 2 MiB. Symlinks, executable bits, `scripts/`,
`assets/`, binary files, and files outside those paths are rejected. This is a
deliberate durable-mode subset of the ordinary authored-skill layout. Markdown
includes are resolved at admission only when their target remains inside the
admitted `references/` tree. Environment substitution in skill bodies is
rejected; environment-dependent or secret text must not enter immutable skill
content. The canonical resolved UTF-8 bundle, not deployment paths or process
environment, determines the version hash. Reference files are loaded only with
their parent skill and share its version/hash fence.
This FRD explicitly adds deterministic include resolution to the durable
provider; it does not change FRD 0002's ordinary runtime behavior. MAF context
providers remain empty in durable model activities, so MAF-owned
`load_skill`/`run_skill_script` operations never enter the frozen catalog.

Provider credentials, Blob URLs, SAS tokens, full instructions, reference-file
content, and tool grants are never model-visible metadata. Metadata and content
documents use strict Pydantic models, canonical JSON, bounded fields, and
duplicate-key rejection at external boundaries.

The packaged filesystem provider is part of the runtime implementation because
it adapts the existing `skills/<name>/SKILL.md` convention. The provider
interface, validation, and error taxonomy are core. A Blob provider is
first-party but separately packaged so storage dependencies do not become
mandatory for every runtime installation. Applications pin its version with the
runtime version.

A provider failure is explicit. It never becomes an empty catalog or silently
substitutes the currently active version.

### 4.3 Catalog admission and metadata budget

Before the first model call, admission freezes:

- catalog revision and canonical catalog hash;
- each admitted skill's ID, version, provider, and content hash;
- agent and existing tool-policy identities;
- the effective retention policy;
- the maximum initial metadata budget.

The ordered snapshot is persisted in the existing content store and represented
inside the versioned durable plan by `ContentRefV1`; it never enters an entity,
orchestration input/output, custom status, or activity envelope inline. The
snapshot is immutable for the run. Activation or catalog changes affect only
new runs. Every V2 plan and checkpoint carries the catalog reference and hash.

Initial model context contains at most 32 complete metadata records, each with a
description of at most 320 Unicode code points, and no more than 24 KiB of
canonical serialized metadata. The runtime never cuts a record to fit. Catalogs
that exceed any limit remain available through
`search_skills(query, cursor)`, whose opaque cursor and result page are bounded.
Selection order is deterministic and must not depend on mutable usage history
during orchestration replay.

These are prototype defaults. They may become author-configurable only within
hard runtime ceilings and without changing a run after admission.

### 4.4 Durable skill search and load

`search_skills` and `load_skill` are reserved runtime-owned model tools.
Startup injects their strict declaration-only descriptors into the frozen tool
catalog with `ToolProvenance.RUNTIME`; authored tools with either name fail
closed. The orchestrator intercepts them before customer tool dispatch.
`search_skills` is a non-mutating runtime read. `load_skill` is serialized
because it changes subsequent model instructions, even though it creates no
external effect. Neither operation enters the authored tool policy or consumes
an external-effect receipt.

The version-2 durable budget defaults to at most 8 searches, 16 successful
loads, 32 KiB per search result, and 512 KiB total loaded instructions. Hard
ceilings are 64 searches, 64 loads, 256 KiB per search result, and 2 MiB loaded
instructions. These counters are `completed_skill_searches` and
`completed_skill_loads`; they do not increment `completed_model_steps` or
`completed_tool_calls`. Each call has a deterministic operation key
derived from run, model step, call ordinal, admitted catalog hash, normalized
arguments, and expected version/hash.

The runtime injects `load_skill` only when the snapshot is non-empty and
`search_skills` only when admitted metadata exceeds the initial context budget.
Each runtime skill operation must be the sole function call in a model decision,
as must `request_human_input`. A mixed runtime/customer/human batch executes
nothing and appends bounded tool-error results for one repair attempt, consuming
one model step but no customer-tool or skill-operation count. A second mixed
batch terminalizes with `invalid_runtime_operation_batch`, matching the
existing one-repair clarification boundary. An unknown skill or duplicate load returns a deterministic tool result
(duplicate load is successful and reports `already_loaded`). Search/load budget
exhaustion, provider unavailability without materialized content, or a hash
mismatch terminalizes the run with a stable budget, availability, or integrity
error respectively.

A load activity:

1. validates the requested skill against the admitted catalog;
2. reads the exact admitted provider, version, and catalog revision;
3. verifies the canonical content hash and rejects executable assets;
4. persists a content reference and a content-free load receipt;
5. returns only the bounded content reference, hashes, and classification needed
   by the orchestrator checkpoint.

The deterministic checkpoint records the skill ID, provider, version, catalog
revision, content hash, stable step ID, and content reference. Replaying a
completed load reuses that checkpoint; it does not call the provider again.
Loading the same skill version twice is idempotent and does not duplicate
instructions.

The one-step model request uses root agent instructions plus loaded skills in a
deterministic order. Loaded skill content is treated as instruction data beneath
runtime policy. It cannot change the frozen tool catalog, identity, endpoint
authentication, owner, sandbox profile, connector grant, or effect policy.
The model activity resolves skill references immediately before constructing
the request. Loaded instruction bytes count toward `context_max_bytes`,
compaction thresholds, provider request limits, and actual input-token/cost
accounting on every model step. They are never exempt from run budgets; a load
that would exceed the frozen context ceiling fails with
`skill_context_budget_exceeded`.
The mixed-batch repair uses the existing frozen `repair_steps_used` counter and
`mixed_batch_repair_steps` limit; it does not introduce a second repair budget.

After a human-input resume, the run may search or load additional skills only
from the same admitted catalog and existing grants. The runtime does not infer
semantic scope changes from human text. Frozen tools, identity, targets, and
effect policy remain unchanged; an unavailable capability is rejected through
the existing tool/authority error path, and the agent may tell the user that a
new run is required.

### 4.5 Trigger admission

Triggers are request producers, not alternate execution engines. Supported
handlers normalize their input into the existing durable start contract,
persist an idempotent admission request, and attempt the existing admission
flow. The persisted request is an outbox, not a second admission engine.

| Surface | Stable idempotency material | Response behavior |
| --- | --- | --- |
| Hosted chat / run API | authenticated owner, session ID, client request ID, normalized body hash | Existing accepted/replayed/conflict semantics and run links. |
| Generic HTTP trigger | resolved owner, deterministic session ID, authored trigger identity, required `Idempotency-Key`, and normalized body hash | `202 Accepted` after the request is durably staged, with the same stable run resource links. Its `http_auth` must equal the durable public-route policy. |
| Timer trigger | agent/trigger identity plus normalized `ScheduleStatus.Next` from a monitored timer | Function invocation completes after the occurrence is durably staged; duplicate delivery resolves to the same staged request/run. One deterministic session per trigger registration serializes overlapping occurrences. |
| Connector trigger | agent/trigger identity, hashed connection identity, and authoritative provider event ID selected by authored `event_id_path` | Function invocation completes after the event is durably staged; duplicate delivery resolves to the same staged request/run. Payload claims never select an owner. |

If a trigger cannot supply stable event identity, durable admission for that
trigger fails validation rather than using a random key. Trigger payloads remain
subject to the existing serializer and content-reference limits. Trigger
support does not enable legacy chat, Dynamic Workflows, sub-agents, executable
skills, or the ordinary in-process runner while durable-loop mode is active.

Admission separates the **initiator** from the **public resource owner** and
adds a durable-only implementation of the reserved `trigger_binding` principal
kind for non-HTTP initiators:

- chat and Entra-authenticated HTTP use the resolved request principal for both;
- function/admin HTTP uses the existing app owner for both;
- timer and connector initiators are stable `trigger_binding` identities derived
  from stable app identity, agent slug, trigger registration, and hashed
  connection identity where applicable;
- anonymous HTTP uses the existing separate shared anonymous owner.

Timer and connector authoring requires an explicit `trigger.args.durable_owner`
that is compatible with `builtin_endpoints.http_auth`:

```yaml
durable_owner:
  kind: app                 # requires function or admin auth
allow_human_input: false

# or
durable_owner:
  kind: entra_principal     # requires entra auth
  tenant_id: <tenant-guid>
  object_id: <service-principal-object-guid>
allow_human_input: true
```

`app` reuses `FunctionAppPrincipal`. `entra_principal` pre-binds the run to the
exact service principal that the portal BFF presents through Easy Auth; current
Entra allowlists still apply on every request. Background triggers are rejected
under anonymous auth. A BFF authenticates its human user under application
policy, then calls the runtime as the configured service principal. This is not
payload-derived user ownership and adds no new runtime auth mode.

`DurableTriggerBindingPrincipalV1` extends the durable-loop
`_owner_hash` canonical input with trigger-registration and hashed connection
identity. It does not change session-state's version-pinned `OwnerContext`/`o1`
hash or claim FRD 0009's reserved `TriggerBindingOwnerContext`; this FRD owns
only the durable-loop initiator identity. The configured `entra_principal` path
creates the same canonical tenant/object owner shape as an Easy Auth Entra
principal, but from validated application configuration rather than request or
payload claims.

`request_human_input` is included in a background run's frozen tool catalog
only when `allow_human_input=true` and the configured owner can access the
public routes. Otherwise it is omitted and model attempts to call it fail as an
unknown tool. This prevents an unattended background run from parking on an
unanswerable wait.
Both current unconditional freeze paths in `durable_loop_catalog.py` and
`durable_loop_tools.py` are changed to use this same predicate.

Durable connector authoring requires `trigger.args.event_id_path`, a bounded
JSON-path subset that resolves one non-empty scalar provider ID after native
trigger serialization. It may optionally specify `session_id_path` to partition
the background session fence by a stable non-secret scalar; otherwise all events
for that trigger registration share one deterministic session. Missing, ambiguous, oversized, or non-scalar path results fail before staging;
the provider's configured failure/dead-letter behavior applies. The runtime
never falls back to a payload hash or random identifier. These runtime-only
arguments are removed before the Functions binding decorator is called.

Timer admission requires a non-null scheduled occurrence from the native timer
binding. Durable timer registration requires `use_monitor: true`. The normalized
UTC value of `TimerRequest.schedule_status["next"]` identifies the current
scheduled occurrence delivered by the monitor, including the first monitored
invocation. Missing, sentinel, non-UTC-normalizable, or otherwise invalid
`next` fails before staging with a configuration/host-contract error.
`past_due`, `last`, `last_updated`, current wall clock, and host invocation ID
are not identity fallbacks.
`run_on_startup: true` is rejected for durable timers because a startup
invocation has no independent schedule-derived occurrence identity.
These constraints apply to authored agent timers, not the runtime-owned
NCRONTAB sweeper.

`DurableTriggerAdmissionRecordV1` is keyed independently of session state by
owner hash, trigger registration, stable event/request ID, and normalized body
hash. Staging atomically appends that bounded record to one of a fixed number of
CAS-sharded `DurableTriggerPendingLedgerV1` page chains; the ledger entry is the
authoritative record, allocates the deterministic run ID, and is itself
discoverable recovery state. There is no separate record-then-index crash
window. A trigger reports delivery success only after the ledger append is
durable. Append CAS deduplicates by record key and returns the existing entry;
compaction retains one canonical entry per key.

After staging, the handler best-effort starts
`durable_trigger_admission_outbox_v1` with a deterministic instance ID derived
from the record key, then invokes the shared admission routine. A recurring
runtime sweeper walks the bounded active-page chain from each fixed shard head
and starts or reconciles the same deterministic outbox instance for every
pending entry. Compaction may remove only terminal/expired entries, so every
unresolved entry remains reachable from an active shard head. Host loss after staging
but before start is therefore recoverable without provider redelivery. Duplicate
deliveries or the sweeper may race starts, but deterministic instance identity
and existing start-status reconciliation permit only one logical outbox.

If admission is busy, capacity-limited, or transiently unavailable, the record
stays `pending`. The outbox retries with deterministic durable timers and the
same run/session/request identity until admitted, terminally rejected, expired
by the frozen admission deadline, or cancelled by an operator. Function-level
retry policies are not relied upon. Connector providers without retry/dead-
letter support are still safe after a successful stage because runtime retry is
owned by the outbox; failures before the CAS stage remain ordinary provider
delivery failures and are never acknowledged by the runtime.

The normalized request hash includes agent slug, trigger type, canonical
trigger-configuration identity, owner hash, session ID, stable event/request
ID, serialized payload, and derived prompt. HTTP session IDs follow the
existing validation rules but not its random fallback. A supplied
`x-ms-session-id` is validated and used; otherwise the generic HTTP trigger derives
a private session ID from owner hash, trigger registration, and
`Idempotency-Key`. Redelivery therefore selects the same session while unrelated
requests do not share committed context. The session-independent trigger
admission record is checked first; reuse of a key with a different explicit
session or body returns `409 idempotency_conflict`. Timer sessions are the canonical hash
of app identity, agent slug, and trigger registration. Connector sessions add
the resolved `session_id_path` partition when present. Admission/start
acknowledgement uncertainty, `session_busy`, capacity exhaustion, and transient
admission failure leave the staged record pending for outbox retry; they do not
create another run or rely on platform redelivery.

The durable registrar resolves one public `EndpointAuthConfig` even when legacy
`chat_api` is disabled. A new durable-specific validator requires declared HTTP
`http_auth`, background `durable_owner`, and the public policy to have exactly
compatible modes and ownership classes; missing or mismatched policy fails
startup. It does not reuse `resolve_aca_submission_auth`, whose current
fallback semantics are specific to ACA submission.

The prototype continues to admit exactly one durable main agent per Function
App, but that agent may be public-API-only or trigger-only and need not enable
legacy `chat_api`. The durable startup guard is narrowed from rejecting all
skills/triggers to accepting only the instruction-only skill and trigger subset
in this FRD; multi-agent durable apps, executable skills, and unsupported
triggers remain rejected.

For declared HTTP agents with a response schema, admission freezes the schema
and validator hash in the durable plan. The final-response activity validates
the model output before terminal commit, preserving the ordinary HTTP
contract; invalid output terminalizes the run with a bounded validation error.

### 4.6 Public run resource and event contract

The version-1 prototype keeps the existing route family:

```text
POST /experimental/durable-agent-runs
GET  /experimental/durable-agent-runs/{run_id}
GET  /experimental/durable-agent-runs/{run_id}/events?after_sequence={n}
GET  /experimental/durable-agent-runs/{run_id}/result
POST /experimental/durable-agent-runs/{run_id}/cancel
GET  /experimental/durable-agent-runs/{run_id}/input/{request_id}
POST /experimental/durable-agent-runs/{run_id}/input/{request_id}
```

Paths are relative to the effective Functions host prefix. Start responses
include absolute-path link relations so clients do not construct URLs. Public
request, response, link, event, error, and expiry documents are strict
`DurablePublic*V1` models in `experimental/durable_loop_protocol.py`; all errors
have `schema_version`, a stable `code`, the endpoint's backward-compatible
`error` string, `status`, and optional `resource_expiry`. New clients branch on
`code`; `error` is preserved for existing clients and may contain legacy
human-readable text.
Unknown fields are rejected on writes and may be ignored by clients on reads.

All data responses use `Cache-Control: no-store`, `X-Content-Type-Options:
nosniff`, and `Referrer-Policy: no-referrer`. Events use UTF-8 SSE with one
strict frame per `id:`/`event:`/`data:` group; `id` is the decimal sequence,
`event` is the public event type, and `data` is canonical JSON. Reconnect uses
either `after_sequence` or `Last-Event-ID`. When both are supplied they must be
equal; disagreement is rejected with `400 invalid_event_cursor`.

The resource semantics are:

| Operation | Required behavior |
| --- | --- |
| Start | Returns `202` with run ID, session ID, status, events, result, cancel, and applicable human-input links. Identical admission is replay-safe; conflicting use of one idempotency key returns `409`. |
| Status | Returns content-free lifecycle state, bounded progress, `created_at`, `updated_at`, and resource expiry timestamps. |
| Events | Returns an owner-authorized snapshot or contiguous events after an exclusive cursor. A cursor ahead of the visible watermark is rejected. |
| Result | Returns `202` until terminal; terminal success returns an immutable result while retained; terminal failure/cancellation returns its stable disposition. |
| Cancel | Is idempotent, same-origin protected for browser requests, and uses the existing durable delivery outbox. |
| Human input | Validates run, wait, owner/actor policy, schema, expiry, and submission idempotency before using the existing durable delivery outbox. |

The HTTP outcome table is normative:

| Operation/outcome | Status | Stable code/disposition |
| --- | --- | --- |
| Start admitted or replayed while active | `202` | `admitted` or `replayed`; includes all run links |
| Start acknowledgement uncertain | `202` | `run_start_acknowledgement_lost`, `possibly_committed=true`, same run ID |
| Start replayed after successful completion | `200` | `replayed`; includes retained immutable result |
| Start replayed after failed/cancelled completion | `410` | Retained terminal disposition and `possibly_committed` |
| Start idempotency/body conflict or interactive session busy | `409` | `idempotency_conflict` or `session_busy` |
| Start transient admission/capacity failure | `503` | `admission_unavailable` or `capacity_unavailable`; includes `Retry-After` when known |
| Start content persistence failure | `503` | `content_persistence_failed` |
| Start bounded entity receipt window exhausted before externalization | `503` | `idempotency_capacity_exceeded`; retryable after reconciliation |
| Status available | `200` | Current authoritative lifecycle projection |
| Result nonterminal | `202` | `result_not_ready` |
| Result successful and retained | `200` | Immutable result |
| Result failed or cancelled | `409` | Stable terminal error/disposition and `possibly_committed` |
| Result terminal projection exists but content is unavailable | `410` | `result_unavailable` or typed expiry |
| Cancel accepted or replayed while cancelling | `202` | `accepted` or `replayed` |
| Cancel already cancelled | `200` | `already_cancelled` |
| Cancel after successful/failed terminal state | `410` | `run_terminal` |
| Cancel delivery conflicts with current state | `409` | `cancel_conflict` |
| Human-input request unknown/foreign | `404` | `human_input_not_found` |
| Human-input answer conflicts with a retained submission | `409` | `human_input_conflict` |
| Events cursor valid | `200` SSE | Snapshot when required, then contiguous deltas |
| Events cursor invalid or ahead | `400` | `invalid_event_cursor` or `event_cursor_ahead` |
| Any owner-matching expired resource | `410` | Typed expiry code from section 4.8 |
| Missing/invalid authentication | `401` | Existing auth error |
| Unknown or foreign-owned resource | `404` | `run_not_found` |
| Retention/observation state cannot be read safely | `503` | `retention_state_unavailable` or `events_unavailable` |
| Staged trigger terminally rejected | `409` | `trigger_admission_rejected` with retained bounded reason |
| Staged trigger admission deadline elapsed | `410` | `trigger_admission_expired` |
| Staged trigger cancelled by operator | `410` | `trigger_admission_cancelled` |

SSE begins with either `event: snapshot` containing the complete bounded
projection and its through-sequence, or the first requested delta. Subsequent
frames use the generic event vocabulary. Keepalive frames are SSE comments
(`: heartbeat`) with no ID and no state transition. A bounded stream lease ends
with a comment and clean close; reconnect resumes from the last event ID.
Compaction returns a snapshot rather than a gap. Observation degradation may
close the stream with `event: observation_degraded` and status/result links.

An admission idempotency receipt is retained at least through the run
tombstone's replay deadline. Reuse of the same key and body before that deadline
returns the original run or its typed expired disposition; reuse with a
different body returns `409 idempotency_conflict`. After
`idempotency_expires_at`, the key may admit a new run. Start responses expose
that timestamp.

Public events are an observation API, not orchestration history. The existing
Blob journal remains the single implementation: immutable content is written
before a compare-and-swap manifest advances the public watermark; sequence
numbers are monotonic per run; snapshots reconcile compacted history.
The generic events route and journal initialization are registered for every
new durable run, independent of whether the hosted durable-chat UI is enabled.
The generic journal code is moved into an ungated durable observation module;
`durable_chat_*` remains a gated UI/V1 adapter over that module rather than
owning generic registration.
The data-free shell, `/experimental/durable-chat/config`, `/diagnostics`, and
retained `/sandbox` endpoint remain compatibility surfaces with their existing
auth and expiry behavior; they are not added to the generic public contract.

The public event vocabulary adds generic skill and run events while retaining
the current model/tool/sandbox observation adapters:

- `run_started`;
- `skill_search_completed`;
- `skill_load_started`;
- `skill_load_completed`;
- `model_progress`;
- `tool_started`;
- `tool_completed`;
- `human_input_required`;
- `assistant_delta`;
- `message_committed`;
- `run_completed`, `run_failed`, or `run_cancelled`;
- `observation_degraded`.

Every event includes schema version, run ID, sequence, timestamp, type, and a
strict event-specific payload. New runs use generic frame schema version 2.
Existing schema-version-1 chat runs remain readable until their original
expiry; an adapter projects their current strict union without rewriting stored
batches. The journal manifest identifies the frame schema, and the hosted UI
continues to consume its V1 adapter rather than raw V2 frames. The adapter maps
V2 `run_started` to its initial snapshot, `model_progress`/`assistant_delta` to
`assistant_text`, paired `tool_started`/`tool_completed` to one bounded `tool`
observation, `human_input_required` directly, and terminal run events to
`terminal`. Skill events are summarized into the bounded V1 system-status
projection. Public payloads contain no credentials, raw
skill instructions, provider IDs, encrypted reasoning, full tool
arguments/results, or authorization material.
The generic `/durable-agent-runs/{run_id}/events` route serves V2 for V4 runs
and projects legacy V1 storage into V2. The separate hosted
`/durable-chat/events` compatibility route always serves the V1 adapter,
including for V4 runs. No content negotiation changes schema on a given route.

Durable lifecycle/result state remains authoritative. Journal publication is
best effort and cannot fail, retry, or roll back successful execution. On read,
the existing reconciliation path may synthesize the missing terminal
observation from authoritative terminal state. Clients must treat
`observation_degraded` or unavailable events as a status/result fallback, not
as evidence that execution failed.

### 4.7 Public authentication and ownership contract

All public run-data routes reuse the agent's resolved public
`EndpointAuthConfig` and existing `_auth` helpers. Entra and key modes use
`resolve_owner_principal`; the existing durable-specific anonymous wrapper
produces the separate shared `anonymous_app` owner after the resolved policy
explicitly allows anonymous access. This FRD adds no new auth mode and does not
validate bearer tokens in-process.

| Auth mode | Runtime owner | Public contract |
| --- | --- | --- |
| `entra` | User owner derived from immutable `tid` + `oid` in a platform-validated Easy Auth principal | Recommended for user-facing portals. Every run, session, wait, event stream, result, and mutation is isolated to that owner. Missing or invalid identity returns `401`. |
| `function` / `admin` | App owner; key bytes and key names are never persisted | Intended for trusted service-to-service or operator access. Keys authenticate the app caller but do not create per-user ownership. |
| `anonymous` | Separate shared anonymous app owner | Explicit opt-in for demos only. It is not per-user isolation and cannot access Entra- or key-owned runs. |

Authorization is evaluated before status input, content references, event
snapshots, diagnostics, or results are returned. A valid caller requesting a
run owned by another principal receives the same `404 run_not_found` response
as an unknown run, preventing existence disclosure. Browser-originated
mutations retain the existing same-origin check in addition to authentication.

Admission freezes an `access_namespace_hash` over stable app identity, auth
mode, ownership class, and route family. It deliberately excludes mutable Entra
allowlists, model deployment identity, keys, tokens, principal headers, and all
credential material. A request must first satisfy the currently deployed
`EndpointAuthConfig`, including its current allowlists, then match the run's
frozen access namespace, ownership class, and owner hash. Tightening an
allowlist may deny access but does not rewrite run identity; restoring an
allowed principal restores access without migration.

A custom portal may call Entra-protected APIs directly or use a same-origin
backend-for-frontend. The recommended browser pattern is a same-origin BFF
protected by App Service Authentication so runtime credentials are not exposed
to JavaScript. This is deployment guidance, not a second runtime auth path.
Function keys must stay in headers, never URLs, logs, browser persistence, or
public event data.

Ownership is immutable for a run. Browser-local namespacing remains a
convenience only and is never authorization evidence.

### 4.8 Public retention and expiry contract

Retention is a runtime policy, not an incidental consequence of a Durable
backend or Blob lifecycle rule. The retention subsystem extends the existing
content and CAS keyed-document stores with three bounded records:

- `DurableRunResourceHeaderV1`: create-once run/owner/policy root, terminal
  projection, paged artifact-index head, and expiry timestamps;
- `DurableRunArtifactPageV1`: immutable bounded pages of exact content
  references and keyed-document names, linked by hash;
- `DurableObjectReferenceV1`: sharded per canonical content object ID
  (`kind` plus digest) with run ID, class, deadline, and a CAS deletion fence.

There is no single unbounded mutable run manifest. Producers know the content
digest before upload and acquire a pending object reference through CAS, then
commit-or-verify the immutable content, append it to the run's bounded artifact
page, mark the object reference live, and only then publish state that exposes
the reference. Immediately after upload and immediately before publication, the
writer re-reads the root/shard revisions and verifies that no deletion fence
was acquired; otherwise it reacquires/reuploads and retries. A crash leaves a timestamped pending reference discoverable
through a time-bucketed keyed cleanup index; recovery either completes the same
operation or expires the pending reference after its safety window.

For deduplicated content, cleanup first CAS-fences the object reference root as
`deleting` only after all bounded shards contain neither a live nor unexpired
pending run reference. New
acquisitions that observe the fence wait/retry and cannot publish the object.
Cleanup deletes the exact Blob, then marks the root absent; a new writer may
subsequently upload and publish the same digest. A concurrent acquisition that
commits a reference changes a shard/root revision and invalidates the cleanup
CAS. This closes check-then-delete races without depending on tags or Blob
metadata and works with hierarchical-namespace accounts.

The existing chat journal adopts tracked content puts. Its public CAS manifest
continues to root visible batches/snapshots; failed publication leaves only a
tracked pending/orphan candidate, never an unindexed Blob. Broad container
scans, wildcard prefix deletion, and storage lifecycle rules over shared
`objects/` or `runtime-state/` paths are forbidden.

Admission freezes policy durations and an initial active-run deadline. A CAS
terminalization update sources immutable `terminal_at` from the Durable
Entity's terminal receipt and computes explicit timestamps for each artifact
class without mutating the existing chat initialization document:

| Artifact class | Prototype default | Rule |
| --- | --- | --- |
| Public events and terminal result | 30 days after terminal state | Active and waiting runs are never aged out by the terminal-retention clock. |
| Tool receipts and uncertain-effect records | 90 days after terminal state | An unresolved ambiguous effect may extend retention under application policy. |
| Skill/catalog content and load receipts | While referenced by any content-bearing run resource, then 30 days | A content-bearing run means a run whose result/events/recovery records have not expired; the longer content-free tombstone does not extend the skill lease. A retained run never resolves a missing version to a newer one. |
| Human questions and answers | Until the run's public result expiry, or less under configured policy | No indefinite retention merely because input resumed a run. |
| Admission idempotency receipts | Through tombstone replay expiry; 90 days after terminal by default | Lost acknowledgements and repeated trigger deliveries cannot create a second run while the receipt is retained. |
| Trigger pending-ledger entry | Pending through the frozen admission deadline; terminal disposition through admission-receipt expiry | Acknowledged trigger delivery remains discoverable until admitted, explicitly rejected, expired, or cancelled. Terminal entries are compacted from active shard pages into the keyed receipt/tombstone record. |
| Session committed context | 30 days after the session's last successful commit, renewed by each successful turn | Never delete while the session has an active/waiting run. Reuse after expiry returns `410 session_expired`; callers create a new session. |
| Durable instance history | Through the longest recovery/public-read deadline, then purged | The retained public terminal projection and tombstone authorize expired responses after backend purge. |
| Minimal tombstone | 90 days after terminal by default and never shorter than admission-receipt retention | Contains resource ID hashes, owner/access-namespace hashes, terminal disposition, and expiry class only; no prompt, response, skill content, tool payload, or human answer. |

Defaults are configurable per application within runtime-defined minimum and
maximum bounds. Shorter human-content retention is allowed. Receipt retention
must not be configured below result retention when doing so would remove the
only evidence needed to reconcile an effect exposed by that result.

Every status/result/event response exposes the applicable expiry timestamp or
timestamps. Public reads have stable outcomes:

- `404 run_not_found`: no visible resource exists for this owner, including an
  ownership mismatch;
- `410 run_expired`: a matching tombstone proves that this owner's run existed
  but its public resource expired;
- `410 events_expired`: status/result may still exist but replayable events
  expired;
- `410 result_expired`: status or tombstone exists but result content expired;
- `410 session_expired`: the session context expired and cannot be silently
  reconstructed under the old session ID;
- `410 human_input_gone`: the wait is expired, consumed, orphaned, or terminal;
- `503 retention_state_unavailable`: the service cannot safely determine
  visibility or expiry and therefore fails closed.

Expiry changes availability immediately even if physical cleanup is delayed.
Cleanup is asynchronous, idempotent, owner- and reference-aware, and deletes
only exact resources in artifact pages, journal manifests, object-reference
records, or the pending cleanup index. Session committed context is rooted by a
separate `DurableSessionResourceV1`, renewed atomically on successful commit
and fenced against active admission. On expiry it is replaced by a minimal
owner/access-bound `DurableSessionTombstoneV1` retained for the same 90-day
default replay window; admission checks that tombstone before creating an empty
entity so an expired ID cannot silently become a fresh session. It never uses a wildcard rule over broad
`objects/` or `runtime-state/` prefixes. Content references' `retention_class`
values remain metadata until this cleanup component enforces them.

Long-lived admission receipts do not stay in the 128-entry Durable session
entity. Before entity admission, an activity checks the keyed receipt store.
The entity retains only its bounded recent/active window; after terminal state
is durably copied to the keyed receipt and run header, terminal entity entries
may be evicted. Replayed requests consult the keyed receipt and return the
original active, terminal, or expired disposition without consuming entity
capacity.

Public authorization first checks the run resource header/tombstone owner and
access namespace. Active runs then read Durable instance state; terminal runs
may use the retained immutable terminal projection. Durable instance purge is
allowed only after the run is terminal and every recovery deadline has passed.
Backend purge therefore cannot turn an owner-matching expired run into an
indistinguishable `404` while its tombstone remains.

The cleanup order is:

1. CAS-terminalize the run header, persist the keyed admission receipt, and create
   or update its content-free tombstone;
2. stop serving expired content;
3. remove unreferenced event batches, snapshots, results, human payloads, and
   skill content after their class deadline;
4. retain receipts and uncertain-effect evidence through their longer policy;
5. reconcile pending/orphan candidates created before a failed page/reference
   update;
6. purge admission receipts and the tombstone together only after every
   externally observable replay/expiry distinction is no longer required;
7. purge Durable instance history and expired session roots only after their
   independent fences and deadlines allow it;
8. compact terminal trigger-ledger entries into retained keyed
   receipt/tombstone dispositions and remove expired entries from active pages.

Changing application defaults affects new admissions only. Existing runs keep
their frozen policy unless an operator performs an explicit, audited shortening
required by law or incident response.

### 4.9 Compatibility and rollout

The durable loop remains behind its existing experimental gate. When the gate
is absent, ordinary discovery, triggers, chat, and `runner.py` behavior are
unchanged.

New admissions use `durable_agent_turn_orchestrator_v4`, a versioned V2 session
entity/document contract, and the new frozen budget/resource schemas. V4
subsumes normal V2 behavior and the V3 `model_apim_429_once` fault-injection
behavior through an explicit frozen plan flag; APIM routing recognizes V4 while
retaining the existing V3 check for old instances. V1, V2, and V3 orchestrator
functions and the V1 entity contract remain registered and unchanged until
their admitted instances finish. Code shared with those orchestrators may
receive additive helpers, but their recorded decisions, activity names, input
schemas, and replay behavior do not change.

The existing global `DURABLE_LOOP_SCHEMA_VERSION` remains `"1"` for legacy
protocol models and deterministic V1 call/model keys. New V2 plan/resource
models carry their own version fields, and new operation keys add plan version,
catalog hash, and operation kind without changing legacy key derivation.

Existing durable start/status/result/cancel/human-input routes and the hosted
chat continue to work. Their implementation is adapted to the generic public
models and journal rather than duplicated. Existing clients that do not request
skills receive the same model/tool behavior. Newly admitted runs write generic
schema-v2 event frames and indexed resource records. Readers remain dual-stack
for schema-v1 chat observations and documents.

Automatic cleanup is opt-in by indexed schema. Unindexed V1 artifacts are never
inferred from paths or deleted automatically because non-chat V1 runs do not
have a complete expiry/reference index. V1 hosted-chat runs retain their
original immutable admission-relative expiry semantics. Operators may run a
separate audited migration that proves ownership, references, and deadlines
before writing new indices; failure to prove any field leaves the legacy
artifact untouched. There is no implicit migration during reads or admission.

Ordinary built-in `/agents/{slug}/chat` and `/chatstream` handlers remain
disabled in durable-loop mode in this prototype. Interactive clients use the
public durable start route plus status/result/events, and the existing hosted
durable chat remains an adapter over those resources. This avoids introducing
a second async compatibility behavior under the legacy synchronous routes.

Executable skills, sub-agents, Dynamic Workflows, session runtime, legacy chat,
and MCP-agent surfaces remain fail-closed in durable-loop mode unless a later
FRD explicitly admits them.

### 4.10 Private prototype configuration

The following process settings extend `DurableLoopSettings.from_environment()`.
They remain private experimental settings and are validated fail-closed at host
startup. Durations are integer seconds:

| Setting | Default | Valid range / purpose |
| --- | ---: | --- |
| `AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_SKILL_PROVIDER` | `filesystem` | Selects `filesystem`, `blob`, or a registered provider ID. |
| `AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_SKILL_BLOB_URI` | unset | Credential-free HTTPS service/container origin for the optional Blob provider. |
| `AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_SKILL_BLOB_CLIENT_ID` | unset | Optional user-assigned managed-identity client ID; secrets and SAS values are rejected. |
| `AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_EVENT_RESULT_RETENTION_SECONDS` | `2592000` | 1 hour-365 days; public events and result retention after terminal state. |
| `AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_RECEIPT_RETENTION_SECONDS` | `7776000` | Result retention-730 days; effect and admission receipts after terminal state. |
| `AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_SKILL_GRACE_SECONDS` | `2592000` | 1 day-365 days after the final content-bearing run reference. |
| `AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_HUMAN_CONTENT_RETENTION_SECONDS` | `2592000` | 0-result retention; capped by result expiry. |
| `AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_SESSION_RETENTION_SECONDS` | `2592000` | 1 day-365 days after the last successful turn. |
| `AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_TOMBSTONE_RETENTION_SECONDS` | `7776000` | At least admission-receipt retention and at most 730 days. |
| `AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_TRIGGER_ADMISSION_DEADLINE_SECONDS` | `604800` | 5 minutes-30 days from durable staging; pending outbox retry stops with an explicit expired disposition after this deadline. |

Runtime-defined minimum/maximum validation enforces the ordering constraints in
§4.8. Existing durable content storage settings remain the storage authority;
the Blob skill provider does not introduce a second credential mechanism.
Provider-specific opaque secrets are not accepted in agent files.

An agent's existing authored skill list is the complete grant set for admitted
skill IDs. The provider may contain more records, but admission filters to those
grants before catalog hashing, initial disclosure, or search. No new skill
authorization field is introduced.

Supported durable trigger frontmatter adds private fields under `trigger.args`:

- `durable_owner`, as specified in §4.5;
- connector `event_id_path` and optional `session_id_path`;
- optional `allow_human_input`, defaulting to `false` for background triggers.

Unknown provider IDs, missing provider configuration, invalid durations,
incompatible trigger/public authentication, unresolved authored paths, and
unsupported owner/input combinations fail application registration.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Scope | Product-specific integration / Generic runtime capability | Generic runtime contracts only; application policy and UI remain outside the runtime. | Human | 2026-09-29 |
| 2 | Existing durable machinery | Replace / Duplicate / Reuse and formalize | Reuse admission, ownership, content, receipts, and journal; add only missing contracts and lifecycle enforcement. | Human | 2026-09-29 |
| 3 | Skill execution | Instruction-only / Executable assets | Instruction-only; executable skills stay blocked because loading instructions must not add effect authority. | Human | 2026-09-29 |
| 4 | Skill storage packaging | All providers in core / Core contract plus extensions | Core provider contract and filesystem adapter; first-party Blob provider as a separately pinned package or extra. | Human | 2026-09-29 |
| 5 | Initial metadata | Full catalog / Fixed bounded subset plus search | At most 32 records, 320 code points each, and 24 KiB total; overflow uses paginated search. | Human | 2026-09-29 |
| 6 | Resume-time skill loading | Never / Unrestricted / Admitted catalog only | Permit new loads after human input only from the frozen catalog and existing grants. | Human | 2026-09-29 |
| 7 | Event authority | Durable history / New log / Existing external journal | Durable state owns lifecycle; the existing Blob journal owns replayable observations and remains execution-independent. | Human + Agent | 2026-09-29 |
| 8 | Public authentication | New portal auth / Existing endpoint auth | Reuse `EndpointAuthConfig` and owner principals; Entra provides user ownership, keys provide app ownership, anonymous is shared opt-in. | Human | 2026-09-29 |
| 9 | Portal integration | Shared browser key / Direct token only / BFF only / Both Entra patterns | Support direct Entra or same-origin BFF; recommend BFF, with no new runtime credential mechanism. | Agent | 2026-09-29 |
| 10 | Public retention | Storage-default / One TTL / Artifact classes | Freeze per-run policy: events/results 30 days, receipts 90 days, reference-aware skills, minimized human content, and content-free tombstones. | Human | 2026-09-29 |
| 11 | Expiry semantics | Collapse into 404 / Stable 410 states | Preserve 404 concealment for unknown/foreign resources and return typed 410 responses only when an owner-matching tombstone proves expiry. | Agent | 2026-09-29 |
| 12 | Trigger execution | Inline per trigger / Normalize to durable admission | Supported triggers only validate, idempotently admit, and return; the durable loop is the sole execution engine. | Human | 2026-09-29 |
| 13 | Background-trigger ownership | Infer user / App-owned / Explicit Entra principal / No public resource | Require authored `durable_owner`: app ownership under key auth or one explicit BFF service principal under Entra; never derive ownership from payload data. | Agent, review amendment | 2026-09-29 |
| 14 | Cleanup authority | Prefix lifecycle rules / Single run manifest / Paged references and deletion fences | Use bounded artifact pages, sharded per-object CAS references, pending-reference indices, and exact deletion only. | Agent, review amendment | 2026-09-29 |
| 15 | Provider immutability | Best-effort version lookup / Retained snapshot | Admission opens a retained immutable snapshot and materializes content when a provider cannot guarantee later reads. | Agent, review amendment | 2026-09-29 |
| 16 | Public protocol version | Informal semantics / Strict V1 resources and V2 event frames | Fix routes, HTTP/SSE framing, errors, links, cache headers, and compatibility behavior before implementation. | Agent, review amendment | 2026-09-29 |
| 17 | Runtime skill operations | Ordinary tools / Reserved orchestrator operations | Inject reserved declarations, intercept before dispatch, and apply separate deterministic budgets. | Agent, review amendment | 2026-09-29 |
| 18 | Timer overlap | Session per occurrence / Session per trigger | Use one deterministic session per timer registration; the existing active-turn fence serializes scheduled occurrences. | Agent, mapping amendment | 2026-09-29 |
| 19 | Connector identity | Payload hash / Conventional field guesses / Authored paths | Require `event_id_path`; optionally use `session_id_path`, and fail closed when either authored path cannot resolve safely. | Agent, mapping amendment | 2026-09-29 |
| 20 | Durable HTTP validation | Generic text result / Preserve response schema | Freeze and apply the declared response schema before terminal commit. | Agent, mapping amendment | 2026-09-29 |
| 21 | Legacy chat routes | Convert to async / Keep blocked | Keep legacy synchronous chat routes blocked; clients use the public durable run API and existing hosted adapter. | Agent, mapping amendment | 2026-09-29 |
| 22 | Admission-receipt retention | Keep all in entity / Evict without replay / External keyed receipts | Persist long-lived receipts in the keyed CAS store and retain only a bounded active/recent entity window. | Agent, review amendment | 2026-09-29 |
| 23 | Durable backend retention | Keep forever / Purge with resources / Independent safe purge | Purge instance history after recovery deadlines; retain a minimal owner-bound terminal projection/tombstone for public expiry semantics. | Agent, review amendment | 2026-09-29 |
| 24 | Session context retention | Indefinite / Run-only / Idle session TTL | Retain committed context for 30 days after the last successful turn, fenced by active runs; expired reuse returns `410 session_expired`. | Agent, review amendment | 2026-09-29 |
| 25 | Deterministic rollout | Modify V3 / Version orchestration and state | Admit new functionality only through orchestrator V4 and versioned V2 entity/documents; preserve V1-V3 replay. | Agent, review amendment | 2026-09-29 |
| 26 | Legacy cleanup | Infer paths / Automatically index / Explicit migration only | Never auto-clean unindexed V1 artifacts; preserve V1 chat expiry and require an audited proof-based migration to index legacy data. | Agent, review amendment | 2026-09-29 |
| 27 | Background retry | Platform retry / Function-level retry / Durable staged outbox | CAS-stage delivery in a sharded pending ledger before acknowledgement; deterministic outbox plus recurring sweeper owns admission retry. | Agent, phase-2 review amendment (revised) | 2026-09-29 |
| 28 | Skill activity envelope | Return instructions / Return content reference | Preserve the refs-only Durable invariant; only the model activity resolves loaded instruction content. | Agent, phase-2 review amendment | 2026-09-29 |
| 29 | Trigger identity implementation | Reuse session-state reserved shape / Durable-specific initiator | Add `DurableTriggerBindingPrincipalV1` to durable `_owner_hash` input without changing FRD 0009 or session-state `o1` identities. | Agent, phase-2 review amendment (revised) | 2026-09-29 |
| 30 | Timer occurrence identity | Previous occurrence / Current monitored occurrence / Wall clock | Require monitored `ScheduleStatus.Next`, reject startup invocations and every non-schedule fallback. | Agent, phase-2 review amendment | 2026-09-29 |
| 31 | Generic HTTP session | Random fallback / One trigger session / Explicit-or-derived | Use validated `x-ms-session-id` when supplied; otherwise derive from owner, trigger registration, and idempotency key. | Agent, phase-2 review amendment | 2026-09-29 |
| 32 | FRD sign-off | Revise / Defer / Approve | Approved as written; finalize FRD 0013 and authorize implementation on `harshivcodes/durable-skills-and-events`. | Human | 2026-09-29 |

## 6. Test plan

- [ ] Unit `tests/test_durable_skill_providers.py`: strict metadata/content
      parsing, immutable versions, hash verification, executable rejection,
      duplicate IDs, explicit provider failures, and Blob adapter boundaries.
- [ ] Unit `tests/test_durable_loop_catalog.py`: deterministic catalog hashing,
      32/320/24-KiB metadata budgets, complete-record truncation, cursor
      validation, and tool/skill authority separation.
- [ ] Unit `tests/test_durable_loop_protocol.py`: skill catalog/load receipts,
      retention policy/tombstones, public event payloads, duplicate-key
      rejection, size limits, and canonical hashes.
- [ ] Unit `tests/test_durable_loop_activities.py`: exact-version loads,
      content-hash mismatch, idempotent reload, restart/replay reuse, ordered
      instruction composition, ref-only activity envelopes, context/token/cost
      accounting, and no tool-grant mutation.
- [ ] Unit `tests/test_durable_loop_registration.py`: skill steps before/after a
      human wait, frozen catalog after activation changes, provider
      unavailability, V4/V2-state admission, unchanged V1-V3 replay, external
      admission-receipt lookup, bounded entity eviction, and continue-as-new
      preservation. V4 also covers normal V2 and APIM-429 V3 fault behavior.
- [ ] Unit `tests/test_registration_triggers.py`: HTTP, timer, and connector
      admission normalization, deterministic idempotency, duplicate delivery,
      conflict, timer overlap, connector event/session path extraction,
      required timer monitor state and `ScheduleStatus.Next`, startup rejection,
      stable `x-ms-session-id` or derived HTTP sessions, sharded pending-ledger
      staging, deterministic outbox start races, sweeper recovery after host
      loss, admission-deadline expiry, busy retry, response-schema commit
      validation, and no inline runner/model invocation.
- [ ] Unit `tests/test_durable_loop_http.py`: public links, auth modes, same-origin
      mutation checks, cross-owner `404`, direct/BFF Entra ownership, and stable
      ready/terminal/expired responses.
- [ ] Unit `tests/test_durable_chat_journal.py` and
      `tests/test_durable_chat_http.py`: generic skill/run events, sequence
      replay, compacted snapshots, terminal reconciliation, and degraded
      status/result fallback.
- [ ] Unit `tests/test_durable_retention.py`: frozen deadlines, active-run
      protection, typed `410` states, tombstones, reference-aware cleanup,
      admission-receipt lifetime, bounded artifact-page rollover, pending
      reference recovery, shared-reference/deletion-fence races, session
      renewal and active-run fencing, Durable instance purge, terminal
      projection authorization, longer effect-receipt retention, retry safety,
      and no broad-prefix deletion.
- [ ] Fixture scenarios under `tests/fixtures/config_scenarios/`: durable
      instruction-only skills accepted; executable skills and trigger identities
      without stable idempotency rejected.
- [ ] Recovery tests: host loss after skill load, catalog change while waiting,
      host loss between trigger staging and outbox start, duplicate
      timer/connector delivery racing the sweeper, observation publication
      failure, cleanup retry, and ambiguous tool effect retained after result
      expiry.
- [ ] Compatibility tests: existing durable schema-v1 records and hosted chat
      remain readable; V1 chat retains original expiry; unindexed V1 artifacts
      are excluded from cleanup; explicit migration is fail-closed; gate-off
      ordinary runner, skills, triggers, and endpoint behavior remain
      unchanged.
- [ ] Testing review confirms failure, replay, authorization, and cleanup
      boundaries before the full repository gate runs.

## 7. Docs impact

- [ ] `docs/architecture.md` - add durable skill, public resource, and retention
      ownership to the private durable-loop flow and module map.
- [ ] New `docs/durable-agent-loop.md` - document progressive skills, public run
      APIs, auth ownership table, expiry codes, and operator retention duties.
- [ ] `docs/durable-agent-loop-chat-ui.md` - point to the generic public contract
      and retain only UI-specific behavior.
- [ ] `docs/front-matter-spec.md` and generated reference - update only if
      retention bounds become public authoring fields.
- [ ] `docs/triggers.md` - document durable admission, idempotency requirements,
      and asynchronous responses for supported triggers.
- [ ] `README.md` - add an experimental example only after implementation is
      validated.
- [ ] `docs/frds/README.md` - list FRD 0013 and its current status.

## 8. Status & sign-off

- **Architecture review (phase 2):** Approved 2026-09-29 after three independent
  passes against `docs/architecture.md`, FRD 0010's deterministic/effect
  boundaries, FRD 0012's journal and owner model, and the actual branch
  implementation. No blocking or high-severity findings remain.
- **Human sign-off:** Approved 2026-09-29. FRD status is `Finalized`;
  implementation is authorized on `harshivcodes/durable-skills-and-events`.
