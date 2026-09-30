---
frd: 0009
title: Copilot SDK agent harness
status: Finalized
author: larohra
created: 2026-09-28
updated: 2026-09-29
issues:
  - https://github.com/Azure/azure-functions-bucees-planning/issues/1332
pull_requests: []
branch: null
---

# FRD 0009 - Copilot SDK agent harness

## 1. Summary

Replace Microsoft Agent Framework (MAF) with the GitHub Copilot Python SDK as
the agent-execution harness, while retaining this runtime's markdown authoring,
Azure Functions surfaces, capability policy, and workflow behavior. Copilot
becomes the sole harness in the end state, not another public plugin. A temporary
app-level preview flag leaves MAF as the default and enables isolated Copilot
previews with explicit capability checks. Direct conversations continue between
completed turns using Copilot-native session state, not converted MAF messages.

## 2. Motivation / problem

The application owns discovery, configuration, registration, authorization,
delegation policy, and Dynamic Workflows; MAF currently supplies the agent loop,
tool wrappers, model-client integration, and message/context management.
Replacing only `ClientManager` cannot replace that harness: `runner.py`,
discovery, history providers, and observability also contain MAF-specific seams.
The desired replacement uses Copilot's native loop and compaction without
recreating either in the host or changing how an application defines agents.

This specification describes intended behavior, not established SDK parity.
Its repository baseline is `main` at
`781ee63d49cee03b167016903faf2c4371b2c04d` (0.1.0b16). The
[architecture](../architecture.md) and
[authoring specification](../front-matter-spec.md) describe that implementation;
the evidence limits in section 4.8 distinguish feasibility from qualification.

## 3. Goals / Non-goals

**Goals**

- Preserve discover -> translate -> register -> lazy execute, with resolved
  configuration and capabilities remaining the authority for every execution role.
- Preserve model/provider selection, timeouts, authoring inheritance, HTTP/SSE
  contracts, local tools, MCP, scoped skills, structured responses, and safe telemetry.
- Preserve direct agents, chat-time delegation, Workflow Sub Agents, Dynamic
  Workflow management/Activities, `web_request`, and ACA Dynamic Sessions `execute_python`.
- Support Blob-backed native session continuation in Azure and file-backed local
  development, isolated by `(agent_slug, session_id)`, including native compaction state.
- Reject unsupported configured behavior explicitly; never silently remove a
  capability, weaken its policy, or fall back to MAF after selecting Copilot.

**Non-goals**

- A permanent multi-harness extension framework, per-agent selection, or revival
  of legacy `runtime:` frontmatter as a harness selector.
- MAF Message JSONL import, transcript injection into fresh native sessions, or
  a host-owned summarizer/parallel compacted-context format.
- Durable mid-turn checkpoints, recovery controllers, custom durable `ask_human`,
  empty `send_messages` continuation, exactly-once effects, or general Brain/Hands dispatch.
- Replacing the existing Durable Functions workflow engine. Its current features
  are required parity, not part of the excluded durable-agent-loop work.

## 4. Proposed design

Use a thin internal Copilot adapter at the execution boundary. Reuse
`ResolvedAgent`, `AgentCapabilities`, `AgentCatalog`, `AgentResult`, and existing
workflow types; do not expose a harness registry or a new public plugin protocol.
Some current capability payloads are MAF objects, so preserving these boundaries
requires changing their internal representation/adaptation, not pretending a
`FunctionTool` or MAF MCP wrapper is already SDK-neutral.

| Pipeline stage | Existing modules | Required responsibility |
| --- | --- | --- |
| discover | `discovery/tools.py`, `discovery/mcp.py`, `discovery/skills.py`, `_function_tool.py` | Keep project inventories and discovery rules; separate framework wrapping from author intent. Do not run inference or launch the native runtime during discovery. |
| translate | `config/schema.py`, `config/merge.py`, `config/validation.py` | Preserve typed composition, inheritance/null semantics, and effective capability validation. Interpret no new harness selector in agent files. |
| compose/register | `app.py`, `registration/capabilities.py`, `registration/catalog.py`, `registration/_handlers.py`, `registration/endpoints.py`, `registration/triggers.py` | Resolve the app's preview choice before harness-specific bootstrap; validate the complete catalog before FunctionApp mutation; pass resolved values to lazy handlers. Keep Azure registration and inbound authorization here. |
| execute | `runner.py`, `client_manager.py`, internal Copilot adapter | Create/resume sessions, bind approved tools, enforce deadlines, and translate events/results. `ClientManager` remains provider access, not the agent loop, tool dispatcher, or session manager. |
| persist | `_history_identity.py`, `_session_id.py`, separate native SessionFs adapter; existing `_blob_history.py`/`_file_history.py` on the MAF path | Reuse identity validation, keep native and MAF storage disjoint, and enforce the completed-turn contract below. |
| cross-cutting | `workflows/*`, `system_tools/*`, `_observability.py` | Preserve workflow authorization/Activities, system-tool policies, correlation, and content controls independently of SDK object types. |

### 4.1 App-level preview selection

The only preview selector is `AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT`:

| Value | Selected harness |
| --- | --- |
| Unset, `false`, `0` | MAF |
| `true`, `1` | Copilot |
| Any other value, including an empty string | Explicit configuration error |

Boolean text is case-insensitive; arbitrary nonempty strings are not truthy.
Proposed normalization trims surrounding whitespace, but preserves whether the
variable is present: an empty or whitespace-only value is invalid, not unset.
The existing `runtime_env_value()` helper collapses that distinction and cannot
be reused without preserving presence.

**Selection boundary and propagation.** The public app-creation boundary,
`create_function_app()`, resolves the flag once before harness-specific bootstrap.
It creates a small immutable internal app execution context containing the
resolved app-root identity and the selected execution adapter. This is a bound
execution interface, not a public harness registry; selecting it does not create
a native client or start inference.

Registration accepts that context explicitly and captures it in HTTP, streaming,
MCP, non-HTTP trigger, and history-handler closures. `register_workflow_runtime()`
passes the same context to `register_workflows()`, whose Activity closures retain
it. The runner entry points `run_agent()`, `run_agent_stream()`, and
`run_leaf_agent_task()` accept the context as a keyword argument and dispatch
through its already-selected adapter. Delegate-tool closures pass their parent's
context into leaf execution. None of these paths independently selects a harness
or rereads the flag.

Each constructed app gets its own context, including two apps constructed from
the same root; there is no process-global "last registered app wins" selection.
Standalone runner calls may supply a context explicitly. If omitted, the shared
resolver creates a default standalone context on first use for that resolved app
root and reuses it; constructing a FunctionApp does not replace that default.
Changing the environment does not mutate any existing context. Callers needing a
new standalone app lifetime create a fresh context through the same resolver.

With the flag off, there is no Copilot native-process launch, runtime download,
authentication, or telemetry bootstrap. Existing MAF client/tool extensions and
MAF observability remain intact. With it on, missing SDK assets, unsupported
configuration, or runtime failures are errors, never reasons to retry under MAF.
All roles served by one initialized app instance use its selected harness; a
delegate cannot switch away from its parent's context. This is an app-instance
guarantee, not a promise to pin a persisted workflow across deployments.

**Durable lifecycle.** Follow the app's existing Durable Functions deployment,
replay, retry, and version-routing behavior. A worker restart does not itself
terminate persisted workflows: completed Activity results can be replayed, and
pending or redelivered Activities can execute on a replacement worker. An Activity
uses the execution context captured by the app instance serving it. If deployment
routing sends later work to an app initialized with a different flag value, that
work uses the new selection; we do not introduce a harness-mismatch rejection.
Any coexistence or draining of old and new workers remains platform/deployment
behavior, not something this feature promises to control.

Do not persist the execution context or a new harness selector in orchestration
history, read the flag during orchestrator replay, or add a custom workflow
pinning, cancellation, migration, or restart controller. Preserve existing
Activity contracts and at-least-once semantics. Breaking deployment changes
still need the application's normal Durable compatibility/versioning practices;
native-versus-MAF conversation-history incompatibility remains explicit under
section 4.6. See Durable's [reliability model](https://learn.microsoft.com/en-us/azure/durable-task/common/durable-task-orchestrations#reliability)
and [deployment/versioning guidance](https://learn.microsoft.com/en-us/azure/durable-task/durable-functions/durable-functions-versioning).

An isolated preview may use only capabilities actually supported by its build.
Validate effective configuration, including inherited and default-on features,
before inference or tool effects; reject known incompatibilities during app
composition, or before execution when only invocation-time information is
available. For example, unavailable default-on `web_request` must be explicitly
disabled by the author, not silently omitted by the adapter. No additional
per-capability preview flags are introduced.

This temporary app-level choice deliberately changes the current `AGENTS.md`
MAF-only convention when implemented; it does not restore per-agent/frontmatter
runtime branching.

### 4.2 Authoring and response contracts

Keep `.agent.md`, `agents.config.yaml`, `mcp.json`, `tools/`, and `skills/`
conventions; agent identity, trigger routes, endpoint authentication, and
capability filters do not change. Omission and empty objects inherit; explicit
`null` clears the inherited leaf/subtree, including the whole
`agent_configuration` object. A specialist uses its own resolved configuration,
never its coordinator's overrides. Existing model and timeout precedence,
environment substitution, and standalone runner `None` versus explicit-empty
tool-list semantics remain unchanged.

Preserve `AgentResult` and the built-in chat envelope
`{session_id, response, tool_calls}`, the `x-ms-session-id` header, and each
trigger/MCP surface's existing response shape. SSE remains `data: <JSON>\n\n`
with the currently emitted `session`, `delta`, `intermediate`, `tool_start`,
`tool_end`, `done`, and `error`; correlate tool IDs and emit a start before its
result. The documented/UI-tolerated `message` event is not currently emitted by
the runner and is not a newly required emission.
Reasoning remains optional, not fabricated. SDK/internal events and specialist
text must not leak into the public stream. Errors are terminal, not followed by
a success `done`. Exact token chunk boundaries are not a compatibility promise.

Keep `input_schema`, `response_example`, and `response_schema` semantics and
existing HTTP JSON parsing/schema-validation errors. An SDK structured-output
mechanism must not weaken host validation or imply that every existing endpoint
has the same structured-output behavior.

The read-only history endpoint retains `{messages: [{role, text}], truncated}`,
its 200-message bound, and its projection of nonempty user/assistant text only,
excluding internal/tool entries. Keep empty success for an absent session ID or
unconfigured Blob storage, and invalid-ID errors. Local native persistence does
not by itself expand today's Blob-only transcript endpoint. With history
storage configured, other-harness-only state is an explicit incompatibility
error; corruption must not become an empty-success transcript. The Copilot path
needs a supported read-only native projection, not MAF deserialization or a
replay input to the model. That projection, especially after compaction, remains
an open design question.

### 4.3 Models, tools, and execution roles

Retain configured OpenAI, Azure OpenAI, and Foundry access through BYOK and
refreshable Entra credentials where applicable, including model/deployment
precedence and authoritative `InferenceTarget` metadata. Do not silently use
ambient Copilot login or an interactive login flow instead of the configured
provider. Credential/token failures surface explicitly; tokens stay out of logs.
Provider-side conversation storage remains disabled (today's `store=false`
behavior); a retained provider conversation ID must not become another
continuation authority. The supported SDK/provider mapping, including that
setting and custom-manager compatibility, requires the decisions in section 4.8.

| Role/capability | Required behavior under Copilot |
| --- | --- |
| Direct agent | Its own instructions, resolved model/configuration, filtered tools/MCP/skills, allowed system tools, workflow management, and declared delegates; persistent native history when continuing a session. |
| Non-HTTP trigger (direct role) | Keep trigger serialization, fresh runtime-generated session identity, allowed direct-role capabilities, and existing fire-and-forget completion/error behavior. Native execution does not add a trigger continuation API. |
| Chat-time `delegate_<slug>` | Host-named custom tool with the existing `task: str` schema and immutable catalog lookup. Each call gets a fresh isolated specialist session with its own instructions/model/configuration, static tools/MCP/skills and `web_request`; no parent history, persistent conversation, sandbox, workflow-management tools, or nested delegation. |
| Delegation concurrency/errors | Concurrent calls to the same specialist remain independent and may overlap. Only the final result returns through the coordinator's tool result; no specialist SSE. Specialist-local failures/timeouts remain sanitized recoverable tool failures; parent cancellation propagates. The deadline is bounded by both the specialist timeout and the parent's remaining time. |
| Workflow Sub Agent | A fresh leaf execution in an existing Durable Activity, using the specialist's allowed static capabilities and `web_request`, with no parent history, nested delegates, request sandbox, or workflow tools. Preserve `{agent, text}`, policy authorization, failure/cancellation, and at-least-once Activity semantics. |
| Dynamic Workflows | Keep independent `workflows.subagents` grants, management tools, complete handler catalog, per-agent policy, Activity reauthorization, decorator-owned retry/timeout behavior, and existing orchestration/status contracts. |

Plain sync/async functions, runtime `@tool(schema=...)` with Pydantic validation,
and `@workflow_tool` discovery/decorator-order semantics remain supported.
Invalid arguments must fail before the callable runs; a sync result or awaitable
must be handled once, with existing names, descriptions, and return semantics.
Raw MAF `FunctionTool` features and MAF-specific options passed through the
runtime's `@tool(**kwargs)` cannot be declared compatible merely by extracting a
callable; preserve them on the flag-off path and reject unmapped features in
Copilot previews.

Preserve remote HTTP MCP configuration, per-server tool allowlists, headers,
Entra token refresh, and per-agent filtering. SDK support for additional MCP
transports does not expand the product's supported authoring surface. Skills stay
limited to resolved paths, lazily loading instructions/resources/scripts with
the existing autonomous approval behavior; workflow-only runtime guidance must
not become a global skill. Native ambient skills, shell/file tools, planning,
memory, search, or native `task` delegation must not bypass this capability set.
Host-authorized autonomous execution must not acquire an interactive SDK
approval gate. Preserve authored approval semantics where supported; reject
unmapped approval options instead of granting blanket native permissions.

Keep the host `web_request` implementation's default, disable/exclude behavior,
SSRF controls, budgets, and error shape. Keep ACA Dynamic Sessions
`execute_python` endpoint/authentication/session scoping and result/error behavior;
do not replace remote execution with a local SDK shell or code interpreter.

### 4.4 Native runtime lifetime

The proposed default is the external native Rust runtime over stdio, with one
lazily initialized, process-long SDK client per frozen app/storage context in a
Functions worker, reused across invocations with isolated sessions. Concurrent
initialization must not launch duplicate runtimes for the same context.
Request cancellation must not close the shared client or poison unrelated
sessions; worker shutdown must release its client/process.
Initialization, ownership waits, execution, and final persistence acknowledgments
are bounded by the request deadline.

No embedded FFI dependency is proposed. A compatible Python SDK/native-runtime/
protocol combination, deployment asset acquisition, and supported Functions
hosting behavior need explicit qualification; the assessment version is not a
production pin or permission to download a runtime at invocation time.

### 4.5 Native session continuation and persistence

Continuation means a later user turn resumes the same native conversation
**after a completed turn**, including after replacement of both Python worker
and native runtime. It does not mean resuming an interrupted tool/model operation.
Use the SDK's SessionFs seam with Blob Storage in Azure and a local-development
filesystem implementation. `AZURE_FUNCTIONS_AGENTS_COPILOT_SESSION_STORAGE`
accepts only `local` or `blob` (trimmed, case-insensitive; an empty/invalid
value fails). When unset, select `local` without `WEBSITE_INSTANCE_ID` and
`blob` with it. Reject `local` on a deployed instance. Deployed or explicit
`blob` requires the existing `AzureWebJobsStorage` connection string or
`AzureWebJobsStorage__blobServiceUri` (with storage-specific identity precedence);
missing settings or any Blob failure is an error, never a local-disk fallback.
Resolve and freeze mode, account/credential selection, container and app
namespace in the immutable `AppHarness`/app context, not per request. Include
that complete configuration (secrets represented by a digest, never raw keys)
in the native client identity: two same-root contexts with different settings
must not reuse one SDK connection. Off-path MAF selection remains unchanged.

Persistent native state applies to direct runs, including the fresh IDs used by
non-HTTP triggers, whose current MAF path also writes history. This preserves
that behavior but costs a native file tree, not one JSONL transcript, per trigger
invocation. Retention remains customer-controlled; no automatic retention limit
or new retention API is introduced. Retain/delete a session as a complete unit,
never prune live compaction references. Delegates and Workflow Sub Agents instead
use fresh ephemeral native storage, disposed at call end; they do not populate
the persistent namespace or require its completed-turn storage barrier.

Use a separate, versioned native namespace keyed by validated logical
`(agent_slug, session_id)`. In the existing session container and beneath the
local session directory use the state path
`copilot-native/v1/{app_key}/{agent_key}/{session_key}/state.json`.
Derive each opaque key as an unpadded URL-safe Base64 encoding of SHA-256 over
UTF-8 `copilot-native/v1:<kind>\0<identity>` with fixed `a_`, `g_`, or `s_`
prefix for `kind=a`, `g`, or `s`, respectively; never interpolate raw identities
into native paths.
For deployed app identity, use `WEBSITE_SITE_NAME`, NUL and
`WEBSITE_SLOT_NAME` (default `production`; fail if the site name is missing);
locally use the resolved app root. Never use a worker-specific ID. Validate
both logical components before encoding and retain them in envelope identity
fields to detect mismatches. Canonicalize SDK paths, including absolute virtual
paths, and reject escapes, invalid components, and local symlink traversal.
Persist **all** SDK-owned files: journals, metadata, workspace files, compaction
checkpoints/references and their targets. Do not whitelist only `events.jsonl`,
edit journal records, or serialize native state as MAF `Message` JSONL.

Only one turn may execute at a time for a given agent/session. Concurrent requests
must wait within their deadline or fail clearly. A failed startup that neither
began a turn nor damaged session state must leave the previous conversation
usable. If an interrupted turn cannot be continued safely, return an explicit
error rather than silently resetting the conversation or replaying work.

The storage guarantees below are the human-approved contract, not claims about
the demonstrated Blob adapter.

| Concern | Required contract |
| --- | --- |
| Ownership | Enforce single-active-turn isolation across workers, not only within one process. Ownership protection must prevent stale owners from mutating session state; independent sessions remain concurrent. |
| Lost ownership | Fence stale writes, deny new dispatch, and cancel the affected turn; never let an old owner publish completion after a replacement owner proceeds. Cancellation cannot undo an already-started external effect. |
| Filesystem operations | Qualify every operation used by the selected native runtime, including append, replacement, rename, delete, listing, and missing-file behavior. Reads see acknowledged writes. Rename emulation must be crash-recoverable and protected from concurrent readers/writers; Blob is not assumed to provide POSIX rename. |
| Acknowledgment | A successful persistent SessionFs mutation acknowledges durable storage, not a queued upload. A successful persistent turn requires acknowledged native state and all references, including background compaction writes. The exact SDK quiescence/flush signal and storage barrier must be established. |
| Completion | Non-streaming success or SSE `done` means the turn finished and the native state required for continuation is durably acknowledged. Earlier stream deltas are provisional. This is a completed-turn guarantee, not per-model/tool checkpoints or a recovery controller. |
| Failure/interruption | Storage/ownership failures abort rather than become an ordinary model-visible tool result that permits continued inference. Uncertain turn progress, corruption, missing referenced state, or an unsupported format must fail explicitly on restore, not reset, partially restore, or automatically replay tools. A known-safe startup failure must not invalidate the last completed conversation. |
| Restore | A clean worker reopens the complete acknowledged native state and accepts the next user prompt without host transcript injection. No automatic continuation of pending work, no empty `send_messages`, and no exactly-once guarantee. |

The mechanics below are **Agent-proposed for architecture review**; they do
not extend the existing human sign-off.

**Storage schema and file operations.** A session has exactly one mutable
Blob/local-file envelope, `state.json`, containing `schema_version=1`,
`sdk_version="1.0.14"`, `native_version="1.0.85"`, `protocol_version=4`,
`app_key`, validated logical `agent_slug` and `session_id`, `native_session_id`,
monotonic `owner_epoch` and `revision`, `state`
(`empty|preparing|active|ready|uncertain|deleted`),
`handoff_may_have_started` (Boolean), the creating worker's `workspace_path`,
nullable `working` and `completed`
logical SessionFs trees, and `integrity_sha256` over the canonical envelope
excluding that field. Each tree contains canonical-path-keyed `directories`
with `birthtime`/`mtime` and `files` with full UTF-8 `content`, `size_bytes`,
`birthtime` and `mtime`. `empty` and `deleted` have neither tree; `ready` has
`completed` and no `working`; `preparing`/`active`/`uncertain` keep an
independent `working` copy and any prior `completed` tree. Conditionally create
a content-free `empty` envelope for a new ID, then acquire ownership before
any SDK writes. This first-time conditional creation is the sole unleased
write; it carries no SDK file content. An `empty` first-create retry is
allowed; never reset a completed identity.
Resume requires `ready` with a complete `completed` tree. Start a turn by
fenced-copying `completed` (or an empty tree) into `working` and setting
`preparing`; reset the handoff marker on this transition, rollback and
completion.

**Atomicity and ownership.** A per-session async lock serializes every
SessionFs callback, including reads, and every envelope revision. Owner
reads/stat/listing use the in-memory `working` tree during a turn; other
readers project only `completed`. `exists` uses only the already-loaded
owner tree. Under a finite lease on `state.json`, every Blob
write/append/mkdir/remove/rename and state transition replaces the **entire**
envelope with a single-request conditional Put Blob using the current lease ID
and prior ETag, never staged blocks or side blobs. Increment `revision` and
update in-memory state only after acknowledgment.
Azure's strong consistency provides read-after-write; rename is one
replacement, never a multi-blob copy. Cap the serialized envelope at a
qualified single-put/memory budget and reject over-limit mutations before
acknowledgment. Local mode uses the same envelope and serial lock, a stable
data-free sidecar for a deadline-bounded cross-process OS lock across the turn
(never lock the replaceable state inode), and atomic replace plus file/directory
`fsync` on each mutation. A crash exposes either the old or new full tree.
A conflict, ambiguous acknowledgment or lost lease aborts rather than
silently continuing; renewal runs through SDK detach and
the storage barrier. Acquiring a lease advances the fenced owner epoch.
Competing turns wait within their deadline or fail clearly. No SDK state is
written outside the leased object; a stale lease ID cannot upload content,
replace state or publish completion. Never release another owner's lease.

**Handoff and completion.** Create/resume may mutate `working` in `preparing`.
Mark `active` durably before dispatch, then persist
`handoff_may_have_started=true` under the lease before creating any send task
or invoking a send RPC. Separately track in process whether either was actually
created; no unmarked send is permitted. Cancellation, deadline or fault before
any send task/RPC exists, including after `active`, may roll back only after
detaching/disposing any initialized SDK session and proving send/RPC and
callback quiescence plus valid ownership, even if a failed create/resume RPC
returned no handle. Conditionally replace the envelope with `empty` for a
same-ID first-create retry, or `ready` with the unchanged prior `completed`
tree; discard `working`. Qualify SDK cleanup/recreation of a never-dispatched
session with the same deterministic native ID. Once a send task/RPC may have
started, or its absence/quiescence cannot be proved (including after a crash),
retain `active`/mark `uncertain` and fail closed, without replaying tools. A
persisted handoff marker alone is conservative after restart; a live owner
may roll back despite that marker only on proof no send task/RPC was created.
After a verified completed turn, await SDK detach and drain all provider
in-flight callbacks under valid ownership, then conditionally replace the
envelope with `completed=working`, `working=null`, `state=ready` before success.
Qualify that detach prevents further background writes; otherwise require a
supported SDK flush/quiescence signal before shipping.

**Failure barrier.** Expected filesystem result errors such as `ENOENT`,
`EEXIST`, `ENOTEMPTY`, `EISDIR` and `ENOTDIR` are ordinary callback responses:
they report that the requested filesystem operation could not be applied and
do not latch a storage failure. The provider latches and re-raises unexpected
callback exceptions, including path-normalization, validation and in-memory
invariant failures. Only a successfully normalized, authoritative `exists`
lookup may return `true`/`false`; `exists` performs no remote probe. Although
the SDK can convert an `exists` exception to `false`, the orchestration
independently checks the latch and ownership before dispatch, races execution
with subsequent latch/lease loss to abort promptly, and re-checks through
detach, storage barrier and completion, independent of SDK results.
Persistence, lease/ETag, corruption and unexpected callback failures therefore
still fail closed. Load/validate the entire envelope before exposing callbacks;
do not advertise SQLite support. Never allow further inference on a known
storage failure; SDK behavior that defeats this barrier blocks this slice.

**Versions and lifecycle.** Validate the envelope schema, SDK/native/protocol
triple, logical identity, digest, file sizes and state before resume. Only the
pinned triple is compatible with `v1` until a reviewed compatibility rule says
otherwise. Unknown/newer formats, version mismatches, corruption and
`active`/`uncertain`/`deleted` fail explicitly; `preparing` requires fenced,
proven pre-dispatch recovery before retry. There is no migration or partial
restore. Persist native compaction state through the same barrier. Enable
`InfiniteSessionConfig` with its native defaults; do not translate MAF token
limits into native compaction thresholds. Effective non-null
`agent_configuration.agent_framework` compaction settings remain rejected,
while null/unset settings permit native defaults; the separately unsupported
portable output cap remains rejected. Retention is customer-controlled:
for customer-initiated whole-session deletion, acquire the lease/OS lock and
atomically replace the entire envelope with a minimal `deleted` tombstone
(schema, versions, identity, owner epoch, revision, digest; no file content).
Repeat safely if already tombstoned; never reuse that ID. Current stale lease
IDs cannot write after replacement. Blob soft-delete, versioning and backup
retention remain customer-controlled: replacing the live blob does **not**
claim to erase service-retained versions. Document targeted deletion and no
automatic TTL; never prune a live compaction reference independently.

Native compaction alone owns triggering, summarization, and context
transformation. Native compaction references and their targets are part of the
same durability obligation as conversation history. Acceptance requires a real
compact -> complete -> replace both processes -> restore -> follow-up sequence
showing reuse of the saved summary without a replacement compaction LLM call.
Separate passing compaction and uncompacted-restore tests do not prove this.

### 4.6 Compatibility and history break

Existing MAF files and blobs remain untouched. There is no automatic conversion,
import, dual-write, or merge between formats. This slice adds **bidirectional,
metadata-only** existence guards: Copilot checks the scoped MAF file/blob
before creating/resuming, and current MAF checks existence of the scoped native
state file/blob before creating/continuing. If only opposite-harness history
exists for the same logical ID, reject it explicitly; never parse or modify
either format.
Failed/indeterminate metadata probes fail closed, not as "absent". A fresh ID
starts a new conversation. Where both formats already exist, each harness
uses only its own history. Switching back to MAF can continue existing MAF
history only; Copilot-only sessions require new MAF IDs. Older rollback binaries
cannot enforce this guard, so operational rollback requires fresh IDs.

The portable `agent_configuration.max_output_tokens` contract must be enforced
through a verified SDK/provider mapping or rejected as unsupported. The exact
mapping is unresolved. The MAF-specific
`agent_configuration.agent_framework.compaction.max_context_window_tokens`
cannot silently become a Copilot utilization threshold or an ignored field.
Until an explicit compatibility decision is made, a non-null effective
MAF-specific setting is rejected on the Copilot path; clearing it via existing
`null` semantics allows native defaults, not MAF compaction parity.

Likewise, an installed custom `ClientManager` or MAF `FunctionTool` extension
must either have an explicitly supported mapping or fail before inference/tool
effects. Neither a silent default provider nor an incomplete callable adapter is
acceptable. Check the active manager at agent/session construction too:
`set_client_manager()` can replace it after app composition. These extensions
continue to behave as before when the flag is off.

### 4.7 Errors and observability

Preserve runtime spans, provider/model attribution, usage accounting, delegate
error counts, workflow correlation, and system-tool metrics without double
counting host and native events. A specialist failure must remain attributed to
the delegate boundary, not accidentally treated as a successful ordinary tool.
Do not invent token counts or expose hidden model reasoning to fill SDK gaps.

Keep the shared logger, optional exporter behavior, and `ENABLE_SENSITIVE_DATA`
content policy. Native SDK/runtime telemetry must obey the same policy; prompts,
instructions, tool arguments/results, credentials, and native session files must
not escape through a second default-on export path. The flag-off path must not
bootstrap Copilot telemetry.

Unsupported capability, incompatible history, corruption, ownership loss, and
persistence failures must be diagnosable without sensitive payloads. Map them
through existing HTTP/MCP error envelopes and terminal SSE `error`, not a new
success-shaped response or an automatic MAF fallback. Cancellation stays
cancellation. Already-dispatched tool effects may remain after an unsuccessful
turn; the feature does not claim transactional or exactly-once execution.

### 4.8 Qualification evidence and remaining implementation decisions

The supplied 2026-09-25 assessment used SDK revision
`4001c1da7d832c51bad1d38619c1a082af390efb`, runtime `1.0.84-5`, protocol 3.
It reported real local-file and Azure Blob native restores with single-writer
rename recovery, and host-named custom delegation with overlapping same-specialist
calls and no specialist-stream leakage. These establish feasibility, not the
contracts above. Native `custom_agents`/`task` delegation did not match the host
contract. A Functions-shaped Windows transport comparison favored external stdio;
it was not an Azure Functions deployment qualification.

That assessment did not establish distributed ownership, concurrent-reader-safe
rename or compacted cold restore, and one storage-error path allowed inference
before acknowledgment. Issue #1335 subsequently qualified the implemented
storage and compaction slice with sanitized, real-service evidence:

- A 15-test Microsoft Entra-authenticated Blob integration suite used a
  disposable, pre-existing container and passed two-client exclusion, lease
  loss, ETag-interrupted write/rename, cross-client completed-tree restore and
  zero-leftover cleanup cases.
- With SDK `1.0.14`, native runtime `1.0.85` and a real model, a host-free
  tool-using turn completed against Blob. A new Python process and native
  process, using a different worker root, resumed the same state and recalled
  the tool result with zero new tool calls. Protocol 4 advanced revision
  `20 -> 29` and owner epoch `1 -> 2`; the exact test-owned blob was then
  deleted.
- A qualification-only forced threshold produced exactly one native compaction
  start and one successful completion and persisted one checkpoint. A later
  fresh Python/native process using native defaults returned `READY` and
  followed a standing rule present only in the persisted compaction summary,
  with zero new compaction events and unchanged checkpoint, summary and event
  identifiers. Revision advanced `36 -> 45`, owner epoch `2 -> 3`, and the
  exact test-owned blob was then deleted.

The arbitrary opaque nonce and checkpoint filename used during qualification
were intentionally not retained or exposed. This proves semantic reuse of the
persisted native summary, not verbatim retention of arbitrary tokens.
Functions hosting, dual-harness end-to-end qualification, MCP, scoped skills,
public streaming/structured-response parity and content-safe telemetry remain
unqualified. The assessment's mid-turn recovery experiments do not add those
capabilities to this feature's scope.

Sign-off approves the feature-level contracts, not a production SDK pin or
unverified compatibility mappings. The following table records the resolved
#1335 items and the obligations that remain open. Unsupported capabilities must
continue to follow the explicit preview-rejection rules.

| Implementation item | Resolution or evidence required for supported behavior |
| --- | --- |
| SDK/runtime and hosting contract | SDK `1.0.14` / native `1.0.85` / protocol 4 are qualified for the host-free Blob continuation flow above. Deployment acquisition, dependency coexistence, provider `store=false`, worker lifecycle and target Functions hosting (including Linux/Flex) remain for #1357. The earlier assessment SHA is not a production pin. |
| Native storage protocol | Section 4.5's lease-fenced envelope passed the real Entra Blob integration suite described above, including two-client exclusion, lease loss, interrupted conditional replacement/rename and cross-client restore. Expected filesystem result errors remain ordinary callback results; actual persistence, lease/ETag, corruption and unexpected callback failures fail closed. Service-retained versions, long-running/large-session limits and deployed multi-worker behavior are not claimed. |
| Native continuation and presentation | This slice proves real tool-result continuation and compacted semantic-summary reuse across replacement Python/native processes and worker roots. A supported native history projection remains a later parity item and must not become a second execution-state authority. |
| Configuration compatibility | MAF-specific compaction remains rejected when effective/non-null; null/unset selects native defaults without threshold mapping. The portable output-limit mapping remains unresolved and the preview continues to reject it. |
| Extension compatibility | Define the supported custom `ClientManager` contract and MAF `FunctionTool` conversion boundary, including authored decorator kwargs, approval semantics, unsupported hooks/options, and construction-time validation. Preserve MAF extensions with the flag off. |

### 4.9 Delivery plan

Each implementation slice carries its own tests and accurate behavior docs;
later slices do not excuse a failing gate or weaken the default-off MAF path.

| Slice | Scope and evidence | Dependency / review focus |
| --- | --- | --- |
| [PR #241](https://github.com/Azure/azure-functions-agents-runtime/pull/241) — foundation (merged) | Default-off, local-only SDK stdio, supported non-streaming HTTP/tools, native local resume and explicit preview rejections. | No Blob, compaction or multi-worker qualification; preserve its MAF isolation. |
| Native sessions, Blob persistence and compaction (#1335, complete) | Added the internal native SessionFs module, integration and metadata-only guards; implemented the single-envelope local/Blob protocol, lease-fenced handoff/rollback, completion/tombstone and native defaults. Real Entra Blob, replacement-process tool-result continuation and compacted semantic-summary restore qualification passed as recorded in section 4.8. | Depends on #241. This completion does not qualify Functions hosting or production activation; those remain explicitly in #1357 and #1337. |
| Further parity (separate reviewable PRs) | Native history projection/streaming, MCP/scoped skills, delegation/workflows, system tools, model/output controls, extension mappings and telemetry as each obtains evidence; update its tests/docs with each behavior. | Depends on the relevant qualified foundation/sessions behavior. Keep unsupported features explicitly rejected until their own slice passes review. |

## 5. Decisions log

Dates below record the original scope approvals and proposals. Decision 12
records human sign-off on the feature specification, without claiming that its
remaining implementation decisions or qualification obligations are complete.

| # | Decision | Options considered | Choice | Decided by | Date |
| --- | --- | --- | --- | --- | --- |
| 1 | Harness destination | Permanent plugins / sole Copilot harness | Sole Copilot end state; thin internal adapter, no public harness framework | Human (supplied requirements) | 2026-09-28 |
| 2 | Preview selection | Per-agent controls / app-level opt-in | `AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT`; MAF default, strict Boolean values, one selection per app, no mixed roles or fallback | Human (supplied requirements) | 2026-09-28 |
| 3 | Product boundary | Reduced runtime / preserve existing capabilities | Preserve authoring/API/role/workflow/system-tool contracts; unsupported previews fail explicitly | Human (supplied requirements) | 2026-09-28 |
| 4 | Conversation state | MAF import or host summarizer / native state | New SessionFs namespace, Blob/local backing, completed-turn continuation, native compaction only | Human (supplied requirements) | 2026-09-28 |
| 5 | Recovery scope | Include durable agent loop / completed turns only | Exclude mid-turn controllers/checkpoints, durable human wait, transcript injection, experimental continuation, and exactly-once claims | Human (supplied requirements) | 2026-09-28 |
| 6 | Native transport/lifetime | External stdio / embedded FFI | Propose lazy process-long stdio client per worker, consistent with assessment evidence; version/hosting contract still open | Agent proposal | 2026-09-28 |
| 7 | Storage and compatibility details | Implicit reset/best effort / explicit failure contracts | Propose sections 4.5-4.6; concrete SDK/storage mappings remain open in section 4.8 | Agent proposal | 2026-09-28 |
| 8 | Architecture-review clarifications | Assume SDK parity / retain explicit contracts | Clarify provider-side storage, approval behavior, selector presence, history projection, direct/leaf storage lifetimes, and packaging; retain unresolved mappings in section 4.8 | Agent review/proposal | 2026-09-28 |
| 9 | App execution selection | Repeated environment reads / one bound app execution interface | Select once at the app boundary and propagate the same immutable context through handlers, delegates, and registered Activities; use the same resolution boundary for standalone calls | Human | 2026-09-28 |
| 10 | Workflows across app restarts/deployments | Custom harness pinning/lifecycle rules / existing Durable behavior | Follow Durable replay, retry, worker lifetime, and configured deployment routing; each executing app instance supplies its own selection, with no new persisted harness pin or mismatch rejection | Human | 2026-09-28 |
| 11 | Session startup failures | Prescribe marker sequencing / specify observable behavior | Require one active turn per agent/session, safe retry after a startup failure that did not begin a turn or damage state, and explicit errors for uncertain continuation; leave marker ordering and cleanup to implementation | Human | 2026-09-28 |
| 12 | Feature specification sign-off | Keep In review / finalize the agreed feature contracts | Finalized after approving decision 11; section 4.8 remains an explicit record of unresolved implementation choices and required evidence, not a claim of parity or production readiness | Human (larohra) | 2026-09-28 |
| 13 | Native session delivery and persistence | Shared MAF JSONL or fragmented native Blob tree / one isolated lease-fenced envelope | Propose sections 4.5/4.9's encoded identities, frozen context, single-object local/Blob SessionFs, serialized/latching callbacks, safe pre-handoff rollback, uncertain post-handoff state, metadata-only history guards, tombstone deletion and native compaction after #241. Dedicated architecture-agent re-review APPROVED these mechanics on 2026-09-29 after two REVISE reviews; qualification was still open at this decision and is completed by decision 15. Human-approved contracts/status are unchanged. | Agent proposal; architecture-agent approval | 2026-09-29 |
| 14 | Recorded host workspace on resume | Fuzzy suffix matching of recorded paths / persist the creating worker's workspace and alias it | The runtime resolves the recorded initial working directory through SessionFs before loading events, so the envelope records `workspace_path` and the provider maps that alias plus the current worker's directory to the virtual `/workspace`; unaliased host-qualified paths still fail closed and `protocol_version` moves to 4 so pre-alias envelopes are rejected. Dedicated architecture-agent re-review on 2026-09-30 returned REVISE: host-qualified paths outside the declared aliases were normalized instead of rejected, a failed create/resume RPC could permanently strand a completed tree as `uncertain`, and the multi-worker/deployed startup rejections had been dropped. Those findings are now implemented; explicit human acknowledgement of this decision is still pending | Agent proposal; architecture-agent REVISE addressed, human acknowledgement pending | 2026-09-30 |
| 15 | #1335 qualification boundary | Treat mocked/local evidence as sufficient / qualify real Blob continuation and compaction while retaining later gates | Accept the sanitized section 4.8 evidence as completing #1335: real Entra Blob protocol tests, replacement-process tool-result continuation and semantic compacted-summary reuse. Do not claim verbatim arbitrary-token retention, Functions hosting, dual-harness end-to-end qualification or production activation; retain those gates in #1357/#1337. | Human (supplied qualification evidence) | 2026-09-30 |

## 6. Feature-level acceptance and test plan

Acceptance is behavioral, not an assertion that SDK feature names imply parity.
Extend tests mirroring the affected source modules, with configuration scenarios
under `tests/fixtures/config_scenarios/`. Use real SDK/storage/hosting evidence
where mocks cannot establish process, transport, authentication, or durability.

| Area | Acceptance evidence |
| --- | --- |
| Selection/isolation | Exercise unset, `false`, `0`, `true`, `1`, mixed-case/padded text, empty/whitespace-only/invalid values, multiple app contexts, and standalone entry points. Test local/cloud storage defaults, explicit overrides, missing Blob settings and no fallback. Off starts no Copilot process/download/auth/telemetry; on is uniform across all roles and never falls back. |
| Context propagation/lifetime | Construct two same-root app contexts with different frozen storage settings; assert neither changes on environment mutation nor shares a native client. Delayed handlers, delegates and Activities retain their context. Cover explicit/default standalone contexts and normal Durable replay/routing across worker replacement, without persisting harness selection into orchestration history. |
| Unsupported features | Effective inherited/default-on capabilities and unmapped configuration/extensions fail before provider inference or tool effects. Isolated previews of supported capabilities execute real SDK turns. |
| Authoring/API | Existing precedence/null scenarios, tool `None`/empty semantics, routes/auth, response envelopes, structured-output validation/errors, history projection/degradation/errors/bounds, and SSE ordering/cancellation remain compatible. No native or specialist events leak. |
| Models/extensions | Verify supported providers/Entra refresh, model metadata, disabled provider conversation storage, deadlines, output limits, and explicit custom-manager/tool compatibility, including a manager replaced after composition. MAF hooks remain intact off. |
| Tools/MCP/skills | Cover sync/async, Pydantic, both decorator orders, workflow-only tools, approval options, allowlists, HTTP MCP headers/refresh/errors, lazy scoped skills/resources/scripts, and denied ambient capabilities. Assert callable/effect counts and no unexpected interactive approval gate. |
| Delegation/workflows | Prove fresh same-specialist concurrent sessions, catalog/role isolation, no child SSE, parent cancellation and specialist-local errors, Workflow Sub Agent grants/results, existing management/Activity retry/timeout/authorization, and at-least-once semantics. |
| Role storage/trigger execution | Run a non-HTTP trigger with its generated identity, serialization, logging/error behavior, and direct capabilities. Persistent direct state is isolated; delegates/Workflow Sub Agents leave no persistent native tree and dispose ephemeral state. |
| System tools | Exercise `web_request` defaults/exclusion/SSRF/budgets/errors and real ACA `execute_python` scoping/results without substituting local execution. |
| Completed-turn restore | **#1335 complete.** A real tool-using Blob turn was resumed by fresh Python/native processes under a different worker root; the result was recalled with zero tool calls and protocol/revision/epoch continuity was inspected. Local mirrored coverage remains in the test suite. |
| Compacted restore | **#1335 complete.** Qualification forced native compaction once, persisted one checkpoint, then restored in fresh processes using production native defaults. A rule available only through the saved semantic summary was followed with zero new compaction events and unchanged checkpoint/summary/event identifiers. This does not claim verbatim arbitrary-token retention. |
| Storage failures/concurrency | **#1335 real-Blob items complete.** The 15-test Entra suite covered two-client exclusion, lease loss, ETag-interrupted write/rename, cross-client completed-tree restore and zero leftovers. Expected filesystem result errors (`ENOENT`, `EEXIST`, and peers) are normal callback responses and do not latch storage failure; path/invariant exceptions and persistence, lease/ETag or corruption failures still fail closed. Broader long-running, service-retention and deployed multi-worker qualification remains outside #1335. |
| History break | Metadata-only native and MAF guards reject opposite-only IDs in both directions, without parsing or modifying either format; failed existence probes fail closed. Verify older-binary rollback guidance requires fresh IDs; native history rendering remains a later parity slice. |
| Hosting/telemetry | Demonstrate supported Functions deployment assets, lazy single-client startup, concurrent isolation, bounded cancellation/shutdown, and no orphan native process. Verify usage/correlation/error accounting and sensitive-data-off behavior in host and native telemetry. |

## 7. Docs impact

This specification and its FRD index entry describe intended behavior, not
shipped implementation.
Implementation documentation must change with the behavior it documents:
`docs/architecture.md` for the adapter/lifecycle/storage boundaries;
`docs/front-matter-spec.md` for preserved contracts and explicit incompatible
settings; `docs/observability.md` for native telemetry; `docs/workflows.md` and
`docs/triggers.md` where execution/error behavior changes; and `README.md`,
`docs/index.md`, `docs/getting-started.md`, and relevant samples for supported
preview use and the history break. Update the current MAF-only statements in
`AGENTS.md` when the implementation changes that invariant. Any schema change
requires regenerating the configuration reference and synchronizing examples;
this document does not introduce an unimplemented schema or rewrite runtime docs.

## 8. Status & sign-off

- **Status:** Finalized; the approved feature specification describes intended
  behavior, not delivery order or production qualification. The parent migration
  is not complete or production-qualified.
- **Architecture review:** Dedicated agent review completed on 2026-09-28;
  review clarifications cover app-bound selection, Durable lifecycle, and safe
  startup failure behavior (decisions 8-11).
- **Human sign-off:** Laveesh Rohra (`larohra`), 2026-09-28, explicitly approved
  the behavior-focused session contract and requested sign-off on the FRD
  (decision 12).
- **Implementation design:** Section 4.5 mechanics and section 4.9 slicing
  received dedicated architecture-agent APPROVE on 2026-09-29 after two REVISE
  reviews (decision 13). This approves design, not implementation or production
  qualification; existing human-approved feature contracts remain unchanged.
- **Recorded-workspace alias review (decision 14):** Dedicated architecture-agent
  review on 2026-09-30 returned REVISE. The blocking findings — unbounded
  host-qualified callback paths, permanently stranded `uncertain` sessions after
  a transient create/resume RPC failure, and the removed multi-worker/deployed
  startup rejections — have been implemented, with the SessionFs callback
  surface now limited to the `/workspace` and `/session-state` virtual roots and
  their declared aliases. **Explicit human acknowledgement of decision 14 is
  still pending**; no human sign-off is recorded for it.
- **#1335 qualification:** Complete for real Entra Blob lease/fencing and
  cross-client durability, replacement-process tool-result continuation, and
  compacted semantic-summary cold restore (§4.8 and §6).
- **Remaining release gates:** Deployed-host and dual-harness end-to-end
  qualification remain in #1357; final rollout/production activation remains
  in #1337. This FRD does not claim either.
