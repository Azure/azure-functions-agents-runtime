---
frd: 0009
title: Public Durable Agent Loop
status: Finalized
author: larohra
created: 2026-09-16
updated: 2026-09-23
issues: []
pull_requests: [226, 234]
branch: larohra/durable-agent-loop
---

# FRD 0009 - Public Durable Agent Loop

## 1. Summary

- Opt-in public-preview Durable execution for markdown-first MAF agents.
- HTTP/chat first; one authorized session Entity; one orchestration per turn.
- One foreground model step or tool invocation per activity checkpoint.
- Optional session-bound Sandbox Group; remote MCP stays worker-side.
- **Architecture finalized:** human sign-off recorded in §8; implementation
  proceeds through the scoped delivery layers and required qualification gates.

## 2. Motivation / problem

- Baseline `8c8a682`: `runner.py` executes the whole MAF loop in-process;
  session locking is process-local.
- Ordinary Blob/file history does not coordinate turns across workers.
- Existing workflow infrastructure supplies native Durable conventions, not
  this conversation loop.
- Leadership/UI/ACA spikes supply reusable parts, not a branch-wide port.

## 3. Goals / Non-goals

| Goals | Non-goals |
| --- | --- |
| Global/per-agent opt-in; unchanged ordinary path when disabled/absent | New runtime selector; replacing MAF |
| Mandatory Entity history; owner-bound IDs; retry-safe admission | Exactly-once external effects |
| Per-call recovery; parallel tool batches; durable human input | Provider background polling; whole-loop activities |
| Same sandbox across session turns; explicit workspace loss | Sandbox recreation, cross-session workspaces, persistent processes |
| Native offload; complete model state; explicit capacity failures | Silent truncation; custom transcript store |
| Qualified identity, isolation, networking and operational profiles | GA/compliance claims from preview evidence |

- V1 supports HTTP/chat ingress. Inbound agent-as-MCP-tool exposure is deferred
  to post-v1; outbound remote MCP tools remain supported worker-side.
  Other authored trigger kinds remain outside v1.
- **Post-v1:** Durable Agent Loop composition with Dynamic Workflows (FRD 0004),
  `subagents:` delegation (FRD 0007), or `workflows.subagents`.
- Independent ordinary, workflow-enabled and durable agents may coexist in one
  app; the restriction is composition, not app-wide coexistence.
- Reject unsupported configurations and references before registration;
  never fall back to the ordinary runner.
- No ingress/tool-policy DSL or tool-action approval engine.
- The existing debug UI gains Durable result polling in v1 when enabled.
  Richer UI/SSE and Timer research remain outside core release dependencies.
- §4.15 tracks post-v1 fast-follows and distinguishes agreed deferrals from
  optional work and uncommitted candidates.

## 4. Proposed design

### 4.1 Pipeline and module ownership

| Stage | Modules | Responsibility |
| --- | --- | --- |
| Discover | `discovery/{tools,skills,mcp}.py` | Read-only inventories; stable descriptors |
| Translate | `config/{schema,merge,validation,loader}.py` | Shared typed settings; inheritance; compatibility |
| Compose | `app.py`, `registration/{catalog,capabilities}.py`, new `durable/policy.py` | Freeze policies/catalogs; choose one `DFApp` |
| Register | `registration/{triggers,_handlers,endpoints,_auth}.py` | Per-agent authenticated HTTP dispatch; shared Durable execution bindings |
| Execute | New `durable/{engine,activities,model,tools}.py` | Native orchestration; separate model/tool checkpoints |
| Session | New `durable/{session,contracts,intake}.py` | Entity authority; admission; versioned envelopes |
| Sandbox/lifecycle | New `durable/{sandbox,lifecycle}.py` | Binding, packaging, resume, expiry, cleanup |
| Debug UI | Existing `public/index.html`, endpoint adapter | Public API client; automatic polling for Durable agents |
| Shared retry mechanics | New `_durable_retry.py`; workflow and Durable adapters | SDK retry mapping and bounded sanitized envelope codec; no workflow-plan or agent-outcome imports |

- Validate the complete agent graph before app mutation; clients remain lazy.
- Registration remains Azure-aware; no YAML reparsing or discovery-time provisioning.
- Reuse workflow/auth/identity conventions without importing workflow-plan semantics.
- `registration/catalog.py:build_catalog()` is followed by a side-effect-free
  `durable/policy.py:build_durable_agent_policy_catalog(catalog)`. The immutable,
  slug-keyed `DurableAgentPolicyCatalog` freezes resolved limits, effective auth,
  model/deployment bindings, sandbox-group settings and manifest/package digests
  in pass 1 before app creation. Durable execution reauthorizes by slug against
  this catalog and never reads YAML, front matter or `GlobalConfig`.
- This catalog is process-local configuration, not a cross-deployment version
  lock. New work uses the executing worker's deployed configuration; §4.11
  defines persisted-session continuity and the operator's compatibility duties.
- `app.py:create_function_app()` selects one `DFApp` when either workflow or
  Durable policies exist. Workflow and Durable app-wide runtimes register
  independently and at most once before per-agent registration; neither reads
  the other's policy catalog.

### 4.2 Authoring and inheritance

```yaml
# agents.config.yaml
http_auth: entra
durable:
  enabled: false
  history:
    session_ttl: P10D  # Explicit example, not a default.
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
    # Alternatively: disk_id: $APPROVED_CUSTOM_DISK_ID
```

| Setting | Resolution |
| --- | --- |
| Global `durable` | Object only; absent `enabled` defaults false |
| Per-agent absent / `true` / `false` | Inherit / enable with inheritance / disable |
| Per-agent object | Recursively override authored fields |
| Count ceiling omitted / `null` | Inherit / clear inherited ceiling |
| `sandbox_group: null` | No workspace for new sessions |
| Group override | Complete resource-ID/region pair; `disk`/`disk_id` mutually exclusive |

- Shared validators; reject unknown keys, boolean/nonpositive counts,
  nonfinite durations and incompatible settings.
- `compose()` remains pure translation. It resolves Durable inheritance and
  retains the existing required `ResolvedAgent.timeout` value populated by the
  per-agent/global/environment/900-second chain.
- Per-agent validation rejects effective `durable.enabled: true` with
  `workflows.enabled: true`, nonempty `subagents`, nonempty
  `workflows.subagents`, or explicit workflow/delegation capabilities.
- App-wide validation runs beside duplicate-slug and subagent-reference checks
  before capability construction. It rejects any `subagents` or
  `workflows.subagents` reference targeting a Durable agent, including from an
  ordinary or workflow-enabled coordinator.
- Do not inject workflow-management tools (including `start_workflow`) or
  `delegate_<slug>` wrappers into durable agents. Explicit tool references
  resolving to those runtime capabilities are invalid; prompt text is not scanned.
- No default `max_model_steps` or `max_tool_calls`; `durable.limits` contains
  optional call-count ceilings only and introduces no second timeout setting.
- Preserve the existing authoring contract: effective `ResolvedAgent.timeout`
  is the deadline for one complete logical agent run/turn, including model
  steps, tools, retries and human-input waits. Durable orchestration records the
  deadline from its deterministic start time and races unfinished work against
  a Durable timer; replay never resets or extends it.
- The clock starts with the turn orchestration, not the admission request;
  pre-start handoff/reconciliation latency is outside this run timeout.
  ContinueAsNew carries the original absolute deadline with the call counts.
  An elapsed deadline ends the turn as `timed_out`; a missing required deadline
  is an explicit incompatible-contract failure, never a fresh timeout window.
- On expiry, stop scheduling new calls and durably record terminal `timed_out`.
  Durable activity dispatch does not itself provide remote cancellation. Request
  best-effort cancellation only where an operation has a qualified cancellation
  channel; otherwise dispatched work may continue. Reject late conversation
  commits by turn generation and retain unfinished call IDs as `outcome_unknown`.
  This fences state updates, not external effects or shared-workspace writes.
  Use §4.11's bounded best-effort cancellation; unknown external effects do
  not impose a quarantine gate on subsequent turns.
  Session idle TTL starts after the timed-out turn is committed.
- Provider, Functions-host and infrastructure ceilings may terminate an
  individual attempt earlier, but do not replace or extend the user-visible
  whole-run deadline.
- Hosting, SDK and provider constraints still apply; session idle TTL is separate.
- Count logical scheduled calls per turn, including model-based compaction;
  replay does not recount; ContinueAsNew carries counts.
- Initial effective prompt states configured budgets; later prompts state
  remaining counts. No budget instructions for absent ceilings.
- Final available model step is answer-only. Oversized tool batch: dispatch
  none; record truthful outcomes; finalize within remaining budget or report
  `budget_exceeded`. `tools: false` disables tools.
- Physical retries/billing are not bounded by logical call counts.
- Native provider, task hub, offload and transport settings stay in
  `host.json`/app settings, not agent YAML.
- Durable agents preserve the existing effective endpoint auth modes for
  admission and management endpoints except `anonymous`, which has no stable
  owner credential. Entra, function-key and admin-key ingress all map to the
  owner model in §4.4. The separate static debug page in §4.13 carries no run
  data or credentials and is outside this admission-auth rule.
- V1 rejects effective `durable.enabled: true` with
  `builtin_endpoints.mcp: true`, including the `builtin_endpoints: true`
  shorthand, with a diagnostic that inbound Durable MCP is deferred and that
  authors should explicitly enable `chat_api` instead. Never silently expose
  the ordinary runner for that agent. Ordinary agents' built-in MCP endpoints
  and Durable agents' outbound `mcp.json` tools are unchanged.
- `chat_api` means Durable admission; ordinary `chatstream`, blob-backed
  history and workflow-status endpoints are not registered.

### 4.3 HTTP session contract

- Validate `x-ms-session-id` through `_session_id.py`'s shared
  `SESSION_ID_PATTERN`; case-sensitive values. Root payload `session_id`, when
  supplied, must agree.
- Reject malformed, blank, non-string or conflicting IDs; no recursive
  business-payload searches or per-agent correlation mappings.
- Session ID is correlation, **not authentication**.
- `Idempotency-Key` is the sole client retry key (`logical_request_id`):
  case-sensitive, full-match `[A-Za-z0-9._-]{1,128}`. Missing/invalid keys return
  400 before admission; no body alias, trimming or case folding.
- Retries recover the same generated session; a new request key creates another
  session unless the caller supplies the returned session ID to continue the conversation.
- Identity scope within the bound backend/task hub is exactly the ordered
  tuple `(agent_slug, normalized_owner_id, entry_id)`. V1 `entry_id` is
  `builtin` for the built-in chat entry or `http_trigger` for the agent's
  authored HTTP trigger, assigned by registration, never accepted from the
  request. Route parameter values, route text, credentials, delivery/attempt
  metadata and a separate app identifier are not additional scope fields.
  Use these same entry IDs in registration names and policy lookup.
- Retries are scoped to the originating entry. The same owner/session/key
  submitted through the other entry does not deduplicate to the original run;
  generated session/request IDs and internal session lookup remain entry-scoped.
  Return management links for the originating entry and use its auth policy.
  Renaming a route does not change its entry ID; adding multiple authored
  HTTP entries per agent would require a separately defined stable-ID contract.
- `H(tag, fields...)` is lowercase SHA-256 of domain-tagged, length-prefixed
  UTF-8 fields; `scope` expands to the ordered fields above.
  Normalize `session_id` first: supplied ID, otherwise
  `H("session", scope, Idempotency-Key)`.
- Return `request_id = H("request", scope, session_id, Idempotency-Key)`;
  never a fresh random request ID. Retrying with the returned session ID
  preserves this mapping. Persist the accepted receipt.
- Caller IDs are allowed; atomic creation binds owner and immutable incarnation.
  Another owner cannot claim an existing ID.
- Use non-revealing internal IDs; mask missing/unauthorized resources consistently.
  Tombstones prevent implicit resurrection after expiry/deletion.

### 4.4 Ownership

- **App isolation uses the existing bound backend/task-hub namespace.** No
  additional app ID is serialized into identity hashes, and no new setting or
  runtime-generated UUID is required. A task-hub name alone is not globally
  unique: the configured provider/backend and hub together locate the state.
  The client binding selects that namespace; do not reconstruct it by hashing
  connection strings, credentials, hostnames or deployment metadata.
- Scope every orchestration, Entity, receipt, owner index and management lookup
  to that same bound namespace. Request parameters cannot override the backend,
  connection or hub. Identical logical IDs in different backend/hub namespaces
  are valid and confer no cross-namespace access. Any process-local caches must
  likewise remain isolated by bound client context.
- Independent apps and deployment slots require isolated task hubs; sharing one
  means sharing execution/state infrastructure, not achieving app isolation.
  Keep the production backend/hub configuration slot-sticky where slots are
  used. Code deployment or hosting-resource rename does not change identity
  while the bound backend/hub remains the same. Changing a configured or
  default-derived hub/backend selects a different state namespace, not an
  automatic ownership transfer; moving state requires explicit operator-owned
  migration. Backend access authorization and caller ownership are separate.
- C2 must verify namespace confinement on the pinned client/Entity stack:
  identical IDs in two isolated hubs cannot cross-read/write, all workers for
  one hub see the same state, and compatible deployments retain that state.
  This is a qualification requirement, not a claim that those tests have run.
- Normalize every authenticated ingress to a non-secret owner ID:
  - Entra: verified `(tenant_id, object_id)`; `_auth.py` requires exactly one
    `tid` and one `oid`, and absent or multi-valued claims fail closed with 401.
    Platform-enforced Easy Auth and a configured tenant allowlist are required;
    the operator-only `AZURE_FUNCTIONS_AGENTS_ENTRA_EASY_AUTH` assertion is not
    sufficient for Durable ownership.
  - Function/admin: a domain-separated SHA-256 fingerprint of the credential
    presented on the host-authorized request: `H("key-owner", credential)`,
    using §4.3's encoding. It is interpreted only inside the bound backend/hub,
    not salted with a code version, app ID or auth-level label. All callers
    presenting the same key share one owner and may access that key owner's runs;
    rotating the key creates a new owner and does not transfer old runs.
    The raw key is never logged, checkpointed or returned.
  - Anonymous: unsupported because it supplies no stable credential or principal.
- App IDs, body claims and spoofable identity headers are not owner identity.
- Preserve built-in global `http_auth` and custom `trigger.args.http_auth`
  precedence; reject incompatible effective auth instead of silently upgrading it.
- Extend `_auth.py` without breaking its ordinary allow/error helper.
- Reauthorize management, answers and tool execution against current policy;
  captured descriptors cannot retain revoked permission.
- No cross-owner delegation policy in v1.
- Never expose system keys, raw DTS management URLs, Entity state or storage references.
- The Functions host remains responsible for validating function/admin keys
  before the handler runs. The runtime fingerprints only the validated presented
  credential and fails closed if the hosting surface cannot provide it.
- Each agent HTTP entry registers submission and its management paths in
  **one function**, with one effective `AuthLevel` and one set of function keys.
  Function-scoped keys are supported; do not require a host-wide key to follow
  that entry's returned status/history/input/cancel/delete links. There is no
  shared management function that demands a different key. Different agents
  can retain different effective auth modes.
- Reject repeated credential carriers or conflicting `x-functions-key` and
  `code` values with `400 ambiguous_credential`; never guess which credential
  the host accepted. Qualify carrier visibility and host enforcement on the
  supported stack, including master keys and proxies. A missing carrier or
  bypassed host auth cannot be treated as a verified key owner. Never broaden
  ownership to all key holders merely because credential propagation is absent.

### 4.5 Admission, idempotency and reliable handoff

- Require `Idempotency-Key` and normalized request-body fingerprint.
- Remove the root `session_id` carrier before fingerprinting; the normalized
  target session is already part of request identity.
- Entity serializes authorization, lifecycle checks, accepted-request lookup
  and the one-active-turn decision; no process lock or separate ordering service.
- Request acceptance and arranging execution must be **one reliable durable
  handoff**. Return acceptance only after both are durably established.
- Native b3/`durabletask==1.10.0`:
  [`EntityContext.schedule_new_orchestration(orchestration_name, input=None, instance_id=None) -> str`][entity]
  (`durabletask.entities`; also exposed by `DurableEntity`).
- Supply `instance_id=stable_run_id` from scoped request identity; the default is a UUID.
- SDK stages the start action with state/reply in the same Entity completion.
  Admission orchestration obtains/returns that decision; **no separate later client start**.
- This is source evidence, not proven provider failure behavior.
- Retry locates the **same accepted job/run and receipt**, never a blind
  replacement. Unknown handoff outcomes require recovery of that job.
- Missing/purged run lookup never authorizes replacement.
- A stable instance-ID string does not prove the same execution;
  this Entity API has no client `reuse_id_policy`.
- The Entity assigns a monotonically increasing generation to each accepted turn.
  The turn orchestration must receive an accepted start acknowledgement from the
  Entity for that generation before dispatching any model/tool activity.
  Admission also schedules a bounded, generation-tagged delayed self-signal.
  If no start acknowledgement exists at reconciliation, the Entity transitions
  the accepted turn to terminal `start_unconfirmed`/`outcome_unknown`, releases
  the active-turn slot and starts idle TTL. It does not create a replacement run.
- Slot release does not prove that the original orchestration is dead. The Entity
  rejects late start, model/tool, answer-consumption and terminal commits unless
  they belong to its current active turn generation. A fenced orchestration stops
  scheduling and cannot alter the newer turn's conversation. Once a start
  acknowledgement has been accepted, a lost reply is not grounds for this
  no-start slot-release path; use §4.11 recovery instead.
- Short admission instances return authoritative Entity decisions; signal
  acknowledgements and stale reads are not admission results.
- Admission-attempt identity is separate from accepted-request identity;
  bound late attempts with a trusted deadline.

| Result | HTTP contract |
| --- | --- |
| Accepted and execution arranged | 202; session/run/request IDs; relative status links |
| Accepted key + same fingerprint | Original receipt/session/run, including after completion; no new turn |
| Same key + different fingerprint | 409 `idempotency_conflict` |
| Different request while active | 409 `session_busy`; no implicit queue |
| Busy rejection retried later | Fresh admission attempt; may now succeed |
| Acknowledgement unknown | Explicit unknown outcome; retry same key/job |
| Deleted session | 410 after authorization; retained tombstone prevents original-key recreation |
| Capacity exhausted | Bounded 429/503 before allocating new resources |

- Concurrent first submissions converge on one session/accepted receipt.
- Retain non-content request/deletion tombstones through the documented retry
  horizon. Purging run history does not remove retry authority; administrative
  removal of both receipt and tombstone ends the retry guarantee.
- Pinned-provider qualification: lost response; crashes before/after native
  completion/start, including fast completion; duplicate/changed/busy requests;
  completed/purged/deleted lookups with retry authority retained.
  Assert **same execution/job**, not just ID string.

### 4.6 Turn execution and human input

1. Read authorized Entity state and scheduled-operation inputs; resolve new
   work against the executing deployment's catalog under §4.11.
2. Prepare context; schedule one foreground model step.
3. Commit complete model output through the Entity.
4. Schedule each tool separately; fan out the batch.
5. Commit a completed parallel batch in one Entity operation, with results
   ordered by original call ID; continue or commit terminal outcome. Do not
   rewrite full Entity state once per tool result.

- Orchestrator uses native tasks/time/timers only; no network, filesystem,
  credential lookup, ordinary clock/random or mutable configuration discovery.
- Entity owns conversation, receipts, active turn, lifecycle and sandbox binding;
  Durable history owns execution replay.
- A checkpoint does **not** guarantee a fresh Functions-worker OS process.
- Success follows final Entity acknowledgement; retain partial/failed/cancelled
  turns rather than rewriting them as success.
- Preserve complete reasoning/assistant/tool dependency groups for continuation.
  Runtime-authored `not_executed`, `cancelled`, `failed`, `outcome_unknown`
  outcomes retain call IDs; never fabricate successful results.
- Use `not_executed` only for known-undispatched calls; otherwise preserve uncertainty.
  Invalid reconstruction fails `context_incomplete`, not silent context reset.

#### Paused human input

- Reserved `request_human_input`: question, optional choices/free text, response schema.
- One pending question; Entity stores its `question_id` and authenticated
  complete-answer receipt. Question ID is distinct from the turn's `request_id`.
- The question deadline is capped by the logical run deadline from §4.2; human
  input cannot extend or pause `ResolvedAgent.timeout`.
- Same Entity serializes answer/cancel/expiry/delete in [**Entity operation order**][input-order],
  not HTTP-arrival order. Operations may share a batch; no separate-commit-per-operation promise.
- Check question deadline/generation and lifecycle in the Entity.
- Mixed question + ordinary-tool batch: dispatch **none**; protocol error.
- Human request consumes one logical tool slot; wait/answer/replay consume no extra slots.
- Wait through Durable events/timers, not a held HTTP connection/activity/worker.
- Resume only the **same run, session and sandbox**; the receiver validates/consumes
  the original Entity receipt before continuing.
- Answer POST uses the same `Idempotency-Key` validation, scoped to the question;
  persist the complete receipt before acknowledging.
- Receipt acceptance and original-run consumption are not one transaction;
  **accepted receipt != resumed/executed work**.
- [`send_event`][input-event] is one-way; an absent target may drop the wake event.
- Detect accepted-but-unconsumed answers; no replacement run.
- Subject to owner authorization/revocation: exact retries return receipt;
  conflicting answers 409; closed/cancelled/expired questions, including those
  closed by whole-run timeout, reject **new** answers (410).
- No terminal-session revival; after deletion revocation, reject receipt consumption.
- Cancellation stops further turn scheduling even after answer acceptance but
  before consumption; retain the accepted-answer receipt without claiming
  execution or rollback.
- Input supplies information, not tool authorization. Reject mandatory SDK
  approval combinations rather than bypassing them.

### 4.7 Foreground model adapter

- Fresh single-step MAF invocation; disable automatic tool loops at Agent/client
  layers; pass schemas, not executable wrappers.
- Reuse a shared, role-agnostic extraction of `runner.py` agent construction:
  `create_harness_agent`, `ResolvedAgent.agent_configuration` limits and
  `ClientManager` acquisition. Durable execution never enters
  `run_agent()`/`run_agent_stream()` or attaches their process-local
  loop/timeout/history implementation.
- Preserve user harness configuration and skills; skill-triggered executable
  calls follow the same activity/checkpoint boundary as other tools. A separate
  pinned-MAF investigation verifies single-step behavior, state restoration and
  hidden skill/tool execution. It is **not an FRD approval or merge gate**.
  Report any breaking finding and address it in a focused follow-up PR; do not
  silently drop skills or bypass the harness. Shipping behavior still needs its
  normal tests and cannot claim qualification based on this design alone.
- No provider-hosted tools, hidden model calls or background submit/poll/cancel.
- Responses-compatible providers: foreground, `store: false`; preserve full
  compatible reasoning/encrypted items, calls, arguments and outputs.
- Provider conversation/response IDs are not session authority.
- Runtime owns `ClientManager` integration and compatible dependencies;
  customers supply normal endpoint/model/credentials, not serialization adapters.
- Record non-secret model/deployment/API/schema bindings for diagnosis; resolve
  credentials and configuration from the executing deployment. Authored changes
  to a model or tool do not reset stored conversation, and the runtime never
  silently selects a fallback model to repair incompatible provider state.
  Compatibility of newly deployed bindings with retained state is the user's
  responsibility under §4.11; real incompatibilities fail explicitly.
- Model-based compaction gets its own activity; working context may change,
  retained transcript does not. Reject hidden-loop compaction combinations.
- MAF OpenAI 1.10.2 reasoning defects reproduced offline; fixed in 1.11.0
  via [microsoft/agent-framework#7233][maf-fix]. Preserve wire/fresh-process regressions.
- V1 selects core/OpenAI/Foundry `1.17.0/1.14.2/1.12.0`; the exact trio moves
  together in C1. The former `1.13.0/1.10.2/1.10.3` set remains a regression
  baseline only. Foundry [microsoft/agent-framework#7536][maf-foundry] requires
  correct reasoning opt-in; offline fidelity is not live-provider qualification.

### 4.8 Tool execution

| Profile/tool | Placement |
| --- | --- |
| No group | Functions activity; no cross-worker local-file affinity |
| No group + legacy Dynamic Sessions | Worker driver; remote isolated interpreter |
| Group + local tools | Bound physical session sandbox; never worker fallback |
| Remote MCP, with/without group | Worker activity/credentials; remote server executes |

- Preserve capability filters and SSRF/egress restrictions; reject unpackageable tools.
- Effective Dynamic Sessions + group is invalid while tools are enabled;
  authors can set `system_tools.dynamic_sessions_code_interpreter: false`.
- Legacy pool identifiers use authorized incarnation; pool owns reset/expiry;
  no retained-workspace or REPL-state guarantee.
- LLM drives tool calls and resolves shared-workspace conflicts; dependent
  operations belong in successive steps. No new locking/conflict-reconciliation feature.
- No new default parallelism cap.
- Every sandbox local-tool call/activity launches a **new guest process**;
  files persist within the bound sandbox, process state does not.
- Durable delivery is at-least-once. Stable operation IDs support downstream
  idempotency; recorded-result replay differs from lost completion.
- Reuse matching sandbox request/result receipts after lost responses;
  no extra blind retry around unknown effects or false termination claim.
- Extract generic mechanics from `workflows/native_retry.py` into shared
  `_durable_retry.py`: SDK retry-policy mapping and the bounded sanitized
  exception-envelope codec. The shared module imports neither workflow nor
  Durable-agent schemas; each engine adapter validates its own policy/outcome
  types before encoding and after decoding. Workflow result validation stays
  in the workflow layer; Durable-agent result validation belongs to
  `durable/contracts.py`. Preserve existing workflow envelope versions and
  persisted exception identity compatibility with adapter shims and regressions.
  The new engine must not import `workflows/native_retry.py` or
  `workflows/activity.py` directly.
- All model/tool activities use that shared retryable-versus-terminal mechanism:
  terminal failures return structured outcomes; only sanitized, explicitly
  retryable failures are raised into native retry policy. Unknown-effect tool
  outcomes are not retried unless a matching downstream receipt proves reuse safe.

### 4.9 Sandbox lifecycle and packaging

- Customer provisions group; runtime allocates session sandboxes only.
- Worker managed identity, group-scoped RBAC, approved resource/region and private paths.
- Capture immutable group, sandbox ID, incarnation, manifest/package and policy digests.
  Config edits do not move existing sessions.
- Default base: Python 3.13. Qualify OS/Python/native-wheel ABI for every advertised
  base, including Python 3.14/custom disks. No shared mutable disk across sessions.
- Deterministic deployment bundle: tools, skills, needed modules/assets/dependencies;
  exclude credentials, settings, caches and repository metadata.
- Build bundles only from frozen `AgentCapabilities` and the captured policy
  manifest digest. Sandbox activities never call `discovery/*` or re-import
  project tools. Verify integrity against the session's installed bundle; do not
  reject a new application deployment solely because its catalog digest changed.
  Incompatible code/ABI changes may fail explicitly; operators own compatibility.
- Worker uploads through SDK file transport; guest validates digest, archive
  paths/size, ABI and binding before staged activation/readiness. No guest
  upload server or customer-deployed MCP server.
- Fixed one-shot runner consumes structured JSON arguments; correlate operation
  ID/request hash/result. No shell interpolation, pickle, arbitrary imports,
  arbitrary runtime code downloads or host-environment forwarding.
- Reuse verified matching bundle. Reconcile ambiguous creation by stable
  operation label; labels are not authorization or permission to allocate again.
- Same-ID attach/resume only after owner/group/manifest checks.
  Permanent loss yields `workspace_lost`; explicit new session required.
- No fallback/recreation. Qualify service availability, retention/TTL alignment,
  quotas, output/package bounds and untrusted-tool isolation.
- Keep control-plane/model/MCP credentials out of guest code.
- Session expiry/deletion requires actual cleanup of the bound sandbox and
  owned workspace resources, with retryable progress.

### 4.10 Entity history and size limits

- Mandatory versioned Entity conversation; prefer native offload over custom references.
- Durable execution attaches no `BlobHistoryProvider` or
  `ScopedFileHistoryProvider`; the Entity is the only transcript authority.
- **Native payload lifecycle belongs to the native Durable extension.**
- Qualify exact host/provider stack and separate state, operation input/output,
  activity, query and aggregate-envelope limits.
- Inspected common library 1.24.2: default threshold 900,000 UTF-8 bytes;
  cap 10,485,760 bytes. Standalone Python/Azure Storage defaults are different contracts.
- Management-read bound is an independent profile check: S0 client 4,194,304 bytes;
  exact-cap minimal response 10,485,793 bytes before additional metadata.
- `extensions.durableTask.maxGrpcMessageSizeInBytes` is source-wired in native b3;
  native-Python support/tuned profiles still need confirmation.
- Bound state with lifecycle/error headroom; use bounded authorized history views.
  Paging needs explicit consistency; client-side slicing cannot fix an oversized full read.
- No silent truncation on offload/capacity failure; preserve existing state and
  report explicit failure. Offload does not compact context or eliminate full-state cost.
- ContinueAsNew only at acknowledged safe boundaries, without unfinished tools
  and with explicit pending-event policy; it does not reset Entity conversation.
- Public preview supports the qualified AzureManaged/DTS provider profile.
  Deployment checks/runbooks verify host provider configuration; the Python
  worker does not promise automatic provider detection without an observed
  signal. Unsupported profiles are outside the published support boundary;
  actual offload/transport failures surface explicitly.

### 4.11 Cancellation, recovery and deployments

- Cancellation is acknowledged intent, not rollback or guaranteed remote termination.
- Persist terminal intent, stop new steps/retries and fence late conversation
  updates by turn generation. Release the active-turn slot on terminal commit;
  subsequent turns are allowed without quarantine or mandatory reconciliation,
  even when an earlier operation's external outcome is unknown.
- Request cancellation of each outstanding operation where supported, wait at
  most five seconds in total (in parallel, not per operation), then stop waiting
  and close/cancel the operation's local request/stream. This fixed internal
  cleanup budget introduces no frontmatter field and does not extend the
  logical run deadline or delay its terminal state. Do not close shared clients
  or kill a worker to cancel one operation.
- Cancellation must reach the activity owning the call, not merely cancel the
  orchestrator's wait. **This is required new C2 adapter work, not an existing
  verified end-to-end capability.** Existing workflow cancellation stops
  scheduling; it does not establish cancellation delivery to an already-running
  model/tool call on another worker. Do not assume orchestration termination,
  an external event, or cancelling a Durable task automatically cancels that
  activity's local Python task or remote execution.
  Activities receive the absolute run deadline and enforce
  it locally. For explicit cancellation they observe the Entity's persisted
  terminal/generation state through a bounded worker-side check while a call is
  active, then cancel the local awaitable and invoke any supported remote
  cancellation operation. No in-memory cross-worker task registry or reliable
  delivery claim is required. Qualify the check cadence and transport in C2;
  the five-second budget starts when the owning worker observes cancellation,
  not when a management request is received by another worker.
- C2 must demonstrate cancellation issued through one worker being observed
  by a different worker running a harmless blocking async operation, with
  local cleanup and an ignored-cancellation negative case. Verify the pinned
  SDK's worker-side Entity-read/client capability before choosing that transport;
  if unavailable, revise this delivery mechanism explicitly. Mocks or the
  existing orchestration-only cancellation path are not sufficient evidence.
- For foreground model/HTTP calls, cancelling the local task and closing that
  request is sufficient best effort; do not claim provider compute stopped.
  Arbitrary synchronous Python tools cannot be force-killed safely in-process.
  If a tool ignores cancellation or its worker is unavailable, record an
  unconfirmed outcome and move on rather than waiting indefinitely.
- For sandbox exec operations, use an authenticated, operation-scoped process
  control handle to request SIGTERM (or the provider's equivalent), when that
  capability exists. Wait within the same cleanup budget for process exit,
  then abandon the local HTTP wait if unconfirmed. Closing an exec HTTP request
  does not itself signal or kill the guest process. Never signal a reused PID,
  an unrelated process or the entire shared sandbox; qualify the selected
  provider's control/exit acknowledgement in C3. The existing Dynamic Sessions
  synchronous executions adapter is not evidence of such a signal API.
- **C2 sandbox cancellation assessment:** inspect the selected exec SDK/API for
  the simplest supported operation-scoped cancel/signal mechanism, stable
  execution handle, process-exit acknowledgement and disconnect semantics.
  Record whether SIGTERM/equivalent can be sent independently of the active
  exec HTTP request and how it fits the bounded cleanup contract. Prefer the
  existing provider primitive over a new supervisor/control service. Hand C3
  an evidence-backed adapter choice or an explicit unsupported-capability
  finding; do not assume HTTP disconnect kills the process. C3 still owns
  sandbox implementation and real process-stop qualification. Assessment alone
  does not authorize cloud provisioning or add premature sandbox schema fields.
- A cancellation-request acknowledgement is not a process-exit acknowledgement.
  Retain operation-level confirmed-stop versus `outcome_unknown` results and
  surface sanitized cancellation failures. Late results cannot reopen the run
  or modify the next turn's conversation. Tool authors/operators own
  cancellation cooperation, idempotency and effects that continue, including
  overlapping external effects or workspace writes in a subsequent turn.
  No rollback, remote termination or effect-isolation guarantee is made.
- Native retries use captured policy and the structured retry bridge from §4.8;
  account separately for SDK transport retries.
- Lifecycle recovery covers hard termination and crashes outside orchestration cleanup.
- Version persisted contracts; incompatible continuation fails explicitly.
- **Session consistency spans deployments:** with the same app/task hub/storage
  and owner identity, retain session IDs, Entity conversation, receipts, turn
  ordering and sandbox binding. A code deployment alone never creates a replacement
  session, discards history or replays an accepted request as a new run.
- Do not pin a session to a deployment/catalog digest, require an old worker
  version, or reject solely because code/configuration changed. Already scheduled
  operations retain their serialized inputs and the turn's recorded deadline;
  newly executed work uses available deployed code/configuration. During rollout,
  workers may run different versions; the operator must keep those versions
  compatible with persisted state, tool/provider contracts and each other.
- Users own breaking-change handling, rollout/drain strategy and any migration.
  The runtime reports genuine deserialization, provider/tool or continuation
  errors without resetting the session or claiming recovery succeeded. Session
  continuity is not a promise that incompatible user code will execute successfully.
- **Deployment/version retention is the user's choice, not a runtime requirement
  or release gate.** No requirement to preserve runnable old versions.
- Independent admission drain is recommended; `durable: false` is not deletion.
- No automatic private-spike migration. Qualify Python 3.13/3.14 on
  Flex/Premium/Dedicated separately; legacy Linux Consumption is not advertised.
  Platform timeouts and worker interruption still apply.

### 4.12 TTL, logical deletion and operations

- Require explicit positive `history.session_ttl`; ISO-8601 fixed-unit
  `PnDTnHnMnS` only. Reject numeric/object/null/calendar/overflow forms.
- Extract the fixed-unit ISO-8601 duration parser into a dependency-free shared
  module consumed by workflows and Durable config; config does not import workflows.
- Capture interval/policy at session creation; ordinary default edits do not migrate it.
- Idle TTL starts at last completed turn, including failure/cancellation.
  Accepted active work invalidates old idle expiry; terminal completion renews it.
- Generation-tagged delayed Entity self-signals implement idle/wait expiry;
  superseded generations are dropped when received.
- Accepted start acknowledgement atomically stores the turn's absolute deadline
  and schedules a generation-tagged Entity self-signal at that deadline. This
  independently enforces the same deadline if the turn orchestration is lost,
  terminated or purged. Delivery may be late; this is not an exact-time execution
  guarantee. A duplicate acknowledgement never changes or extends the deadline.
- If that generation remains active when the deadline signal is processed, the
  Entity commits terminal `timed_out`, records unfinished calls as
  `outcome_unknown`, releases the slot and starts idle TTL. It fences late
  conversation commits, not external effects; §4.11 best-effort cancellation
  does not prevent admission of the next turn.
- Reads, polling, rejected requests and duplicate receipts never renew TTL.
- Human wait neither starts nor renews session idle TTL. It is bounded by the
  whole-run deadline in §4.2; unanswered expiry ends the run as `timed_out`,
  not the session. Idle TTL starts at terminal turn commit. Superseded question
  generations cannot consume answers or reopen a terminal turn.
- Unknown work cannot receive indefinite active-work exemption.
- Owner-authorized deletion atomically revokes access/admission; reject stale
  completion/answer attempts; remove logical conversation/run/index/UI records
  and clean up the actual sandbox. Retain a non-content, owner-bound deletion
  receipt/tombstone; completion means these runtime-owned tasks finished.
- Repeated `DELETE` for the same session incarnation returns the same
  `deletion_id` and progress; no new cleanup operation or TTL renewal.
- Return `202` + `pending`, `200` + `completed`, or `500` + `failed` with a
  sanitized error. A failed operation is not reported complete or silently restarted.
- Current endpoint authorization and original-owner checks still apply after
  revocation. The owner may read deletion progress through `DELETE`, not
  revoked history/execution; other callers cannot inspect the receipt.
- Least privilege and private connectivity qualify independently for DTS,
  storage, models, MCP and sandbox paths.
- No secrets/content in default logs, labels, status or metrics; no credentials
  checkpointed. Track bounded admission/retry/failure/size/cleanup diagnostics
  and actual model attempts without replay double-counting.
- Where supported by the pinned native stack, attach bounded non-sensitive
  orchestration tags (agent slug and operation kind) for DTS dashboard display.
  Verify tag propagation in qualification; never include prompts, credentials
  or owner identifiers. Dashboard tags are observability, not authorization.
- Pending human input is not a ContinueAsNew boundary. Purging a live waiting
  instance can prevent continuation; report that explicitly rather than starting
  a replacement run. Deployment/version retention remains the user's choice
  under §4.11, not a new runtime retention requirement.
- Preserve the documented `af.*` contract as one logical `agent.run` trace per
  turn across activities; replay does not double-count and content attributes
  remain behind `ENABLE_SENSITIVE_DATA`.

### 4.13 API, registration footprint and optional UI

| Method/path under `/agents/{slug}` | Contract |
| --- | --- |
| `POST /chat` or authored HTTP entry | Durable acceptance, not synchronous answer |
| `GET /manage/runs/{run_id}` | Bounded status/result/pending question |
| `POST /manage/runs/{run_id}/cancel` | Idempotent cooperative cancel |
| `POST /manage/runs/{run_id}/input/{question_id}` | Authenticated idempotent answer to the stored question |
| `GET /manage/sessions` | Owner-scoped paginated discovery |
| `GET /manage/sessions/{session_id}/history` | Authorized conversation, not raw Durable history |
| `DELETE /manage/sessions/{session_id}` | Start deletion or return its existing receipt/progress; §4.12 |
| Optional `GET /manage/runs/{run_id}/events` | Run-scoped SSE observations |

- Return `session_id` and `x-ms-session-id`; relative runtime-owned status links.
- Resolve auth independently for every HTTP entry, even when an agent exposes
  both built-in and authored entries. Built-in APIs use
  `builtin_endpoints.http_auth`; authored APIs preserve
  `_resolve_http_trigger_auth` precedence: `trigger.args.http_auth`, otherwise
  legacy `auth_level` with its existing warning, otherwise function auth.
  Neither entry overrides the other. Capture entry-specific policies in the
  catalog and propagate the originating entry identity through admission and
  management so reauthorization selects the correct policy.
  Each entry uses its own policy for submission and management within its one
  HTTP function registration.
- Durable agents suppress ordinary chatstream, blob-backed history and
  workflow-status registrations. When `builtin_endpoints.debug_chat_ui` is
  enabled, retain the existing separate GET chat-page function with Functions
  `AuthLevel.ANONYMOUS`, serving `public/index.html` at `agents/<slug>/`.
  This permits the existing API-key prompt to load before credentials are
  supplied. Platform Easy Auth still applies where configured; this does not
  bypass platform authentication. The page contains only static UI and
  non-secret endpoint/mode configuration, never run data or credentials.
  All data requests use the protected dispatcher and its owner checks.
  The flag is never silently accepted without a working page.
- Preserve `BuiltinEndpointsConfig.debug_chat_ui_requires_chat_api`: enabling
  the debug page forces the effective `chat_api` flag true, even if authored
  false. Inventory uses resolved flags, so a Durable page never exists without
  its backing admission/management dispatcher.

#### Inbound MCP: post-v1

- No Durable MCP invocation or management tools register in v1. See FF1 in
  §4.15 for the deferred adapter and its unresolved transport/client contracts.
- Keep admission, execution and lifecycle logic reusable by a future adapter;
  do not add unused MCP-specific APIs or claim transparent client compatibility.
- Outbound MCP tool calls still execute through worker-side activities (§4.8).

| Registration | Trigger |
| --- | --- |
| `agents_<slug>_<entry>_durable_http_v1` | One registration per HTTP entry, uniquely named: submit, management and optional SSE |
| Existing built-in chat-page function, when enabled | Separate anonymous-at-Functions-layer GET; static debug UI only |
| `agents_durable_orchestrator_v1` | Independent typed admission/turn/input/lifecycle instances |
| `agents_durable_state_v1` | Entity; disjoint session/owner-index kinds |
| `agents_durable_execute_v1` | Activity; typed model/tool/maintenance operation |
| `BuiltIn__HttpActivity` | SDK outbound-HTTP activity |
| `BuiltIn__HttpPollOrchestrator` | SDK outbound-HTTP polling orchestration |

- **Three shared execution registrations + one HTTP dispatcher per entry +
  two native SDK helpers, plus enabled static chat pages.** One built-in
  Durable chat agent registers six without its page, seven with its page.
- SDK helpers are outbound machinery, not inbound APIs or model background polling.
- Shared registration does not merge checkpoints, serialize activities or let
  model arguments select maintenance operations.
- Each built-in entry owns one constrained route based on
  `agents/<literal-slug>/{*path}`, admitting only enabled operation paths. Its
  dispatcher accepts `POST chat` and the specified `manage/...` method/path
  combinations only; optional SSE adds an explicit path, never permissive
  catch-all execution. Exclude the empty page path from this dispatcher; it
  belongs to the separate static-page registration. Register no competing
  ordinary data endpoints for that slug.
  Unknown paths/methods return 404/405. Admission drain checks apply to submission
  only; owners can still manage existing runs through the same function.
- Declarative `http_trigger` routes are still runtime-generated handlers, not
  customer-written HTTP code. They retain the same-function key/Entra guarantee.
  Customer-written handlers outside this registration pipeline own their routing
  and authorization; this runtime never returns raw Durable system-key URLs.
- For an authored submit route `R`, preserve its submission URL and place
  management below `R/manage/...`, using the operations in the table above.
  For example, `orders/{customer}/ask` accepts submission at
  `orders/acme/ask` and returns a status link at
  `orders/acme/ask/manage/runs/<run_id>`. Keeping `ask` in the management
  prefix avoids capturing sibling paths such as `orders/acme/invoices`.
- Register exactly one HTTP trigger for that entry, conceptually
  `R/{*operation}` with a host constraint matching only that entry's submission
  suffix (empty for an authored route `R`; `chat` for a built-in entry, whose
  empty suffix belongs to the static page) or the enabled management path
  shapes. This is one constrained route
  template, not multiple triggers on one function. The local-host probe below
  verifies a minimal mapping; the full generated operation set still needs C2
  qualification.
- Retain authored submission methods. The trigger's method list is their union
  with the required management methods (`GET`, `POST`, `DELETE`); the dispatcher
  then checks the exact method/path pair. A management-method addition must not
  enable that method for submission. Unknown paths return 404 and unsupported
  methods on known paths return 405. No captured operation name can dynamically
  select an orchestrator, activity, Entity operation or arbitrary callable.
- Validate captured path segments before dispatch, including encoded separators
  and normalization ambiguities. The host regex alone does not enforce these
  boundaries; do not repeatedly decode captured values or allow encoded input
  to introduce extra operation segments.
- Compile links from the matched originating entry and its bound route
  parameters, encoding each parameter once as a path segment. Preserve parameter
  constraints; never derive links from untrusted forwarded hosts or copy
  credentials into them. Recheck owner and agent scope on every operation;
  a matching route parameter is not authorization.
- The initial route-expansion contract covers fixed-depth paths with required
  single-segment parameters and a final literal segment. Existing catch-alls,
  optional/defaulted segments, or shapes whose submit and management languages
  cannot be separated are not silently rewritten or broadly matched. Report
  the authored route and the ambiguity before app mutation; use the built-in
  entry or a separately reviewed explicit mapping for those shapes. This
  qualification concerns generated Durable entries only; ordinary routes remain
  unchanged.
- Validate reserved-path and inter-entry collisions before registration,
  including enabled built-ins and ordinary routes; never rely on registration
  order or wildcard priority. If disjointness cannot be established, report
  that conflict rather than registering an unrestricted catch-all.
- **Side validation (advisory for this FRD):** a tiny real Python Functions host
  probe must show that the constrained route accepts submission without a
  trailing slash and the enumerated management paths, rejects unrelated/extra
  segments, and leaves sibling routes reachable. Record host/SDK versions,
  encoded-path and method cases, and the actual registration inventory.
  Documentation or Python binding metadata alone is not a pass. This is not
  evidence of hosted key/Entra enforcement. A failed probe requires a routing
  design follow-up, not silent fallback to broad routing, host-only keys or
  extra management functions; C2 must qualify the selected mapping before ship.
- **Local probe result (2026-09-23): PASS, 26/26 real HTTP checks**, on Core
  Tools 4.13.0, Functions host 4.1051.300.26316 and Python 3.13.15. One target
  HTTP function plus an unrelated sibling loaded. The minimal working template
  was `orders/{customer}/ask/{*operation:regex(^(manage/runs/[^/]+(/cancel)?)?$)=}`.
  The initial version without the empty default (`=`) returned 404 for
  submission; adding that default allowed submission without a trailing slash.
  Submit/status/cancel succeeded, the sibling remained reachable, unrelated
  suffixes/extra segments returned 404 and wrong methods returned 405.
  Encoded-separator probes required dispatcher guards, not regex alone.
  This proves the minimal local routing mechanism, not the full management
  surface, built-in dispatcher/static-page coexistence, key/Entra enforcement,
  cloud behavior or arbitrary authored route
  shapes. Owned probe processes were stopped; no product code changed.
- One built-in Durable chat agent: 6 without its page, 7 with it;
  many: `5+B+U`; each enabled page: +1; SSE/group: +0;
  custom-only: `5+C`. Existing workflow engine: separate 3; SDK helpers counted once.
- Total `F = O + 2I + 3W + 3D + B + C + U`: ordinary registrations O; native
  helper pair I; workflow engine W; Durable shared engine D; built-in Durable
  HTTP entries B; custom HTTP entries C; Durable static chat pages U (ordinary
  pages remain in O). Ordinary MCP registrations remain part of O;
  outbound MCP tools add no inbound function registrations.
- V1 composition exclusions add no registrations; `3W` still covers independent
  workflow-enabled agents in the same app.
- JSON clients request-scoped; SSE client lives inside generator; static assets
  need none. Preserve auth/route precedence; reject conflicting reserved paths.

#### Existing debug UI and optional SSE

- V1 reuses `public/index.html`, not a separate Durable UI. The UI remains
  opt-in through `builtin_endpoints.debug_chat_ui`, but its implementation is
  part of C2, not a deferred capability.
- Supply the resolved agent's Durable-enabled flag and relative endpoint paths
  as non-secret page configuration. Ordinary agents retain their existing
  chat/stream behavior. For Durable agents, send to the admission endpoint with
  one `Idempotency-Key` per user submission (reuse it on retry), retain the
  returned session/run IDs, and automatically poll the returned status link.
  Never discover mode by submitting the same prompt to both execution paths.
- Reuse existing chat presentation for results and errors; an accepted run
  handle is not an assistant answer. Poll with bounded backoff, honor server
  retry guidance, and stop at completion/failure/cancellation/timeout or when
  the selected agent/session changes. A browser disconnect or polling failure
  does not cancel or resubmit the run; surface errors and allow resuming status.
- Use the same authorized entry for polling and Entity-backed history. Display
  pending human questions and submit answers through the existing input contract
  with idempotent retries; do not start another turn to answer a question.
  Preserve existing UI auth handling and add no default credential persistence.
- Entity history remains authoritative; no observation journal or Durable SSE
  is needed for v1 polling.
- Another client or cleared browser cache restores from server history.
- SSE publishes bounded provisional foreground observations; retry epochs replace
  drafts. Disconnect/publisher failure never restarts execution or renews TTL.
- Optional batched Blob journal stays separate from conversation; authorized
  cursors/reconnect, bounded retention and owned-observation cleanup.
- No token-frequency writes to conversation Entity; no Blob I/O in orchestrator.

### 4.14 Delivery and selective reuse

| Layer | Purpose and scope | Dependencies | Compatibility/docs | Review focus |
| --- | --- | --- | --- | --- |
| D0 | FRD-only #226 | None | Design only | Architecture |
| C0 | Characterize ordinary/workflow registration inventory and runner behavior | D0 | No product change | Regression baseline |
| C1 | Bump Durable b2→b3 + `durabletask==1.10.0`; move MAF trio to `1.17.0/1.14.2/1.12.0` | C0 | Existing workflow/runner suites unchanged on 3.13/3.14 | Dependency blast radius |
| C2 | Complete HTTP Durable core: schema/catalog, real MAF model/tool execution and skills, budgets/compaction, Entity admission/history, same-function auth/management, human input, whole-run deadline, cross-worker cancellation, TTL/deletion, existing debug UI with automatic Durable polling; assess simplest sandbox cancellation API for C3 | C1 | Usable real turns with complete lifecycle and enabled debug UI; generated reference/spec, architecture, API/auth and observability docs together | Backend/hub confinement and cancellation evidence; review work packages below; no dead flags or fake shipped engine |
| C3 | Sandbox packaging, parallel execution, affinity/loss/cleanup; introduce Sandbox fields here | C2; #196/#197 merged or pinned assets vendored with source SHA | Optional workspace capability and sandbox deployment guide | Isolation and ambiguous effects |
| C4 | Enumerated deployed qualification profiles and support matrix only | C3 | Runbooks, observability, README/onboarding | Required evidence |
| Promotion | Feature branch → `main` after all blocking gates | C4 | Final coherent docs | Qualified public preview |
| Post-v1 | Fast-follow backlog in §4.15 | Separate scope/review per item | Not required for v1 promotion | Protocol compatibility and composition |

- Defer composition design/implementation to post-v1: existing delegation runs
  a whole MAF loop, bypassing per-call checkpoints and call budgets.
- DTS sub-orchestrations are a candidate, not a selected implementation.
  Decide their registration impact in that later design; no v1 gate or count change.
- Integration branch originates from `main`; no feature increments directly to `main`.
- Stack: `feature/durable-agent-loop <- C0 <- C1 <- C2 <- C3 <- C4`;
  the FRD revision precedes C0. Each product layer owns one branch/PR.
- Start dependent layers from recorded buildable pushed parents; overlap review,
  implementation and qualification; owner-scoped bottom-up rebases.
- Keep tests/docs with each layer; no broken/dead public flags or C4 catch-all.
- Introduce Sandbox fields with C3, not before their implementation.
- C1 must pass the complete existing workflow and runner suites unchanged before
  C2 starts. C2 carries generated schema docs with the working implementation.
- Review C2 in three coordinated work packages/commit groups: **contracts and
  Entity execution**, **real harness/tools and budgets**, and **HTTP ownership,
  management and lifecycle**. Assign disjoint files where practical and agree
  interfaces first. One integration owner assembles the working vertical slice;
  merge the packages together, not as independently exposed incomplete features.
- Fakes remain test doubles under `tests/`, never a shipped execution path.
  Every merged layer must be usable and retain its tests/docs; do not add temporary
  authoring restrictions just to make mechanical module splits possible.
- Parallelize isolated core work, sandbox source preparation and qualification preparation;
  create stacked layer branches only from committed/pushed parents. Keep reviews
  continuous rather than serializing six core PRs.
- The harness investigation runs in parallel and does not block
  FRD approval/merge. A discovered breaking issue gets a focused follow-up PR.
  Skipped tests, unapproved cloud runs and missing release evidence are not
  passes; report blockers explicitly.
- Architecture sign-off precedes product implementation; cloud runs require
  separate consent. Branch targeting does not establish required-check policy.

| Pinned source | Reuse/adaptation |
| --- | --- |
| `larohra/durable-loop-leadership-demo@d3ebac5afadeb8cb6d59f54a53177a39de4275f5` | `experimental/durable_loop_*` and tests → C2 native model/session/receipt/HTTP primitives |
| `feature/aca-sandboxes@88f553ed6a67e399a8fb660cf7aaef15905f0590` + leadership | `controller/{package,bootstrap_delivery,sandbox_config}.py`, `harness/bootstrap.py`, `transport/aca_sdk.py`, manifests/tests → C3 |
| Same ACA pin; [#196][e2e-assets], [#197][e2e-ci] | Fixture/wheel assembly, dependency export, smoke/provenance, deployed suites and Python matrix → C2–C4 |
| `larohra/durable-loop-chat-ui@9fe3edb8df1292533508e3742df13b42ad8c3e33` | Reference for selective polling/history test reuse; v1 extends existing `public/index.html`, not a separate UI |

- Source module paths are relative to `src/azure_functions_agents/`.
- Record source SHA/files → destination, port/adapt/omit rationale and tests;
  revalidate pins/bases. No blind branch merge or unexamined rewrite.
- Omit legacy Durable `<2`, custom Blob transcript, Table session authority,
  background polling, private APIM/demo auth, hidden serial/fixed-timeout
  assumptions and sandbox recreation.
- Reuse `aca_qualification_pipeline.py`, `aca_deployed_qualification.py`,
  `aca_pr_smoke.py`, owned-resource reaper, CI templates and live fixtures;
  adapt assertions to asynchronous Entity/DTS contracts.
- Preserve Python 3.13/3.14 Linux matrix, fail-fast provenance, exact wheel/dependency
  evidence, trusted-run credentials and pipeline-owner approval.
- Source `continueOnError` jobs are advisory, **not release passes**.
  Required profiles/checks must block release; label skipped/not-run outcomes.

### 4.15 Post-v1 fast-follow tracker

This is a scope tracker, not a delivery-date or implementation commitment.
**Deferred** items are agreed post-v1 work; **research/candidate** items require
a separate decision. **Optional** items may ship independently and are not
newly deferred by this table. None is a core-v1 release gate.

| ID | Fast-follow | Why / intended outcome | Status and basis | Decisions or evidence needed before implementation |
| --- | --- | --- | --- | --- |
| FF1 | Inbound Durable MCP exposure | Let MCP clients invoke and manage the same checkpointed agent runs | **Deferred to post-v1**; Decision 75 | Verify authenticated owner information and retry identity exposed by the binding; define reconnect/session continuity, long-running results and human-input interaction; select pre-registered management tools or a dispatcher and count every registration; test real client compatibility. Reuse core admission/lifecycle, never the ordinary whole-loop fallback. |
| FF2 | Dynamic Workflows composition | Combine Durable turns with FRD 0004 workflows without hidden agent loops | **Deferred to post-v1**; Decision 64 | Define supported directions of composition, shared identity, budgets, retry/cancel semantics and per-call recovery. Evaluate DTS sub-orchestrations; they are not yet selected. |
| FF3 | Agent delegation and workflow subagents | Support `subagents:` and `workflows.subagents` while preserving checkpoints inside specialists | **Deferred to post-v1**; Decision 64 / FRD 0007 | Replace whole-loop delegation with a checkpoint-aware contract; define parent/child ownership, call accounting, cancellation and any registration changes. Coordinate with FF2. |
| FF4 | Timer and other non-HTTP ingress | Start Durable work from trusted scheduled/event producers | **Research; outside v1**; Decisions 21/22/31 | Define producer/service-actor ownership, stable delivery identity, retries and Timer catch-up behavior; qualify one trigger at a time. Earlier service-actor grants are not a selected v1 design. |
| FF5 | Richer Durable UI enhancements | Add dedicated run/session browsing and richer views beyond the existing chat UI | **Optional enhancements only**; §4.13, Decision 80 | Basic automatic polling, result/question handling and Entity-backed chat history ship in C2. Scope additional UI separately; do not make a new UI or SSE a prerequisite for v1. |
| FF6 | Optional SSE and observation journal | Provide live provisional output and reconnectable observations | **Optional, independently deliverable**; Decision 38, §4.13 | Qualify authenticated stream lifetime, retry epochs and disconnect behavior; if a Blob journal is used, define bounded retention/cursors and cleanup separately from authoritative Entity history. |
| FF7 | A2A transport | Evaluate a protocol designed for long-running agent interactions | **Candidate only**; non-blocking suggestion in #234 | Compare supported client task/status/input semantics with the HTTP core and FF1; decide whether to pursue a separate adapter. No commitment to replace MCP. |

- Tool-action approvals, provider background polling, automatic sandbox
  recreation and GA/compliance claims remain non-goals, not promised fast-follows.
- Whole-run timeout, HTTP key-owner correctness, harness/skill execution
  correctness, and required v1 qualification remain core work; this backlog
  does not defer unresolved architecture blockers.

## 5. Decisions log

- Append-only numbering; compressed wording; superseded choices explicitly marked.
- Human choices are not final architecture approval; Agent proposals remain reviewable.

| # | Decision | Options | Choice | By | Date |
| --- | --- | --- | --- | --- | --- |
| 1 | Scope | Patch / feature | Medium+ FRD; selective reuse | Human | 2026-09-16 |
| 2 | Security | Controls / certification | Isolation, MI, networking, lifecycle, audit; no certification | Human | 2026-09-16 |
| 3 | Contention | Reject / queue | One active turn; 409 | Human | 2026-09-16 |
| 4 | Initial trigger scope | HTTP / broader | Broader triggers: **OUTDATED; SUPERSEDED by 31** | Human | 2026-09-16 |
| 5 | Correlation | Mappings / standard | Standard session carriers; v1 scope per 31 | Human | 2026-09-16 |
| 6 | Creation IDs | Generated / supplied | Caller IDs with owner/collision checks | Human | 2026-09-16 |
| 7 | Large state | Custom / native | Investigate native b3; custom recommendation withdrawn | Human; Agent recommendation | 2026-09-16 |
| 8 | Entity layout | Split / one | One authority; native replay | Agent; Human clarification | 2026-09-16 |
| 9 | Deletion scope | Original / revised | Immediate revocation retained; other scope **superseded by 52** | Human | 2026-09-16 |
| 10 | Workspace loss | Replace / fail | Same sandbox; `workspace_lost` | Human | 2026-09-16 |
| 11 | Configuration | Agent-only / inheritance | Global defaults; per-agent bool/object | Human | 2026-09-16 |
| 12 | UI identity | Own / external | `larohra/durable-loop-chat-ui` | Human | 2026-09-16 |
| 13 | Release | GA wait / preview | Qualified public preview | Human | 2026-09-16 |
| 14 | Inference | Foreground / polling | Foreground; per-call checkpoints | Human | 2026-09-16 |
| 15 | Storage | Custom / native | Native Entity offload; earlier responsibility **superseded by 52** | Agent proposal | 2026-09-16 |
| 16 | Execution | Whole loop / per-call | Turn orchestration; short Entity; per-call activities | Agent proposal | 2026-09-16 |
| 17 | Tool concurrency | Serial / parallel | Serial proposal **SUPERSEDED by 32** | Agent proposal | 2026-09-16 |
| 18 | UI reuse | As-is / adapt | Optional API client; server history | Agent proposal | 2026-09-16 |
| 20 | Retry address | Lookup / derivation | Scoped SHA-256 identity; Entity receipt | Agent proposal | 2026-09-16 |
| 21 | Trigger ownership | Implicit / grants | Service-actor grants **deferred by 31**, not v1 | Agent proposal | 2026-09-16 |
| 22 | Timer recovery | Catch-up / delivery | Normal pre-acceptance delivery; research-only per 31 | Human | 2026-09-16 |
| 23 | Effect framework | Redelivery / claims | Effect-claim proposal **SUPERSEDED by 34** | Agent proposal | 2026-09-16 |
| 24 | Partial context | Drop / reconstruct | Truthful complete dependency groups or explicit failure | Agent proposal | 2026-09-16 |
| 25 | Policy/retirement | Recalculate / capture | Capture policy; earlier scope **superseded by 30/52/54** | Agent proposal | 2026-09-16 |
| 26 | Lifecycle completion | Earlier / revised | Earlier runtime responsibility **SUPERSEDED by 52** | Agent proposal | 2026-09-16 |
| 27 | Legacy interpreter | Reject / coexist | Blanket rejection **SUPERSEDED by 29** | Agent proposal | 2026-09-16 |
| 28 | Count budgets | Default / opt-in | No defaults; initial/remaining prompt; enforce | Human | 2026-09-16 |
| 29 | Interpreter compatibility | Blanket / conditional | Allowed without group; effective group conflict rejected | Human; Agent concurrence | 2026-09-16 |
| 30 | TTL | Absolute / idle | ISO strings; idle after completion | Human | 2026-09-16 |
| 31 | V1 triggers | Broad / HTTP | HTTP/chat only; Timer research; supersedes 4 | Human | 2026-09-17 |
| 32 | Tool batches | Serial / fanout | Parallel calls; ordered assembly; supersedes 17 | Human | 2026-09-17 |
| 33 | Remote MCP | Sandbox / worker | Worker credentials; remote execution | Human | 2026-09-17 |
| 34 | Delivery configuration | DSL / platform | At-least-once; no public tool-policy framework | Human | 2026-09-17 |
| 35 | Sandbox base | Fixed / authored | `disk`/`disk_id`; qualified Python ABI | Human | 2026-09-17 |
| 36 | Call deadlines | Default / authored | Authored-deadline proposal **SUPERSEDED by 60** | Human; Agent clarification | 2026-09-17 |
| 37 | Evidence | Assumption / probe | Native offload probe; independent upstream MAF check | Human | 2026-09-17 |
| 38 | UI/SSE | Core / optional | Optional; Blob observations acceptable | Human | 2026-09-18 |
| 39 | Registration proposal | Split / consolidate | Eight-function proposal **SUPERSEDED by 45** | Agent proposal | 2026-09-18 |
| 40 | Human input scope | Input / approval | Action-approval scope **SUPERSEDED by 42** | Human | 2026-09-18 |
| 41 | Human-wait TTL | Separate / reuse | Session-TTL wait proposal **SUPERSEDED by 73**; whole-run timeout now bounds human wait | Human | 2026-09-18 |
| 42 | Approval scope | Enforce / defer | Input only; no approval engine; supersedes 40 | Human | 2026-09-18 |
| 43 | HTTP helpers | Outbound / inbound | SDK helpers outbound; authenticated facade required | Agent clarification | 2026-09-18 |
| 44 | Polling UI | Journal / state | Status/questions/Entity history; no journal required | Agent clarification | 2026-09-18 |
| 45 | Shared execution | Split / typed | Five-owned/seven-total footprint **SUPERSEDED by 66/76**; typed shared execution retained | Agent recommendation | 2026-09-18 |
| 46 | Qualification | Repeat / targeted | Preserve offload/MAF evidence; qualify remaining runtime/profiles | Human | 2026-09-18 |
| 47 | Delivery | Serial / stacked | D0 separate; C1–C4 dependent stack; base per 51 | Human; Agent proposal | 2026-09-21 |
| 48 | Reuse | Implicit / mapped | Pinned source-to-target disposition and tests | Human; Agent mapping | 2026-09-21 |
| 49 | E2E | Rebuild / adapt | ACA #196/#197; required checks not advisory | Human; Agent proposal | 2026-09-21 |
| 50 | Publication | Wait / review | FRD-only now; no implementation/sign-off authorization | Human | 2026-09-21 |
| 51 | Integration | Main / feature | D0/C1 to feature branch; separate main promotion | Human | 2026-09-21 |
| 52 | Payload lifecycle | Runtime / native extension | Native extension; no feature-specific lifecycle gate; supersedes 9/15/25/26 scope | Human | 2026-09-21 |
| 53 | Workspace coordination | Runtime / LLM | LLM resolves conflicts; fresh guest process per call | Human | 2026-09-21 |
| 54 | Deployment versions | Runtime / user | User discretion; no retention gate; supersedes 25 scope | Human | 2026-09-21 |
| 55 | Generated sessions | Implicit reuse / explicit | Retry/new-key rule in §4.3 | Human | 2026-09-21 |
| 56 | Acceptance/start | Split / reliable | One handoff; retry same job; SDK failure proof pending | Human | 2026-09-21 |
| 57 | Answer/terminal ordering | Extra coordinator / Entity | Native Entity operation order; acceptance != consumption; cancellation policy in 59 | Agent source clarification | 2026-09-21 |
| 58 | Review format | Narrative / compact | Bullets/tables/snippets; preserve numbering; remain In review | Human | 2026-09-21 |
| 59 | Cancellation after accepted answer | Reject cancellation / honor cancellation | Honor cancellation; retain accepted-answer receipt; no execution/rollback claim; resolves 57's policy question | Human | 2026-09-21 |
| 60 | V1 timeouts | Authored / platform-only | Platform-only proposal **SUPERSEDED by 73**; previously superseded 36 | Human; Agent validation detail | 2026-09-21 |
| 61 | Request identity | Ambiguous / explicit | Validated client key; deterministic scoped session/request IDs; §4.3 | Human; Agent validation detail | 2026-09-21 |
| 62 | Deletion progress | New endpoint / repeat DELETE | Original owner repeats DELETE for same receipt and pending/completed/failed status | Human; Agent HTTP detail | 2026-09-21 |
| 63 | HTTP route isolation | Shared dispatcher / separate namespaces | Two-handler topology **SUPERSEDED by 76**; submit/manage paths remain distinct within each entry | Human | 2026-09-21 |
| 64 | Workflow/subagent composition | Support in v1 / defer | Post-v1; reject §4.2 combinations/references; evaluate DTS sub-orchestrations later; independent app coexistence allowed | Human | 2026-09-22 |
| 65 | Policy ownership | Re-resolve / frozen catalog | Pass-1 immutable Durable policy catalog; activity-time reauthorization by slug | Agent architecture review | 2026-09-22 |
| 66 | Mixed-app submit routing | Shared wildcard / per-agent literal / new namespace | Shared-management topology **SUPERSEDED by 76**; previously superseded constant-seven part of 45/63 | Agent architecture review | 2026-09-22 |
| 67 | Durable owner authentication | Existing modes / mandatory Entra | Entra-only proposal **SUPERSEDED by 74** | Agent architecture review | 2026-09-22 |
| 68 | Unconfirmed start | Replace / remain busy / terminal reconcile | No replacement; generation-guarded reconciliation ends as `start_unconfirmed`/`outcome_unknown` and releases the slot | Agent architecture review | 2026-09-22 |
| 69 | Activity retries | Retry every exception / classified bridge | Structured terminal outcomes; native retry only for sanitized retryable failures | Agent architecture review | 2026-09-22 |
| 70 | Dependency baseline | Keep current / selected compatible set | Durable b3 + durabletask 1.10.0; MAF `1.17.0/1.14.2/1.12.0`; existing workflows/runner requalified first | Agent architecture review | 2026-09-22 |
| 71 | Delivery split | C1-C4 / smaller stack | C0 retained; separately mergeable C2a-C2f proposal **SUPERSEDED by 79** | Agent architecture review | 2026-09-22 |
| 72 | Durable call ceiling | New runtime value / external ceilings | No separate per-call setting; whole-run conclusion **SUPERSEDED by 73** | Agent architecture review | 2026-09-22 |
| 73 | Timeout compatibility | Reject/ignore / preserve existing meaning | Honor effective `ResolvedAgent.timeout` across the complete logical Durable run using deterministic Durable time; no new front-matter variant | Human | 2026-09-22 |
| 74 | Owner modes and inbound MCP | Entra-only/reject MCP / preserve existing authenticated surfaces | Entra principal or function/admin credential fingerprint owns runs; anonymous remains invalid; supersedes 67. Inbound MCP v1 inclusion **SUPERSEDED by 75**; key-owner intent remains unchanged | Human | 2026-09-22 |
| 75 | Inbound MCP timing and follow-up tracking | Include in v1 / defer adapter | Defer inbound Durable MCP to post-v1; keep outbound remote MCP in v1. Remove inbound adapter/counts/qualification from v1; track agreed deferrals, optional work and candidates separately in §4.15 | Human | 2026-09-23 |
| 76 | HTTP function-key scope | Shared management / co-located paths | Each agent HTTP entry owns submission and management in one function so its individual function key works for both; no host-wide-key requirement; supersedes shared-handler topology in 63/66 | Human; Agent routing detail | 2026-09-23 |
| 77 | Harness investigation gate | Block FRD / side investigation | Verify actual pinned harness/skills behavior separately; do not block FRD approval/merge; breaking findings go to a follow-up PR without silent capability removal | Human | 2026-09-23 |
| 78 | Cross-deployment compatibility | Version pin/gate / user-owned compatibility | Preserve session state/identity/order across deployments without catalog/deployment pinning; users own breaking-change handling and migrations, and real errors surface explicitly; extends 54 | Human | 2026-09-23 |
| 79 | Usable delivery slices | Six fragmented core layers / coherent core plus parallel work packages | Replace C2a-C2f with one usable C2 core PR, followed by optional Sandbox C3 and qualification C4; no waived gates; supersedes 71 | Human; Agent delivery plan | 2026-09-23 |
| 80 | Debug UI compatibility | Separate/deferred UI / reuse existing UI | Keep existing debug UI in v1; detect Durable mode and automatically poll results through its authorized entry. Include working enabled-flag behavior in C2; only richer enhancements/SSE remain optional | Human | 2026-09-23 |
| 81 | Limited same-function routes | Separate management function / unrestricted catch-all / constrained single-function dispatch | Keep individual function-key and Entra support; expose only explicit operations through one constrained route and method/path allowlist. Validate host syntax separately without blocking FRD review; never expose system-key management URLs. Authored mapping and conservative ambiguity checks are specified in §4.13 | Human direction; Agent mapping | 2026-09-23 |
| 82 | Entry auth, retry boundaries and UI bootstrap corrections | Agent-wide policy / per-entry policy; direct workflow imports / shared mechanics; protected page / existing static page | Preserve independent entry auth; extract generic retry mechanics with engine-owned validation; retain separate static page and count it as U. Refines Decisions 76/80/81 without weakening data-endpoint auth | Human | 2026-09-23 |
| 83 | Canonical app namespace | Inferred hosting identifier / explicit persistent UUID | Required-setting proposal **SUPERSEDED by 85/86**; previously required deployment-level AZURE_FUNCTIONS_AGENTS_APP_ID for Durable apps; preserve across code deployments, define slot/rename/migration behavior separately from caller identity | Human direction; Agent contract | 2026-09-23 |
| 84 | Cancellation scope | Mandatory quarantine / bounded best effort then continue | Request supported cancellation, wait briefly for stop evidence, abandon the local wait and allow subsequent turns; retain unknown outcomes and generation fencing, not effect isolation. Five-second internal cleanup budget; cross-worker observation and sandbox control require qualification. Supersedes mandatory quarantine language | Human direction; Agent contract | 2026-09-23 |
| 85 | App identity configuration burden | Required user UUID / reuse backend or platform identity | Withdraw required AZURE_FUNCTIONS_AGENTS_APP_ID from Decision 83. Select a no-new-required-setting namespace after comparing existing backend/task-hub scope, platform identity and backend-generated identity; selection remains open | Human | 2026-09-23 |
| 86 | Namespace selection and early sandbox cancellation assessment | Extra app identifier / existing backend-hub boundary; defer all sandbox investigation / assess in C2 | Use bound backend/task hub as state namespace, no extra app identifier in hashes or new setting; C2 verifies confinement and assesses existing sandbox cancellation primitives, C3 implements and qualifies them. Closes Decision 85's selection | Human | 2026-09-23 |
| 87 | HTTP retry identity | Cross-entry deduplication / originating-entry scope | Scope session/request identity to agent slug, owner and registration-assigned entry ID (builtin or http_trigger) within the backend/hub. Switching entries does not deduplicate; management retains originating-entry auth | Human | 2026-09-23 |
| 88 | Final architecture sign-off | Keep in review / finalize | Approve finalized architecture after focused independent review, corrections and closure of all 14 current PR review threads. Implementation and qualification gates remain required; supersedes Decision 58's review-status hold | Human (larohra) | 2026-09-23 |

## 6. Test plan

- Tests mirror modules; authoring fixtures under `tests/fixtures/config_scenarios/`.
- Config fixtures include Durable inheritance/clear, timeout inheritance and
  precedence, workflow/subagent rejection, authenticated owner modes, anonymous
  rejection, inbound Durable MCP rejection (including shorthand), unchanged
  ordinary MCP and supported outbound MCP, and Sandbox-group compatibility scenarios.
- Targeted tests with each layer, then canonical ruff/mypy/full pytest gate
  on Python 3.13/3.14; separate testing-review checkpoint.
- Real-host/cloud qualification requires scoped approval and synthetic data.
- Replace prior answer-wins/409 cancellation expectations; earlier probe passes
  do not validate Decision 59.

| Area | Required coverage |
| --- | --- |
| Config/compatibility | Inheritance/clear/invalid settings; unchanged timeout precedence for ordinary and Durable agents; unsupported graph combinations |
| Composition exclusions | Reject durable + enabled workflows/nonempty subagents, runtime workflow/delegate tool references, and delegation targeting durable agents; allow independent same-app coexistence |
| Identity/admission | Entra and function/admin key owners; key rotation; raw-key non-persistence; anonymous/spoofing/cross-owner negatives; ID/key bounds and case; deterministic returned IDs with/without session carrier; lost ack; same-job retries; busy-then-admit; fingerprint conflicts |
| Handoff | Pinned-provider fault matrix in §4.5; no orphan reservation or replacement execution/job; delayed start acknowledgement rejected after slot release; no dispatch before accepted start acknowledgement; superseded commits cannot change the newer turn |
| Model/replay | Per-call boundary; whole-run timeout across replay/retry/human wait; late-result fencing; actual pinned wire fidelity; cold restore; partial dependency groups; compaction; count budgets; platform-profile limits |
| Human input | Same-run/session/sandbox; Entity batch order; duplicate/conflicting/lost answers; cancel accepted-but-unconsumed answer while retaining receipt; reject new answers to closed questions; terminal ordering; mixed batches dispatch none |
| Tools | Overlapping activities/guest processes; ordered results; stable operation IDs; at-least-once window; worker MCP; no hidden retry |
| Sandbox | Digest/ABI/archive validation; structured args; guest-control tampering; interrupted setup; same-ID resume; permanent loss; actual owned cleanup |
| State/transport | Low-compressibility >1 MiB; threshold/cap/envelope bounds; cold hydration; storage failures; management-read limit separately |
| Lifecycle | Idle/wait expiry; stale timers; no read/retry renewal; post-acknowledgement orchestration loss expires through the Entity-held deadline without permitting late commits; duplicate acknowledgement never extends deadline; active protection; revocation; late work; retained retry authority; repeated-DELETE identity, owner checks and pending/completed/failed outcomes |
| Registration/API | Exact once-only inventory with no inbound Durable MCP handlers; reject unsupported MCP exposure without ordinary-runner fallback; preserve ordinary MCP and outbound MCP tools; auth/methods/routes; chat cannot be shadowed by management; encoded paths; client/generator lifetime; independent drain; optional UI/SSE |
| Debug UI | Page loads without a Functions key and can prompt for one; no secrets/run data in page; data endpoints still require auth; exact +1 page inventory; ordinary streaming unchanged; Durable admission and automatic polling with stable retry key; terminal/error/question display; authorized history; stale responses discarded after agent/session switch; no duplicate run on polling failure |
| Entry auth and retry boundaries | Same agent with distinct built-in/authored policies; preserved legacy/default auth precedence; originating-entry reauthorization; no Durable-to-workflow implementation imports; existing persisted workflow retry envelope/exception compatibility |
| Entry retry identity | Same-entry retries return original receipt; same owner/session/key on another entry is separately scoped; registration-assigned entry ID cannot be spoofed; route rename preserves entry identity; management links and policy stay with originating entry |
| App namespace | No new identity setting or app-ID hash field; same IDs in separate backend/hubs cannot cross-read/write; all workers for one hub share state; request cannot override bound namespace; cache confinement; slot/backend pairing; deployment/rename continuity with unchanged backend/hub; explicit migration on namespace change |
| Bounded cancellation | Deadline enforced by call-owning activity; explicit cancel observed across workers; bounded parallel cleanup, not per-call serial waits; ignored/unavailable cancellation allows subsequent turns; late commits fenced; unknown outcomes retained; local request closure does not imply remote stop; sandbox signal uses owned operation handle and distinguishes request acknowledgement from process exit |
| Sandbox cancellation assessment (C2) | SDK/API evidence for operation handle, independent cancel/signal request, exit acknowledgement and HTTP-disconnect behavior; simplest supported adapter handed to C3, or explicit unsupported finding; no inferred process-stop guarantee |
| Authored HTTP routing | One trigger per entry; unchanged submit URL/methods; parameter-bound same-entry links; management method union cannot broaden submission; host-constrained suffix plus dispatcher allowlist; empty suffix; encoded/extra paths; ambiguous shapes and collisions rejected before registration; sibling routes not shadowed |
| Reuse/CI | Source-to-target regressions; fixture/wheel provenance; matrix/trust boundaries; required versus advisory results |

| Blocking gate | Layer | Environment | Required evidence |
| --- | --- | --- | --- |
| Ordinary/workflow behavior and exact registration inventory unchanged | C0/C1 | CI, Python 3.13/3.14 | Full existing workflow/runner/app suites |
| Durable config and unsupported-combination matrix | C2 | CI, Python 3.13/3.14 | Named scenario fixtures and generated-reference check |
| Replay, ordered fan-out, call budgets and state boundaries | C2 | CI plus pinned live DTS | Deterministic test doubles plus real adapter integration; 899,999/900,000/900,001-byte state cases; 10,485,759/10,485,760/10,485,761-byte cap cases |
| Admission fault matrix and same execution | C2 | Pinned live DTS | Same instance ID, original creation time and one orchestration-written execution marker; no replacement on lost acknowledgement |
| Ownership, lifecycle and management reads | C2 | Host-enforced function/admin auth, deployed Entra + DTS | Same function key submits/manages; different keys isolated; mixed per-agent auth; ambiguous carrier rejection; 4,194,303/4,194,304/4,194,305-byte reads; deletion/TTL/tombstone outcomes |
| Human-input ordering and cancellation | C2 | CI plus pinned live DTS | Accepted-versus-consumed receipt, same run/session, stale generation and cancellation cases |
| Model wire fidelity and terminal/retry classification | C2 | Supported live providers | Restored reasoning/tool dependency groups; no retry for terminal outcomes |
| Deployment continuity | C2 | CI and supported host | Same IDs/history/receipts after compatible deployment; no rejection solely for catalog change; incompatible user code yields explicit errors without session reset |
| Sandbox affinity, loss and cleanup | C3 | Deployed sandbox profile | Same-ID resume, digest/ABI checks, `workspace_lost`, owned-resource cleanup |
| Supported hosting profiles | C4 | Flex/Premium, Python 3.13/3.14 | MI/private-network paths and published support matrix |

- The promotion PR is blocked until every required gate above is green. Other
  experiments are explicitly advisory and cannot substitute for a required gate.

## 7. Docs impact

| Documents | Updates with implementation |
| --- | --- |
| `docs/architecture.md` | Module map, pipeline, Entity/checkpoint/workspace boundaries |
| Front-matter spec/reference | Inheritance, examples, invalid combinations; regenerate reference |
| `docs/triggers.md`, `docs/workflows.md` | HTTP/chat v1 scope; inbound MCP deferral versus supported outbound MCP; composition restrictions |
| `docs/observability.md` | Cross-activity trace continuity, replay-safe counts and sensitive-data gating |
| New Durable guide | API, support/size matrix, auth, retry/cancel/TTL/cleanup runbooks |
| Durable deployment/runbook sections | Same-function key access; selected automatic namespace and slot/backend pairing; session continuity without deployment pinning; user-owned compatibility/migrations and post-cancellation effects; supported-provider verification |
| README, docs landing/onboarding | Secure preview setup; ordinary compatibility |
| FRD index, `mkdocs.yml`, samples | Navigation; HTTP/group/worker-MCP examples |

- Schema slices: run `eng/scripts/generate_config_reference.py` and
  `update-schema-docs`; no private identifiers, credentials or unsupported promises.

## 8. Status & sign-off

- **Finalized**; #226 merged the initial FRD; revision PR #234 targets
  `feature/durable-agent-loop`. C0 characterization tests are open draft #235;
  C1 has not started. Post-v1 tracking is in §4.15.
- Human architecture sign-off: **larohra, 2026-09-23**, explicitly approved
  after the focused Claude Opus 5 review and correction pass. All 14 current
  PR review threads were resolved at sign-off. This approves the design, not
  unexecuted runtime qualification or release promotion; §6 gates still apply.
- **Linux normal path passed:** scripted model/tool checkpoints, parallel tools,
  Entity-backed human pause and same-execution completion. Broader qualification remains open.
- Further experiments have separate consent; none authorized by this edit.
- Mark Implemented only after qualified feature promotion to `main`.

### Source-pinned evidence and open qualification

| Evidence | Established | Still required |
| --- | --- | --- |
| S0; [native b3][native], [payload library][payload] | Native >1 MiB state/IO/query hydration and cold restore passed on local host/live DTS | Deployed profiles, failure/scale limits, MI/private networking |
| MAF [microsoft/agent-framework#7233][maf-fix], [microsoft/agent-framework#7536][maf-foundry] | Old defects reproduced; compatible wire state preserved offline | Supported live provider/options and integrated recovery |
| Sandbox SDK `0.1.0b4`, `azure-core==1.35.1` | Narrow Python 3.13/Linux bundle, overlapping guest calls, same-ID resume, owned deletion passed | Integrated Durable path, MI/private/restricted identity, Python 3.14/native dependencies |
| Linux normal-path fixture; [native b3][native], [registrations][registrations] | Seven bindings; five separate scripted activity completions; 1.997s tool overlap; Entity-backed human pause/resume in one public execution ID; app-MI/DTS connectivity | Admission/crash/retry, cancel/expiry, production Entra auth, real models and broader profiles |
| Native admission; [Entity API][entity] | State/reply/start API available; public execution ID observed on the Linux stack | Same-job handoff recovery and provider failure guarantees |
| [Common byte bounds][payload], [b3 client][native-client] | Offline library/envelope checks | Native management-read profile, host-setting support |

- S0 pins: Python `3.13.15`; `azure-functions-durable==2.0.0b3`;
  `durabletask==1.10.0`; Core Tools `4.13.0`; host `4.1051.300.26316`;
  bundle `4.38.1`; Durable extension `3.14.0`; AzureManaged host/backend/adapter
  `1.10.0`; common payload library `1.24.2`.
- Linux normal-path pins: Python `3.13.14`, `azure-functions==2.3.0`,
  `azure-functions-durable==2.0.0b3`, `durabletask==1.10.0`; scripted calls only.

[native]: https://github.com/microsoft/durabletask-python/blob/46602d5221591b6aaeff1238cd0ec952419e2e29/azure-functions-durable/pyproject.toml
[entity]: https://github.com/microsoft/durabletask-python/blob/46602d5221591b6aaeff1238cd0ec952419e2e29/durabletask/entities/entity_context.py#L131-L164
[input-order]: https://github.com/microsoft/durabletask-python/blob/46602d5221591b6aaeff1238cd0ec952419e2e29/durabletask/worker.py#L1420-L1482
[input-event]: https://github.com/microsoft/durabletask-python/blob/46602d5221591b6aaeff1238cd0ec952419e2e29/durabletask/task.py#L365-L375
[registrations]: https://github.com/microsoft/durabletask-python/blob/46602d5221591b6aaeff1238cd0ec952419e2e29/azure-functions-durable/azure/durable_functions/decorators/durable_app.py#L37-L70
[native-client]: https://github.com/microsoft/durabletask-python/blob/46602d5221591b6aaeff1238cd0ec952419e2e29/azure-functions-durable/azure/durable_functions/client.py#L148-L182
[payload]: https://github.com/microsoft/durabletask-dotnet/blob/29d53ff5091ba1edeefb6b7798716a259d138ae6/src/Extensions/AzureBlobPayloads/Interceptors/PayloadInterceptor.cs#L169-L190
[maf-fix]: https://github.com/microsoft/agent-framework/pull/7233
[maf-foundry]: https://github.com/microsoft/agent-framework/pull/7536
[e2e-assets]: https://github.com/Azure/azure-functions-agents-runtime/pull/196
[e2e-ci]: https://github.com/Azure/azure-functions-agents-runtime/pull/197
