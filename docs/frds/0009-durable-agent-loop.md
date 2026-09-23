---
frd: 0009
title: Public Durable Agent Loop
status: In review
author: larohra
created: 2026-09-16
updated: 2026-09-22
issues: []
pull_requests: [226]
branch: larohra/durable-agent-loop
---

# FRD 0009 - Public Durable Agent Loop

## 1. Summary

- Opt-in public-preview Durable execution for markdown-first MAF agents.
- HTTP/chat first; one authorized session Entity; one orchestration per turn.
- One foreground model step or tool invocation per activity checkpoint.
- Optional session-bound Sandbox Group; remote MCP stays worker-side.
- **Review draft:** no final architecture sign-off or implementation authorization.

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

- V1 excludes non-HTTP triggers and inbound MCP agent exposure.
- **Post-v1:** Durable Agent Loop composition with Dynamic Workflows (FRD 0004),
  `subagents:` delegation (FRD 0007), or `workflows.subagents`.
- Independent ordinary, workflow-enabled and durable agents may coexist in one
  app; the restriction is composition, not app-wide coexistence.
- Reject unsupported configurations and references before registration;
  never fall back to the ordinary runner.
- No ingress/tool-policy DSL or tool-action approval engine.
- UI/SSE and Timer research remain outside core release dependencies.

## 4. Proposed design

### 4.1 Pipeline and module ownership

| Stage | Modules | Responsibility |
| --- | --- | --- |
| Discover | `discovery/{tools,skills,mcp}.py` | Read-only inventories; stable descriptors |
| Translate | `config/{schema,merge,validation,loader}.py` | Shared typed settings; inheritance; compatibility |
| Compose | `app.py`, `registration/{catalog,capabilities}.py` | Freeze policies/catalogs; choose one `DFApp` |
| Register | `registration/{triggers,_handlers,endpoints,_auth}.py` | Shared authenticated Durable routing/bindings |
| Execute | New `durable/{engine,activities,model,tools}.py` | Native orchestration; separate model/tool checkpoints |
| Session | New `durable/{session,contracts,intake}.py` | Entity authority; admission; versioned envelopes |
| Sandbox/lifecycle | New `durable/{sandbox,lifecycle}.py` | Binding, packaging, resume, expiry, cleanup |
| Optional UI | `public/durable-chat/`, endpoint adapter | Public API client only |

- Validate the complete agent graph before app mutation; clients remain lazy.
- Registration remains Azure-aware; no YAML reparsing or discovery-time provisioning.
- Reuse workflow/auth/identity conventions without importing workflow-plan semantics.
- `registration/catalog.py:build_catalog()` is followed by a side-effect-free
  `durable/policy.py:build_durable_agent_policy_catalog(catalog)`. The immutable,
  slug-keyed `DurableAgentPolicyCatalog` freezes resolved limits, effective auth,
  model/deployment bindings, sandbox-group settings and manifest/package digests
  in pass 1 before app creation. Durable execution reauthorizes by slug against
  this catalog and never reads YAML, front matter or `GlobalConfig`.
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
- On expiry, stop scheduling new calls, durably record terminal `timed_out`,
  request best-effort cancellation of supported in-flight work and fence late
  completions. Timeout is not rollback and does not claim that an unknown
  external effect was prevented. Session idle TTL remains a separate lifecycle
  policy and starts after the timed-out turn is committed.
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
- Durable agents preserve the existing effective endpoint auth modes except
  `anonymous`, which has no stable owner credential. Entra, function-key and
  admin-key ingress all map to the owner model in §4.4.
- `builtin_endpoints.mcp: true`, including through the
  `builtin_endpoints: true` shorthand, registers a Durable-aware MCP adapter.
  It uses the same Entity admission and checkpointed turn engine rather than
  calling the ordinary runner. `chat_api` similarly means Durable admission;
  ordinary `chatstream`, blob-backed history and workflow-status endpoints are
  not registered.

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
- Identity scope: deployment/app, agent slug, normalized owner identity from
  §4.4 and trusted producer scope; no delivery/attempt metadata.
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

- Normalize every authenticated ingress to a non-secret owner ID:
  - Entra: verified `(tenant_id, object_id)`; `_auth.py` requires exactly one
    `tid` and one `oid`, and absent or multi-valued claims fail closed with 401.
    Platform-enforced Easy Auth and a configured tenant allowlist are required;
    the operator-only `AZURE_FUNCTIONS_AGENTS_ENTRA_EASY_AUTH` assertion is not
    sufficient for Durable ownership.
  - Function/admin: a domain-separated SHA-256 fingerprint of the credential
    presented on the host-authorized request, scoped to deployment/app and auth
    mode. The raw key is never logged, checkpointed or returned. All callers
    presenting the same key share one owner and may access that key owner's runs;
    rotating the key creates a new owner and does not transfer old runs.
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
- The turn orchestration acknowledges start to the Entity as its first durable
  step. Admission also schedules a bounded, generation-tagged delayed self-signal.
  If no start acknowledgement exists at reconciliation, the Entity transitions
  the accepted turn to terminal `start_unconfirmed`/`outcome_unknown`, releases
  the active-turn slot and starts idle TTL. It does not create a replacement run.
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

1. Read captured definition/model/tool bindings and authorized Entity state.
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
  conflicting answers 409; closed/cancelled/expired questions reject **new** answers (410).
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
- No provider-hosted tools, hidden model calls or background submit/poll/cancel.
- Responses-compatible providers: foreground, `store: false`; preserve full
  compatible reasoning/encrypted items, calls, arguments and outputs.
- Provider conversation/response IDs are not session authority.
- Runtime owns `ClientManager` integration and compatible dependencies;
  customers supply normal endpoint/model/credentials, not serialization adapters.
- Capture non-secret model/deployment/API/schema bindings; resolve credentials
  at execution. Incompatible provider state fails explicitly; no model switching.
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
- All model/tool activities use the existing retryable-versus-terminal bridge:
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
  project tools; a cold-worker digest mismatch fails instead of repackaging.
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
- Public preview supports the pinned AzureManaged/DTS provider profile. A known
  incompatible provider fails startup; an unidentifiable provider fails Durable
  admission explicitly rather than weakening the offload contract.

### 4.11 Cancellation, recovery and deployments

- Cancellation is acknowledged intent, not rollback or guaranteed remote termination.
- Stop new steps; preserve late results/unknown effects. New mutating work
  waits for completion, supported fencing or explicit quarantine/reconciliation.
- Native retries use captured policy and the structured retry bridge from §4.8;
  account separately for SDK transport retries.
- Lifecycle recovery covers hard termination and crashes outside orchestration cleanup.
- Version persisted contracts; incompatible continuation fails explicitly.
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
- Reads, polling, rejected requests and duplicate receipts never renew TTL.
- Human wait starts TTL when question is durably opened; timely accepted answer
  invalidates that wait generation.
- Unanswered wait ends **run and session**, with no second TTL.
- Unknown/quarantined work cannot receive indefinite active-work exemption.
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
- Pending human input is not a ContinueAsNew boundary. Instance retention/purge
  policy must exceed `session_ttl`.
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
- Resolve the effective Durable auth policy from
  `builtin_endpoints.http_auth` when built-ins are enabled, otherwise from the
  authored HTTP trigger's `trigger.args.http_auth`. Reject agents for which
  management and ingress cannot resolve to the same owner-identity mode.
- Durable agents suppress ordinary chat page, chatstream, blob-backed history,
  and workflow-status registrations. Optional Durable UI is served only through
  the management adapter.

#### Built-in MCP adapter

- Preserve the existing agent-as-MCP-tool surface. The MCP trigger parses the
  prompt and transport session as today, then submits to the same Durable Entity
  admission and turn orchestration; it never calls `run_agent()` directly.
- The adapter derives its owner from the host-authorized MCP credential using
  the same credential-fingerprint rule as function/admin HTTP ingress. If the
  MCP binding cannot provide the validated credential to the handler, startup
  fails for Durable MCP rather than assigning a shared anonymous owner.
- Map the MCP transport session deterministically through the existing
  `_extract_mcp_session_id()` convention. Map the protocol invocation/request ID
  to the Durable idempotency key so transport retries recover the same run. The
  implementation must qualify the exact MCP binding payload; absence of a stable
  invocation ID is a C2c blocker, not permission to use prompt hashing.
- MCP tool invocation remains request/response shaped: wait for the Durable run
  up to the transport's response window. If it completes, return the existing
  `{session_id,response,tool_calls}` result. If it remains active, return a
  structured accepted result with session/run IDs and MCP-callable status,
  input, cancel and history tools backed by the same management contracts.
  Disconnect never cancels or restarts the Durable run.

| Registration | Trigger |
| --- | --- |
| `agents_<slug>_durable_submit_v1` | Per-agent literal HTTP admission |
| `agents_<slug>_durable_mcp_v1` | Per-agent Durable-aware MCP adapter when enabled |
| `agents_durable_management_v1` | HTTP management/optional UI/SSE |
| `agents_durable_orchestrator_v1` | Independent typed admission/turn/input/lifecycle instances |
| `agents_durable_state_v1` | Entity; disjoint session/owner-index kinds |
| `agents_durable_execute_v1` | Activity; typed model/tool/maintenance operation |
| `BuiltIn__HttpActivity` | SDK outbound-HTTP activity |
| `BuiltIn__HttpPollOrchestrator` | SDK outbound-HTTP polling orchestration |

- **Four shared owned + one literal submit per built-in Durable chat agent +
  one MCP adapter per MCP-exposed Durable agent + two native SDK helpers.**
  One chat-only Durable agent therefore registers seven; chat + MCP registers eight.
- SDK helpers are outbound machinery, not inbound APIs or model background polling.
- Shared registration does not merge checkpoints, serialize activities or let
  model arguments select maintenance operations.
- Per-agent submitters use literal `agents/<slug>/chat` routes, so they cannot
  collide with ordinary literal routes or depend on literal-over-template
  precedence. Shared management uses `agents/{slug}/manage/{*path}`, rejects
  submission paths and resolves the slug against the frozen Durable catalog.
- One chat-only Durable agent: 7; many: `6+B+M`; UI/SSE/group: +0;
  custom-only: `6+C`. Existing workflow engine: separate 3; SDK helpers counted once.
- Total `F = O + 2I + 3W + 4D + B + M + C`: ordinary registrations O; native
  helper pair I; workflow engine W; Durable shared engine D; built-in Durable
  chat agents B; Durable MCP exposures M; custom ingress C.
- V1 composition exclusions add no registrations; `3W` still covers independent
  workflow-enabled agents in the same app.
- JSON clients request-scoped; SSE client lives inside generator; static assets
  need none. Preserve auth/route precedence; reject conflicting reserved paths.

#### Optional UI/SSE

- Adapt `larohra/durable-loop-chat-ui`; Entity history is authoritative;
  browser cache optional, no default sensitive/credential persistence.
- UI remains optional through `builtin_endpoints.debug_chat_ui`.
- Polling-only UI uses status, questions, session discovery/history; no journal.
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
| C2a | Durable schema, merge, validation, cross-agent checks and fixtures | C1 | Generated reference/spec, workflows/triggers restrictions; zero new functions | Translation and rejection rules |
| C2b | Frozen policy catalog plus Entity/contracts/engine over fake model/tools | C2a | Architecture module map | Determinism, replay, batches, budgets |
| C2c | Per-agent HTTP/MCP admission, ownership and start reconciliation | C2b | Durable API/auth guide begins | Idempotency, auth, protocol adapters, route inventory |
| C2d | Management API, history, TTL, deletion and tombstones | C2c | Lifecycle/runbook docs | Revocation and cleanup truthfulness |
| C2e | Durable human-input protocol | C2d | Input API docs | Acceptance versus consumption/order |
| C2f | Real foreground MAF adapter and compaction activity | C2e | Model support matrix | Wire fidelity and restored context |
| C3 | Sandbox packaging, parallel execution, affinity/loss/cleanup | C2f; #196/#197 merged or pinned assets vendored with source SHA | Sandbox deployment guide | Isolation and ambiguous effects |
| C4 | Enumerated deployed qualification profiles and support matrix only | C3 | Runbooks, observability, README/onboarding | Required evidence |
| Promotion | Feature branch → `main` after all blocking gates | C4 | Final coherent docs | Qualified public preview |
| Post-v1 | Dynamic Workflows/subagents composition design | Promotion | Separate FRD | DTS sub-orchestrations |

- Defer composition design/implementation to post-v1: existing delegation runs
  a whole MAF loop, bypassing per-call checkpoints and call budgets.
- DTS sub-orchestrations are a candidate, not a selected implementation.
  Decide their registration impact in that later design; no v1 gate or count change.
- Integration branch originates from `main`; no feature increments directly to `main`.
- Stack: `feature/durable-agent-loop <- C0 <- C1 <- C2a <- C2b <- C2c
  <- C2d <- C2e <- C2f <- C3 <- C4`; D0 separate.
- Start dependent layers from recorded buildable pushed parents; overlap review,
  implementation and qualification; owner-scoped bottom-up rebases.
- Keep tests/docs with each layer; no broken/dead public flags or C4 catch-all.
- Introduce Sandbox fields with C3, not before their implementation.
- C1 must pass the complete existing workflow and runner suites unchanged before
  C2a starts. C2a carries generated schema docs in the same change.
- Architecture sign-off precedes product implementation; cloud runs require
  separate consent. Branch targeting does not establish required-check policy.

| Pinned source | Reuse/adaptation |
| --- | --- |
| `larohra/durable-loop-leadership-demo@d3ebac5afadeb8cb6d59f54a53177a39de4275f5` | `experimental/durable_loop_*` and tests → C2 native model/session/receipt/HTTP primitives |
| `feature/aca-sandboxes@88f553ed6a67e399a8fb660cf7aaef15905f0590` + leadership | `controller/{package,bootstrap_delivery,sandbox_config}.py`, `harness/bootstrap.py`, `transport/aca_sdk.py`, manifests/tests → C3 |
| Same ACA pin; [#196][e2e-assets], [#197][e2e-ci] | Fixture/wheel assembly, dependency export, smoke/provenance, deployed suites and Python matrix → C2–C4 |
| `larohra/durable-loop-chat-ui@9fe3edb8df1292533508e3742df13b42ad8c3e33` | `experimental/durable_chat_*`, `public/durable-chat/`, browser/history tests → optional API adapter |

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
| 41 | Human-wait TTL | Separate / reuse | Session TTL; expiry ends run/session once | Human | 2026-09-18 |
| 42 | Approval scope | Enforce / defer | Input only; no approval engine; supersedes 40 | Human | 2026-09-18 |
| 43 | HTTP helpers | Outbound / inbound | SDK helpers outbound; authenticated facade required | Agent clarification | 2026-09-18 |
| 44 | Polling UI | Journal / state | Status/questions/Entity history; no journal required | Agent clarification | 2026-09-18 |
| 45 | Shared execution | Split / typed | Five owned + two SDK; seven total | Agent recommendation | 2026-09-18 |
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
| 63 | HTTP route isolation | Shared dispatcher / separate namespaces | Two HTTP handlers; management under `/agents/{slug}/manage/...`; seven registrations retained | Human | 2026-09-21 |
| 64 | Workflow/subagent composition | Support in v1 / defer | Post-v1; reject §4.2 combinations/references; evaluate DTS sub-orchestrations later; independent app coexistence allowed | Human | 2026-09-22 |
| 65 | Policy ownership | Re-resolve / frozen catalog | Pass-1 immutable Durable policy catalog; activity-time reauthorization by slug | Agent architecture review | 2026-09-22 |
| 66 | Mixed-app submit routing | Shared wildcard / per-agent literal / new namespace | Per-agent literal submit routes; shared management wildcard; supersedes the constant-seven part of 45/63 | Agent architecture review | 2026-09-22 |
| 67 | Durable owner authentication | Existing modes / mandatory Entra | Entra-only proposal **SUPERSEDED by 74** | Agent architecture review | 2026-09-22 |
| 68 | Unconfirmed start | Replace / remain busy / terminal reconcile | No replacement; generation-guarded reconciliation ends as `start_unconfirmed`/`outcome_unknown` and releases the slot | Agent architecture review | 2026-09-22 |
| 69 | Activity retries | Retry every exception / classified bridge | Structured terminal outcomes; native retry only for sanitized retryable failures | Agent architecture review | 2026-09-22 |
| 70 | Dependency baseline | Keep current / selected compatible set | Durable b3 + durabletask 1.10.0; MAF `1.17.0/1.14.2/1.12.0`; existing workflows/runner requalified first | Agent architecture review | 2026-09-22 |
| 71 | Delivery split | C1-C4 / smaller stack | Add C0 and split C2 into config, engine, admission, lifecycle, input and MAF adapter slices | Agent architecture review | 2026-09-22 |
| 72 | Durable call ceiling | New runtime value / external ceilings | No separate per-call setting; whole-run conclusion **SUPERSEDED by 73** | Agent architecture review | 2026-09-22 |
| 73 | Timeout compatibility | Reject/ignore / preserve existing meaning | Honor effective `ResolvedAgent.timeout` across the complete logical Durable run using deterministic Durable time; no new front-matter variant | Human | 2026-09-22 |
| 74 | Owner modes and inbound MCP | Entra-only/reject MCP / preserve existing authenticated surfaces | Entra principal or function/admin credential fingerprint owns runs; anonymous remains invalid; built-in MCP routes through Durable admission/checkpoints and exposes async management tools when not immediately complete; supersedes 67 and the earlier MCP rejection | Human | 2026-09-22 |

## 6. Test plan

- Tests mirror modules; authoring fixtures under `tests/fixtures/config_scenarios/`.
- Config fixtures include Durable inheritance/clear, timeout inheritance and
  precedence, workflow/subagent rejection, authenticated owner modes, anonymous
  rejection, MCP combinations and Sandbox-group compatibility scenarios.
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
| Handoff | Pinned-provider fault matrix in §4.5; no orphan reservation or replacement execution/job |
| Model/replay | Per-call boundary; whole-run timeout across replay/retry/human wait; late-result fencing; actual pinned wire fidelity; cold restore; partial dependency groups; compaction; count budgets; platform-profile limits |
| Human input | Same-run/session/sandbox; Entity batch order; duplicate/conflicting/lost answers; cancel accepted-but-unconsumed answer while retaining receipt; reject new answers to closed questions; terminal ordering; mixed batches dispatch none |
| Tools | Overlapping activities/guest processes; ordered results; stable operation IDs; at-least-once window; worker MCP; no hidden retry |
| Sandbox | Digest/ABI/archive validation; structured args; guest-control tampering; interrupted setup; same-ID resume; permanent loss; actual owned cleanup |
| State/transport | Low-compressibility >1 MiB; threshold/cap/envelope bounds; cold hydration; storage failures; management-read limit separately |
| Lifecycle | Idle/wait expiry; stale timers; no read/retry renewal; active protection; revocation; late work; retained retry authority; repeated-DELETE identity, owner checks and pending/completed/failed outcomes |
| Registration/API | Exact once-only inventory; auth/methods/routes; chat cannot be shadowed by management; Durable MCP immediate/accepted responses, protocol request-ID retry recovery and management tools; encoded paths; client/generator lifetime; independent drain; optional UI/SSE |
| Reuse/CI | Source-to-target regressions; fixture/wheel provenance; matrix/trust boundaries; required versus advisory results |

| Blocking gate | Layer | Environment | Required evidence |
| --- | --- | --- | --- |
| Ordinary/workflow behavior and exact registration inventory unchanged | C0/C1 | CI, Python 3.13/3.14 | Full existing workflow/runner/app suites |
| Durable config and unsupported-combination matrix | C2a | CI, Python 3.13/3.14 | Named scenario fixtures and generated-reference check |
| Replay, ordered fan-out, call budgets and state boundaries | C2b | CI plus pinned live DTS | Fake model/tool suite; 899,999/900,000/900,001-byte state cases; 10,485,759/10,485,760/10,485,761-byte cap cases |
| Admission fault matrix and same execution | C2c | Pinned live DTS | Same instance ID, original creation time and one orchestration-written execution marker; no replacement on lost acknowledgement |
| MCP transport compatibility | C2c | Pinned Functions MCP binding | Validated credential availability, stable protocol invocation ID, retry recovery, immediate completion and accepted-management flows |
| Ownership, lifecycle and management reads | C2c/C2d | Deployed Entra + DTS | Spoofing/cross-owner negatives; 4,194,303/4,194,304/4,194,305-byte reads; deletion/TTL/tombstone outcomes |
| Human-input ordering and cancellation | C2e | CI plus pinned live DTS | Accepted-versus-consumed receipt, same run/session, stale generation and cancellation cases |
| Model wire fidelity and terminal/retry classification | C2f | Supported live providers | Restored reasoning/tool dependency groups; no retry for terminal outcomes |
| Sandbox affinity, loss and cleanup | C3 | Deployed sandbox profile | Same-ID resume, digest/ABI checks, `workspace_lost`, owned-resource cleanup |
| Supported hosting profiles | C4 | Flex/Premium, Python 3.13/3.14 | MI/private-network paths and published support matrix |

- The promotion PR is blocked until every required gate above is green. Other
  experiments are explicitly advisory and cannot substitute for a required gate.

## 7. Docs impact

| Documents | Updates with implementation |
| --- | --- |
| `docs/architecture.md` | Module map, pipeline, Entity/checkpoint/workspace boundaries |
| Front-matter spec/reference | Inheritance, examples, invalid combinations; regenerate reference |
| `docs/triggers.md`, `docs/workflows.md` | HTTP-only scope; composition restrictions |
| `docs/observability.md` | Cross-activity trace continuity, replay-safe counts and sensitive-data gating |
| New Durable guide | API, support/size matrix, auth, retry/cancel/TTL/cleanup runbooks |
| README, docs landing/onboarding | Secure preview setup; ordinary compatibility |
| FRD index, `mkdocs.yml`, samples | Navigation; HTTP/group/worker-MCP examples |

- Schema slices: run `eng/scripts/generate_config_reference.py` and
  `update-schema-docs`; no private identifiers, credentials or unsupported promises.

## 8. Status & sign-off

- **In review**; public PR #226 targets `feature/durable-agent-loop`.
- Architecture **not finalized**; human sign-off outstanding.
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
