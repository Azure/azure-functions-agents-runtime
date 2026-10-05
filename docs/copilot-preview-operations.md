# Copilot preview: session storage operations

Operational reference for the **default-off internal** Copilot harness preview and its
native session persistence. Design rationale lives in
[`architecture.md`](architecture.md#bounded-copilot-migration-preview); the
runnable local walkthrough lives in the
[preview sample](https://github.com/Azure/azure-functions-agents-runtime/tree/main/samples/copilot-preview).

> **Status.** This preview is not production-activated. Nothing here authorizes
> enabling the flag on a customer-facing app. Microsoft Agent Framework (MAF)
> remains the default and is unchanged while the flag is off.

## Qualification scope

Offline filesystem checks establish adapter behavior, not real-service
authentication, SDK continuation or Functions hosting. Earlier Blob
continuation and compaction runs exercised a different persistence
implementation and do **not** qualify this thin filesystem adapter.
Deployed-host and multi-worker behavior remain unqualified.

The SDK owns continuation, recovery and compaction. The host does not promise
transactional turns or exactly-once tool effects.

## Opt in and opt out

`AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT` is the only harness selector and is read
once per app construction. Standalone runners retain a separate first-use
default per resolved app root.

| Value | Result |
| --- | --- |
| Unset, `false`, `0` | MAF (default). No Copilot SDK import, native process, download, auth or telemetry. |
| `true`, `1` | Internal Copilot preview for local qualification of every role served by that app. |
| Present but empty, whitespace-only, or any other text | Explicit configuration error; the app does not start. |

Values are trimmed and case-insensitive. Changing the variable does **not**
affect an app or standalone default that has already been constructed:
**restart the Functions host** to switch in either direction. There is no
per-agent selector, no mixed-harness app, and no automatic fallback to MAF once
Copilot is selected. The legacy `runtime:` front-matter field remains ignored.

Restarting with the flag off selects MAF and its existing history provider.
Only the selected harness constructs, uses and closes its persistence adapter;
shared configuration does not initialize or probe the other implementation.

## Storage selection

There is no Copilot-specific storage selector. Blob is selected whenever
`AzureWebJobsStorage` or `AzureWebJobsStorage__blobServiceUri` is configured;
local files are selected only when neither is configured. This applies to
local hosts as well as storage configured for an app. Configured Blob auth,
network or configuration failures surface as errors, never a local fallback.

| Setting | Purpose |
| --- | --- |
| `AzureWebJobsStorage` | Existing Functions storage connection string; takes precedence over the service URI. Azurite also selects Blob storage. |
| `AzureWebJobsStorage__blobServiceUri` | Existing identity-based Blob service URI, used with `DefaultAzureCredential` when no connection string is configured. |
| `AzureWebJobsStorage__clientId` / `AZURE_CLIENT_ID` | Optional managed-identity client ID for storage; the storage-specific `clientId` takes precedence. |
| `AZURE_FUNCTIONS_AGENTS_SESSION_CONTAINER` | Blob container name; defaults to `azure-functions-agents` (the same container the MAF history provider uses, in a disjoint prefix). |
| `AZURE_FUNCTIONS_AGENTS_SESSION_DIR` | Existing local root override; defaults to `~/.azure-functions-agents`. Determines the local SDK workspace even with Blob persistence, and the native file storage root when no Blob setting is configured. Keep it stable when resuming SDK sessions. |

Storage configuration is captured per app. Restart the host after changing
captured settings, and check both process variables and `local.settings.json`
for conflicting values. Never put storage credentials in a checked-in template.

For an optional local-host Blob run, use an already-approved disposable target.
For example, the identity-based path uses these existing settings:

```powershell
$env:AzureWebJobsStorage__blobServiceUri = "https://<account>.blob.core.windows.net"
$env:AZURE_FUNCTIONS_AGENTS_SESSION_CONTAINER = "<approved-sample-container>"
```

Ensure no connection string is configured if you intend to use the service
URI. Use your approved credential with the required Blob data-plane access;
this guide does not authorize access changes or real-service test runs.

## Where state lives

The SDK reads and writes opaque files through `SessionFs`. The host stores an
ordinary local file or one Blob for each SDK file, under this readable name:

```text
copilot-native/{agent_id}/{session_id}/{sdk_relative_path}
```

`agent_id` is the unchanged shared `_agent_identity.agent_id(slug)` result:
trimmed, lower-case `WEBSITE_SITE_NAME` plus canonical agent slug. An unset,
empty or whitespace-only site name uses `local`; owner and deployment values
do not affect it. The adapter calls this helper directly rather than caching
or reconstructing an identity prefix. For agent `main` without a site name,
the session prefix is `copilot-native/local/main/{session_id}/`.

The name is relative to the configured Blob container or local storage root.
Session IDs retain the `^[A-Za-z0-9._-]{1,128}$` validation rule. SDK paths are
normalized and contained within the adapter's configured roots, including
handling the SDK's declared host-path convention. The adapter does not parse
file contents to resolve paths or restrict persistence to known filenames.
Only the exact current host workspace is accepted. Unknown paths recorded by
the SDK are denied; restoring after a workspace change is not qualified.

MAF history uses its own provider and `agent-sessions/` namespace. Neither
harness imports, initializes, probes or cleans up the other's persistence.

### Filesystem and concurrency boundary

The adapter implements read, write, append, exists, stat, directory listing
with entry types, mkdir, remove and rename, using SDK-shaped metadata and
errors. Blob rename can require copy/delete and is **not crash-atomic**.
These file operations are not a whole-session transaction.

Turns for the same `(agent, session)` are serialized within one Python
process, with bounded waiting. Independent sessions remain concurrent. The
same-loop runtime-owner guard permits concurrent invocation admission within
that process.
Cross-worker overlap is unsupported and caller-owned, as on the MAF path;
Blob storage alone does not coordinate turns across workers.

## Inspecting a session

Use the app's shared agent identity and returned session ID to identify only
the session you own. For a dedicated local sample run, inspect file metadata
without assuming a native file format:

```powershell
Get-ChildItem -LiteralPath "<sample-run-directory>" -Recurse -File |
  Select-Object FullName, Length, LastWriteTimeUtc
```

For one known Blob file, a read-only metadata query is:

```powershell
az storage blob show --account-name "<account>" --container-name "<container>" `
  --name "copilot-native/<agent-id>/<session-id>/<sdk-relative-path>" `
  --auth-mode login --query "{size:properties.contentLength, modified:properties.lastModified}"
```

Native files may contain conversation content or tool output. Do not publish
their contents in diagnostics or hand-edit them; the SDK owns their format.

## Error behavior

Missing files, existing destinations, wrong entry types, nonempty directory
removal and denied paths use the SDK's filesystem error contract. The pinned
SDK narrows some result codes to `ENOENT` or `UNKNOWN` and can map some `exists`
errors to `false`. That SDK behavior is not evidence that storage is healthy;
the adapter must report failures correctly rather than manufacture absent or
empty data.

Configured Blob auth, network and configuration failures are errors, not
reasons to change backend or switch to MAF. Session and provider failures
surface through the runtime's existing error responses, without prompts,
tool payloads, file contents or credentials in diagnostics. Resume and format
compatibility decisions belong to the SDK, not a host lifecycle/error table.

## Retention and targeted cleanup

The host adapter adds no TTL or background pruning. SDK-requested removal is
a filesystem operation, not a public session-management endpoint. Operator
retention and Blob soft-delete, version, snapshot and backup policies remain
the storage owner's responsibility.

For sample cleanup, stop the host first and identify the exact test-owned
session prefix or dedicated run directory. Review the files before removal.
Do not remove shared containers, unrelated sessions, MAF history or SDK caches.
Do not prune apparent compaction artifacts by filename; their relationships
belong to the SDK. Removing live files does not erase service-retained copies.

## Native compaction

Native compaction is enabled with the SDK's own defaults; the host adds no
summarizer, no second compacted-context format and no token-threshold mapping.
MAF-specific `agent_configuration.agent_framework.compaction.max_context_window_tokens`
is rejected on the Copilot path when its effective value is non-null; clearing
it with `null` (or omitting it) selects native defaults. The portable
`max_output_tokens` cap remains rejected in this preview.

All compaction artifacts are persisted as opaque SDK files. Filesystem tests
alone do not prove native compaction or session restoration, and the host adds
no guarantee about the SDK's internal restore behavior.

## Negative-case walkthrough

Use isolated local settings or offline fixtures, not a customer's storage:

1. **Invalid harness selector** — an empty or invalid
   `AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT` value fails app construction.
2. **Configured Blob failure** — an invalid or unavailable configured storage
   target surfaces an error rather than writing session files locally.
3. **Unsupported surfaces** — `/agents/main/chatstream` and
   `/agents/main/history` return 501 rather than an empty success shape.

## Known limits

- The preview requires a local host and a single Functions worker. An app
  with the flag on fails to start when
  `FUNCTIONS_WORKER_PROCESS_COUNT` is set to anything other than `1`, or when
  `WEBSITE_INSTANCE_ID` shows a deployed Functions instance. Rejecting those
  pre-existing Azure Functions platform settings is this runtime's bounded
  preview qualification policy, not a Copilot SDK or Functions platform limit.
  Both rejections are
  `UnsupportedCapabilityError`s raised before any native process, download or
  provider call. They stay until deployed-host and multi-worker qualification
  lands (issues #1357 and #1337).
- Streaming, history projection, MCP, skills, delegation, Workflow Sub Agents,
  workflow-enabled agents, non-HTTP triggers and the debug chat UI remain
  rejected before inference on the Copilot path.
- Same-session serialization is process-local only. Callers must avoid
  cross-worker overlap; SDK recovery does not provide exactly-once tool effects.
