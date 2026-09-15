# Durable Agent Loop chat UI

> **Private experimental surface.** The Durable Agent Loop chat UI is a
> separate, Function-App-hosted interface for the private Durable Agent Loop.
> It is not a replacement for the ordinary built-in chat UI or the
> recording-oriented focused demo. It adds no public front-matter or
> `agents.config.yaml` contract.

This document describes the implemented interface and its operational
boundaries. The retained reviewed design record is
[FRD 0012](frds/0012-durable-loop-chat-ui.md).

## Scope, activation, and routes

The UI is registered only when the existing
`AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_ENABLED=true` gate
enables the Durable Agent Loop. There is **no second chat-specific gate**.
With the default Functions HTTP route prefix, open:

```text
/api/experimental/durable-chat/
```

The effective prefix comes from the Functions host configuration. A nested
prefix is preserved, and an empty prefix produces
`/experimental/durable-chat/`; the page obtains all durable API paths from
policy-checked bootstrap rather than assuming `/api`.

The static shell is deliberately data-free. It is a fixed allowlist of five
sibling assets, not an arbitrary `/assets` file route:

```text
index.html
styles.css
rendering.js
history.js
app.js
```

`GET /experimental/durable-chat/` serves the shell. Its
`GET /experimental/durable-chat/config` bootstrap and every run-data route use
the main agent's existing HTTP authentication policy. Bootstrap returns
same-origin descriptors for the existing start, status, result, cancel, and
human-input APIs, plus owner-authorized event and diagnostics routes:

```text
GET  /experimental/durable-agent-runs/{run_id}/events
GET  /experimental/durable-agent-runs/{run_id}/diagnostics
```

The shell, bootstrap, event stream, and diagnostics responses use `no-store`.
The shell also has a restrictive CSP, `no-referrer`, and `nosniff` headers.

## Authentication and origin boundary

The page connects automatically to the Function App that serves it; there is
no app URL to enter. After a successful connection without a Function key,
the key form and its explanatory note stay hidden. An authentication failure
still exposes key entry for Function-protected apps.

The sandbox-backed demo explicitly sets the existing authoring policy:

```yaml
builtin_endpoints:
  chat_api: true
  http_auth: anonymous
```

**This demo is public when its Durable Loop gate is enabled.** Anyone who can
reach the app can start agent/tool runs and access anonymous-run data.
Anonymous callers share a separate app-level owner and browser-history
namespace; this is not per-user isolation. Previously Function-key- or
Entra-owned runs remain inaccessible from anonymous mode, and their saved
browser history is retained separately. Start a new session after switching
authentication modes.

To protect the app again, change the sample's authored policy back to
`http_auth: function` or to `entra` with properly configured Easy Auth, then
redeploy. Other apps retain their existing authentication defaults. The shared
non-Durable persistent-session authentication resolver still rejects
anonymous ownership.

The shell contains no bootstrap data, key, session, or run identifier. In
function-key mode, a supplied key is sent only in the `x-functions-key` request
header and is held in page memory. It is not placed in a query URL, IndexedDB,
browser history, transcript, or diagnostic link.

Easy Auth continues to determine the owner for Entra-protected requests; the
server checks that owner before opening an SSE response or reading durable
content. Function/admin-key mode remains **app-owned**, rather than creating a
distinct owner namespace for each key holder. Browser history namespacing is
organizational convenience, not an authorization mechanism.

Browser-originated mutations use the existing same-origin check. When the
Functions proxy supplies sanitized forwarded host and protocol headers, the
server compares the request `Origin` with that effective origin and rejects a
cross-origin mutation. This check also applies in anonymous mode, but it does
not make a public app private. It is in addition to the configured
authentication policy, not a CORS-only defense.

## Using the UI

Create a session with **New session**, then send one request at a time. A
session can be reopened later from the sidebar. Selecting another session,
hiding the request-details panel, closing the tab, or reloading the page does
not cancel a durable execution. Cancellation is an explicit action and human
input stays available in the conversation even while details are hidden.

Before a request is sent, the browser stores its complete normalized body and
a fresh idempotency key in the local history record. The durable-chat client
uses this UI extension of the existing start body:

```json
{
  "schema_version": "1",
  "prompt": "…",
  "session_id": "…",
  "request_id": "…",
  "sandbox_profile": "per_call",
  "fault_profile": "none",
  "ui": {
    "schema_version": "1",
    "stream_response": true
  }
}
```

If acknowledgement is lost after admission, the browser reconciles or retries
that **same** saved body and idempotency key. It never silently generates a new
key or changes the body. The Durable Entity remains the authoritative
one-active-turn-per-owner/session admission fence, including across tabs.

`per_call` is the default sandbox profile. `retained_session` appears only
when the existing retained-sandbox gate enables it. The UI neither enables a
gate nor changes Azure access.

### Browser-local history and privacy

History is versioned IndexedDB storage, scoped by a hash of the
deployment/route, agent, and effective owner. It is local to that browser
profile: it is not server session enumeration, a backup, or cross-device
synchronization.
The sidebar, requests, normalized start bodies, idempotency keys, transcripts,
drafts, and stream cursors are stored in transactional IndexedDB records for
recovery. The hide/show-details preference is separately remembered in
`localStorage`; if that preference cannot be saved, the page remains usable.

That data can contain sensitive prompts, assistant text, tool/result-derived
content, and session/run/sandbox identifiers. Remove a session explicitly from
the sidebar to delete its local session and request records; removal does not
cancel a server run. There is no automatic history eviction or destructive
schema reset.

Browser profile clearing, storage quotas, disabled IndexedDB, and private-mode
rules can remove or prevent persistent history. The UI makes a storage failure
visible. A user can explicitly continue in volatile mode, which keeps history
only until the page closes; it is never presented as saved history.

## Streaming and observation behavior

For foreground model mode, the pinned MAF one-step adapter runs
`Agent.run(stream=True)` with automatic tool invocation disabled. It publishes
only genuine assistant-visible text deltas, then still obtains the final MAF
response and applies the existing decision, usage, and final-response
validation. It does not split a completed answer into simulated streaming.

The displayed text is a draft until the durable result is committed. A retry
or a new observation epoch supersedes the prior draft; failed or cancelled
attempts clear active drafts. Background model mode remains status-only. An
explicit `stream_response=true` request in background mode is rejected before
admission rather than being represented as streaming.

Progress, tool state, sandbox observations, and drafts are published to a
bounded external journal, not to V1/V2/V3 orchestration histories. The journal
uses bounded batches, storage reservations, retries, snapshots, cursors, and
publisher drain time independently of model and tool budgets. Its failure is
visible as degraded or status-only UI behavior, but cannot fail an otherwise
successful execution, force a model/tool retry, or block cleanup.

UI initialization is create-once and bound to the admitted run, including the
effective model mode, request hash, plan/input references, lifetime, and
diagnostic metadata. Recovery reuses that winner; it does not rebuild a plan
from newer settings.

## Request inspector and sandbox identity

The inspector intentionally distinguishes configured metadata from observed
execution:

| Value | Meaning |
| --- | --- |
| **Configured Sandbox Group** | The current bootstrap display value from the existing hybrid Sandbox Group setting. |
| **Configured Sandbox Group in diagnostics** | The value frozen when that run was admitted. |
| **Sandbox observations** | Historical physical sandbox IDs captured at local-tool execution boundaries for the selected request. |

Actual local-tool observations take precedence over configured fallback
metadata. A remote MCP tool is explicitly shown as **no sandbox**. Do not
interpret the presence of a Sandbox Group as proof that every tool used one.

For each local call, the UI preserves the call key, physical sandbox ID,
generation, profile, and timestamped last-observed state. It can show
`not_allocated`, `executing`, `retained_idle`, `replacement_instance`,
`delete_requested`, `confirmed_deleted`, `unavailable`, or `stale` as
applicable. These are historical observations, not a live provider-health
claim:

- `delete_requested` is not confirmation that deletion completed.
- `confirmed_deleted` records a successful deletion observation.
- `replacement_instance` distinguishes a recreated physical sandbox from reuse.
- A retained sandbox normally remains available for reuse after a successful
  turn; failure/cancellation cleanup requests deletion.

Per-call IDs and historical state survive later turns and sandbox deletion
while the observation record is available. A last-observed ID does not mean
the sandbox is currently running.

## Diagnostics

The request inspector has a copyable run ID and exposes only credential-free
links that were frozen for that admitted request.

### Durable Task Scheduler

A DTS link is available only when the **effective** Durable storage provider is
`azureManaged`. The resolver honors host configuration and host setting
overrides. It supports:

- a validated `DTS_TASK_HUB_DASHBOARD_URL` override;
- `http://localhost:8082` for a local emulator at `http://localhost:8080`; and
- the cloud task-hub dashboard link constructed from the configured endpoint
  and task hub.

The link indicates configuration, not live service connectivity. The UI opens
the dashboard separately and intentionally does not claim an exact-instance
deep-link pattern.

### Application Insights

An Application Insights Logs link needs both a valid non-secret
`APPLICATIONINSIGHTS_RESOURCE_ID` and active runtime tracing/exporter
configuration. A connection string alone is insufficient. The generated
resource-scoped query uses the runtime activity attribute
`af.durable_loop.run_correlation`, whose value is the hashed correlation
computed by `durable_chat_run_correlation(run_id)`, and the frozen absolute
request interval. It queries exported dependency records and their
`customDimensions`; it is not a link to an incidental status-poll trace.

Sampling, ingestion delay, trace retention, Azure permissions, missing
exporters, and portal availability can still prevent results. An unavailable
integration is disabled with its reason. The UI does not verify a live Azure
Portal link or grant monitoring-read permissions.

## Observation retention is not browser retention

Server observations and browser history have different lifetimes. When the
admitted request expires, live observation/event access can expire even though
the browser still retains the saved conversation and frozen diagnostics it
already received.

The current implementation stores all durable-loop data in the configured
dedicated durable content container. Its relevant physical layout is:

| Blob prefix | Contents | Retention caution |
| --- | --- | --- |
| `objects/durable-chat-observation-batch/` | Immutable journal batches | Isolated durable-chat observation payloads. |
| `objects/durable-chat-observation-snapshot/` | Immutable replay snapshots | Isolated durable-chat observation payloads. |
| `runtime-state/durable-chat/journal/` | CAS journal manifests | Isolated journal state. |
| `objects/durable-chat-plan/` and `objects/durable-chat-input/` | Frozen UI run-plan/input records | Not observation-only; preserve through active admission/recovery. |
| Other `objects/` and `runtime-state/` paths | Run documents, content, checkpoints, receipts, capacity/retained state, and fault records | Never target with a broad durable-chat retention rule. |

`retention_class` on a content reference is metadata; it does **not** create an
Azure Blob lifecycle rule or delete a blob. No lifecycle policy is supplied by
this feature. If an operator adds one, it must be scoped only to the three
observation-only prefixes above, after proving its age threshold exceeds the
maximum run/replay need. It must not target broad `objects/`,
`runtime-state/`, checkpoint, receipt, retained-execution, plan, or input
prefixes.

When that safe scope cannot be guaranteed, use a separately scoped observation
container/prefix or a controlled, run-expiry-aware cleanup process that deletes
only exact journal-reachable batch and snapshot references. Do not invent a
wildcard deletion policy for the shared durable content container.

## Validation boundary

Use the committed test harness from an activated Python 3.13 or 3.14
environment with the project's development dependencies. Run the focused
suite first:

```powershell
python -m pytest -q `
  tests\test_durable_chat_config.py `
  tests\test_durable_chat_protocol.py `
  tests\test_durable_chat_journal.py `
  tests\test_durable_chat_execution_observer.py `
  tests\test_durable_chat_http.py `
  tests\test_durable_chat_history.py `
  tests\test_durable_chat_frontend.py `
  tests\test_durable_chat_stream_contract.py
```

The default pytest configuration excludes native E2E tests. Start local
Azurite, then select those tests explicitly:

```powershell
python -m pytest -m e2e -q tests\endtoend\test_durable_chat_e2e.py
```

The native suite uses real Functions/Core Tools, Azurite, and the pinned MAF
runtime paths, with controlled model and sandbox file/process transport
doubles. It is not a live LLM, Azure Sandbox, DTS, or Azure Portal validation.
Core Tools must be on `PATH`, with a Chromium browser available for CDP;
`DURABLE_CHAT_CHROMIUM` can name its executable explicitly. Node.js must also
be on `PATH` for the repository's JavaScript checks. The native suite defaults
to Azurite's loopback Blob, Queue, and Table endpoints on ports 10000, 10001,
and 10002. For isolated ports, set `DURABLE_CHAT_E2E_STORAGE_CONNECTION` to a
connection string containing explicit loopback endpoints. Remote storage
endpoints are rejected. A skipped browser or native test is not E2E coverage.

The completed local gate passed Ruff and strict mypy, with 3,041 tests passed
(67 skipped and 94 deselected). All eight durable-chat native API/browser
scenarios passed on both Python 3.13 and 3.14. The final wheel was checked for
all five UI assets and five new runtime modules, matching the source bytes.
These results do not claim a live Azure deployment or live cloud E2E
verification.
