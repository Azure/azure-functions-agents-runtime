# A2A staged implementation plan

> Proposed delivery for [FRD 0009](../frds/0009-a2a-server.md).
> [Architecture and invariants](a2a-server.md) govern each slice.
> The bottom review PR is documentation-only. P3 alone is now authorized for
> implementation on its own stacked branch; P4 and every later capability still
> require separate human sign-off.

## Review strategy

One observable outcome per PR, with code, tests, and immediately affected docs
together. A few hundred lines can be a useful review budget, not a hard quota:
split by coherent behavior, never by leaving failing tests, unused public APIs,
dead flags, or broken intermediate states. Each product PR follows the existing
ruff/mypy/pytest gate; host/package isolation gates are added where relevant.

**Priority: D0 -> P3 -> P4 -> P5a -> P5b -> P1 -> P2 -> P6a onward.**
Keep the existing PR IDs as stable references; their numbers no longer imply
execution order. First build the working non-SSE A2A implementation and sample
in P3, use them to validate/discuss the design, then prioritize basic SSE with
only its necessary P4/P5a preparation. Durable import cleanup and packaging
isolation must not block these learning milestones. Until P2, the existing
mandatory Durable package and eager imports remain; simple/basic use no Durable
execution or bindings when workflows are disabled, but are not yet Durable-free
installations. P1/P2 establish that separate acceptance criterion afterward.
Basic SSE requires startup rejection of deployed/multi-worker use; if that cannot
be enforced reliably, it remains an internal test profile. All durable mode
configuration and cards remain internal/experimental through P9b; P10 is the
public release gate. Private components must have real contract-test/harness
consumers. Bounds and security ship with each first affected surface, not at P10.

| PR | Outcome | Depends on | Requirements |
| --- | --- | --- | --- |
| D0 | Approve FRD, architecture, library tuple, and staged contract | None | R1-R12 |
| P3 | Deliver working non-SSE MAF A2A server/sample for design validation | D0 + library gate within P3 | R1-R3, R5, R11 |
| PV | Validate a runtime-wide MAF core/provider upgrade in a standalone PR | P3 evidence; otherwise independent | Compatibility gate only; no new A2A capability |
| P4 | Extract private typed runner events with unchanged chat output | P3 | R5 |
| P5a | Deliver bounded local task lifecycle with internal tests | P4 | R4, R11 |
| P5b | Deliver local SSE adapter and deployment guard | P5a | R4-R5, R11-R12 |
| P1 | Remove eager Durable import coupling, preserving chat/A2A behavior | P5b | R5-R7 |
| P2 | Make Durable installation optional and migrate workflow/A2A installs | P1 | R6-R7 |
| P6a | Implement atomic shared projection/journal store and contract tests | P2 + storage gate | R8-R11 |
| P6b | Implement scoped admission, deduplication, and recoverable dispatch | P6a | R8, R11 |
| P6c | Wire managed execution and scoped Get/List polling | P6b | R7-R8, R11 |
| P7 | Harden execution ownership, context ordering, and retry recovery | P6c | R8, R10-R11 |
| P8 | Deliver any-worker ordered subscriptions and streaming | P7 | R4, R9 |
| P9a | Deliver distributed cancellation | P8 | R9-R11 |
| P9b | Deliver interrupted continuation | P9a | R10-R11 |
| P10 | Qualify/tune existing bounds and release distributed profile | P9b | R11-R12 |

This is a sequencing proposal, not simultaneous branches. Branch each slice
in its own worktree; normally wait for its dependency to merge. Stack only when
explicitly approved. Reverting a later capability must leave earlier modes and
existing workflow/chat behavior usable.

PV is not part of the capability chain and does not block P4/P5 or P1/P2.
Run it before any later slice that deliberately needs MAF 1.15+ runtime APIs.
It must validate the core/OpenAI/Foundry tuple together across workflows and
Durable bindings, telemetry, middleware and context providers, runner sessions,
tools, streaming, and the clean resolver/wheel matrix. Do not bundle it with
P3/P4/P5 or optional-Durable work.

## D0 - Design and compatibility approval

Review these three documents; record human sign-off in the FRD Decisions log.
Approve the recommended Alpha hosting-a2a/hosting `1.0.0a260730` + published SDK
`1.1.2` candidate against the existing MAF pins, including any narrow HTTP extra.
Resolve native-1.0 versus legacy model namespaces and identify the public hooks
for converters, app-owned `RequestHandler`, async cards, and the Functions HTTP
response bridge. Approve the approach for preserving runtime policy and later
substituting distributed handling without private patches, plus the local SSE
contract and authentication scope. P3's working sample provides the executable
integration evidence and the next design-discussion checkpoint; do not make
Durable dependency cleanup a prerequisite for that evidence.

**Acceptance:** reviewers can name the public hosting entry points, supported
wire methods, model namespace, optional closure, and upgrade/compatibility choice.
No declaration that A2A is available; no code or dependency changes in this slice.

## P3 - Simple non-SSE server

**Scope:** `[a2a]` dependency tuple, typed config/merge/validation,
`registration/a2a.py`, MAF adapter, app registration, card/JSON-RPC routes, auth,
and one runnable minimal sample with reproducible startup/client request steps.
Include generated reference and authoring/onboarding docs.
Reuse existing non-streaming runner policy rather than build a parallel agent path.

**Acceptance:** the sample serves an authorized direct Message through the real
MAF hosting/SDK and Functions response bridge, using a fresh `[a2a]` server
installation that still includes the baseline mandatory Durable dependency.
Its primary client uses public `A2ACardResolver` and `A2AAgent.run()` APIs from
an isolated client-only `agent-framework-a2a==1.0.0b260821` environment. The
client fetches the explicit per-agent card path and negotiates JSONRPC 1.0; it
does not install the editable runtime or change the server's core 1.13 tuple.
Without workflows, the server neither executes Durable work nor registers
Durable bindings. Durable-free installation/import validation belongs to P1/P2,
not this PR. Verify
version negotiation, explicitly configured card URL/SDK `card_path` (not automatic
domain-root discovery), JSON-RPC correlation and SDK error shapes,
supported input parts, unknown IDs, oversized requests, unsupported task methods,
and direct Message responses with either value of `returnImmediately` (no effect).
A2A-only agents validate; disabled A2A and
`builtin_endpoints: true` preserve current routes. Combined workflows+A2A uses one
`DFApp` with unchanged workflow bindings.

**Design checkpoint:** demonstrate the working sample, discuss the authoring/API,
card/client interoperability and runtime adapter, and record resulting decisions
in the FRD before P4/P5. Validate assumptions with this implementation rather
than front-loading dependency refactors.

**Non-goals / exposure:** streaming false; no Task persistence, subscribe/cancel,
outbound A2A client tools, REST, or durable execution. Preserve the raw HTTP
sample client for wire-contract inspection. Pinning changes to the runtime's
existing MAF trio belong in standalone PV with its full compatibility matrix,
not in P3 or a later capability/optional-Durable slice.

**Finalized P3 contract:** `[a2a]` uses hosting-a2a and hosting
`1.0.0a260730` plus SDK `[http-server]` `1.1.2`, without upgrading the existing
MAF trio. The narrow extra supplies the SDK's public Starlette JSON-RPC route
hook; `[fastapi]` and `[all]` are not used. The only method is native wire-1.0
`SendMessage`; a missing `A2A-Version` header means 0.3 and is rejected.
Supplied `taskId`, Task operations, streaming, and non-text Parts are rejected
with SDK-shaped JSON-RPC errors. Optional `contextId` is scoped and hashed
before it becomes a runner session ID; the response echoes the context and
carries a new Message ID.

`builtin_endpoints.a2a.url` is the required trusted external JSON-RPC URL.
The card is served at the per-agent
`agents/{slug}/.well-known/agent-card.json` path with the same resolved auth as
the RPC route, advertises JSON-RPC 1.0, text/plain, streaming false, push
notifications false, and matching security metadata. Clients configure that
exact URL or SDK `card_path`; no root discovery is implied.

P3 fixes conservative limits per agent: 32 in-flight executions, 256 KiB raw
JSON request, 16 Parts, 32 KiB per text Part, 64 KiB aggregate text input, and
256 KiB response text. It never publishes reasoning, tool arguments/results,
or arbitrary runtime metadata.

## P4 - Chat-preserving typed execution events

**Scope:** private runner event types and existing chat serialization consumer;
minimal extraction around current streaming logic. No new public Python API.

**Acceptance:** characterization tests preserve all current chat event kinds,
text/tool ordering, model/harness/tool policy, history/session handling, deadlines,
usage attribution, and failure behavior. Exercise cancellation while waiting,
timeout before the next item, GeneratorExit while suspended at yield, and nested
MAF cleanup exactly as current runner tests require. Preserve owned Durable
client lifetime in workflow-enabled streams.

**Non-goals / exposure:** no A2A SSE endpoint/capability yet, no change to debug
UI, no conversion of chat payloads into A2A messages by reparsing SSE.

## P5a - Local task lifecycle

**Scope:** private local task owner/store, scoped get/cancel/subscription behavior,
and real lifecycle contract tests. Bound active tasks, reader memory, output,
retention, and event batching from the start.

**Acceptance:** task execution outlives an individual reader; two local readers
receive the same ordered events independently. Test authorization, terminal
transitions, cancel races, expiry, and cleanup without a Durable execution
backend. The mandatory package may still be installed/imported until P1/P2.

**Non-goals / exposure:** no public SSE or configuration yet, no cross-worker or
restart recovery. The private backend has an executable internal test consumer.

## P5b - Local SSE adapter

**Scope:** MAF typed-event mapping, SDK/Functions SSE bridge, local-mode routes,
and required startup guard rejecting deployed or multi-worker configurations.
Extend the P3 sample with a streaming client to demonstrate incremental output
and support the next design discussion.

**Acceptance:** SDK parsing proves Task-first output, status/artifact ordering,
stable artifact IDs and append/last-chunk semantics; Message-only output is one
Message. Test reader disconnect independence, get/cancel/subscribe integration,
EOF/error cleanup, and guard rejection. If local-only operation cannot be reliably
enforced, keep this mode internal/test-only rather than expose configuration or
`streaming: true`.

**Non-goals / exposure:** at most guarded local-development streaming; never a
production/distributed capability or a crash-recovery promise.

## P1 - Import-boundary refactor

**Scope:** after the P3/P5 working samples, update `__init__.py`, `app.py`,
`registration/endpoints.py`, `workflows/__init__.py`, `integration.py`,
`context.py`, `tools.py`, and directly required import edges, including the new
A2A path. Preserve pure schema/metadata access and public workflow exports.
Keep the mandatory dependency during this refactor for separate packaging review.

**Acceptance:** import-blocking subprocess tests prove package/runner imports,
no-workflow startup, and simple/basic A2A never load Durable. Existing workflow
registration, real handler annotations, helper compatibility, and stream-owned
client close semantics remain intact. Preserve the P3/P5 samples and inspect
generated bindings, not just class names.

**Non-goals / exposure:** no new A2A semantics, extra names, or broad workflow
restructuring. Review focus is the import graph and lifetime.

## P2 - Optional dependency and workflow migration

**Scope:** `pyproject.toml`, `uv.lock`, workflow and A2A sample/E2E requirements,
contributor setup, migration docs, and missing-extra checks. Move the Durable pin
to `[workflows]`; provision development workflow coverage explicitly. Retain the
`[a2a]` extra introduced by P3 and update its installation guidance.

**Acceptance:** build/install actual wheels into base, base+monitor, `[a2a]`,
workflows, and combined workflows+A2A environments. Base/monitor/simple/basic
without workflows exclude Durable from the closure and work without importing
it or registering Durable bindings. Run the existing P3/P5 samples in these clean
environments. Enabled workflows fail fast with
`install azurefunctions-agents-runtime[workflows]` guidance when absent and retain
behavior with the extra. A broken installed SDK raises its original failure,
not a misleading missing-extra error. Python 3.13/3.14 remain supported.

**Non-goals / exposure:** no new A2A behavior or MAF upgrade unless separately
required and approved. This PR completes R6's installation/import isolation,
which was explicitly deferred from P3/P5.

## P6a - Atomic shared store

**Scope:** private shared projection/journal contracts and concrete transactional
store, with real backend contract tests/internal harness. Define partition keys,
ETag/CAS invariants, initial owner-epoch fields, and atomic state/event publication.

**Acceptance:** failure injection cannot expose a projection without its committed
events or vice versa. Exercise payload/transaction limits, expiry, bounded journal
reads, large-artifact references and authorization. Document cross-partition
index consistency; the index is not the admission/authorization authority.

**Non-goals / exposure:** no public durable mode, dispatch, or HTTP wiring.

## P6b - Admission and recoverable dispatch

**Scope:** private scoped deduplication, admission transaction, outbox, dispatcher,
and reconciliation; internal harness exercises actual store and scheduling seam.

**Acceptance:** initial context-less requests deduplicate by authenticated scope +
agent + client message ID **before** generating context/task IDs. Retrying without
context returns the original mapping; payload-hash conflicts fail. Existing-context
dedup is separately scoped. Accepted work survives disconnect, scheduling failure,
and lost acknowledgement; reconcile both accepted-but-unscheduled and
scheduled-but-unacknowledged windows. Bound admission, retry/backoff, and retention.

**Non-goals / exposure:** no public mode, agent execution, or detached success
based on best-effort dual writes.

## P6c - Managed execution and polling wiring

**Scope:** `[a2a-durable]`, internal backend configuration, Durable registration,
one execution activity, and scoped GetTask/ListTasks wiring. Coexist with workflows
without duplicating its engine. Keep durable SSE disabled.

**Acceptance:** default SendMessage waits as specified; explicit early return
acknowledges durable admission only. Auth filtering precedes bounded pagination;
state/results survive replacement workers. Test execution deadlines, output
limits, missing-extra errors, and workflows+A2A-durable package/binding matrix.

**Non-goals / exposure:** internal/experimental only until P9b and P10 pass; no
public durable config/card, exactly-once claim, or blanket activity retries.

## P7 - Execution ownership and recovery

**Scope:** owner epochs/CAS publication, per-context distributed coordination,
retry policy, idempotent activity dispatch, and partial-output recovery policy.
Include failure-injection integration tests against the selected backend.

**Acceptance:** crash after admission, before/after scheduling, during execution,
and after a side effect has specified outcomes. Old attempts cannot append or
complete after takeover; two tasks in one context cannot corrupt shared history.
Stable logical tool idempotency keys are available where tools support them.
Partial artifacts are resumed or explicitly superseded, never duplicated by
blind retry append. Orchestrator replay never invokes an LLM directly.

**Non-goals / exposure:** no exactly-once external operations and no subscriber
transport changes. Review failure windows independently from HTTP streaming.

## P8 - Distributed ordered subscriptions

**Scope:** independent cursor readers over P6a's already atomic projection/journal;
wire durable execution into SendStreamingMessage and SubscribeToTask. Add snapshot
watermark/concurrency handling, bounded reader memory/cursor reads, slow-reader
gap/expiry policy, and teardown; preserve existing publication/batching bounds.

**Acceptance:** task execution on worker A and subscription on worker B works.
Force an event between snapshot acquisition and live reading; deliver it exactly
once within that subscription's sequence. Two independent subscribers receive
the same ordered events, terminal output follows all artifact chunks, and a
disconnected reader does not stop execution. Terminal-task subscribe is rejected
according to the selected protocol; reconnect starts from current Task state.

**Non-goals / exposure:** no full historic replay/Last-Event-ID contract, queue
competing-consumer fanout, host affinity, or infinite HTTP duration promise.
Keep production exposure gated until lifecycle and operational requirements pass.

## P9a - Distributed cancellation

**Scope:** authorized cancellation intent, executor signaling/acknowledgement,
and terminal races, with bounded cancellation waits and reader notification.

**Acceptance:** cancellation from worker B reaches execution on A and produces
honest current/terminal state. Already running activities are not claimed killed
by orchestration termination; stale output is fenced. Race completion/cancel,
repeat cancel, owner changes, and unauthorized cancel.

**Non-goals / exposure:** no compensation of completed tools; durable mode remains
internal/experimental.

## P9b - Interrupted continuation

**Scope:** authorized continuation for supported interrupted states using existing
context coordination, message deduplication, and bounded retention/turn limits.

**Acceptance:** INPUT_REQUIRED and AUTH_REQUIRED retain resumable state; terminal
tasks reject new messages. Test scope/context mismatch, duplicate continuation,
expiry, and concurrent turns. Do not universally close AUTH_REQUIRED streams in
conflict with protocol behavior. Unsupported interaction types fail explicitly.

**Non-goals / exposure:** no cross-agent transfer; public durable mode still waits
for P10 qualification.

## P10 - Aggregate qualification and production capability gate

**Scope:** qualify and tune existing retention, reader/quotas, artifact security,
batching, and sanitized telemetry; do not introduce safety bounds for the first
time here. Complete deployment guidance, supported profile matrix, migration guide,
and independent coverage review.

**Acceptance:** automated multi-worker restart/fanout/cancel scenarios, expired
task/cursor responses, bounded memory under slow subscribers, backpressure,
artifact authorization, list pagination, and missing-extra matrix all pass.
Measure host/proxy behavior in an explicitly authorized later test environment;
distinguish idle and total duration limits. Heartbeats remain SSE comments.
Perform a separate coverage review before enabling production distributed
streaming in the card/configuration docs.

**Non-goals / exposure:** no push notifications, REST/gRPC, full historical replay,
or performance superiority claim without evidence. Finalize implementation status
only after the last accepted slice merges; design approval alone is not shipment.
