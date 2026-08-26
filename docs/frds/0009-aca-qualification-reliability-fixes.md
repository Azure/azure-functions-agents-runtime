---
frd: "0009"
title: ACA qualification reliability fixes
status: Finalized
author: larohra
created: 2026-08-25
updated: 2026-08-25
issues: []
pull_requests: []
branch: larohra/jubilant-eureka
---

# FRD 0009 — ACA qualification reliability fixes

## 1. Summary

This amendment hardens the ACA Sandbox session runtime against the failures
found during the two qualification runs. It preserves upstream error meaning,
proves the deployed identity before scored traffic, isolates request work from
global stale state, bounds file and reconciler work, honors the synchronous
streaming contract, and makes run recovery deterministic. It defines eight
implementation fixes; this document is the design gate and does not implement
product code.

## 2. Motivation / problem

Qualification was invalidated by 65 opaque Sandbox Group resolution failures,
unrelated stale sandboxes being reconciled during a fresh request, 146 file-plane
409s in the rerun, a timer that exhausted its 240-second deadline, and a
default `/chatstream` request silently becoming a long-lived async SSE stream.
Immediate post-`done` submissions also encountered contract-correct active-run
fences without a run identity or management URL on successful synchronous
responses. These behaviors obscure the cause of failures, amplify load, and
leave durable state converging too slowly.

## 3. Goals / Non-goals

**Goals**

- Preserve safe ARM status categories, error codes, correlation metadata,
  `Retry-After`, and retryability through the provider/controller boundary.
- Gate qualification on an actual deployed Function-identity provider and
  data-plane preflight, including intended scale-out workers.
- Keep request-path reconciliation targeted to the current session/operation.
- Make file readiness and timer reconciliation lifecycle-aware, bounded, fair,
  and observable under deadlines.
- Keep `/chatstream` sync/async behavior explicit and below the Functions
  deadline.
- Make run fencing, terminal adoption, and recovery uniformly addressable.
- Always return `x-ms-run-id` and `Location` on successful synchronous stream
  responses, including terminal runs.

**Non-goals**

- Changing ACA Sandbox service behavior, SDK semantics, or declaring an
  unproven upstream cause for the 65 resolution failures.
- Making Durable Functions mandatory or adding a second identity layer.
- Retrying external model side effects or weakening one-active-run fencing.
- Re-running qualification or modifying Azure resources as part of this FRD.

## 4. Proposed design

The changes remain within the existing discover → translate → register →
execute architecture. No authoring or configuration format changes are
required.

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| discover | `app.py`, deployment/qualification helpers | Add a deployed identity preflight that exercises the configured ARM GET and label-scoped data-plane list; repeat across the intended worker population before scored traffic. |
| translate | `transport/aca_sdk.py`, `controller/http.py` | Translate ARM and file-plane failures into stable typed outcomes while retaining redacted status/correlation data, `Retry-After`, and retryability. |
| register | `registration/endpoints.py`, `registration/_handlers.py` | Preserve the caller's async preference; project run-management headers and linked error responses consistently from registered HTTP routes. |
| execute | `controller/reconciler.py`, `controller/readiness.py`, `controller/package.py`, `execution/aca_sandbox.py`, `execution/run_control.py`, `session_state/*`, `harness/journal_writer.py` | Scope opportunistic reconciliation, unify bounded file readiness, make timer progress fair/cursor-safe, honor stream deadlines, and converge run/operation state without opaque 500s. |

### Eight agreed fixes

1. **Classify Sandbox Group resolution failures.** Preserve a redacted
   401/403/404/429/5xx category, ARM error code/correlation ID and
   `Retry-After`; apply bounded retry only to retryable outcomes. Failed opens
   are not cached; successful provider objects remain process-cached.
2. **Preflight the deployed Function identity.** Before qualification, the
   deployed identity must successfully perform the configured Sandbox Group ARM
   GET and a label-scoped data-plane list. Exercise enough concurrent invocations
   to create the intended scale-out population and require a quiet window with
   zero provider-bind failures.
3. **Scope request-path reconciliation.** Post-create and capacity-retry passes
   may inspect only the requested session/operation (or a small explicit quota).
   Global orphan, expiry, inventory, and backlog work belongs to the timer;
   tombstoned/already-absent candidates are excluded before readiness I/O.
4. **Unify lifecycle-aware file 409 readiness.** Inspect lifecycle state,
   resume only in a flow that owns that mutation, centralize retryable status
   classification, honor `Retry-After`, and use capped exponential backoff with
   jitter plus per-candidate and whole-flow budgets. Never probe absent backing.
5. **Make timer reconciliation fair and cursor-safe.** Page and bound platform
   inventory, prioritize expired active operations and missing backing, use
   bounded concurrency and per-candidate time slices, advance the cursor only
   after successful page completion (or persist item progress), and emit
   processed/deferred/partial-progress counters on deadline.
6. **Honor `/chatstream` setup-timeout semantics.** Pass the actual caller
   preference to submission. A committed timeout without `Prefer:
   respond-async` returns a linked 504; explicit async returns 202. Any SSE
   lease is anchored to the original request wall deadline and closes with host
   headroom.
7. **Expose sync-stream recovery identity.** Every successful synchronous
   stream response returns `x-ms-session-id`, `x-ms-run-id`, and `Location`
   (status URL), regardless of the observed run phase. `done` means output
   completion, not slot availability; documentation and clients follow the
   management URL to `terminal` before same-key resubmission.
8. **Normalize operation-state conflicts and self-heal pointers.** Map
   executing/settling or terminal-operation conflicts to linked 409 or
   retryable 503 with management URL and `Retry-After`, rather than opaque 500.
   A completed operation still referenced by a session is adopted and its
   pointer/fence is repaired deterministically.

### Authoring / API surface

There are no new front-matter, `agents.config.yaml`, MCP, or directory keys.
The HTTP contract changes are additive: `x-ms-run-id` and `Location` are
required on every successful synchronous `/chatstream` response; linked
management errors use stable status, phase, and recovery URL fields. Existing
explicit async callers retain `202` and management URLs. The documented stream
`done` event is output-complete and may precede durable slot release.

### Compatibility

Existing callers may ignore the additive headers. Clients that submit another
turn immediately after `done` must follow `Location` until `phase=terminal`, or
handle the linked 409/503 recovery response. No silent sync-to-async conversion
is permitted. Existing one-active-run fencing, idempotency keys, auth, and
provider caching semantics remain intact.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| --- | --- | --- | --- | --- | --- |
| 1 | ARM failure fidelity | Collapse all / preserve category | Preserve category, safe metadata, retryability, and `Retry-After` so RBAC, absence, throttling, and service failure are actionable | Agent | 2026-08-25 |
| 2 | Qualification admission | Role read-back / deployed identity preflight | Require actual Function-identity ARM GET plus data-plane list across intended scale-out before scored traffic | Agent | 2026-08-25 |
| 3 | Opportunistic reconciliation ownership | Global request pass / targeted request pass + global timer | Keep requests session-scoped; timer owns cross-session stale-state convergence to prevent head-of-line blocking | Human + Agent | 2026-08-25 |
| 4 | Readiness retry policy | Fixed polling / lifecycle-aware bounded backoff | Inspect lifecycle, use capped jittered backoff with `Retry-After`, and enforce candidate/flow budgets | Agent | 2026-08-25 |
| 5 | Timer progress | Sequential unbounded / fair bounded cursor-safe pass | Page inventory, prioritize recovery, bound concurrency/time slices, and do not skip unfinished cursor work | Agent | 2026-08-25 |
| 6 | Sync setup timeout | Silent SSE / contract-preserving projection | No `Prefer` returns linked 504; explicit `Prefer` returns 202; SSE lease uses the original wall deadline | Agent | 2026-08-25 |
| 7 | Sync recovery headers | Headers only for non-terminal / always expose both | Always expose `x-ms-run-id` and `Location`, including terminal success, for deterministic recovery and observability | Human (larohra) | 2026-08-25 |
| 8 | Operation conflict recovery | Opaque 500 / linked conflict + pointer repair | Use linked 409/503 with management metadata and self-heal completed operation pointers without weakening fencing | Agent | 2026-08-25 |

## 6. Test plan

- [ ] Unit: ARM 401/403/404/429/500/502/503/504 and token-acquisition
  classification, retry policy, metadata redaction, and provider caching.
- [ ] Deployment/qualification: Function-identity ARM/data-plane preflight,
  scale-out worker coverage, and quiet-window gate.
- [ ] Regression: unrelated stopped, expired, tombstoned, and missing-backing
  sessions receive no request-path readiness calls.
- [ ] Unit: initial and attach/resume file 409, lifecycle states, `Retry-After`,
  jittered backoff, budget exhaustion, and no duplicate create/launch.
- [ ] Unit: timer fairness, bounded inventory, cancellation before cursor
  advancement, deferred counters, and already-absent convergence of operation,
  session, and capacity fence exactly once.
- [ ] HTTP: no-Prefer committed setup timeout is linked 504; explicit Prefer is
  202; delayed streams close below host headroom.
- [ ] HTTP/stream: every sync success includes `x-ms-run-id` and `Location`;
  `done` followed by immediate resubmit yields linked recovery, polling reaches
  terminal, and same-key resubmission succeeds.
- [ ] Regression: completed-operation pointers and executing/settling conflicts
  produce linked 409/503 rather than opaque 500 and self-heal durably.

## 7. Docs impact

- [ ] `docs/architecture.md` — clarify timer-owned global reconciliation,
  bounded request fast paths, and the HTTP recovery projection.
- [ ] `docs/aca-sandbox-session-runtime.md` — document lifecycle-aware
  readiness, deadline/fairness invariants, and operation convergence.
- [ ] `docs/triggers.md` / `README.md` — update `/chatstream` sync timeout,
  `done`, `x-ms-run-id`, `Location`, and async recovery examples as applicable.
- [ ] Add or update qualification/runbook documentation for deployed identity
  preflight and failure-classification gates.

## 8. Status & sign-off

- **Architecture review (phase 2):** Deep-dive evidence and issue drafts were
  reconciled against FRD 0008, `docs/architecture.md`, and the source paths
  named in the ACA failure report. The eight fixes above preserve the existing
  controller, transport, execution, and timer boundaries.
- **Human sign-off:** larohra, 2026-08-25 — approved all eight fixes and chose
  to always expose both `x-ms-run-id` and `Location` on successful sync streams.
  Status is `Finalized`; implementation and validation are deferred to the
  subsequent work.
