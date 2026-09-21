---
frd: 0009
title: Storage-backed agent content
status: In review
author: swapnil-nagar
created: 2026-09-21
updated: 2026-09-21
issues: []
pull_requests: []
branch: swapnil/storage-backed-agent-content
---

<!-- markdownlint-disable-next-line MD025 -->
# FRD 0009 — Storage-backed agent content

## 1. Summary

Separate frequently changed agent content from the Azure Functions code package. In an
opt-in mode, the runtime will load agent Markdown, `agents.config.yaml`, and `mcp.json`
from the Function App's existing Blob Storage account. Publishers will upload loose,
content-addressed objects and atomically advance a small manifest pointer, so changing
one file does not require rebuilding, zipping, or redeploying the application package.
The runtime will compare a canonical startup plan for each content generation with the
plan indexed from an immutable boot manifest. Execution-only changes will become visible
to new invocations without a restart; changes to Functions bindings or registered
surfaces will be staged and reported as requiring host restart and trigger
synchronization.

## 2. Motivation / problem

Today `create_function_app()` reads all agent Markdown and `mcp.json` from the deployed
application directory during Python worker indexing. It composes `ResolvedAgent` and
`AgentCapabilities` objects once, freezes them in the app-wide catalog, and captures
them in registered handler closures. Consequently, changing instructions or an outbound
MCP server follows the same code-package deployment path as changing Python code, even
though neither change normally alters an Azure Functions binding.

This coupling causes several problems:

- Prompt, model, schema, and MCP configuration changes incur package build, upload, host
  recycle, and indexing costs.
- A physical ZIP is an unnecessarily large update unit when one small Markdown or JSON
  document changed.
- Operators cannot determine mechanically whether a content change actually requires
  Functions metadata to be re-indexed.
- Reading mutable files independently would risk composing agents from a mixture of
  generations during a multi-file update.
- Scale-out workers need a bounded, observable convergence model that does not mutate a
  configuration object while an invocation is using it.

The Function App already has Blob Storage connectivity through `AzureWebJobsStorage`,
including managed-identity configuration used by the blob history provider. The runtime
can reuse that connectivity without introducing another Azure resource.

## 3. Goals / Non-goals

### Goals

- Keep agent Markdown, `agents.config.yaml`, and `mcp.json` in Blob Storage as an
  independently publishable content plane.
- Upload only changed documents; do not require an archive or code deployment.
- Preserve one atomic logical generation across all documents.
- Determine restart requirements from the normalized startup plan rather than from
  filenames or a manually maintained list of mutable fields.
- Hot-activate validated execution-only changes for new HTTP and non-HTTP invocations.
- Reuse `AzureWebJobsStorage`, including connection-string and managed-identity forms.
- Preserve local-file behavior as the default and as the local development experience.
- Keep one immutable content snapshot for the full lifetime of each invocation.
- Provide deterministic rollback to an earlier manifest.
- Emit the active, observed, and pending generations and restart decision in telemetry.
- Provide a publishing command suitable for local use and CI automation.

### Non-goals

- Mutating Azure Functions decorators or binding metadata in a running worker.
- Loading executable Python tools from Blob Storage.
- Loading project skills from Blob Storage in the first version; MAF skill providers
  continue to receive directories from the deployed application package.
- Storing secrets in content blobs. Environment variables and managed identity remain
  the supported secret and credential boundary.
- Automatically granting the worker Azure Resource Manager permission to restart its own
  Function App.
- Interrupting or changing the snapshot held by an in-flight invocation.
- Providing instantaneous, globally synchronized activation across all scaled-out
  workers. Convergence is bounded by the configured refresh interval.
- Supporting arbitrary external content stores in the first implementation.

## 4. Proposed design

The pipeline gains an **acquire** step before discovery. Acquisition is infrastructure
aware but has no knowledge of Azure Functions decorators. It returns a read-only logical
project snapshot. Existing discovery and translation consume that snapshot, while Python
tool and skill discovery continue to use the deployed `app_root`.

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| acquire | new `content/source.py`, `content/local.py`, `content/blob.py`, `content/manifest.py` | Resolve local or Blob-backed documents into an immutable, generation-tagged project snapshot. |
| discover | `config/loader.py`, `discovery/mcp.py`, `discovery/tools.py`, `discovery/skills.py` | Parse agent/global/MCP documents from the snapshot; continue discovering executable tools and skill directories from `app_root`. |
| translate | `config/merge.py`, `config/validation.py`, `registration/capabilities.py`, new `content/runtime_snapshot.py` | Build and validate one complete resolved agent/capability catalog for a content generation. |
| register | `app.py`, `registration/triggers.py`, `registration/endpoints.py`, new `registration/startup_plan.py` | Build a canonical startup plan, register it at startup, and retain its fingerprint as the structural compatibility boundary. |
| execute | `registration/_handlers.py`, `registration/endpoints.py`, `runner.py`, `workflows/engine.py`, new `content/manager.py` | Handler closures capture stable registration identity and obtain the current immutable runtime snapshot by slug before each invocation. |
| publish | new packaged CLI module | Validate documents, upload missing content objects, write an immutable manifest, classify the change, and conditionally advance the active pointer. |

### 4.1 Content source boundary

Introduce a `ContentSource` protocol that produces a `ProjectContentSnapshot` containing:

- a generation identifier;
- an immutable mapping from normalized logical path to document bytes;
- content hashes and source markers for diagnostics;
- the active-pointer ETag, when applicable.

`LocalContentSource` preserves today's file layout and behavior. `BlobContentSource`
loads the same logical paths from a manifest. The parsers will operate on document text
and logical source paths rather than opening files internally. This keeps environment
substitution, Pydantic validation, slug derivation, and diagnostics shared across both
sources.

Acquisition is storage-aware but not Azure Functions-aware: it knows Blob APIs, but not
`FunctionApp`, decorators, routes, or trigger bindings. Discovery remains read-only and
registration remains the only stage that mutates or understands Azure Functions
surfaces. Because `create_function_app()` is synchronous, initial Blob acquisition uses
the synchronous Storage SDK with bounded timeouts. Invocation-time refresh uses the
async SDK and never blocks the event loop with synchronous network I/O.

The first Blob-backed document set is:

- root-level supported agent files;
- supported agent files under the logical `agents/` directory;
- `agents.config.yaml`;
- `mcp.json`.

The Blob source is authoritative when enabled. It will not merge remote documents with
local agent/config documents because precedence between independently deployed sources
would make generation identity and rollback ambiguous.

### 4.2 Loose-object storage protocol

Use a dedicated container, separate from the Functions deployment package container and
the session-history prefix:

```text
azure-functions-agent-content/
  objects/sha256/<content-hash>
  manifests/<generation>.json
  active.json
```

Each immutable manifest contains at least:

```json
{
  "schema_version": 1,
  "generation": "<opaque generation id>",
  "created_at": "<UTC timestamp>",
  "files": {
    "agents/main.agent.md": {
      "object": "objects/sha256/<hash>",
      "sha256": "<hash>",
      "size": 1234
    },
    "mcp.json": {
      "object": "objects/sha256/<hash>",
      "sha256": "<hash>",
      "size": 456
    }
  },
  "content_fingerprint": "<canonical hash>",
  "runtime_contract_version": 1
}
```

The runtime recomputes and verifies hashes; manifest values are not trusted merely
because they were supplied by the publisher. Validation normalizes logical paths to `/`,
rejects absolute paths, `..`, backslashes, unsupported filenames, duplicate case-folded
paths, and object references not exactly matching
`objects/sha256/[a-f0-9]{64}`. It downloads each referenced object, verifies its byte
length, recomputes SHA-256, and rejects the complete generation on any mismatch.
Manifest signing is not part of the first release: Blob RBAC controls publisher trust,
while hashes provide integrity and identity rather than protection from an identity that
can legitimately replace manifests and objects.

Initial safety limits are a 1 MiB manifest, 1,000 documents, 4 MiB per document, and
32 MiB total uncompressed document bytes per generation; `active.json` is limited to
16 KiB. A storage operation is bounded to 30 seconds and complete acquisition to 120
seconds. These defaults may become configurable below fixed hard ceilings if
implementation measurements require it; limit violations reject the candidate before
parsing or activation.

`active.json` is a small commit pointer to one immutable manifest. Publishers update it
with an ETag precondition so concurrent publishers cannot silently overwrite each other.
The pointer contains only its schema version, manifest path, and manifest hash. Both the
pointer and manifest paths are restricted to their configured prefixes. Object creation
and immutable manifest creation use `If-None-Match: *`; a publisher never overwrites a
hash-addressed object or an existing manifest. The runtime hashes the downloaded manifest
bytes and requires an exact match with the SHA-256 in the pointer before parsing it. A
boot manifest setting carries the same `path@sha256` reference, so startup applies the
identical verification without trusting mutable blob properties. The runtime separately
recomputes the manifest's `content_fingerprint` from its normalized file entries.

The manifest does not carry an authoritative startup fingerprint because environment
substitution and deployed Python tool/skill inventories can differ from the publisher's
machine. The runtime computes that fingerprint after substitution and discovery. A
publisher may calculate an advisory plan diff, but content identity is the only portable
manifest claim.

Unchanged objects are reused by hash. Publishing a one-line prompt edit therefore
uploads one Markdown object, one manifest, and one small pointer. A later garbage
collection command may remove objects unreachable from retained manifests, but garbage
collection is not required for activation correctness.

### 4.3 Canonical startup plan and restart classification

Add a pure `StartupPlan` representation generated from a fully parsed and validated
content snapshot plus the deployed tool/skill inventory. The plan covers both Azure
Functions metadata and runtime structures that are intentionally frozen at startup:

- Function App kind (`FunctionApp` or `DFApp`);
- agent identity set and registered function names;
- trigger binding types and normalized binding arguments;
- built-in routes, methods, effective host authorization levels, descriptions published
  on registered MCP surfaces, and other decorator metadata;
- app-wide Durable registration and workflow-bound handler signatures;
- workflow-enabled agent identities, workflow tool grants, and workflow Sub Agent
  topology;
- chat Sub Agent topology and generated `delegate_<slug>` tool identities;
- the startup contract version and deployed executable capability inventory identity.

Registration and restart classification must share this representation or its canonical
normalizers. The classifier must not duplicate an independently maintained raw-field
allowlist. A sorted canonical serialization plus a versioned hash produces the
`startup_fingerprint`; a structured comparison of the same representation explains why
a restart is required.

`create_function_app()` creates one process-local `ContentManager`, gives it the startup
plan and immutable boot snapshot, and passes that manager explicitly to registration
functions and handler closures. The manager, rather than a module global or an
undocumented `FunctionApp` property, owns the startup fingerprint, current snapshot,
refresh single-flight, and retired-generation leases. Each closure captures only the
manager, the registered slug, and structural adapter data fixed by its binding.

Blob mode requires `AZURE_FUNCTIONS_AGENTS_CONTENT_BOOT_MANIFEST` to identify an
immutable manifest. Every worker in a deployment registers from that manifest, including
cold scale-out workers that start while `active.json` is changing. After registration, a
worker may use the active generation only when its startup fingerprint equals the boot
fingerprint. This makes the app setting the stable scale-out boundary rather than
whichever active pointer a worker happened to observe during indexing.

Before returning the constructed `FunctionApp`, startup also attempts to load
`active.json`. If its generation is compatible, that generation becomes the initial
execution snapshot so a cold scale-out worker does not temporarily regress to old
instructions. Failure to read or validate the active pointer is non-fatal after the boot
manifest has registered successfully: the worker starts on the boot snapshot and retries
active refresh on a later invocation.

When a candidate generation is loaded:

1. Parse and validate the entire generation and resolve environment placeholders once in
  the Function App environment.
2. Build a completely new resolved catalog, capability object graph, and startup plan
  without mutating the Function App or any published snapshot.
3. Compare its startup fingerprint with the manager's boot fingerprint.
4. If equal, atomically publish the candidate as the current execution snapshot.
5. If different, retain the last compatible snapshot and record the candidate as pending
  restart, including a structured startup-plan diff.

The manager records the observed pointer ETag and candidate result even when a candidate
is invalid or incompatible. It does not download and reclassify the same rejected ETag on
every interval; it retries only after the pointer changes or after an operator-requested
diagnostic retry.

Typical hot-reloadable changes include instructions, model, timeout, agent-framework
limits, input/response schemas, response guidance, logging metadata, outbound MCP server
configuration, system-tool settings, and non-workflow capability filters over the
already deployed Python tool and skill inventories. Adding executable tools or skill
directories still requires code deployment.

Typical restart-required changes include adding/removing/renaming an agent, changing a
trigger or binding argument, changing built-in endpoint registration or effective
host-level auth, and enabling/disabling Durable workflow registration. The plan
comparison remains authoritative for conditional cases, such as display metadata that
is also emitted into a registered MCP surface. Entra tenant/audience/client allowlists
may hot refresh when the effective route `AuthLevel` is unchanged.

For the first release, workflow policy, workflow Sub Agent execution configuration, and
chat Sub Agent topology changes are startup-static even if a future design could make a
subset dynamic. This preserves the current immutable workflow catalogs, Durable
authorization assumptions, and generated delegation tool contracts while the basic
content refresh path is established.

### 4.4 Refresh and invocation consistency

Each worker owns a `ContentManager` with an immutable current runtime snapshot. Handler
closures capture only the registration-time agent slug and structural settings required
by the Functions binding. At invocation start they acquire a snapshot lease and resolve
that slug to its `ResolvedAgent`, `AgentCapabilities`, current catalog, and applicable
runtime policy. Snapshot construction creates a detached object graph: generations do
not share mutable config models, capability lists, tool lists, or policy mappings, and
published objects are never mutated in place.

Refresh is lazy and invocation-driven rather than dependent on a permanent background
thread, which is not reliable under serverless suspension:

1. If the refresh interval has not elapsed, return the current snapshot without storage
   I/O.
2. Otherwise, one process-local single-flight task conditionally reads `active.json`
   using its last ETag.
3. If unchanged, advance the next-check deadline.
4. If changed, fetch the immutable manifest and only objects absent from the process-local
  content cache.
5. Parse, validate, compose, and classify the complete candidate.
6. Atomically replace the current snapshot only when it is structurally compatible.

Concurrent requests continue using the snapshot lease they acquired at invocation start.
Each generation builds fresh MCP tool objects, header providers, credentials, token
caches, and HTTP clients from that generation's `mcp.json`; the existing app-root MCP
cache is not reused across Blob generations. The snapshot owns an async resource stack.
When a generation is replaced, it is marked retired, but its resources are closed only
after its final lease is released. A failed refresh disposes the unpublished candidate
and never partially mutates the current generation.

The published snapshot exposes read-only mappings and tuple-backed capability
collections. Candidate construction deep-copies parsed models and never shares mutable
lists or policy objects with an earlier generation. Existing APIs that require lists
receive invocation-local copies. Tests deliberately attempt mutation and verify that one
generation cannot alter another.

Durable orchestrator registration, handler catalogs, and authorization policy remain
boot-static. Each agent-executing Activity acquires the current compatible snapshot at
Activity start for dynamic fields such as instructions, model, timeout, and outbound MCP
configuration. Therefore, later Activities and retries may observe a newer compatible
content generation, just as they may observe a newer code deployment today; Durable
orchestrator state and persisted capability authorization do not change. Exact
generation pinning for an entire long-running workflow is a future option, not part of
this feature.

Workers converge independently within the refresh interval. Polling is capped at one
active-pointer check per interval per worker process, not one per concurrent invocation.
A value of zero requests an ETag check before every invocation, coalesced by the
single-flight; a small nonzero default avoids adding Blob latency to each model call. An
in-flight invocation is never migrated to a newer generation.

### 4.5 Structural activation and restart coordination

The worker must not restart its own Function App. Self-restart would require management-
plane privileges, cannot atomically coordinate scaled-out workers, and couples the data
plane to deployment control.

The publisher stages every manifest and reports an advisory classification of:

- `no_change`;
- `hot_reloadable`;
- `restart_required` with a structured startup-plan diff;
- `invalid` with validation diagnostics.

Publisher-side classification uses the same library and the local project inventory, but
the running `ContentManager` is authoritative because it has the deployed inventory and
Function App environment. A publisher may advance `active.json` for any validated
generation using compare-and-swap. Existing workers hot-activate it only when its startup
fingerprint matches their boot plan; otherwise they retain the previous generation and
report `restart_required`.

The publisher's classification is advisory metadata only and is never trusted as an
activation instruction. Every worker independently resolves, validates, and classifies
the candidate. If publisher and runtime classifications differ, the runtime decision
takes precedence and telemetry records the mismatch.

Structural activation changes `AZURE_FUNCTIONS_AGENTS_CONTENT_BOOT_MANIFEST` to the
candidate immutable manifest, which recycles the Function App, then synchronizes trigger
metadata where required by the hosting plan. New workers register and execute from that
boot generation even if `active.json` still points to the previous incompatible plan.
After the new deployment is healthy, automation advances `active.json` to the candidate.
Old workers reject it against their old boot plan until the platform retires them. A
deployment slot is recommended when structural changes require an externally atomic
cutover; an ordinary rolling recycle may briefly serve old and new registered surfaces.

The exact management-plane integration for structural activation is an open decision.
The initial publisher may emit machine-readable output and documented Azure CLI commands
rather than acquire broad management-plane permissions itself.

Rollback selects a retained manifest. A compatible rollback advances the pointer with
ETag compare-and-swap; a structural rollback follows the same boot-setting and restart
path. The first release performs no automatic deletion: manifests and objects are
retained until an explicit garbage-collection command is run. Garbage collection is
dry-run by default, retains every object reachable from a retained manifest, and is
deferred from the first implementation slice so it cannot block activation or rollback.

### 4.6 Failure and availability behavior

- In local mode, behavior remains unchanged.
- In Blob mode, the pinned boot manifest is required for indexing. A missing,
  unreachable, corrupt, oversized, or invalid boot generation fails startup with
  actionable diagnostics rather than registering a partial application. `active.json`
  is never an indexing dependency.
- After successful startup, refresh failure retains the last known good in-memory
  snapshot and records refresh health. There is no fallback to unrelated local content.
- If a candidate removes the slug for a currently registered handler, it is structural
  and cannot be hot-activated.
- If a worker cold-starts while an incompatible structural candidate is staged, it uses
  the explicitly pinned startup manifest rather than an unapproved active pointer.
- If `active.json` is missing, corrupt, oversized, or unparseable during cold start or
  refresh, treat it as unavailable, retain the valid boot/current snapshot, and report
  the error. Publishers replace this small block blob with one conditional atomic write,
  so readers observe either the complete prior pointer or the complete new pointer, never
  a torn JSON update.
- Object downloads use bounded timeouts, retries appropriate for idempotent reads, and
  the concrete manifest/file/total limits in Section 4.2.

### 4.7 Security

- Reuse `AzureWebJobsStorage` and its standard connection-string or
  `AzureWebJobsStorage__blobServiceUri` plus managed-identity configuration.
- Allow a dedicated content container and pointer path to be configured, but reject path
  traversal, absolute paths, unsupported logical file types, duplicate case-folded paths,
  and manifest references outside the configured object prefix.
- Require the content and history container names to differ and fail startup on a known
  collision. The deployment-package container is also unsupported as a content
  container.
- Scope the runtime identity to `Storage Blob Data Reader` on the content container where
  infrastructure permits. The publisher needs `Storage Blob Data Contributor` on that
  container; these identities need not be the same. Existing applications may retain
  broader account-level access required for Functions host storage or session history,
  but samples document the narrow content-plane grants.
- Never log connection strings, SAS tokens, raw authorization headers, or substituted
  secret values.
- Keep placeholders in stored content and resolve them in the Function App environment.
- Do not accept `tools/*.py`, scripts, or arbitrary executable payloads from the content
  container in this feature.

### 4.8 Observability

The runtime emits `content.acquire` and `content.refresh` spans with
`af.lifecycle_stage=content_acquire` or `content_refresh`. Attributes use the `af.content.*`
namespace:

- `af.content.source`: `local` or `blob`;
- `af.content.boot_generation`, `af.content.active_generation`, and
  `af.content.invocation_generation`: opaque generation identifiers capped for telemetry;
- `af.content.content_fingerprint` and `af.content.startup_fingerprint`: first 12
  hexadecimal characters only;
- `af.content.object_count` and `af.content.total_size_bytes`;
- `af.content.outcome`: `boot_loaded`, `no_change`, `hot_activated`, `restart_required`,
  `invalid`, or `error`;
- `af.content.pending_generation` and `af.content.plan_diff_categories` when restart is
  required;
- `af.content.error_category` on failure.

Refresh duration comes from the span duration. The current invocation generation is also
attached to the enclosing `agent.run` or workflow Activity span. Low-cardinality counters
record refresh outcomes; generation identifiers and fingerprints are not metric labels.

Logs must use logical source markers such as
`blob:<container>/<logical-path>@<generation>` and never include credentials or signed
URLs.

### 4.9 Authoring / API surface

The Markdown, YAML, and JSON schemas do not change. This feature changes where those
documents may be sourced and how they are deployed.

Proposed bootstrap settings:

| Setting | Meaning | Default |
| --- | --- | --- |
| `AZURE_FUNCTIONS_AGENTS_CONTENT_SOURCE` | `local` or `blob` | `local` |
| `AZURE_FUNCTIONS_AGENTS_CONTENT_CONTAINER` | Dedicated Blob container | `azure-functions-agent-content` |
| `AZURE_FUNCTIONS_AGENTS_CONTENT_ACTIVE_BLOB` | Active manifest pointer path | `active.json` |
| `AZURE_FUNCTIONS_AGENTS_CONTENT_BOOT_MANIFEST` | Immutable `path@sha256` manifest reference pinned for Blob-mode startup/rollback | required in Blob mode |
| `AZURE_FUNCTIONS_AGENTS_CONTENT_REFRESH_SECONDS` | Minimum interval between active-pointer checks | proposed `5` |

The proposed five-second interval caps checks at 12 per minute per active worker process,
not per invocation; concurrent calls are coalesced. A value of `0` is an explicit strict-
freshness option that can add Blob latency to invocations. The operator guide will
document the latency, transaction, and scale implications so deployments may choose a
longer interval.

The packaged publishing command will support validation, incremental upload, JSON output,
optimistic concurrency, dry-run classification, activation of compatible generations,
and selection of a prior manifest for rollback. Command naming and whether structural
activation invokes Azure management APIs remain open for human sign-off.

### 4.10 Compatibility

- `local` remains the default, so existing applications behave exactly as today.
- `create_function_app(app_root=...)` remains supported and continues to anchor Python
  tool and skill discovery in both modes.
- Blob mode is opt-in and authoritative for agent/config documents.
- Existing environment-variable substitution occurs after document acquisition in the
  Function App environment.
- Existing storage connection-string and managed-identity conventions are reused.
- Existing registered routes, function names, history identity, and session storage do
  not change merely because content came from Blob Storage.
- Deployments can migrate by publishing their current local documents as the first Blob
  generation, configuring the Blob source, and performing one initial restart.

### 4.11 Proposed delivery slices

This feature should be delivered in reviewable slices after the FRD is finalized:

1. **Pure content and planning foundations:** document abstraction, local source adapter,
   parser refactor, canonical startup plan, and positive/negative integration tests
   proving local startup metadata is unchanged and every structural mutation changes the
   plan.
2. **Blob acquisition and publishing:** manifest protocol, Blob source, incremental
   publisher, managed-identity configuration, validation, and Azurite coverage. Blob
   content is loaded at startup; every content change still requires restart in this
   intermediate slice.
3. **Compatible hot refresh:** generation manager, ETag refresh, handler and workflow
  Activity snapshot lookup, dynamic MCP lifecycle, restart classification,
  last-known-good behavior, and concurrency tests.
4. **Deployment integration and documentation:** structural activation/rollback workflow,
   sample infrastructure, telemetry, operator documentation, and end-to-end validation.

Every intermediate slice must preserve local mode and pass the full repository gate.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Separate content from code deployment | Keep all files in the code package / store mutable authoring content separately | Store agent Markdown and MCP configuration in Function-accessible storage so common edits do not redeploy code | Human | 2026-09-21 |
| 2 | Physical publication unit | ZIP archive / mutable named blobs / immutable loose objects plus manifest | Use loose content-addressed objects with an atomic manifest pointer; no archive is required | Agent | 2026-09-21 |
| 3 | Multi-file consistency | Read named blobs independently / use Blob snapshots only / immutable logical generation | Resolve every invocation against one immutable manifest generation | Agent | 2026-09-21 |
| 4 | Restart determination | Filename rules / raw-field allowlist / canonical startup-plan comparison | Compare one versioned plan covering Functions metadata and startup-static runtime topology | Agent | 2026-09-21 |
| 5 | Refresh mechanism | Background polling / Blob-trigger broadcast / lazy invocation refresh | Use process-local lazy ETag refresh with single-flight loading | Agent | 2026-09-21 |
| 6 | In-flight behavior | Mutate shared config / cancel old requests / snapshot per invocation | Hold one immutable generation for each invocation and retire it afterward | Agent | 2026-09-21 |
| 7 | Storage resource | New account / deployment package container / existing account with dedicated container | Reuse `AzureWebJobsStorage` through a dedicated content container | Agent | 2026-09-21 |
| 8 | Compatibility default | Blob by default / automatic merge / local by default and Blob opt-in | Preserve local mode as default; Blob mode is authoritative when selected | Agent | 2026-09-21 |
| 9 | Refresh failure | Fail every request / fall back to local / retain last known good generation | Fail initial Blob indexing, then retain the last validated compatible snapshot on refresh errors | Agent | 2026-09-21 |
| 10 | Restart authority | Worker restarts itself / publisher or deployment control plane coordinates restart | Worker reports structural incompatibility; deployment control coordinates recycle and trigger sync | Agent | 2026-09-21 |
| 11 | Executable content | Include tools and skills / include tools only / keep executable and path-based capabilities in code | Keep Python tools and project skills in the deployed package for the first version | Agent | 2026-09-21 |
| 12 | Scale-out registration baseline | Read mutable active pointer at startup / persist a shared worker record / pin an immutable boot manifest | Require every Blob-mode worker to register from the manifest named by an app setting | Agent | 2026-09-21 |
| 13 | Runtime snapshot ownership | Module global / undocumented app property / explicitly injected manager | `create_function_app()` constructs and injects one process-local `ContentManager` | Agent | 2026-09-21 |
| 14 | MCP lifecycle | Reuse startup cache / mutate clients / fresh generation-owned object graph | Each generation owns fresh MCP tools, credentials, token caches, and HTTP clients until its last lease ends | Agent | 2026-09-21 |
| 15 | Environment substitution | Publish-time / per invocation / once per runtime generation | Resolve placeholders while composing each candidate in the Function App environment, matching current load-time semantics | Agent | 2026-09-21 |
| 16 | Content integrity trust | Trust publisher metadata / sign manifests / runtime hash verification plus Blob RBAC | Recompute object hashes and rely on RBAC for publisher trust; signing is deferred | Agent | 2026-09-21 |
| 17 | Initial retention | Automatic age/count cleanup / overwrite mutable files / explicit garbage collection | Retain immutable manifests and objects by default; add guarded explicit collection later | Agent | 2026-09-21 |

### Open decisions for architecture review and human sign-off

1. Confirm the default refresh interval (`5` seconds proposed) and whether a per-invocation
   ETag check (`0`) is supported for strict freshness.
2. Confirm that workflow policy and both chat/workflow Sub Agent topology changes remain
  structural in the first version, while Activities use current compatible dynamic
  content.
3. Decide whether the packaged publisher only reports structural activation steps or may
   optionally invoke Azure management APIs when explicitly given Function App scope.
4. Confirm the public command name and whether publishing is exposed as both CLI and
   Python API.
5. Confirm the initial retain-by-default policy and the later explicit garbage-collection
  contract.

## 6. Test plan

- [ ] Unit: local and Blob content sources produce equivalent normalized document maps.
- [ ] Unit: manifest parsing rejects unsupported versions, traversal, duplicate logical
  paths, hash/size mismatches, unsupported files, and configured limit violations.
- [ ] Unit: publishing uploads only missing hashes and updates `active.json` with ETag
  compare-and-swap semantics.
- [ ] Unit: startup-plan serialization is deterministic and changes for every
  Functions decorator/binding change.
- [ ] Unit: execution-only changes retain the startup fingerprint.
- [ ] Unit: trigger, endpoint, auth, agent-identity, and workflow registration changes
  alter the startup fingerprint with actionable diff categories.
- [ ] Unit: workflow policies and chat/workflow Sub Agent topology are startup-static;
  instruction/model/MCP changes for an existing Activity target remain compatible.
- [ ] Unit: environment substitution occurs once per candidate generation before startup
  classification and uses the Function App environment.
- [ ] Unit: handler closures resolve the newest compatible snapshot by slug for HTTP,
  streaming, MCP, and non-HTTP invocations.
- [ ] Unit: workflow Activities resolve the current compatible snapshot while retaining
  boot-static persisted authorization.
- [ ] Unit: only one refresh occurs for concurrent invocations in a worker.
- [ ] Unit: an in-flight invocation retains its old generation while a later invocation
  observes the new one.
- [ ] Unit: published snapshots reject mutation, do not share capability collections,
  and dispose retired resources only after the final lease exits.
- [ ] Unit: malformed/unavailable candidates preserve the last known good generation and
  expose health telemetry.
- [ ] Unit: incompatible candidates are staged as restart-required and never partially
  hot-activate.
- [ ] Unit: an unchanged rejected-pointer ETag is not repeatedly downloaded or parsed.
- [ ] Unit: generation-scoped MCP resources are retired only after old invocations finish.
- [ ] Fixture scenario: add the next numbered
  `tests/fixtures/config_scenarios/*_storage_backed_content/` case for equivalent local
  and manifest-backed authoring inputs.
- [ ] Integration: Azurite covers initial indexing, ETag no-change, incremental update,
  concurrent publisher conflict, hot activation, and rollback.
- [ ] Integration: two workers with the same pinned boot manifest compute the same startup
  plan while `active.json` changes during startup.
- [ ] Integration: cold start uses a compatible active generation immediately, falls back
  to the boot generation when the active pointer is unavailable, and fails only when the
  boot generation is invalid.
- [ ] Integration: simultaneous publisher updates use `If-Match`; exactly one pointer
  update succeeds and the stale publisher receives a precondition failure.
- [ ] Integration: host metadata before and after a hot-reloadable change is identical.
- [ ] Integration: a multi-Activity workflow spanning a compatible generation change
  reauthorizes each Activity against boot-static policy while using the correct current
  MCP/tool configuration without shared state.
- [ ] Integration: a structural activation plus restart produces the expected updated
  Functions metadata.
- [ ] Regression: all existing local discovery, registration, delegation, workflow, and
  sample-start tests remain green.
- [ ] Full gate: `ruff`, strict `mypy`, and CI-equivalent `pytest` with branch coverage.

## 7. Docs impact

- [ ] `docs/architecture.md` - add acquisition, content snapshots, startup plans,
  refresh behavior, and the startup/execution boundary.
- [ ] `docs/front-matter-spec.md` - state that content location does not change
  front-matter semantics and link to the operator guide.
- [ ] `docs/triggers.md` - explain which content changes require trigger re-indexing.
- [ ] `docs/index.md` - mention independently deployable agent content.
- [ ] `docs/getting-started.md` - add opt-in migration and publish workflow.
- [ ] New `docs/storage-backed-content.md` operator guide - publishing, activation,
  rollback, consistency, limits, RBAC, transaction/latency considerations, and
  troubleshooting.
- [ ] `docs/observability.md` - document `content.acquire`, `content.refresh`, and
  `af.content.*` attributes.
- [ ] `README.md` - add the Blob mode quickstart and compatibility table.
- [ ] Sample infrastructure - provision the dedicated container and least-privilege
  publisher/runtime access where practical.
- [ ] Generated schema reference - no update unless implementation adds schema fields;
  bootstrap environment settings alone do not modify `config/schema.py`.

## 8. Status & sign-off

- **Architecture review (phase 2):** Independent `Explore` review completed on
  2026-09-21 with verdict **approve with revisions**. The draft now defines an immutable
  boot baseline, explicit `ContentManager` ownership, generation-scoped MCP disposal,
  manifest verification and limits, startup-static workflow/delegation topology,
  scale-out/concurrency tests, container-scoped RBAC guidance, and concrete telemetry.
  A second independent review verified every blocking finding as resolved and returned
  **approve with minor follow-ups**; its manifest-reference, runtime-authority,
  active-pointer atomicity, and multi-Activity test clarifications are incorporated here.
  Human decisions in Section 5 remain open.
- **Human sign-off:** Pending. Set `status: Finalized` only after decisions and delivery
  boundaries are approved.
