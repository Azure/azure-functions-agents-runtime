---
status: analysis-for-review
date: 2026-09-11
updated: 2026-09-14
implementation_authorized: false
integration_base: origin/main
main_commit: dad2a2a50e3b274643612fbf8d3d272a92c02367
feature_commit: 88f553ed6a67e399a8fb660cf7aaef15905f0590
durable_spike_commit: d3ebac5afadeb8cb6d59f54a53177a39de4275f5
---

# Production migration guide: ACA Tables to the Durable Agent Loop

## 1. Recommendation and scope

**Start a fresh implementation branch from current `main`. Treat the ACA and
Durable spike branches as code donors, not integration bases. Add the Durable
Entity/orchestration engine and selected ACA tool primitives without ever
importing the old Table controller. This is a selective port, not a
ground-up rewrite and not a merge-then-delete exercise.**

**Recommendation revised on 2026-09-14:** the earlier F-based plan optimized
preserving ACA fixes. The user's priority is a clean, contained change with no
Table/lease-management baggage. Current `main` is also substantially different
from the donors: it has adopted Durable Python 2.x and newer workflow/history
behavior. Starting from either donor would import obsolete code and then require
catching up with main. Section 7 now specifies main-based extraction.

The new feature's default scope includes **no `azure-data-tables` dependency,
application Table authority, Table projection, or generic custom lease manager**.
Selected safety checks, external-effect idempotency and ownership fences still
belong in the design; do not confuse those with porting the old Table lease
implementation under new names.

The target is:

```text
Existing authoring, validation and authenticated HTTP entrypoints
  -> immutable owner-scoped request and versioned execution plan
  -> durable admission/run orchestration
       -> session Entity: admission, cancellation, commit, binding authority
       -> one-step MAF model activity in Functions
       -> worker/MCP or ACA tool activity
       -> external context/result/workspace checkpoints
  -> per-session lifecycle orchestration
       -> expiry, readiness, recovery, cleanup and retirement

Blob: immutable content, narrow effect/admission receipts,
      retained terminal records, snapshot/cleanup obligations, optional events
ACA inventory: independent compute/snapshot orphan detection
DTS: orchestration/entity state and work dispatch
Tables: excluded from this feature; any future query projection is separate scope
```

**This changes execution placement, not just persistence.** FRD 0008 runs the
whole MAF agent turn in ACA. The Durable spike runs reasoning/model calls in
Functions and dispatches selected executable tools into ACA. Model credentials,
network access, tool loading, transcript custody and restart boundaries change.
If the requirement is instead "keep the whole agent in ACA, replace only
Tables," that is a different design and does not get step-level model/tool
replay merely by wrapping an existing `agent.run()`.

The user's concurrency/latency failures justify evaluating the replacement now.
They do not require finishing the existing Table controller first. Use that
implementation as the contract and benchmark reference while extracting its
useful components. There is no need to treat time already invested in Tables
as a reason to retain it.

**Evidence qualification:** reported contention is user evidence; this analysis
does not reproduce its cause or measure DTS against Tables. Different rows in
one Table partition do not automatically lock each other. Partition pressure,
same-row ETag retries, application scheduling, provider latency and one-session
contention must be distinguished. Entities should reduce application-owned
coordination races, but introduce scheduler dispatch/queue latency.

### Source pins and how to use citations

| Label | Source | Exact pin |
|---|---|---|
| **M** | `origin/main`, recommended implementation base | `dad2a2a50e3b274643612fbf8d3d272a92c02367` |
| **F** | `origin/feature/aca-sandboxes` | `88f553ed6a67e399a8fb660cf7aaef15905f0590` |
| **D** | `larohra/durable-loop-leadership-demo` | `d3ebac5afadeb8cb6d59f54a53177a39de4275f5` |
| F/D ancestor | F/D merge base | `6ebe06726dfc870f16fcb799529689d62750a9a1` |
| Main/donor ancestor | M/F and M/D merge base | `cc5e14ccda96bfd8ebc3197d33a5730b6cc89762` |

All three heads were checked against origin on 2026-09-14; F and D have not
changed since the September 11 inventory. The actual branch name is
`feature/aca-sandboxes`, plural. The creator branch named
`larohra/durable-agent-loop-spike` is older hybrid work; it is not the latest
Durable source.

Unless a path starts with `docs`, `tests`, `eng`, `samples`, or `.github`, source
citations are relative to `src\azure_functions_agents`. `M:path:lines`,
`F:path:lines` and `D:path:lines` refer to immutable Git objects, not the current
analysis checkout.
Grouped module references inherit the first path's source pin and directory.
The checkout remains at `784aadb`; no source was changed.

Detailed file/symbol manifests accompany this guide:

- [ACA reuse inventory](aca-reuse-inventory.json)
- [Durable reuse inventory](durable-reuse-inventory.json)
- [API/authoring inventory](api-reuse-inventory.json)
- [Release/infrastructure inventory](release-reuse-inventory.json)

Those inventories describe F/D donor contents as of September 11, not the
September 14 `main` dependency baseline. The integration-base and SDK guidance
in this revised guide supersedes their earlier F-based/1.x recommendations.

Categories apply to responsibilities, sometimes different responsibilities in
one file. **Reuse** means keep a proven implementation; **adapt** means retain
it with named changes; **replace** means replace the control model; **retire**
means remove it from the new path, not necessarily delete a supported legacy
feature; **new** means no integrated production implementation was found.

## 2. What to reuse from the ACA feature branch

**Keep main's existing implementation wherever it already provides the
capability.** Do not replace main's loader, auth, runner, endpoint, workflow,
history or CI files with donor versions. Port only missing neutral helpers and
tool-specific mechanisms. "Reuse from F" below identifies source material, not
permission to import an entire directory or its original commits.

| Piece | Disposition | What carries forward / what changes |
|---|---|---|
| Markdown/YAML loading, merge precedence, paths, slugs and validation conventions | **Reuse** | Keep the authoring pipeline and fail-closed capability validation. Add a versioned durable execution selector; do not reinterpret whole-agent ACA settings silently. |
| Principal resolution and canonical namespace | **Reuse/extract** | Keep Easy Auth enforcement, stable tenant/object identities, app/slot/agent scoping, app-owned function-key semantics and full-entropy label encoding. Remove Table-shaped types from their public boundary. |
| ACA adapter and transport ports | **Adapt** | Keep authored region selection, stable-create correlation, exact label matching, manifest verification, file/process operations, error translation and lifecycle update behavior. Add only the tool-executor operations needed by D. |
| Content capture, package cache and delivery | **Adapt** | Preserve deterministic digests, secure capture/exclusions, single-flight construction and integrity-checked upload. Produce a tool bundle instead of shipping a whole reasoning harness unnecessarily. |
| Bootstrap and filesystem safety | **Adapt** | Keep digest/ABI checks, safe staging, import protections and atomic file commit primitives. Run the narrow tool executor, not the guest `run_agent_events` entrypoint. |
| Egress compiler and guest identity separation | **Adapt** | Keep the enforcement mechanism. Compile actual tool destinations; move model-only access to the Functions identity. An unspecified web allowlist can mean `*`, so "default deny" alone is not proof of least privilege. |
| Watchdog, process inspection and journal integrity concepts | **Adapt selectively** | Reuse verified-death, first-result and corruption checks for tools. A local journal is not the authoritative conversation or a distributed exactly-once transaction. |
| HTTP response helpers, budgets and output validation | **Adapt** | Preserve owner checks, validators, response headers, wait budgets, errors and SSE preflight concepts. Replace their dependency on Table operations with a durable run facade. |
| In-language execution and Dynamic Workflows | **Keep separate** | Do not remove these existing products as a side effect of replacing the ACA controller. Durable use is an explicit execution selection, not a global fallback. |
| Regression scenarios and provider doubles | **Reuse as requirements** | Preserve same-key replay, stale-worker fencing, label collision, off-page provider lookup, cancellation, result hold, corruption and bounded cleanup scenarios. Replace ETag-specific assertions, not their safety objectives. |
| CI jobs, fixture assembly and deployed qualification | **Adapt as a first-class workstream** | Preserve the Linux version matrix, real SDK contract check, current-checkout smoke, built-artifact deployment, provenance checks, bounded load and pipeline tests. Replace Table-bound fixtures/evidence with Entity/DTS equivalents; see Section 9's CI/CD manifest. |

Key evidence:

- F:`session_state\identity.py:56-262`,
  `session_state\_label_encoding.py:29-44`,
  `registration\_auth.py:168-271`.
- F:`transport\aca_sdk.py:268-470,727-802,1002-1112,1241-1273`;
  `transport\ports.py:24-87`.
- F:`controller\package.py:258-447,630-1149`;
  `harness\bootstrap.py:117-493`;
  `harness\atomic_commit.py:55-99`.
- F:`egress\policy.py:26-99`;
  `harness\__main__.py:252-323`;
  `config\loader.py:31-83,226-272`.

### What must never enter the main-based feature

| Existing implementation | New destination |
|---|---|
| `session_state\store.py` EGT/ETag operations | Entity operations, orchestration checkpoints, and narrow external-effect receipts |
| `session_state\connection.py` Table connection/fingerprint | DTS binding plus protected Blob binding; do not reuse `s1` Table-account fingerprint as task-hub identity |
| Table operation leases, tokens and forward phase rows | Durable run/lifecycle control flow; retain application epochs where they fence external effects |
| `controller\readiness.py` Table-backed provision/submit state machine | Durable provider/bootstrap activities and explicit readiness transitions |
| `controller\reconciler.py` Table scans and operation takeover | Session lifecycle supervision, durable pending-work records and provider inventory cleanup |
| `execution\aca_sandbox.py` whole-turn execution backend | New durable backend/public-run facade |
| Full-agent `harness\__main__.py` and guest delegation path | Narrow ACA tool executor; model loop remains in Functions |
| Old sandbox journal as conversation/status authority | Owner-scoped committed context and immutable terminal/result/event records |

F:`session_state\store.py:311-606`,
`controller\readiness.py:263-524,1394-2263`,
`controller\reconciler.py:283-493`,
`execution\aca_sandbox.py:461-709`.

**Do not import `session_state` as a directory.** Its neutral owner/label/status
contracts are mixed with row keys, records, leases and eager facades. Fourteen
direct consumers plus facade/manifest dependencies were identified in F. Even
F's in-language backend imports its terminal states, but main has no such
dependency. Extract only needed neutral definitions into appropriately named
modules; do not create donor compatibility imports in main just to make a bulk
copy work.

## 3. What to reuse from the Durable spike

All paths in this table are under `experimental\` at D.

| Module(s) | Disposition | Reusable implementation and required adaptation |
|---|---|---|
| `durable_loop_protocol.py` | **Adapt** | Strict refs-only envelopes, frozen identity/tool descriptors, error dispositions and ordered tool calls. Add production schema migration, byte limits and lifetime definitions. |
| `durable_loop_registration.py` Entity reducers | **Adapt** | Admission replay/busy/conflict, cancel, generation-fenced commit, commit receipts and human-input CAS. Harden namespace, bounded retention, old-receipt replay and cleanup/readiness coupling. |
| `durable_loop_registration.py` orchestrators | **Adapt/split** | Keep deterministic model/tool sequencing, timers, bounded parallel reads, serial writes and continue-as-new. Separate Entity reducers, orchestrators and activity registration into maintainable modules. |
| `durable_loop_activities.py` | **Adapt** | One-step MAF Agent with automatic tool invocation disabled, explicit replayed messages, audit/working-context split, compaction and Blob content. Qualify model fidelity and add replay-safe foreground decision receipts. |
| `durable_loop_receipts.py` | **Reuse/extract** | Conditional create/replace/delete, receipt integrity and explicit effect lifecycle. Move demo fault injection to test-only support. This remains custom Blob CAS, but not a Table phase machine on every transition. |
| `durable_loop_catalog.py`, `durable_loop_tools.py` | **Adapt** | Frozen schemas, placement/provenance, risk classes and dispatch. Integrate with normal authoring and declaration-only discovery instead of a second private config system. |
| `durable_loop_execution.py`, `durable_loop_mcp.py` | **Adapt** | Worker/MCP/ACA dispatch, keyed receipts and ambiguity handling. Do not require APIM/MCP/ACA resources when the selected capability does not use them. |
| `durable_loop_sandbox.py` | **Adapt substantially** | Per-call/retained execution, inventory attach/recreate, snapshots and receipts. Move binding authority into the session Entity and fix cleanup, capacity, namespace and same-batch workspace continuity. |
| `durable_loop_apim.py` | **Adapt/optional** | Provider request/response boundary and background operation machinery. Ship a qualified foreground model path first; retain background behind an explicit capability gate. |
| `durable_loop_http.py` | **Adapt, not public-copy** | Owner checks, run polling, result refs, cancellation and human endpoints. Rebuild admission ordering and map to the chosen public API contracts. |
| `durable_loop_observability.py` | **Reuse/adapt** | Content-free event/metric conventions; integrate Functions traces, replay suppression, stage latency and runtime version tagging. |
| `durable_loop_reaper.py` | **Adapt** | Provider inventory cleanup is real reusable code. Creation-age deletion alone is not production session expiry or snapshot-safe cleanup. |
| `durable_loop_config.py` | **Replace private composition** | Replace environment gate, main-agent-only restrictions and private manifests with validated public authoring/runtime capability selection. |
| `durable_loop.py`, `durable_loop_state.py` | **Split/retire simulator** | Extract shared plan/identity/tool-request helpers; keep useful local doubles in test support. Do not ship two independently evolving state machines as production authorities. |

Evidence: D:`experimental\durable_loop_protocol.py:229-311,482-569,675-734,1044-1202`;
`durable_loop_registration.py:279-426,1546-2266,2360-2695`;
`durable_loop_activities.py:203-377,627-825,922-1114`;
`durable_loop_receipts.py:27-287`;
`durable_loop_execution.py:51-237`;
`durable_loop_sandbox.py:265-845`.

### Required predecessor code: do not copy only `durable_loop*.py`

D still imports:

- `hybrid_apim.py`, `hybrid_config.py`, `hybrid_observability.py`;
- `hybrid_protocol.py`, `hybrid_tools.py`;
- **`hybrid_executor.py`**, whose actual source is uploaded into the sandbox;
- `hybrid_reaper.hybrid_app_hash`, which reaches neutral session identity code.

Extract shared provider, tool-package, executor and identity utilities. Retire
the separate hybrid orchestration/demo path only after those imports are
removed. Retained executor restart-after-resume fixes are part of this
dependency chain.

D:`experimental\durable_loop_sandbox.py:60-67`,
`durable_loop_apim.py:58`,
`hybrid_tools.py:26-87,1001-1038`,
`hybrid_reaper.py:9-14,73-86`.

## 4. What is genuinely new production work

The new work is predominantly **integration and lifecycle authority**, not a new
transport SDK, agent framework or scheduler implementation.

| Work package | Existing seed | New contract required |
|---|---|---|
| **Durable admission coordinator** | Entity `admit`, HTTP lost-start recovery, Blob CAS | Immutable initial input/deadlines, owner-key rendezvous, orchestration-first Entity admission, uncertain-start repair, TTL/key-incarnation and no-key semantics |
| **Session lifecycle supervisor** | Durable timers, ACA reaper, F lifecycle invariants | Survives between runs; touch/expiry/result-hold; active-orphan recovery; backing-loss policy; cleanup fencing; terminal retirement |
| **Unified binding authority** | D retained Blob document + F verified manifest | Entity-owned binding reference/generation/call ownership; every attach/result/cleanup checked against it; no competing retained-Blob authority |
| **Durable outcome/event repository** | Blob content refs and Entity commit receipt | Owner-scoped status independent of retained DTS history; immutable terminal receipts; result eviction; history reads; replayable event cursor if promised |
| **Cleanup and retention ledger** | Blob CAS, inventory reapers, F snapshot rules | Listable obligations for create/delete/snapshots; capacity debt; purge ordering; partial-failure repair; no resurrection after receipt/entity deletion |
| **Public product integration** | Existing loader/auth/routes and private durable HTTP | Supported authoring and execution selector, multi-agent registration, DFApp wiring, trigger policy, wait/poll/SSE/history semantics and output validation |
| **Version/upgrade protocol** | Versioned Durable names and protocol models | Compatibility for active runs, human waits and idle committed sessions; dependency/tool/plan snapshots; backend routing and schema migration |
| **Release qualification** | F live CI + D unit/qualification tooling | Actual DTS privacy collection, cross-worker latency/recovery, exact artifact provenance, private-network deployment, upgrade/cutover drills |

These packages can use the existing Entity and Blob primitives. Do not rebuild
the old general `SessionStateStore` protocol in Blob or add a global owner
Entity that serializes every session. The persistence ledgers should be small,
scoped to a request/effect/cleanup obligation, and off the steady-state
per-transition coordination path where possible.

**Lease-management exclusion:** do not import D's Blob capacity allocator or
independently mutable retained-binding coordinator unchanged either. Put
session/run/binding coordination in Entities. Decide explicitly whether provider
limits are sufficient or stricter application capacity accounting is needed;
do not introduce a generic lease allocator as an extraction convenience.
Conditional effect receipts and immutable workspace checkpoints can remain:
they address activity redelivery, not a second session-lock manager.
This is a semantic boundary, not a ban on an SDK's internal locking or a
resource-handle class merely named "lease."

### Admission: candidate design, not a proven algorithm

A release FRD should finalize this ordering:

1. Authenticate and derive canonical app/agent/owner scope; validate input.
2. Persist immutable request/plan content under that scope. Durable history gets
   a bounded non-authorizing reference, never raw content.
3. Conditional-create a narrow request rendezvous receipt freezing the request
   hash, candidate session/run IDs, incarnation, schema/code versions, request
   ref and deadlines. Same-key retries read the winner instead of rebuilding it.
4. Start the deterministic admission/run orchestration using that frozen ref.
   HTTP does **not** directly reserve the active Entity slot first.
5. The orchestration establishes its lifecycle-supervisor obligation and calls
   the Entity to admit; only then may it schedule external work.
6. A lost start acknowledgement remains explicitly uncertain. Retry the same
   identity. A listable pending-start obligation repairs a claimed request if
   the client disappears before scheduler acceptance.

An orchestration start acknowledgement is not an admission decision. A
compatibility facade must observe accepted/replay/busy/conflict within its
bounded admission wait to preserve F's response codes; otherwise a distinct
queued/uncertain response needs explicit API approval. Do not report admission
success just because `start_new` returned an instance ID.

This avoids an admitted Entity with no durable controller. It still contains
Blob-to-DTS and lifecycle-start crash windows; the repair obligation and
cut-point experiments are required, not optional implementation details.

**Deterministic `hash(owner,key)` alone is insufficient.** F's idempotency has a
bounded replay horizon while logical sessions can outlive it. Reusing a key
must not resurrect an old Entity identity or let old messages mutate a new
incarnation. Preserve an incarnation/tombstone contract in the narrow claim
(Blob CAS or request Entity). Do not time-bucket keys without specifying retry
behavior at bucket boundaries.

### Namespaces and generations

Keep these distinct in types and persisted envelopes:

| Concept | Meaning |
|---|---|
| Canonical authority namespace | App/deployment identity, slot where applicable, agent, principal and session; task hub is an additional boundary |
| Session/key incarnation | Distinguishes reuse after receipt expiry/destruction; rejects messages from retired lifetimes |
| Committed conversation generation | Advances on successful conversation commit, not on compute replacement |
| Sandbox backing generation | Identifies concrete compute/filesystem ownership; suspend/resume is not automatically a new generation |
| Durable history epoch | Advances on continue-as-new; human event correlation must use the authoritative epoch |
| Workspace snapshot sequence | Orders filesystem checkpoints; must not be confused with any of the above |
| Code/plan/schema version | Pins compatible model/tool/protocol behavior across deployment |

D uses separate fields for several of these already, but not consistently at all
boundaries. A compute recreate must not reset conversation dedupe. An
orchestration history rollover must not generate stale human-request event IDs.

## 5. Production blockers already visible in the spike

These are source-based findings to reproduce before fixing, not claims that
the current deployment hit every path.

| Finding | Why it matters | Required treatment |
|---|---|---|
| Entity key omits app/agent; retained records are keyed by session only | Cross-namespace collision or attach with the wrong authorization context | Reuse F canonicalization throughout Entities, claims, content paths, retained binding and manifests |
| HTTP admits before persisting/start; retry rebuilds metadata | Orphaned active slot, changed deadlines/config on a retry | Frozen request receipt and orchestration-first admission |
| Cancel before `mark_running` can return fence-lost without aborting | A canceled run can retain the active slot | Compose real reducer + orchestrator regression; explicit terminal cleanup path |
| Real cleanup failure returns `cleaned:false`, but callers can release ownership | Completed conversation is not proof of reusable workspace | Separate result completion, session readiness and cleanup/capacity debt; never free a shared mutable binding on unverified cleanup |
| Retained expiry/capacity/reaper use different clocks | Active backing can be reclaimed or capacity miscounted | One authoritative expiry/fence; reconcile platform state and debt |
| Activity timeout can be labeled certain after mutation may have started | Unsafe retry or false failure certainty | Track effect-start/receipt provenance and return `Ambiguous` where outcome is unknown |
| Foreground inference has no cross-activity decision receipt; retries are not consistently classified | Duplicate inference costs and avoidable failed drivers | Freeze the winning decision before tool dispatch; distinguish retryable failures from unknown external effects; do not promise exactly-once model billing |
| All same-batch per-call tools receive the pre-batch workspace ref | Second write/read can miss first tool's changes | Sequentially advance the workspace ref for mutations; test read-after-write and loss between calls |
| Human acceptance is durable but outbox startup is not durably linked | Accepted input may never wake a waiting run | Orchestrator-owned delivery obligation and crash repair |
| Reservation without persisted answer can win human timeout/cancel | Incomplete submission can block completion | Distinguish reserved, accepted and delivered facts; finalize race semantics |
| GET status/result depends on DTS retained input | Purge can remove auth/result lookup while public retention is promised | Independent immutable owner-scoped terminal receipt |
| Entity timeout receipts/pruning and overall byte size are not fully bounded | Long-lived sessions eventually hit capacity or lose replay safety | Typed schema, aggregate byte guards, bounded terminal-only pruning and explicit deletion epochs |
| Continue-as-new can leave the external checkpoint epoch stale; background poll count is reused as bounded attempt | Incorrect human correlation or validation failure on long polls | Synchronize authoritative epochs and separate attempt/poll indices; qualify long waits and polls beyond 16/64 if background is supported |
| Simulator tests and hard-coded entity successes don't compose the actual reducers | Green local tests can miss real orchestration/Entity interactions | Integration tests with the real reducer and host, then DTS cut-point qualification |

Evidence:

- D:`experimental\durable_loop_http.py:113-248,862-868,957-983,1091-1118`.
- D:`experimental\durable_loop_registration.py:307-347,563-602,876-918,1556-1580,1785-1801,2147-2152,2252-2266,2624-2695`.
- D:`experimental\durable_loop_sandbox.py:120-166,213-249,449-751,992-1018`;
  `durable_loop_reaper.py:39-95`;
  `hybrid_tools.py:1386-1446`.
- D:`experimental\durable_loop_apim.py:214-427,869-879,1040-1052,1203-1211`;
  `durable_loop_registration.py:1134-1143,1507-1543`;
  `durable_loop_protocol.py:428-479`.
- D:`experimental\durable_loop.py:1394-1457`;
  `tests\test_durable_loop_durable_execution.py:125-151`.

### Foreground inference should be the initial qualified path

A sanitized diagnostic from the related demo session found:

- direct Foundry and APIM **foreground**, `store:false`: encrypted reasoning
  present;
- tested **background retrieval**, `store:false`: encrypted reasoning absent
  before MAF normalization;
- direct retrieval with the tested `include[]` shape: HTTP 400.

This is existing reported live evidence for one deployment/model contract at
`d205c4206b6ac539979aef518c4fb249818fd285`, not a new experiment here and not a
claim about every model. The diagnostic is preserved at:

`C:\Users\larohra\.copilot\session-state\26545db2-8bd8-4a0c-a578-10af56815eed\files\durable-loop-reasoning-diagnostic\FINAL-DIAGNOSTIC.md`

Recommend foreground model activities with explicit customer-owned replay and
automatic MAF tool execution disabled for the first support matrix. The overall
run API remains asynchronous and Durable. Foreground keeps an activity/HTTP
connection open, delays observation of cancellation until return/timeout, and
can repeat inference after acknowledgement loss; qualify these costs.

Background support should remain gated until exact reasoning/call/result replay,
provider retention, affinity and cancellation are demonstrated. Do not
silently switch to `store:true` or `previous_response_id`; that changes custody
and recovery semantics.

Related open issues as of 2026-09-11:
`microsoft/agent-framework#7538` and `Azure/azure-sdk-for-python#46092`.
They concern related tool-history/continuation failures, **not independent proof
of the exact missing-encrypted-field diagnostic**.

## 6. Compatibility: keep, adapt, or deliberately change

**The mandatory compatibility baseline is current main plus the approved new
feature contract, not every behavior of unreleased FRD 0008.** Preserve F's
security/correctness invariants where applicable; do not recreate Table phase
machinery, legacy response shapes or whole-agent placement solely because a
donor test asserts them. If an existing feature deployment has promised those
contracts, use the explicit compatibility/drain plan in Section 11.

| Contract | Production treatment |
|---|---|
| Markdown configuration, per-agent grants, slugs | Keep conventions; add supported durable selection and validate unsupported combinations before registration |
| `session_runtime.aca_sandbox` | Do not silently change its isolation meaning. Introduce an explicit execution-model version/selector or approve a breaking pre-release migration |
| HTTP ownership | Keep F enforcement; missing old `SessionRuntimeBinding` must not skip owner resolution or fall back to legacy execution |
| Chat start/status/result/cancel URLs | Can be preserved through a new facade; preserving paths does not preserve semantics automatically |
| F idempotency conflict `422`, busy links and setup `504` | Translate intentionally or document a versioned API break; D's private `409 session_busy/idempotency_conflict` is different |
| Phase and readiness | Preserve `provisioning/executing/settling/terminal` if maintaining F compatibility; final response alone is not permission to reuse a mutable sandbox |
| Wait/synchronous chat and `AgentResult` | Implement a bounded waiting facade or expose a distinct async API; don't silently replace existing return values with tickets |
| SSE/reconnect | New event repository if F-compatible streaming is required. Progress polling is a valid smaller release scope only with explicit versioned behavior; it is not token streaming |
| History | Preserve main's agent-scoped history paths; add owner-scoped durable refs/terminal records. F/D's older session-only addressing must not overwrite main's fix |
| Results after backing loss | Decide whether Blob-restored context/workspace continues or F's tombstone/410 remains. Restoration is a deliberate behavioral change, not mere parity |
| General triggers | Each requires durable client injection, message-ID dedupe and acknowledgement/retry policy. Do not leave an unmodified trigger calling the legacy runner |
| Inbound MCP | Distinct from outbound MCP. Define run-ticket/wait semantics and its system-key auth boundary, or explicitly defer |
| Chat subagents/workflow subagents | Existing delegates run a full MAF leaf. Durable specialist execution is a separate design; don't imply inner-loop durability by reusing the wrapper |
| Dynamic Workflows | Preserve as a separate engine. Same-app durable coexistence/provider selection requires qualification; the spike rejects it |
| Skills/custom tools/providers | Allow only explicitly qualified placement/replay support. Current executable tool discovery imports customer modules in the host; move executable inspection to an approved build/guest boundary |
| Built-in UI and client | Wire actual public run/history/input endpoints; preserve caller request IDs before send. The private polling demo and PowerShell mock do not establish compatibility |

M:`_blob_history.py:104-139,230-232` and #213 establish the current agent-scoped
baseline. F/D citations below describe donor contracts, not a claim that main
still has the same behavior.

F:`config\schema.py:263-349`,
`config\validation.py:288-436`,
`registration\endpoints.py:96-119,189-199,958-1037`,
`discovery\tools.py:115-174`,
`_blob_history.py:14-28,190-225`,
`runner.py:469-531,627-704`.

D:`app.py:658-731,833-950`,
`registration\endpoints.py:428-433,663-671,762-771`;
F:`docs\aca-sandbox-session-runtime.md:151-165,188-273`;
D:`docs\frds\0010-durable-agent-loop-spike.md:813-831,1180-1301`.

### Recommended initial support scope, pending human sign-off

Keep the first product scope narrow enough to prove:

- Linux Functions 4, DTS, one qualified Python/MAF/Durable SDK matrix, multiple
  independently scoped HTTP agents, foreground inference.
- Async run API plus polling/history/cancel; human input only after its
  acceptance/delivery races are closed.
- Explicitly approved worker/MCP tools and ACA executable tools; no automatic
  MAF tool invocation.
- Choose and qualify the workspace contract: per-call Blob-backed continuity,
  or retained compute as a cache with all liveness/fencing fixes.

Retained compute is not inherently forbidden, but the current retained mode
needs more lifecycle work. A full replacement promising F's streaming and
session semantics must include those adapters before release; a smaller
versioned feature can defer them openly.

## 7. Integration order and parallel work packages

**Use a new worktree/branch from the then-approved `origin/main` tip.** Do not
rebase this analysis worktree or either donor branch. This guide does not create
an implementation worktree or authorize product changes. Follow the feature
FRD, architecture sign-off, implementation, testing review and docs gates.

### Why main is the correct base for this constraint

| Candidate base | Confirmed consequences | Assessment |
|---|---|---|
| F | Contains the Table store and lease/controller packages; 25 commits not in main, while main has 84 not in F | Useful donor; wrong starting tree when old authority must never enter the feature |
| D | Inherits the ACA feature lineage and old Table packages, plus private/demo integration; 103 commits not in main, while main has 84 not in D | Useful implementation reference; not a clean standalone Entity branch |
| M | No `session_state`, `controller`, `transport`, `harness`, `execution` or `experimental` package at the inspected paths; no app Table dependency or searched Table/lease identifiers | Recommended additive starting tree |

Counts are Git reachability counts including merges, not unique patches or an
effort estimate. F/D also differ by 5/83 commits. None is a simple stack on
today's main.

Main already contains:

- Durable Python **2.0.0b2**, with corresponding runtime changes (#189);
- workflow retry and display-name improvements (#193, #201, #207);
- agent-scoped history paths (#213);
- Core Tools **4.14.0**, independently of F's later #214.

Therefore do not copy F/D's `pyproject.toml`, `uv.lock`, `app.py`, workflows,
history, registration or CI wholesale. Reconcile the MAF upgrade intentionally
and port the Entity loop against main's Durable 2.x contract. Qualifying the
actual Entity path on that SDK is an early gate, not assumed from workflow
tests.

### Selective port versus literal cherry-picks

Git can cherry-pick one commit without all its ancestors; that does **not**
make the commit's code independent of those ancestors. Check both its patch
footprint and import/runtime dependencies.

| Donor landmark | Observed contents | Safe treatment |
|---|---|---|
| `34879ad` (#131), identity/schema | Auth plus identity and mixed session row/schema models | Extract neutral canonical identity and its tests; exclude row keys, lease models and eager facade |
| `0b89cd8` (#132), transport | Adapter/ports/manifests plus dependencies, locks, process/docs changes | Port selected code from final F, including later fixes; adapt neutral types and add only the ACA SDK dependency |
| `89bd2b4` (#135), packaging | Package/manifest code plus controller placement, CI and repository instructions | Extract safe capture/bootstrap mechanisms from final F; do not import Table-bound manifest builders or old process files |
| `fcf56d2` (#150), guest harness | Bootstrap/egress mixed with whole-agent guest, readiness, app and runner changes | Extract only tool guest safety code; no full commit |
| `e596f09`, Durable foundation | 37 files; includes app/endpoints, old ACA composition, hybrid prerequisite, manifest/lock and simulator | Select final-D protocol/reducer/orchestration/MAF pieces; rebuild main-native integration, without donor lock or simulator authority |
| `68b9ee4`, execution planes | 36 files; includes hybrid executor/tools, retained sandbox coordination and app wiring | Extract selected tool/receipt/guest code with explicit dependency closure; no generic capacity/retained lease coordinator |
| `67eb34a`, DTS demo | 21 files; DTS IaC mixed with demo recording/media, sample settings and runtime patches | Use main's existing DTS host example; extract missing resource/identity snippets and relevant fixes only |
| `8f948db` (#133), Table store; `343e3e0` (#136)/`064feef` (#144), Table control integration | The authority being replaced | **Do not import** |

No listed large landmark is endorsed as an as-is cherry-pick. Narrow later
fixes may qualify after their target has been ported and reviewed. Prefer final
donor implementations so later fixes are not lost; keep original commit IDs
and source ranges in port-commit provenance. That is reuse without inheriting
unrelated ancestry or retaining a historical broken intermediate revision.

**Do not cherry-pick a broad commit, commit the baggage, and delete it in a
later PR.** Construct each new commit from approved files/hunks, adapt it to
main, and review the staged diff/import graph before committing. Never replace
the whole `experimental`, `controller`, `session_state` or guest directory to
satisfy a missing import.

| Slice | Work | Depends on | Exit evidence |
|---|---|---|---|
| **0. Release FRD and main baseline** | Freeze M; decide placement/API/custody/workspace/ingress; record no-Table/no-custom-lease constraints; translate relevant F safety scenarios | None | Human-approved additive scope and main compatibility baseline |
| **1. Neutral foundations** | Extract minimal owner/label/error contracts; add selected transport/package/bootstrap code without Table-shaped types | 0 | Main behavior preserved; no donor session controller or Table import introduced |
| **2. Main-native Durable kernel** | Port final-D protocol/reducers/orchestrators/activities and required hybrid utilities onto main's Durable 2.x; deliberately reconcile MAF | 0, agreed slice-1 interfaces | Real Entity/host vertical slice and one-step MAF fidelity; no dependency downgrade, private demo or simulator authority |
| **3. Admission and lifecycle authority** | Request rendezvous/incarnation, durable start repair, Entity-owned binding, lifecycle supervision and bounded cleanup/terminal records | 1,2 | All cut points recover or fail explicitly; no orphan active slot or generic custom lease coordinator |
| **4. Public integration** | Authoring selector, multi-agent DFApp registration, authenticated run/history/input facade, supported triggers and UI/client adaptation | 1,2, final contracts from 3 | No legacy fallback; approved HTTP/stream/poll semantics and capability validation |
| **5. CI/CD and production qualification** | Port F's portable/SDK checks, smoke and deployed-qualification job structure, fixture/assembly/provenance helpers and pipeline tests onto main; add real DTS/Entity evidence and selected IaC | Starts with 0/1; runtime lanes follow 2/3/4 | Existing main coverage retained; Table-free CI installs/fixtures; versioned artifact attribution; bounded, authorized live lanes; explicit release evidence |
| **6. Correctness and performance qualification** | Compose API+Entity+activity+Blob; cross-worker load, ambiguity, upgrade, privacy, live bounded diagnostics and human acceptance | 3,4,5 | Section 10 gates and approved SLO/cost evidence |
| **7. Release, cutover and exclusion proof** | Publish only the additive implementation; separately drain old deployments if necessary; regenerate docs and prove excluded code never entered the branch | 6 | Main-to-feature diff has no Table/lease manager, no mixed authority or old-reaper scope overlap |

Slices 1, 2 and 5 can begin independently after shared boundary decisions.
Slice 4 can start against stable interfaces while slice 3 is completed. Keep
one owner for `app.py`, schema and public endpoint edits to avoid parallel
merge conflicts. This is a suggested work breakdown, not an instruction to
spawn sessions or create a PR stack now.

Slice 5 is not end-of-project cleanup. First port the portable gates and their
pipeline-contract tests alongside the primitives; then wire the main-native
Durable host lane and deployed qualification as those implementations arrive.
Each product slice carries its relevant donor regression scenarios and new
tests, rather than leaving coverage migration to the last PR.

## 8. Suggested production module boundaries

These names are **proposals**, not existing public APIs:

| Boundary | Responsibility / provenance |
|---|---|
| Pure identity/domain module | Extract F owner, label and status contracts |
| Durable protocol and Entity module | D DTOs/reducer, production schema/epoch/retention rules |
| Durable run and session-lifecycle modules | D run orchestration plus new supervisor and durable delivery/start obligations |
| Model activity/provider boundary | D one-step MAF and qualified model adapters |
| Tool dispatch and sandbox executor modules | D tool/MCP/sandbox adapters plus extracted hybrid guest executor |
| Content/receipt/event/cleanup repository | D Blob primitives, new scoped terminal/event/admission/cleanup schemas |
| Durable execution facade | Implements public run operations and waiting/projection independently of storage |
| Registration/config modules | Existing conventions, explicit durable capability selection, versioned function registration |
| ACA transport/package modules | Retained F primitives, without Table/session controller policy |

No network clients or customer tool imports belong in deterministic
orchestration code or pure Entity reducers. Activity I/O is expected to be
at least once; each external mutation requires a tested idempotency/reconciliation
strategy or an honest ambiguous result.

## 9. Deployment, dependencies and release posture

### Preserve main's runtime baseline; reconcile donor dependencies deliberately

| Component | Observed state | Migration action |
|---|---|---|
| `azure-functions-durable` | M pins **2.0.0b2**; F/D were 1.x with lock **1.6.0** | Already present. Port/qualify Entity execution on main's 2.x; do not import the donor's `<2` constraint or 1.x lock |
| `azure-functions` | M manifest `>=2.1.0,<3`; F/D lock **2.1.0** | Keep main's selected closure and qualify the actual worker/SDK pairing; don't infer installed versions from the manifest floor |
| MAF | M core **1.13.0**, OpenAI **1.10.2**, Foundry **1.10.3**; D **1.17.0/1.14.2/1.12.0** respectively | Deliberate dependency/one-step replay qualification slice; preserve main's non-durable behavior and regenerate its lock |
| D guest fixture constraints | Still MAF **1.3.0** | Do not reuse stale constraints; regenerate from the selected closure |
| Python/Core Tools | M supports Python `>=3.13` and has Core Tools **4.14.0**; D still **4.12.0** | Keep main's current CI/packaging behavior; no need to import F's mixed #214 merely for that Core Tools version |
| ACA SDK | **0.1.0b4**, preview | Keep real SDK-model contract tests and explicitly state preview dependency |
| Table SDK | Absent from M; bundled in F/D ACA extra | Never add the donor extra wholesale. Add only needed ACA dependencies, without `azure-data-tables` |
| DTS host extension | `azureManaged`, bundle floor **4.32.0** | Record resolved bundle/provider versions; a floating range is not release provenance |
| Durable SDK version changes beyond main | Separate from source extraction | Any move from main's exact 2.x beta to another version needs explicit compatibility qualification; current main already made the 1.x-to-2.x migration |

M:`pyproject.toml`, `app.py`, #189 and `eng\templates\install-core-tools.yml`;
F/D:`pyproject.toml`, `uv.lock`;
D:`eng\constraints\aca-fixture-py313.txt:3-68`,
`eng\constraints\aca-fixture-py314.txt:3-68`;
F:`eng\templates\install-core-tools.yml:1-13`.

The release analyst observed Python Durable **2.0.0rc1** on 2026-09-11; that
external metadata is time-sensitive and does not override main's actual
**2.0.0b2** pin. The earlier recommendation to ship 1.x first is superseded by
the main-based plan. Test the Entity path on the chosen 2.x version early; this
investigation verified source/dependency selection, not runtime compatibility.

Package/import cost is unmeasured: Durable already being installed on main does
not make the new registration/kernel or MAF upgrade free. Compare locked deployment size,
cold import time, host indexing time and resident memory on Linux 3.13/3.14;
record both extension initialization and first-request costs. No wheel-size or
startup-latency saving is claimed here.

### Required resources versus optional resources

- Required: Functions host/storage, DTS scheduler/task hub, protected content
  Blob storage, model endpoint/identity; ACA Group/controller/guest identities
  for the ACA-tool profile.
- Conditional: outbound MCP, APIM and Application Insights/Monitor. A Table
  query projection is not in this feature's scope. Do not require a new model
  resource if an approved one exists.
- **Keep `AzureWebJobsStorage`.** "No Tables" means no custom Table session
  authority or required application Table operations, not removal of Functions
  host storage or a prohibition on a storage account with a Table endpoint.
- Distinguish ACA Sandbox Groups from Dynamic Sessions `sessionPools`; the
  older sample provisioning a PythonLTS pool is not this infrastructure.

D's real IaC is reusable but has demo defaults: public endpoints, DTS
`0.0.0.0/0`, Storage shared keys, fixed resource assumptions and no coordinated
content retention. Parameterize and qualify these; do not call it a private
network deployment because the feature gate is private.

Model/remote credentials stay worker-side. Guest managed identity must not
access DTS, authoritative receipts or manage its Sandbox Group. Any guest model
permission inherited from the whole-agent runtime must be reconsidered for a
tool-only guest.

Use canonical hashes in Entity IDs, not raw principal data or secrets. Scope
content references and authorize dereference server-side; possession of a ref
must not grant access. The privileged DTS administration/history surface is a
separate access boundary from end-user run routes. Keep application users and
guest code out of it and qualify log/trace payloads independently.

DTS documents a 1 MiB Entity/payload ceiling; D targets much smaller 32 KiB
activity envelopes. Entity receipt counts alone do not bound serialized bytes.
DTS terminal-orchestration autopurge does not implement Entity, Blob, snapshot
or idle-session deletion. Define coordinated lifetimes for each plane.

### Runtime and failure-domain choices

One configured Durable provider/task hub per new app is the safest initial
deployment. Do not flip a populated app from the Storage provider to DTS and
assume histories migrate. Separate apps/hubs provide a clearer cutover and
legacy drain. **Flex has no deployment slots**; a slot-swap rollback is not a
valid plan for the current sample hosting model.

DTS Consumption is pay-per-action, up to 500 actions/second; Dedicated capacity
is fixed-price, with HA requiring three CUs. Published limits and SLAs do not
qualify this application. Obtain regional pricing and choose availability/cost
targets before release. Measure actions, Function occupancy, Blob traffic,
model tokens, ACA/snapshot/storage and telemetry, not only coordinator requests.

### Artifact/release pipeline

Extract F's useful qualification assembly/BUILD_INFO mechanisms without copying
its Table-specific pipeline; preserve main's CI fixes and add exact
commit, wheel, dependency-lock, installed-version, guest bundle, host/extension
and deployed-artifact identity. D's private assembler and client are useful
seeds, but its fixture locks and release defaults are not coherent as-is.

The inspected `.github\workflows\release.yml` in both F and current M rebuilds using Python 3.12 and
mutable tooling after publication. Promote the already-qualified artifact
instead. F's new ACA diagnostic stage is nonblocking; add an explicit release
gate for the new supported profile rather than interpreting diagnostics as
acceptance.

**Replace the privacy collector, not its whole matcher.** D's current collector
enumerates Azure Storage Durable artifacts and can pass an empty Entity-table
case. It is not a populated DTS Entity/history scan. Reuse its canaries and
aggregate-only output, but add supported DTS evidence collection and
fail-inconclusive behavior when attribution or coverage is missing.

M:`.github\workflows\release.yml`;
F:`eng\scripts\aca_qualification_pipeline.py:180-253`,
`.github\workflows\release.yml:12-29`,
`eng\ci\library-release.yml:163-197`;
D:`eng\scripts\durable_loop_spike.py:23-36,196-280`,
`eng\scripts\durable_loop_privacy_qualification.py:1022-1039,2018-2156`.

### CI/CD extraction manifest

**Include the ACA branch's CI investment, not its obsolete storage assumptions.**
The reviewed #197 commit is useful source material, but its nine-file patch
still references the old fixture and Table evidence. Port its behavior into
main; do not apply the full `eng` tree or donor job files over main.

| F asset | Keep | Adapt for the main-based Durable feature |
|---|---|---|
| `eng\templates\jobs\ci-tests.yml` | Linux Python 3.13/3.14, lint/types/tests/docs gates and real b4 policy-projection check | Extend main's template; install a Table-free ACA dependency profile, not F's `.[aca_sandbox]` extra |
| `tests\test_transport_aca_sdk.py` and provider doubles | Real SDK-model compatibility and transport/error/lifecycle regressions | Port tests with the extracted transport; don't let fakes alone hide SDK contract drift |
| `eng\templates\official\jobs\e2e-tests.yml` current-checkout smoke | Credentialed smoke separation, fork exclusion, timeout, preflight and `always()` cleanup intent | Keep main's existing E2E jobs and packaged dependency setup; test the tool-only guest and Durable loop rather than whole-agent guest/Table journals |
| `eng\scripts\aca_pr_smoke.py` | Fail-closed environment validation, redacted failures and guest identity checks | Validate the selected controller/guest/DTS split; old guest-model access is not a tool-only requirement or a complete least-privilege proof |
| `eng\ci\e2e-tests.yml` and `eng\templates\official\jobs\aca-qualify.yml` | Separate post-build deployed qualification, two Python targets, parameterized service connection, bounded run budgets | Add the new job to main's graph; use qualified Durable fixtures/hubs and approved resource settings; remove Table URI/name inputs and donor-specific service-connection defaults |
| `eng\scripts\aca_qualification_pipeline.py` | Build-artifact download/assembly, preflight, deployment and embedded BUILD_INFO comparison | Regenerate from main's selected dependencies; include wheel/lock/extension/guest digests and verify the deployed artifact, not just a version string |
| `eng\constraints\aca-fixture-requirements.txt` and constraint tests | Reproducible fixture closure and export validation | Generate a new closure for main's Durable 2.x plus qualified MAF/ACA; no stale donor pins or Table SDK |
| `eng\scripts\aca_deployed_qualification.py`, `tests\live\aca_deployed_*_support.py` and `test_aca_deployed_*.py` | Cold-start/turn/lifecycle/loss/load orchestration, fail-fast provenance, auth handling, bounded polling and diagnostics | Replace Table reads/row assertions with real Entity/DTS, Blob receipts and provider evidence; preserve safety outcomes rather than obsolete state transitions |
| `tests\live\apps\aca-qualification` | Dedicated reproducible fixture app, deterministic test tools and deployment exclusions | Create a Durable-native fixture with explicit auth, task hub, content storage and tool placement; do not copy its old session-runtime config/requirements |
| `eng\scripts\reap_aca_smoke_sandboxes.py` and smoke-support selectors | `always()` cleanup structure, label-based ownership and current-run correlation | Require run-scoped proof for every destructive family, including snapshots; don't copy the broader legacy production-family selector or old Table reaper |
| `tests\test_aca_qualification_pipeline.py` and related helper tests | Tests of the pipeline itself: assembly, provenance, dependency closure, preflight, rejection paths and job contracts | Add assertions for no Table dependencies/settings, correct Durable provider, scoped cleanup, job trust conditions and artifact identity |
| `eng\ci\docs\aca-qualification.md`, `tests\live\README.md`, `eng\scripts\README.md` | Operator prerequisites, evidence interpretation, diagnostics versus acceptance, resource ownership and troubleshooting | Document the new job graph/identities/limits; no old pipeline ID, personal service connection or Table prerequisites |

Main's Core Tools **4.14.0** and
`eng\scripts\install_e2e_dependencies.py` must remain. The donor E2E diff removes
that main dependency installer; importing the whole job would regress it.

The target job graph should distinguish these lanes:

| Lane | Run context | Required evidence |
|---|---|---|
| Portable PR/CI | No live Azure credentials/resources | Main regression suite, new Entity reducer/contract tests, SDK model projection, lint/types/docs, dependency/import exclusion and pipeline-contract tests |
| Local hosted integration | CI-local Functions/DTS emulator plus Blob/host-storage emulator as needed | Actual main-selected Durable 2.x registration, Entity/orchestration/activity interaction, worker replacement and failure cut points; no Table session fixture |
| Current-checkout ACA smoke | Explicitly trusted credentialed job, bounded scope | Exact-checkout tool bundle, provider/bootstrap/executor contract, guest/controller isolation and run-scoped cleanup |
| Deployed qualification | Built-artifact job, approved test apps/resources, Python 3.13/3.14 | Cold start, authenticated turn, lifecycle/loss, real DTS state, content privacy, artifact identity and bounded N=5 concurrency |
| Performance/reliability acceptance | Purpose-built, explicitly approved run | Matched contention/tail-latency and recovery evidence; real ACA N=100 remains human-owned, not ordinary CI |
| Release promotion | Approved release workflow | Required qualification evidence matches the exact wheel/config/runtime promoted; missing or skipped evidence is not a pass |

**Preserve these operational distinctions:**

- F's current-checkout smoke and deployed qualification are different evidence.
  Testing a locally captured guest bundle does not prove the deployed Functions
  wheel or DTS configuration is correct.
- F's deployed job uses `continueOnError: true` and runs for trusted manual
  invocation or main CI, not ordinary PR/scheduled builds. Retain a distinct
  diagnostic lane; making production qualification blocking is an explicit
  release-policy decision, not an accidental side effect of copying the YAML.
- No secrets/deployment identity for untrusted forks. Trusted manual feature
  branch qualification needs explicit queue/service-connection authorization;
  ordinary PR coverage remains credential-free.
- Preserve both Python targets and enforce per-job **and aggregate** resource
  budgets. F's two jobs each provision at concurrency one; that is aggregate
  concurrency two, not one. N=5 diagnostics are not throughput certification.
- Cleanup must run on failure where the runner permits and report incomplete
  cleanup as such. `always()` is not a guarantee after runner loss; retain
  run-scoped cleanup obligations for an authorized later sweep. The existing
  smoke reaper has a broader legacy production family as well as a per-run CI
  family, and the deployed qualification guide leaves post-run cleanup outside
  its stage. Neither is proof of complete end-to-end cleanup.
- Current F documentation and checks are reusable evidence, not permission to
  queue pipelines, deploy apps, change identities or run live tests now.

### CI-specific acceptance

The feature is not release-ready until the new CI jobs themselves are covered:
their run/trust conditions, dependency installs, runtime matrix, artifact
selection, redaction, failure/skip classification and cleanup namespace must be
asserted by tests. A run with no collected tests, unavailable DTS state,
incorrect artifact provenance or a skipped required lane cannot satisfy the
release gate.

## 10. Acceptance and latency gates

Tests below are a plan; none were run as part of this analysis.

| Gate | Evidence required |
|---|---|
| Additive/exclusion boundary | Existing main paths still work; no Table SDK/store, row/lease schema, controller facade or old reaper enters any feature commit |
| CI/CD migration | Portable, hosted, current-checkout and deployed lanes retain the relevant F coverage; no Table fixture/settings or stale dependencies; tests validate the job graph and evidence attribution |
| Durable 2.x vertical slice | Main-selected SDK hosts the real Entity reducer and orchestration/activity round trip, replay, cancellation and Blob refs; no 1.x downgrade to make donor code import |
| Exact Entity semantics | Real reducer composed with orchestrators: replay/conflict, cancel before/after admission, stale commits, old commit retries after newer turns, byte/receipt bounds |
| Admission cut points | Crash before/after immutable input, claim, scheduler start, Entity admit and supervisor start; client never retries in some trials; no permanent active-slot orphan |
| External effects | Effect-before-ack/result receipt, create-label reconciliation, timeout certainty, duplicated activity and safe capacity debt; one logical effect only where idempotency is actually supported |
| Workspace | Same-batch write/read, multiple turns, retained resume, lost compute, restored snapshot, concurrent read/mutation policy, stale backing generation |
| Lifecycle/retention | Touch versus expiry, terminal versus ready, result hold, snapshots after sandbox loss, status after DTS purge, Entity destruction, delayed messages/key reuse and cleanup failures |
| Human input | Answer-before-wait, reservation-without-body, accept-before-outbox-start, duplicates/conflicts, timeout/cancel, epoch rollover and retired run |
| Public contract | Auth isolation, routes, output validation, waiting deadlines, retry headers, history, 422/409/503/504/410, event replay if supported |
| Runtime | Real Functions host + DTS emulator + Blob/Azurite as supported; worker replacement while emulator stays alive; emulator restart is not persistence/HA evidence |
| Privacy | Scan populated DTS Entity/history/activity/custom-status surfaces plus telemetry and content placement; empty/unauthorized/truncated evidence is inconclusive |
| Upgrade | Active runs, human waits and idle committed sessions across code/schema/tool/MAF/bundle changes; no unapproved replay with new behavior |
| Azure acceptance | Approved nonproduction scope, cost/time limits, explicit cleanup ownership, regional/auth/private-network validation and human-owned scale acceptance |

Retain F's regression obligations, including the exact off-page sandbox read and
cleanup pagination. D's edits removed/changed some of those tests; that is not
evidence the underlying safety requirement disappeared.

### Concurrency benchmark required before latency claims

Run the same API-level workload against F and the candidate:

| Workload | Purpose |
|---|---|
| 1 warm/cold invocation | Baseline scheduler/extension overhead and uncontended latency |
| 10, 25, 100+ independent sessions, same app-owned principal | Detect owner-wide bottlenecks without confusing independent sessions with one-session serialization |
| Same load spread across principals/agents | Verify namespace separation and partition/hotspot attribution |
| 25 contenders for one session: duplicate and distinct requests | Deterministic single winner/replay/busy behavior; busy requests are not queued execution successes |
| Cross-worker replacement/slow provider/Blob latency | Tail behavior and bounded recovery under combined failures |

Use simulated ACA/model effects first, multiple processes and the real Durable
host/scheduler. Record admission p50/p95/p99, scheduler queue/dispatch, Entity
service time, receipt/content I/O, busy/replay/conflict latency, retries,
throttles, timeouts, commit and end-to-end time. Separate runs with a single
fast failed admission from successful completed work; don't improve p99 by
dropping or misclassifying requests.

Set numeric production latency/throughput/cost budgets **before** acceptance.
Require an improvement in the observed contention case without unacceptable
uncontended regressions. Do not substitute D's HTTP polling latency metric for
coordinator or end-to-end latency.

Use repository policy for real ACA: agent/CI diagnostics are **N=5**; **N=100**
real ACA acceptance is human-owned. Hundreds of simulated coordination trials
are different and do not authorize hundreds of billable ACA sandboxes.

### Existing tooling to retain

After implementation, use the repo gate in the implementation worktree:

```powershell
python -m ruff check src tests
python -m mypy src
python -m pytest --cache-clear --cov=.\src\azure_functions_agents --cov-report=xml --cov-branch tests
```

Port selected F/D test scenarios into the main-based feature's tests, leaving
the donor trees unchanged. Preserve main's current tests; do not import the
whole Table suite just to satisfy old internal transitions. Add coverage for
missing integrated behavior, not two fake state machines whose matching output
hides orchestration/Entity divergence. Invoke the PowerShell client mock suite
explicitly; pytest does not run it automatically.

## 11. Cutover and rollback: prefer the simplest truthful option

Because F is still in development, **a drained pre-release replacement is the
default recommendation unless there are already promised user sessions to
preserve**. This avoids building a live storage migration product unnecessarily.

### A. No externally promised session continuity

1. Stop admission to the old deployment; let runs finish or explicitly cancel.
2. Reconcile old sandboxes, snapshots, results and cleanup debt through the old
   authority. Preserve required evidence.
3. Deploy the new artifact with a fresh task hub and isolated provider cleanup
   scope; authorize new sessions only.
4. Retire old session IDs explicitly. Do not import random subsets of old Table
   rows into Entities or replay old completed effects.
5. Remove the old deployment/authority only after inventory and retention
   obligations are closed.

### B. Existing sessions must remain usable

Run separately pinned old/new deployments. Route existing sessions to their
original backend; send new sessions to Durable. A versioned ID/router or
separate endpoint can do this, but must preserve the original owner namespace
and not depend on a lagging optional projection. Keep the separately pinned old
deployment and its provider ownership until it drains; do not add the old
Table implementation to the new branch as a compatibility layer.

**Idle sessions matter too.** "No active runs" does not mean a session is
stateless: committed reasoning/context, snapshot refs, idempotency receipts
and tool/package versions may be needed for its next turn. Continuity across
the switch is a separate migration design; choose explicit retirement or
qualified schema/context conversion.

### Critical reaper hazard

F's reconciler selects sandboxes by legacy `app_hash` labels and treats a missing
Table row as an app-owned orphan. A new Entity-owned sandbox wearing those
labels can be deleted after the grace period.

Use a separate Sandbox Group/ownership scope or a positively versioned reaper
filter on **both** deployments before coexistence. Adding a label only to the
new sandbox does not change the old filter. Similar care is needed for
snapshot ownership after sandbox disappearance.

F:`controller\reconciler.py:298-306,2100-2142,2523-2555`.

### Rollback is not automatic failover

Stop creating Durable sessions and route new work to the prior deployment if
needed. Existing Durable sessions remain pinned to their authority. During DTS
outage return typed retryable failures; do not promote Blob receipts or an
optional Table projection into an alternative active coordinator. Pause
destructive cleanup unless a separately approved, fenced procedure proves
ownership and safe termination. Resume/drain through the same authority when
available.

Versioned old orchestrators/activities must remain available for histories still
using them. Do not roll back MAF/tools/model options underneath parked or idle
sessions without a compatibility check. Foreground/background switching must
not strand a stored background-operation reference.

## 12. Exclusion checklist: what the clean-main feature must prove

There should be no Table code to delete from the implementation branch: it was
never imported. Deployment drain is separate from Git history. Before release:

- New app registration does not create the Table binding, perform Table
  requests or register the old Table reconciler.
- New install/dependency closure does not add `azure-data-tables`; main's
  existing supported features remain unaffected.
- Canonical identity, enums, labels and manifests contain only needed neutral
  contracts; no donor facade pulls in Table row/lease/schema code indirectly.
- Main's public imports and integration are extended, not replaced with donor
  files. No compatibility shim imports the excluded ACA controller.
- Review each port commit and the merge-base diff for forbidden paths, imports,
  dependency extras, Table RBAC/config, EGT/ETag lease renewal/takeover and
  independently authoritative Blob retained/capacity coordinators.
- New tools do not import customer code during host discovery unexpectedly.
- Provider/guest work uses the qualified executor rather than full guest agent
  startup; optional retained mode obeys the Entity binding authority.
- Table-only env settings, query projections and application Table RBAC are
  absent from the new sample. Functions host storage remains correctly configured.
- CI dependency profiles, test support, deployed fixtures and cleanup helpers
  also exclude the old Table/lease code; exclusion is not limited to product
  modules. Main's existing E2E/package installation and CI gates are preserved.
- Active runs, idle sessions, legacy sandboxes/snapshots and result retention
  are drained, isolated, migrated with approval, or explicitly retired.
- Old cleanup cannot delete new resources; both positive and negative ownership
  cases are tested.
- Demo faults, forced 429 resources, fixed endpoints, simulator routes and
  duplicate reapers are excluded from production composition.
- Architecture, schema reference/spec, triggers, runtime guide, README, sample,
  client and operator runbook describe the actual supported model.

For schema changes, the future implementation must run the config-reference
generator and the `update-schema-docs` skill. Do not copy a finalized *spike* FRD
status as production sign-off.

## 13. Size, effort and remaining decisions

### Measured inventory, not a rewrite percentage

| Scoped inventory | Files / physical lines | Meaning |
|---|---|---|
| F ACA/control foundations | 54 / 24,695 | 11 reuse; 34 adapt; 5 replace; 4 retire. Mixed files retain useful primitives |
| F core replacement-class files | 5 / 11,347 | Donor store/readiness/reconciler/backend/journal control code to exclude; not a required rewrite or deletion in the main-based branch |
| D Durable modules | 16 / 14,444 | Existing implementation to adapt, including large mixed orchestration/activity modules |
| D hybrid predecessors | 7 / 4,001 | Required dependency extraction, not optional cosmetic demo code |
| D scoped tests | 28 / 14,517, 323 definitions | Includes runtime, demo and tooling tests; not collected cases or passing results |
| Public integration inventory | 41 responsibility entries | Some files have multiple dispositions; don't sum as unique-file counts |

These are donor inventories, not a proposed copy list or the size of a
main-based patch. They include blanks/comments and overlap in testing evidence.
Adding them together does not yield a total project size. Neither LOC nor an
unsupported "80% reusable" estimate establishes delivery time.

The highest uncertainty is lifecycle and public compatibility, not the basic
Entity reducer. Foreground-only models, HTTP-only ingress, versioned polling
and fresh-session cutover reduce scope materially. Transparent streaming,
all legacy triggers/subagents, retained workspace compatibility and online
session migration add separate release work.

### Human decisions needed for the implementation FRD

| Decision | Recommended analysis default |
|---|---|
| Integration base | Fresh branch from current main; F/D are donors, no whole-branch merge |
| Excluded machinery | No app Table dependency/projection, Table lease/controller code or generic replacement lease manager |
| Full Durable loop versus coordinator-only change | Full Durable loop, with explicit model/tool placement |
| Existing session continuity | Drain/retire pre-release sessions unless a real external continuity promise exists |
| First API scope | Explicit async HTTP + polling; full F-compatible SSE only if part of the release promise |
| Workspace promise | Choose Blob-backed logical continuity and separately qualify retained compute; no silent persistence downgrade |
| Background reasoning | Foreground first for the qualified contract; background gated |
| Provider/auth/network support | Narrow explicit matrix; guest cannot access coordination state |
| Trigger/subagent/workflow compatibility | Defer unsupported combinations or fund specific adapters; fail closed |
| Key TTL/reuse and session retirement | Explicit incarnation/replay horizon; no deterministic-ID-only shortcut |
| SKU/SLO/cost/DR | Agree measurable budgets and outage behavior before live acceptance |
| Library release posture | Distinguish runtime release from ACA preview dependency/support limitations |
| Credentialed CI and release gates | Preserve ACA qualification coverage and safety budgets; explicitly approve resource/identity scope and which qualified lanes block release |

No production design sign-off is claimed in this guide. The next implementation
artifact should be a new or explicitly superseding public FRD that records
these choices and the source/versioned upgrade contract.

## 14. Evidence limits and external references

Four independent read-only inventories were compiled against pinned F/D source.
On 2026-09-14, main/donor refs, ancestry, commit footprints, main package trees,
dependency manifest, auth/history, Durable migration and CI/sample settings
were inspected directly to revise the integration strategy.
No source edits, dependency installs, Azure changes, benchmarks, or release
qualification were performed. The prior latency prediction is not treated as a
measurement. Source findings need regression reproduction; prototype capabilities
are not automatically production guarantees.

Official references checked by the release analyst on 2026-09-11:

- [DTS configuration](https://learn.microsoft.com/en-us/azure/durable-task/scheduler/quickstart-durable-task-scheduler)
- [DTS overview, payload limits and emulator](https://learn.microsoft.com/en-us/azure/durable-task/scheduler/durable-task-scheduler)
- [DTS identity/RBAC](https://learn.microsoft.com/en-us/azure/durable-task/scheduler/durable-task-scheduler-identity)
- [DTS billing/capacity](https://learn.microsoft.com/en-us/azure/durable-task/scheduler/durable-task-scheduler-billing)
- [DTS orchestration autopurge](https://learn.microsoft.com/en-us/azure/durable-task/scheduler/durable-task-scheduler-auto-purge)
- [Durable storage providers and migration](https://learn.microsoft.com/en-us/azure/durable-task/common/durable-task-storage-providers)
- [Functions storage requirements](https://learn.microsoft.com/en-us/azure/azure-functions/storage-considerations)
- [Functions deployment slots](https://learn.microsoft.com/en-us/azure/azure-functions/functions-deployment-slots)
- [ACA Sandbox overview](https://learn.microsoft.com/en-us/azure/container-apps/sandboxes-overview)
- [Durable Python package](https://pypi.org/project/azure-functions-durable/)
- [Related MAF continuation issue](https://github.com/microsoft/agent-framework/issues/7538)
- [Related Azure SDK continuation issue](https://github.com/Azure/azure-sdk-for-python/issues/46092)
