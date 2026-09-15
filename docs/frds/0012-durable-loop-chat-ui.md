---
frd: 0012
title: Durable Agent Loop Chat UI
status: Implementation authorized; final browser validation in progress
author: larohra
created: 2026-09-14
updated: 2026-09-14
issues: []
pull_requests: []
branch: larohra/durable-loop-chat-ui
---

# FRD 0012 — Durable Agent Loop Chat UI

This committed record preserves the independently reviewed Durable Agent Loop
chat UI design that implementation followed. On 2026-09-14, the user explicitly
authorized implementation from that reviewed record and requested no additional
FRD drafting or architecture-review cycle. It is not a new review, a merge
claim, or a claim that final browser validation or a cloud deployment completed.
The implementation guide is
[`docs/durable-agent-loop-chat-ui.md`](../durable-agent-loop-chat-ui.md).

## 1. Summary

The delivery adds a separate, Function-App-hosted chat experience for the
private Durable Agent Loop. It combines browser-persistent conversations with
actual session and sandbox identities, live execution progress, foreground
answer streaming, and request-specific diagnostics. The approved composition
is a session sidebar, conversation, and collapsible request-details panel.
Existing built-in chat and the recording-oriented localhost demo remain
unchanged.

## 2. Motivation / problem

The durable HTTP API supports admission, session continuity, status, results,
cancellation, and human input. Its status includes `session_id`, but it did not
provide a browser-ready execution stream or request diagnostics. The existing
sandbox endpoint returns an alias for a currently retained sandbox, not the
actual sandbox ID or historical per-call execution identity.

The focused demo deliberately keeps identifiers behind a process-local proxy.
Its session list does not provide the requested persistent, request-scoped
conversation experience. It must not become a deployed UI by removing those
recording-specific safeguards.

Implementation evidence:

- `experimental/durable_chat_config.py`,
  `durable_chat_protocol.py`, `durable_chat_http.py`, and
  `durable_chat_journal.py` own the hosted-shell, initialization, and
  observation contracts.
- `experimental/durable_loop_activities.py` streams genuine foreground MAF
  updates with automatic tool invocation disabled, then validates the final
  response.
- `experimental/durable_loop_registration.py`,
  `durable_loop_execution.py`, `durable_loop_sandbox.py`, and
  `durable_loop_observability.py` observe actual execution boundaries without
  changing Durable command graphs.
- [FRD 0011](0011-focused-durable-chat-demo.md) remains the source for the
  proxy and alias boundaries of the focused demo.

## 3. Goals / Non-goals

**Goals**

- Show and copy the actual session ID and each request's run ID.
- Show the configured Sandbox Group separately from the actual sandbox used by
  each local tool, including historical per-call and retained instances.
- Show real execution milestones and incrementally stream foreground assistant
  text before model completion.
- Provide a DTS dashboard link when configured and an Application Insights link
  scoped to the selected request when the required metadata and tracing exist.
- Create a new session without deleting, cancelling, or mixing old sessions;
  reopen an old session to read its transcript and continue it.
- Retain browser-local history across refreshes and reconnect to an active run
  instead of creating another one.
- Preserve a clean reading experience with a hide/show details preference.
- Support human-input waits and cancellation through existing durable controls.

**Non-goals**

- Replacing ordinary chat, the private demo proxy, or the DTS dashboard.
- Cross-browser history synchronization, server-wide session listing, a new
  identity system, or automatic Azure permission changes.
- Background-model token streaming. Background mode remains status-only.
- Streaming reasoning, tool arguments/results, provider response IDs, content
  references, or workspace files to the progress UI.
- Running tools from the UI, moving the agent loop into a request handler, or
  making browser connectivity part of execution ownership.
- New public agent front matter, `agents.config.yaml` fields, a frontend build
  system, Azure resources, deployment, or access changes.

## 4. Reviewed delivery design

### 4.1 Composition and interaction

The implemented page uses locally packaged HTML/CSS/JavaScript modules under
`public/durable-chat/`, rather than copying the built-in page or introducing a
frontend build system. Its desktop composition has three regions:

- **Sessions:** new session, recent conversation titles, and run/wait badges.
- **Conversation:** full session ID, prior requests, streamed responses, inline
  human-input controls, and the composer.
- **Request details:** selected run ID, execution environment, progress,
  sandbox observations, and diagnostic links.

The panel starts visible. **Hide details** expands the conversation; **Show
details** restores the selected request. Selecting an older request opens the
panel. Hiding it never stops execution, progress consumption, or history
updates, and compact status/human-input/error information remains in the
conversation. The browser UI keeps keyboard access, focus visibility,
meaningful labels, reduced-motion support, and a narrower-screen secondary
details region.

The session prototype used during review was illustrative only. Its simulated
identifiers, timer-driven text, and diagnostic actions are not runtime
behavior.

### 4.2 Runtime boundaries

| Pipeline stage | Modules | Responsibility |
| --- | --- | --- |
| Discover | No changes | Do not add network discovery or enumerate Azure resources. |
| Translate | `experimental/durable_chat_config.py`, `durable_chat_protocol.py` | Resolve safe display metadata and validate UI options without changing `config/schema.py`. |
| Register | `app.py`, `durable_loop_http.py`, `durable_chat_http.py` | Register the isolated static page, authenticated bootstrap, diagnostics, and event routes only under the existing durable-loop gate. |
| Execute | `durable_loop_activities.py`, `durable_loop_apim.py`, `durable_loop_registration.py`, `durable_loop_execution.py`, `durable_loop_sandbox.py` | Produce real observations at activity/execution boundaries and stream foreground model text. |
| Observe | `durable_chat_journal.py`, `durable_chat_execution_observer.py`, `durable_loop_observability.py` | Persist replayable UI observations outside Durable history and correlate runtime spans without identifier-valued metrics. |
| Present | `public/durable-chat/` | Manage session/request view state, history, reconnection, and inspector preference. |

The UI uses the sole existing
`AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_ENABLED` gate. It is
separate from ordinary chat and focused demo behavior but has no second
chat-only activation gate.

### 4.3 Routes and frozen request options

Paths are relative to the effective Functions host route prefix, commonly
`/api`.

| Surface | Contract |
| --- | --- |
| `GET experimental/durable-chat/` | Data-free static shell; no environment, session, or run data. |
| Fixed sibling assets | Explicit packaged asset allowlist, never arbitrary filesystem paths. |
| `GET experimental/durable-chat/config` | Authenticated agent/display data, supported sandbox profiles, foreground-streaming capability, credential-free integration metadata, and opaque history namespace. |
| Existing `POST experimental/durable-agent-runs` | Optional strict `ui` object, for example `{"schema_version":"1","stream_response":true}`. Absence preserves existing clients. |
| Existing status/result/cancel/input routes | Reuse existing ownership, admission, commit, and human-answer semantics. |
| `GET experimental/durable-agent-runs/{run_id}/events` | Owner-authorized replayable SSE with a validated exclusive cursor. |
| `GET experimental/durable-agent-runs/{run_id}/diagnostics` | Owner-authorized bounded snapshot of identity, timestamps, observation health, historical sandbox observations, and diagnostic links. |

Host prefixes may be nested or empty. Bootstrap supplies same-origin route
descriptors, so the browser does not hard-code `/api`.

UI options normalize before admission and participate in the existing
idempotency hash. A create-once initialization record binds the actually
admitted run, owner, normalized request hash, frozen plan/input references,
committed generation, effective model mode, diagnostic metadata, and lifetime.
Recovery always loads that winner instead of rebuilding from newer application
settings.

The browser persists the complete normalized POST body and its idempotency key
before submission. Lost acknowledgement handling reconciles or repeats the
same request only; it never generates a new key or silently changes the body.
No token/progress scheduling enters V1/V2/V3 orchestration history.

### 4.4 Real streaming and progress

For foreground mode, the one-step MAF adapter builds a fresh agent with the
immutable catalog and automatic tool invocation disabled, then uses
`Agent.run(stream=True)`. It aggregates the final public response and sends it
through the existing decision, usage, and final-response validation. It does
not simulate streaming by splitting a completed answer into words.

Only assistant-visible text updates enter the UI stream. Reasoning,
function-call arguments, encrypted content, raw provider envelopes, and
credentials remain excluded. Text is a **draft** until the durable commit and
result endpoint confirm completion. Retried model attempts allocate a new
observation epoch, explicitly replace/supersede prior drafts, and failed or
cancelled attempts clear active drafts.

The journal assigns run-scoped sequence information outside the deterministic
model/tool contracts. It uses immutable bounded batches and compact snapshots:
content is written first, then a CAS manifest advances the visible watermark.
Snapshots and cursors replace or continue browser state atomically. Bounded
pending buffers, producer epochs, CAS retries, drain deadlines, and reserved
observation bytes are independent of execution budgets. Exhaustion produces
explicit degraded/status-only presentation, never a model/tool retry or an
execution-budget failure.

SSE readers use shared durable storage rather than a worker-local queue.
Disconnect/reconnect leases do not cancel the orchestration. Authorization
precedes snapshot/delta access. A cursor ahead of the published watermark is
rejected, and compacted history returns a snapshot for reconciliation.

Background-model mode remains status-only. An explicit request to stream in
that mode is rejected before admission rather than represented as live text.
Observation publication is best effort: it cannot change a successful result,
consume tool deadlines, enter provider retry scopes, or prevent cleanup.

### 4.5 Actual execution identity

The focused demo keeps its alias-only `/sandbox` contract. The hosted UI's
owner-authorized diagnostics intentionally retain actual historical IDs for
the selected request.

The execution observer records group resource ID, sandbox ID, generation, call
key, model step, profile, provenance, and timestamped lifecycle state from
actual local/remote dispatch boundaries. It records remote tools as
`remote_no_sandbox`, rather than inferring ACA use for every tool. Per-call
observations are published before later deletion state, and retained
observations remain tied to their original request even after a later turn
takes the session fence.

The inspector distinguishes **not allocated**, **remote (no sandbox)**,
**executing**, **retained/idle**, **replacement instance**, **delete
requested**, **confirmed deleted**, and **unavailable/stale** states. It labels
them as last-observed state, not live provider health:

- a successful delete request is not a confirmed deletion;
- an unchanged physical ID, not workspace continuity alone, is reuse evidence;
- a replacement instance names the prior and new physical IDs;
- successful retained turns keep their sandbox available for reuse, while
  failure/cancellation cleanup requests deletion.

Configured Sandbox Group metadata is separate from actual observations.
Bootstrap shows current configured display metadata; request diagnostics freeze
the configured resource ID at admission. Actual local observations take
precedence over configured fallback.

### 4.6 Diagnostics links

**DTS.** The UI inspects the effective Durable provider, including host
overrides, and exposes a dashboard link only when that provider is
`azureManaged`. It accepts a validated
`DTS_TASK_HUB_DASHBOARD_URL` override, maps the local emulator at
`http://localhost:8080` to `http://localhost:8082`, or constructs the cloud
task-hub dashboard URL from safe endpoint/task-hub values. It never exposes a
connection string. The UI opens the dashboard separately and offers **Copy Run
ID**; it does not claim an exact-instance deep-link pattern or live
connectivity verification.

**Application Insights.** The deployment can provide the non-secret
`APPLICATIONINSIGHTS_RESOURCE_ID`, but a connection string alone is
insufficient. The link is available only when that resource ID is valid and
runtime tracing/exporter configuration is active. Runtime recording activity
spans carry the hashed `af.durable_loop.run_correlation` value computed by
`durable_chat_run_correlation(run_id)`; it is not a metric label. The
resource-scoped Logs query filters dependency/custom-dimension records using
that value and the request's frozen absolute interval, not an incidental
status-poll operation.

Missing configuration disables an action with a reason and grants no
monitoring-read permission. Sampling, ingestion delay, retention, tracing
configuration, Azure permissions, and portal availability can still prevent
results. The delivery does not claim live Azure Portal link verification.

### 4.7 History, concurrency, and security

Versioned IndexedDB records contain browser-local sessions, requests,
transcripts, draft snapshots, normalized submission bodies, idempotency keys,
and cursors. The authenticated bootstrap namespace scopes the data by
deployment/route, agent, and opaque owner value. It is organization, not
authorization: Function/admin-key mode remains app-owned, whereas Easy Auth
uses the existing owner isolation.

The UI rehydrates the selected session's own transcript and watches its
nonterminal run after refresh. It does not continuously poll every historical
session. Durable admission remains the active-turn fence across tabs. It
distinguishes session conflicts, human waits, cancellation, expired results,
network loss, authentication changes, observation degradation, and ambiguous
start acknowledgement.

History has no automatic eviction or destructive reset. Removal is explicit
and local; it does not cancel a server run. Prompts, tool/result-derived text,
responses, and identifiers may be sensitive in a browser profile. Quota
limits, profile clearing, private mode, or disabled IndexedDB can remove or
block persistence; volatile fallback is explicit and visibly announced.

Function keys are held only in memory and passed only in same-origin request
headers, never browser storage, URLs, links, or logs. Every data route applies
the existing Function-key/Easy Auth policy and authorizes the persisted owner
before content/SSE access. Browser-originated mutations additionally enforce
the existing same-origin comparison against the sanitized Functions forwarded
origin. User/model text is rendered as text, static assets are locally
packaged, and response headers use restrictive CSP, `no-store`, `no-referrer`,
and `nosniff` protections.

### Temporary anonymous demo access

The operator can explicitly select the existing
`builtin_endpoints.http_auth: anonymous` policy for the Durable Loop demo.
This makes its durable APIs publicly callable; it does not embed or retrieve
a Function key. Anonymous callers share a separate durable owner hash and
browser-history namespace, so changing this policy does not expose previously
keyed or Entra-owned runs. The shared session-runtime authentication resolver
remains unchanged and still rejects anonymous persistent-session access.

The browser already bootstraps against its hosting origin automatically.
After a credential-free connection, it hides Function-key controls; keyed apps
retain those controls when authentication is required or a key is in use.
Same-origin mutation checks, bounded admission/execution, and cleanup behavior
remain unchanged. The sample is explicitly anonymous for this temporary demo;
switch its authored policy back to `function` or configured `entra` to protect
it again.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | UI scope | Upgrade built-in / Separate UI | Separate Durable Loop UI; existing chat remains unchanged. | Human | 2026-09-14 |
| 2 | Hosting | Function App / Local proxy | Function App under the existing durable-loop gate. | Human | 2026-09-14 |
| 3 | Session history | Page memory / Browser persistence / Server history | Browser persistence across refreshes; no cross-device requirement. | Human | 2026-09-14 |
| 4 | Streaming scope | Status only / Foreground text / Both model modes | Real foreground text streaming; leave background mode unchanged. | Human | 2026-09-14 |
| 5 | Layout | Request inspector / Inline details | Option A with a hide/show control for the right-hand details panel. | Human | 2026-09-14 |
| 6 | Frontend implementation | Static modules / Bundled SPA | Static modules; reuse the repository's lightweight delivery model. | Agent, proposed | 2026-09-14 |
| 7 | Progress delivery | Worker-local stream / Shared durable observations | External journal with attempt-aware replay; browser connectivity must not own execution. | Agent, proposed | 2026-09-14 |
| 8 | Identifier boundary | Change alias endpoint / Separate projection | New owner-authorized diagnostics; preserve the recording demo's alias contract. | Agent, proposed | 2026-09-14 |
| 9 | Diagnostics precision | Guessed deep links / Verified links with fallback | Verified task-hub and request-filtered Logs links; add exact transaction links only when proven. | Agent, proposed | 2026-09-14 |
| 10 | Recovery integrity | Rebuild on retry / Reuse frozen initialization | Create-once admitted-run initialization; identical same-key recovery cannot change the plan. | Agent, review amendment | 2026-09-14 |
| 11 | Observation isolation | Execution-coupled writes / Bounded independent observer | Separate deadlines and byte allowance; snapshot plus cursor commits atomically. | Agent, review amendment | 2026-09-14 |
| 12 | Delivery authorization | New FRD cycle / Approved implementation | Implement from the reviewed record; user approved delivery and skipped additional FRD review. | Human | 2026-09-14 |
| 13 | Temporary demo access | Function key / Microsoft sign-in / Anonymous | Explicit anonymous demo with a separate shared owner scope; existing private runs remain inaccessible. | Human | 2026-09-15 |
| 14 | Sandbox-preserving delivery | Merge in-process runtime / Separate branch | Publish the anonymous UI separately, retaining ACA execution and resumption; leave the running app unchanged. | Human | 2026-09-15 |

## 6. Test plan and delivery evidence

The implementation coverage targets:

- gate/packaging absence when disabled, fixed assets, prefix handling, existing
  chat/focused-demo compatibility, and no public schema change;
- Function-key and Easy Auth ownership before content/SSE access; same-origin
  mutation checks; no arbitrary static paths;
- explicit anonymous route bindings, automatic keyless bootstrap, hidden key
  controls, and owner/history separation from existing private runs;
- optional UI admission, idempotent recovery, frozen initialization, legacy
  hashes/command graphs, and observation producer fencing;
- genuine pre-final streaming with unchanged final response/usage validation
  and automatic tool invocation disabled;
- journal CAS contention, batch/manifest interruption, snapshots/cursors,
  bounded storage/degradation, and separate workers;
- actual per-call deletion history, retained reuse/replacement/cancellation
  cleanup, remote-no-sandbox observations, and later-turn history;
- runtime activity span correlation, safe DTS/Logs configuration, and absent
  integrations;
- browser new/reopened sessions, refresh mid-stream, panel preference,
  cancellation, human input, cross-tab conflicts, volatile storage, and lost
  acknowledgement reuse of the exact saved submission.

An initial CI-equivalent gate passed 3,005 tests. Later targeted suites and
native Python 3.13/3.14 API streaming plus actual tool-identity paths passed.
Those native tests use real Functions/Core Tools, Azurite, and pinned MAF
runtime paths with isolated model and sandbox file/process transport doubles;
they are not live LLM, Azure Sandbox, DTS, or Portal validation. The canonical
local gate and all eight native API/browser scenarios pass, including the
explicitly anonymous fixture on Python 3.13 and 3.14. This evidence does not
claim live cloud E2E completion.

## 7. Docs impact

- `docs/architecture.md` documents the hosted UI pipeline, observation path,
  module map, and request/diagnostic boundaries.
- `docs/durable-agent-loop-chat-ui.md` documents usage, authentication,
  browser-history sensitivity, streaming-mode differences, diagnostics,
  retention, and validation limitations.
- This record and `docs/frds/README.md` preserve and index the reviewed design
  decisions without restarting an FRD process.
- Root and sample READMEs link the private UI, retain the focused-demo
  distinction, and document the non-secret Application Insights resource
  metadata.
- `config/schema.py`, `docs/front-matter-spec.md`, and
  `docs/front-matter-reference.md` remain unchanged; no schema-document
  regeneration is required.

## 8. Status and authorization

- **Reviewed design:** the prior independent review and its amendments are
  preserved above.
- **Human decisions:** separate hosted UI, browser persistence, foreground
  streaming, and Option A with collapsible details were confirmed.
- **Delivery authorization:** on 2026-09-14 the user explicitly approved
  implementation from this record and opted out of another FRD drafting or
  architecture-review cycle.
- **Current delivery state:** product implementation and directly related
  documentation are present. The canonical local gate and all eight native
  API/browser scenarios on Python 3.13 and 3.14 passed; the final packaged
  assets were verified. This record does not claim merge, deployment, or
  live cloud verification.
- **Anonymous follow-up:** on 2026-09-15 the user authorized a separate branch
  retaining ACA sandbox execution and resumption, rather than taking the
  leadership branch's newer in-process runtime. The anonymous owner boundary,
  hidden key controls, and sandbox-backed native paths pass locally. The
  follow-up does not deploy or modify the running Azure app.
