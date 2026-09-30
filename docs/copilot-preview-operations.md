# Copilot preview: session storage operations

Operational reference for the **default-off** Copilot harness preview and its
native session persistence. Design rationale lives in
[`architecture.md`](architecture.md#bounded-copilot-migration-preview); the
runnable local walkthrough lives in the
[preview sample](https://github.com/Azure/azure-functions-agents-runtime/tree/main/samples/copilot-preview).

> **Status.** This preview is not production-activated. Nothing here authorizes
> enabling the flag on a customer-facing app. Microsoft Agent Framework (MAF)
> remains the default and is unchanged while the flag is off.

## Qualification, by environment

Evidence is labeled separately per environment. "Unit" means fakes/temporary
directories in `tests/`; it is not storage or hosting qualification.

| Environment | What exists today | What is **not** established |
| --- | --- | --- |
| Local file storage | Unit tests over real temporary files (envelope integrity, atomic replace, OS-lock conflict, symlink rejection, rollback/recovery states) and the sample's documented local two-turn restart flow (not re-executed for this slice). | Multi-process contention beyond the single-owner lock test; long-running or large-session behavior. |
| Azure Blob storage | Unit tests against an in-process fake blob client covering lease acquire/renew, ETag-conditional single-put, stale-owner rejection and lease-loss latching. | **No evidence against a real storage account**: no live lease/fencing, durability, throughput, size or soft-delete behavior; no clean replacement-worker restore. |
| Target Functions host | None. | No deployed Functions (Consumption/Premium/Flex/Linux) run, no multi-worker run, no native-asset acquisition on the host, no scale-in/out behavior. |
| Native compaction | Compaction is enabled in the SDK session options (`InfiniteSessionConfig(enabled=True)`). | No compacted cold-restore evidence (see [Compaction evidence](#compaction-evidence)). |

Mid-turn recovery is explicitly out of scope: only **completed** turns are
guaranteed to be continuable, and no exactly-once tool effect is claimed.

## Opt in and opt out

`AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT` is the only harness selector and is read
once per app root, at app construction (or at first standalone runner use).

| Value | Result |
| --- | --- |
| Unset, `false`, `0` | MAF (default). No Copilot SDK import, native process, download, auth or telemetry. |
| `true`, `1` | Copilot preview for every role served by that app. |
| Present but empty, whitespace-only, or any other text | Explicit configuration error; the app does not start. |

Values are trimmed and case-insensitive. Changing the variable does **not**
affect an app or standalone default that has already been constructed:
**restart the Functions host** to switch in either direction. There is no
per-agent selector, no mixed-harness app, and no automatic fallback to MAF once
Copilot is selected. The legacy `runtime:` front-matter field remains ignored.

Opting out restores MAF behavior on restart, but does **not** migrate anything:
native session state stays where it is and is never converted to MAF history.

## Storage selection

| Setting | Purpose |
| --- | --- |
| `AZURE_FUNCTIONS_AGENTS_COPILOT_SESSION_STORAGE` | `local` or `blob` (trimmed, case-insensitive). Unset selects `blob` when `WEBSITE_INSTANCE_ID` is present, otherwise `local`. Any other value is an error. `local` is rejected on a deployed instance. |
| `AzureWebJobsStorage` / `AzureWebJobsStorage__blobServiceUri` | Required for `blob`. A connection string is used when present; otherwise the service URI is used with Entra credentials. A missing setting or any Blob failure is an error — never a local-disk fallback. |
| `AzureWebJobsStorage__clientId` / `AZURE_CLIENT_ID` | Optional user-assigned managed-identity client ID for the service-URI path. |
| `AZURE_FUNCTIONS_AGENTS_SESSION_CONTAINER` | Blob container name; defaults to `azure-functions-agents` (the same container the MAF history provider uses, in a disjoint prefix). |
| `AZURE_FUNCTIONS_AGENTS_SESSION_DIR` | Local root; defaults to `~/.azure-functions-agents`. Native state is written under `<dir>/agent-sessions/`. |
| `WEBSITE_SITE_NAME`, `WEBSITE_SLOT_NAME` | Deployed app identity. The site name is required on a deployed instance; the slot defaults to `production`. A worker-specific ID is never part of the identity. |

Mode, account/credential selection, container and app namespace are resolved
once and frozen per app; two app contexts with different settings never share an
SDK client. Secrets are never written into session state.

## Where state lives

One session is exactly one object or file:

```text
copilot-native/v1/{app_key}/{agent_key}/{session_key}/state.json
```

Each key is an unpadded URL-safe Base64 SHA-256 digest (`a_`, `g_`, `s_`
prefixes) over the namespace and identity, so agent slugs and session IDs are
never interpolated into a path. In `blob` mode the path is the blob name inside
the configured container; in `local` mode it is relative to
`<AZURE_FUNCTIONS_AGENTS_SESSION_DIR>/agent-sessions/`. Local mode also keeps a
data-free `state.lock` sidecar next to `state.json` for the cross-process lock,
and writes through a `.state-*` temporary file that is atomically replaced.

MAF history (`agent-sessions/<slug>/<session>.jsonl`) is untouched by this
namespace, and neither format is read or rewritten by the other.

## Inspecting a session

Path keys are digests, so resolve the exact name with the (internal, preview-only)
helper rather than guessing. Run this from the app root with the same
environment the host used:

```powershell
.\.venv\Scripts\python.exe -c @"
from pathlib import Path
from azure_functions_agents._native_session_identity import resolve_route, state_name
route = resolve_route(Path.cwd())
name = state_name(route, 'main', '<session-id>')
print(route.mode, route.container, name)
print(route.local_dir / name)
"@
```

The first line prints the storage mode, container and blob-style name; the
second prints the full local path (it already includes `agent-sessions/` and any
`AZURE_FUNCTIONS_AGENTS_SESSION_DIR` override).

Local, read-only:

```powershell
Get-Content "<printed-local-path>" | ConvertFrom-Json |
  Select-Object schema_version, sdk_version, native_version, protocol_version, state, revision, owner_epoch, handoff_may_have_started
```

Blob, read-only (requires read access to the account; makes no change):

```powershell
az storage blob show --account-name <account> --container-name azure-functions-agents `
  --name "<printed-name>" --auth-mode login --query "{size:properties.contentLength, modified:properties.lastModified, lease:properties.lease.status}"
```

Do not hand-edit an envelope. Any edit that breaks the integrity digest, the
version triple or the identity fields makes the session unresumable by design.

### Envelope fields worth reading

`state` is the lifecycle marker; `revision` and `owner_epoch` increase
monotonically; `handoff_may_have_started` records whether a prompt may already
have been dispatched; `integrity_sha256` covers the rest of the envelope.
Conversation content lives in the `completed` tree (and, during a turn, an
independent `working` copy).

| `state` | Meaning | Next request with the same ID |
| --- | --- | --- |
| `empty` | Reserved identity, no completed turn. Created on first use, including a failed resume attempt for an unknown ID. | A new conversation starts; a resume is rejected. |
| `preparing` | A turn was being set up. | Recovered automatically only when no handoff was marked and ownership is re-acquired; otherwise rejected. |
| `active` | A turn is (or was) executing. | Rejected — a concurrent request waits for the lock/lease within its deadline, then fails. |
| `ready` | A completed turn is durably stored. | Resumes the conversation. |
| `uncertain` | The turn may have been dispatched and could not be proven safe. | Rejected; use a new session ID. |
| `deleted` | Tombstoned. | Rejected; the ID is never reused. |

## Error behavior

Persistence diagnostics are content-free (no prompts, tool payloads or
credentials) and map to HTTP status codes on the built-in chat route:

| Condition | Status | Example message |
| --- | --- | --- |
| Busy session, lock/lease not acquired in the deadline | 409 | `Native session is busy; retry after its active turn.` |
| Unknown ID used as a resume | 409 | `Native session has no completed turn to resume.` |
| Opposite-harness history for the same ID | 409 | `This session has MAF history, not native state; use a new ID.` |
| Non-resumable state (`active`, `uncertain`, `deleted`, uncertain handoff) | 409 | `Native session is not safely resumable; use a new ID.` |
| Storage unavailable, lease lost, deadline exceeded mid-turn | 503 | `Native session storage is unavailable.` |
| Envelope corruption or version mismatch | 500 | Content-free validation diagnostic. |

A storage failure aborts the turn; it never becomes a model-visible tool result
or a success-shaped empty response, and it never silently resets a conversation.

## Versions, legacy IDs and rollback

The envelope pins `schema_version=1` with SDK `1.0.14`, native `1.0.85` and
protocol `3`. Only that triple is resumable under `v1`: unknown or newer
formats, mismatched versions, digest failures and identity mismatches fail
explicitly. There is no migration and no partial restore.

- **Legacy native IDs.** A native session created by the foundation slice
  (SDK-owned files, no envelope) is detected and rejected; use a new ID.
- **MAF ↔ Copilot.** Both harnesses probe metadata only, in both directions. A
  MAF-only ID is rejected on the Copilot path and a native-only ID is rejected
  on the MAF path. Probe failures fail closed rather than reporting "absent".
- **Rollback.** Rolling back to a binary without these guards cannot enforce
  them, so any rollback plan must assume **fresh session IDs** for conversations
  started under the preview. Turning the flag off does not migrate native turns
  into MAF history.

## Retention and targeted cleanup

Retention is customer-controlled. The runtime applies **no** TTL, no automatic
pruning and no background cleanup, and it exposes **no public delete API or
management endpoint** — the tombstone transition exists inside the internal
session type only and is not reachable from an application, HTTP route or
supported helper today.

Operationally, remove a session **as a whole unit** and only while no turn is
active (no lease on the blob; no process holding the local lock):

- Local: delete that session's directory (`state.json` plus its `state.lock`
  sidecar). Deleting individual files inside a session, or pruning what looks
  like an unused compaction reference, can leave an unresumable session.
- Blob: delete the single `.../state.json` blob for that session with your own
  tooling (for example `az storage blob delete`). This is destructive and is not
  run for you anywhere in this repository.

Deleting the live object does **not** erase service-retained copies: Blob
soft delete, versioning, snapshots and backup policies remain yours to
configure and to purge. A tombstoned or deleted ID must never be reused.

Stray content-free `empty` envelopes are expected after failed resume attempts
against unknown IDs; they hold no conversation content and are safe to remove
under the same whole-session rule.

## Compaction evidence

Native compaction is enabled with the SDK's own defaults; the host adds no
summarizer, no second compacted-context format and no token-threshold mapping.
MAF-specific `agent_configuration.agent_framework.compaction.max_context_window_tokens`
is rejected on the Copilot path when its effective value is non-null; clearing
it with `null` (or omitting it) selects native defaults. The portable
`max_output_tokens` cap remains rejected in this preview.

Acceptance for compaction is a single end-to-end sequence, not two independent
passing tests:

1. Drive one conversation until native compaction actually runs.
2. Complete that turn (acknowledged state).
3. Replace **both** the Python worker process and the native runtime process.
4. Restore the same `(agent, session)` in a clean worker.
5. Send a follow-up that depends on pre-compaction content and show the saved
   summary/references were reused **without another compaction model call**.

This sequence has not been demonstrated for the current code. Until it is, treat
compaction continuity as unproven.

## Negative-case walkthrough

Run these against a local preview host (see the sample for setup). None of them
should reach a model:

1. **Unknown session ID** — `POST /agents/main/chat` with
   `x-ms-session-id: unknown-<random>` returns 409
   (`Native session has no completed turn to resume.`).
2. **Opposite history** — reuse a session ID that already has MAF history: the
   Copilot path returns 409 and never reads the JSONL file. With the flag off,
   reusing a native-only ID is rejected the same way on the built-in chat route;
   other surfaces surface the same refusal as a generic 500 error.
3. **Concurrency** — send two overlapping requests for the same session ID: one
   turn runs, the other waits for the lock/lease and then fails with 409 rather
   than corrupting state.
4. **Missing Blob settings** — set
   `AZURE_FUNCTIONS_AGENTS_COPILOT_SESSION_STORAGE=blob` without
   `AzureWebJobsStorage`: app construction fails instead of falling back to
   local disk.
5. **Unsupported surfaces** — `/agents/main/chatstream` and
   `/agents/main/history` return 501 rather than an empty success shape.

## Known limits

- Single worker per app is the only configuration exercised; the previous
  `FUNCTIONS_WORKER_PROCESS_COUNT=1` startup check was removed with this slice,
  so nothing now stops you from starting a multi-worker or deployed app with the
  flag on. That is unqualified, not supported.
- Streaming, history projection, MCP, skills, delegation, Workflow Sub Agents,
  workflow-enabled agents, non-HTTP triggers and the debug chat UI remain
  rejected before inference on the Copilot path.
- Interrupted turns are not recovered; a turn whose handoff cannot be proven
  safe fails closed and requires a new session ID.

