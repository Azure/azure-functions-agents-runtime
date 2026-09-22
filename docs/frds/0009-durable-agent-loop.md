---
frd: 0009
title: Public Durable Agent Loop
status: In review
author: larohra
created: 2026-09-16
updated: 2026-09-21
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

- V1 excludes non-HTTP triggers, inbound MCP agent exposure, chat/workflow
  subagent combinations, and durable-loop/Dynamic-Workflow composition.
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
- No default `max_model_steps` or `max_tool_calls`; `durable.limits` contains
  optional call-count ceilings only, not user-configurable timeouts.
- V1 adds no model/tool/turn duration deadline. Resolved Durable policy has no
  user-timeout fields and never consumes `ResolvedAgent.timeout` or the ordinary
  runner's environment/900-second fallback.
- Translation rejects per-agent `timeout` when Durable is enabled. Global
  `timeout` continues to apply only to ordinary agents; their behavior is unchanged.
- If a platform ceiling is necessary, use one platform-owned per-call limit
  shared by model and tool calls, not authoring knobs. No value is selected here.
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

### 4.3 HTTP session contract

- Validate `x-ms-session-id` using full-match `[A-Za-z0-9._-]{1,128}`;
  case-sensitive values. Root payload `x-session-id`, when supplied, must agree.
- Reject malformed, blank, non-string or conflicting IDs; no recursive
  business-payload searches or per-agent correlation mappings.
- Session ID is correlation, **not authentication**.
- `Idempotency-Key` is the sole client retry key (`logical_request_id`):
  case-sensitive, full-match `[A-Za-z0-9._-]{1,128}`. Missing/invalid keys return
  400 before admission; no body alias, trimming or case folding.
- Retries recover the same generated session; a new request key creates another
  session unless the caller supplies the returned session ID to continue the conversation.
- Identity scope: deployment/app, trusted tenant, agent slug, verified caller
  object ID and trusted producer scope; no delivery/attempt metadata.
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

- Require platform-enforced Entra/Easy Auth and existing allowlists.
- Typed principal: verified `(tenant_id, object_id)` for user/service principal;
  app ID, function key, body claims or spoofable headers are insufficient.
- Preserve built-in global `http_auth` and custom `trigger.args.http_auth`
  precedence; reject incompatible effective auth instead of silently upgrading it.
- Extend `_auth.py` without breaking its ordinary allow/error helper.
- Reauthorize management, answers and tool execution against current policy;
  captured descriptors cannot retain revoked permission.
- No cross-owner delegation policy in v1.
- Never expose system keys, raw DTS management URLs, Entity state or storage references.

### 4.5 Admission, idempotency and reliable handoff

- Require `Idempotency-Key` and normalized request-body fingerprint.
- Remove the root `x-session-id` carrier before fingerprinting; the normalized
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
5. Commit results by original call ID/order; continue or commit terminal outcome.

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
- Qualify full sets: baseline core/OpenAI/Foundry `1.13.0/1.10.2/1.10.3`;
  spike `1.17.0/1.14.2/1.12.0`. Foundry [microsoft/agent-framework#7536][maf-foundry] requires
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

### 4.9 Sandbox lifecycle and packaging

- Customer provisions group; runtime allocates session sandboxes only.
- Worker managed identity, group-scoped RBAC, approved resource/region and private paths.
- Capture immutable group, sandbox ID, incarnation, manifest/package and policy digests.
  Config edits do not move existing sessions.
- Default base: Python 3.13. Qualify OS/Python/native-wheel ABI for every advertised
  base, including Python 3.14/custom disks. No shared mutable disk across sessions.
- Deterministic deployment bundle: tools, skills, needed modules/assets/dependencies;
  exclude credentials, settings, caches and repository metadata.
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

### 4.11 Cancellation, recovery and deployments

- Cancellation is acknowledged intent, not rollback or guaranteed remote termination.
- Stop new steps; preserve late results/unknown effects. New mutating work
  waits for completion, supported fencing or explicit quarantine/reconciliation.
- Native retries use captured policy; account separately for SDK transport retries.
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
- Reuse the workflow duration grammar, not its separate 24-hour wait cap.
- Capture interval/policy at session creation; ordinary default edits do not migrate it.
- Idle TTL starts at last completed turn, including failure/cancellation.
  Accepted active work invalidates old idle expiry; terminal completion renews it.
- Runtime timestamps/generations guard stale timers.
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

### 4.13 API, registration footprint and optional UI

| Method/path under `/agents/{slug}` | Contract |
| --- | --- |
| `POST /chat` or authored HTTP entry | Durable acceptance, not synchronous answer |
| `GET /runs/{run_id}` | Bounded status/result/pending question |
| `POST /runs/{run_id}/cancel` | Idempotent cooperative cancel |
| `POST /runs/{run_id}/input/{question_id}` | Authenticated idempotent answer to the stored question |
| `GET /sessions` | Owner-scoped paginated discovery |
| `GET /sessions/{session_id}/history` | Authorized conversation, not raw Durable history |
| `DELETE /sessions/{session_id}` | Start deletion or return its existing receipt/progress; §4.12 |
| Optional `GET /runs/{run_id}/events` | Run-scoped SSE observations |

- Return `session_id` and `x-ms-session-id`; relative runtime-owned status links.
- No accidental direct-run `chatstream`/inbound MCP registration for durable agents.

| Registration | Trigger |
| --- | --- |
| `agents_durable_submit_v1` | HTTP admission |
| `agents_durable_management_v1` | HTTP management/optional UI/SSE |
| `agents_durable_orchestrator_v1` | Independent typed admission/turn/input/lifecycle instances |
| `agents_durable_state_v1` | Entity; disjoint session/owner-index kinds |
| `agents_durable_execute_v1` | Activity; typed model/tool/maintenance operation |
| `BuiltIn__HttpActivity` | SDK outbound-HTTP activity |
| `BuiltIn__HttpPollOrchestrator` | SDK outbound-HTTP polling orchestration |

- **Five owned + two native SDK helpers = seven** with built-in chat:
  2 HTTP, 2 orchestration, 2 activity, 1 Entity.
- SDK helpers are outbound machinery, not inbound APIs or model background polling.
- Shared registration does not merge checkpoints, serialize activities or let
  model arguments select maintenance operations.
- Two HTTP patterns: `agents/{slug}/chat`, `agents/{slug}/{*path}`.
  Recommend separate handlers so admission can drain independently; management
  rejects submission paths. Single-router alternative requires route-level gating.
- One/many durable agents: 7; UI/SSE/group: +0; custom ingress: +C;
  custom-only: 6+C. Existing workflow engine: separate 3; SDK helpers counted once.
- Total `F = O + 2I + 3W + D(4+B) + C`: ordinary O; native app I;
  workflow engine W; durable engine D; built-in durable chat B; custom ingress C.
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

| Layer | Base/target | Complete review unit |
| --- | --- | --- |
| D0 | `feature/durable-agent-loop` | FRD-only #226; In review |
| C1 | `feature/durable-agent-loop` | Native/MAF baseline; ordinary regression coverage |
| C2 | C1 | Config-to-HTTP core, ownership/handoff/input/TTL, fixtures |
| C3 | C2 | Sandbox packaging, parallel execution, affinity/loss/cleanup |
| C4 | C3 | Integrated qualification, support matrix, runbooks |
| Promotion | Feature branch → `main` | Separate reviewed/qualified final PR |

- Integration branch originates from `main`; no feature increments directly to `main`.
- Stack: `feature/durable-agent-loop <- C1 <- C2 <- C3 <- C4`; D0 separate.
- Start dependent layers from recorded buildable pushed parents; overlap review,
  implementation and qualification; owner-scoped bottom-up rebases.
- Keep tests/docs with each layer; no broken/dead public flags or C4 catch-all.
- Introduce Sandbox fields with C3, not before their implementation.
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
| 60 | V1 timeouts | Authored / platform-only | No user timeouts or ordinary fallback; shared platform ceiling only if necessary; supersedes 36 | Human; Agent validation detail | 2026-09-21 |
| 61 | Request identity | Ambiguous / explicit | Validated client key; deterministic scoped session/request IDs; §4.3 | Human; Agent validation detail | 2026-09-21 |
| 62 | Deletion progress | New endpoint / repeat DELETE | Original owner repeats DELETE for same receipt and pending/completed/failed status | Human; Agent HTTP detail | 2026-09-21 |

## 6. Test plan

- Tests mirror modules; authoring fixtures under `tests/fixtures/config_scenarios/`.
- Targeted tests with each layer, then canonical ruff/mypy/full pytest gate
  on Python 3.13/3.14; separate testing-review checkpoint.
- Real-host/cloud qualification requires scoped approval and synthetic data.
- Replace prior answer-wins/409 cancellation expectations; earlier probe passes
  do not validate Decision 59.

| Area | Required coverage |
| --- | --- |
| Config/compatibility | Inheritance/clear/invalid settings; ordinary timeout unchanged and never applied to Durable; reject authored Durable timeouts; unsupported graph combinations |
| Identity/admission | ID/key bounds and case; spoofing/cross-owner negatives; deterministic returned IDs with/without session carrier; lost ack; same-job retries; busy-then-admit; fingerprint conflicts |
| Handoff | Pinned-provider fault matrix in §4.5; no orphan reservation or replacement execution/job |
| Model/replay | Per-call boundary; actual pinned wire fidelity; cold restore; partial dependency groups; compaction; count budgets; platform-profile limits |
| Human input | Same-run/session/sandbox; Entity batch order; duplicate/conflicting/lost answers; cancel accepted-but-unconsumed answer while retaining receipt; reject new answers to closed questions; terminal ordering; mixed batches dispatch none |
| Tools | Overlapping activities/guest processes; ordered results; stable operation IDs; at-least-once window; worker MCP; no hidden retry |
| Sandbox | Digest/ABI/archive validation; structured args; guest-control tampering; interrupted setup; same-ID resume; permanent loss; actual owned cleanup |
| State/transport | Low-compressibility >1 MiB; threshold/cap/envelope bounds; cold hydration; storage failures; management-read limit separately |
| Lifecycle | Idle/wait expiry; stale timers; no read/retry renewal; active protection; revocation; late work; retained retry authority; repeated-DELETE identity, owner checks and pending/completed/failed outcomes |
| Registration/API | Exact once-only inventory; auth/methods/routes; wildcard/encoded paths; client/generator lifetime; independent drain; optional UI/SSE |
| Reuse/CI | Source-to-target regressions; fixture/wheel provenance; matrix/trust boundaries; required versus advisory results |

## 7. Docs impact

| Documents | Updates with implementation |
| --- | --- |
| `docs/architecture.md` | Module map, pipeline, Entity/checkpoint/workspace boundaries |
| Front-matter spec/reference | Inheritance, examples, invalid combinations; regenerate reference |
| `docs/triggers.md`, `docs/workflows.md` | HTTP-only scope; composition restrictions |
| New Durable guide | API, support/size matrix, auth, retry/cancel/TTL/cleanup runbooks |
| README, docs landing/onboarding | Secure preview setup; ordinary compatibility |
| FRD index, `mkdocs.yml`, samples | Navigation; HTTP/group/worker-MCP examples |

- Schema slices: run `eng/scripts/generate_config_reference.py` and
  `update-schema-docs`; no private identifiers, credentials or unsupported promises.

## 8. Status & sign-off

- **In review**; public PR #226 targets `feature/durable-agent-loop`.
- Architecture **not finalized**; human sign-off outstanding.
- Full native runtime qualification **pending**; primitive/offline evidence is narrower.
- Further experiments have separate consent; none authorized by this edit.
- Mark Implemented only after qualified feature promotion to `main`.

### Source-pinned evidence and open qualification

| Evidence | Established | Still required |
| --- | --- | --- |
| S0; [native b3][native], [payload library][payload] | Native >1 MiB state/IO/query hydration and cold restore passed on local host/live DTS | Deployed profiles, failure/scale limits, MI/private networking |
| MAF [microsoft/agent-framework#7233][maf-fix], [microsoft/agent-framework#7536][maf-foundry] | Old defects reproduced; compatible wire state preserved offline | Supported live provider/options and integrated recovery |
| Sandbox SDK `0.1.0b4`, `azure-core==1.35.1` | Narrow Python 3.13/Linux bundle, overlapping guest calls, same-ID resume, owned deletion passed | Integrated Durable path, MI/private/restricted identity, Python 3.14/native dependencies |
| Native runtime; [Entity API][entity], [registrations][registrations] | Native handoff API available; seven definitions indexed | Full native HTTP/parallel/human-input runtime qualification pending. |
| [Common byte bounds][payload], [b3 client][native-client] | Offline library/envelope checks | Native management-read profile, host-setting support |

- S0 pins: Python `3.13.15`; `azure-functions-durable==2.0.0b3`;
  `durabletask==1.10.0`; Core Tools `4.13.0`; host `4.1051.300.26316`;
  bundle `4.38.1`; Durable extension `3.14.0`; AzureManaged host/backend/adapter
  `1.10.0`; common payload library `1.24.2`.

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
