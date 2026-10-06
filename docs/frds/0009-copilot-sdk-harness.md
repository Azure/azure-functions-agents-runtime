---
frd: 0009
title: Copilot SDK agent harness
status: Finalized
author: larohra
created: 2026-09-28
updated: 2026-10-05
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
previews with explicit capability checks. Today's shipped runtime still keeps
MAF as the default and limits Copilot to an internal local-only preview. For
Copilot-owned sessions, the host provides only a thin filesystem adapter and
storage selection boundary; the SDK owns continuation, compaction, recovery,
file contents, and format compatibility.

## 2. Motivation / problem

The application owns discovery, configuration, registration, authorization,
delegation policy, and Dynamic Workflows; MAF currently supplies the agent loop,
tool wrappers, model-client integration, and message/context management.
Replacing only `ClientManager` cannot replace that harness: `runner.py`,
discovery, history providers, and observability also contain MAF-specific seams.
The desired replacement uses Copilot's native loop and native session persistence
without recreating session protocols in the host or changing how an application
defines agents.

This specification describes intended behavior for the bounded Copilot preview.
The current [architecture](../architecture.md) and
[authoring specification](../front-matter-spec.md) document today's runtime;
this FRD defines the future Copilot-path contracts that preserve those product
surfaces while narrowing the host's persistence responsibility. The preview
described here is the current bounded implementation path toward that end state.

## 3. Goals / Non-goals

**Goals**

- Preserve discover -> translate -> register -> lazy execute, with resolved
  configuration and capabilities remaining the authority for every execution role.
- Preserve model/provider selection, timeouts, authoring inheritance, HTTP/SSE
  contracts, local tools, MCP, scoped skills, structured responses, and safe telemetry.
- Preserve direct agents, chat-time delegation, Workflow Sub Agents, Dynamic
  Workflow management/Activities, `web_request`, and ACA Dynamic Sessions `execute_python`.
- Support Copilot-owned session files through a thin SessionFs adapter backed by
  Blob when configured or local files otherwise, isolated by shared readable
  app/agent/session identity and opaque SDK-relative paths.
- Reject unsupported configured behavior explicitly; never silently remove a
  capability, weaken its policy, or fall back to MAF after selecting Copilot.

**Non-goals**

- A permanent multi-harness extension framework, per-agent selection, or revival
  of legacy `runtime:` frontmatter as a harness selector.
- A host-owned session format, summarizer, continuation protocol, recovery
  controller, or format-compatibility layer.
- Durable mid-turn checkpoints, custom durable `ask_human`, empty `send_messages`
  continuation, exactly-once effects, or general Brain/Hands dispatch.
- Replacing the existing Durable Functions workflow engine. Its current features
  are required parity, not part of the excluded durable-agent-loop work.

## 4. Proposed design

Use a thin internal Copilot adapter at the execution boundary. Reuse
`ResolvedAgent`, `AgentCapabilities`, `AgentCatalog`, `AgentResult`, and existing
workflow types; do not expose a harness registry or a new public plugin protocol.
Tool and MCP adaptation remain execution concerns. The persistence interface
does not change shared tool definitions or discovery contracts.

**Internal containment.** Keep the public `runner.py` exports and signatures.
Place common app binding and request contracts, resource-cleanup plumbing, and
neutral storage settings under `harness/`; contain MAF-specific execution and history in
`harness/agent_framework/`, and Copilot-specific execution, providers, and
SessionFs in `harness/copilot_sdk/`. Shared boundaries do not import either
harness's persistence implementation. Implementation-specific execution and
persistence SDK types stay inside their selected implementation, without a new
public plugin protocol or changes to tool, model, discovery, role, or preview
contracts. Private host identifiers name Copilot explicitly; SDK-owned APIs and
persisted path segments stay unchanged.
App bindings, requests, and harness vocabularies each have one canonical shared
definition. Both implementations consume the same result, usage, and session-lock
contracts rather than duplicate them. The execution facade is a private
three-operation composition: one neutral backend contract exposing only
`run_agent`, `run_agent_stream`, and `run_leaf_agent_task`; one concrete
app-bound `AgentRunner` facade that holds the already selected implementation;
and one implementation per harness under `harness/agent_framework/` and
`harness/copilot_sdk/`. No separate history or lifecycle interface is introduced.

| Pipeline stage | Modules / boundaries | Required responsibility |
| --- | --- | --- |
| discover | `discovery/tools.py`, `discovery/mcp.py`, `discovery/skills.py`, `_function_tool.py` | Keep project inventories and discovery rules; separate framework wrapping from author intent. Do not run inference or launch the native runtime during discovery. |
| translate | `config/schema.py`, `config/merge.py`, `config/validation.py` | Preserve typed composition, inheritance/null semantics, and effective capability validation. Interpret no new harness selector in agent files. |
| compose/register | `app.py`, `registration/capabilities.py`, `registration/catalog.py`, `registration/_handlers.py`, `registration/endpoints.py`, `registration/triggers.py` | Resolve the app's preview choice before harness-specific bootstrap; validate the complete catalog before FunctionApp mutation; pass resolved values to lazy handlers. Keep Azure registration and inbound authorization here. |
| execute | Public `runner.py`, `client_manager.py`, common `harness/` binding, app-bound private runner composition, implementations under `harness/agent_framework/` and `harness/copilot_sdk/` | Create/resume sessions, bind approved tools, enforce deadlines, and translate events/results. Public runner entry points remain compatibility shims that forward to one already selected three-method implementation per app binding. `ClientManager` remains provider access, not the agent loop, tool dispatcher, or session manager. |
| persist | `_agent_identity.py`, `_session_id.py`, shared identity validation and storage settings under `harness/`, selected harness's history or SessionFs implementation | Route persistence only through the selected harness. Preserve validation and path-containment rules, reuse the shared readable agent ID for native paths, and treat Copilot session bytes as opaque SDK-owned files. |
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
The immutable binding may hold an app-owned mutable resource holder; that holder
does not own conversation semantics or acquire SDK/storage resources at selection.

Registration accepts that context explicitly and captures it in HTTP, streaming,
MCP, non-HTTP trigger, and history-handler closures. `register_workflow_runtime()`
passes the same context to `register_workflows()`, whose Activity closures retain
it. The runner entry points `run_agent()`, `run_agent_stream()`, and
`run_leaf_agent_task()` accept the context as a keyword argument and dispatch
through its already-selected adapter. Delegate-tool closures pass their parent's
context into leaf execution. None of these paths independently selects a harness
or rereads the flag.

The concrete runner facade is cached once per bound app context, alongside the
existing lazy native-runtime resource cell, so public runner helpers do not
repeat per-operation harness branching after selection. Creating that facade does
not itself acquire native processes, credentials, history providers, or session
filesystems. Those remain lazy backend responsibilities, and unsupported Copilot
configuration must still fail before native/provider acquisition. Direct,
streaming, and leaf execution share that binding. Public helper signatures, deadline
timing, SSE error boundaries, and cleanup guarantees stay unchanged.

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
still need the application's normal Durable compatibility/versioning practices.

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
not by itself expand today's Blob-only transcript endpoint. The Copilot path
needs a supported read-only native projection, not MAF deserialization or host
interpretation of SDK-owned files. That projection, especially after compaction,
remains an open design question.

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
lazily initialized SDK client owned by each frozen app binding in a warm
Functions worker and reused across invocations with isolated sessions. Distinct
app bindings, including bindings for the same root, do not share that client.
Concurrent initialization must not launch duplicate runtimes for one binding.
The runtime startup path uses a single startup lock and reuses one shared
credential owner per app binding for provider and storage authentication.
Request cancellation, session failure, or adapter failure must not close the
shared client or poison unrelated sessions; worker shutdown must release its
client/process and credential owner through the explicit async shutdown path.
Initialization, process-local session-lock waits, execution, and storage
operations are bounded by the request deadline.

The standalone first-use cache retains app bindings, not conversation state.
A shared shutdown callback set may retain only acquired resource owners so all
can be closed without importing unselected implementations. Resource ownership
is intentionally simplified: the selected app binding owns only its lazy SDK
client and shared credential. The request cleanup scope owns its `SessionFs`
adapter directly, keeping the adapter open until session disconnect cleanup
finishes and then closing it before the request returns, even on cancellation
or failure. The runtime does not retain open adapter handles across requests,
does not keep a failed-client retry registry, and does not install a process-exit
`atexit` fallback. Supported Azure Functions handlers reuse the worker's active
event loop. Standalone callers still shut down acquired preview resources
before closing that loop, and reuse across separately created loops is not
diagnosed or supported.

Startup failures attempt bounded immediate cleanup and report the original
startup error as authoritative. Request cancellation or turn failure likewise
keeps the original cancellation/failure authoritative over abort, disconnect,
or adapter-close cleanup errors. Explicit shutdown attempts graceful client stop
with a bounded `force_stop` fallback, attempts credential cleanup even if client
cleanup fails, reports cleanup failures instead of deferring retry to a later
registry, and clears cached client/credential handles so a stopped or closing
resource is never reused. This is a deliberate tradeoff: failed cleanup is
reported immediately, and the runtime does not promise later retry or
process-exit cleanup if the host does not await shutdown.

No embedded FFI dependency is proposed. A compatible Python SDK/native-runtime/
protocol combination, deployment asset acquisition, and supported Functions
hosting behavior need explicit qualification. Do not download a runtime at
invocation time.

### 4.5 Native session persistence boundary

The Copilot path implements SessionFs, the SDK's filesystem callback interface.
The host provides correct filesystem operations, path containment, file metadata,
and SDK-shaped errors. The SDK owns continuation semantics, compaction, recovery,
file contents, file formats, and format compatibility across versions. The host
does not add its own session format, state machine, recovery logic, or native
format-version checks.

**Backend selection and configuration.** There is no new storage app setting.
Reuse the existing `AzureWebJobsStorage` connection string or
`AzureWebJobsStorage__blobServiceUri`, together with the current storage-specific
identity and container behavior. Select Blob whenever either Blob configuration
path is configured; select local files only when neither is configured.
Configured Blob errors surface as errors; there is no auth/network failure
fallback to local storage and no deployed-versus-local environment heuristic.
Shared app/configuration/registration logic routes persistence through the
selected harness. The Copilot path constructs, uses, and closes only its
SessionFs adapter; the MAF path constructs, uses, and closes only its existing
history provider. Do not import, initialize, probe, or clean up the opposite
harness's persistence implementation.

**Identity and path construction.** Store ordinary local files or individual
blobs at `copilot-native/{agent_id}/{session_id}/{sdk_relative_path}`.
Reuse `_agent_identity.agent_id(slug)` unchanged: its readable result already
includes the trimmed, lower-case `WEBSITE_SITE_NAME` (or `local` when blank or
unset) and canonical agent slug. Owner and deployment metadata do not affect
the ID. Consume this helper directly rather than freezing a separate identity
prefix or duplicating its normalization. Do not add another app-identity segment
or a separate hash/version scheme. Preserve existing session-ID validation and
path containment. Store every SDK-requested file without a filename whitelist
or interpretation of its contents.

**Isolation and concurrency.** Match the current MAF boundary: only one active
turn per `(agent, session)` within a Python process, using process-local
serialization with bounded waiting. Independent sessions remain concurrent.
There is no distributed exclusion, lease/fencing protocol, cross-process OS
lock, or worker-to-worker recovery ownership contract in this feature. Cross-worker
overlap is unsupported and owned by the caller/platform. Blob-backed rename may
require copy/delete and therefore cannot be described as crash-atomic; the
adapter's responsibility is to implement the SDK's required file operations and
surface SDK-shaped errors, not to promote those operations into a host-owned
session-consistency protocol. Adapter operation failures use the SDK filesystem
error contract rather than substituting empty or absent data.

**SDK integration contract.** Preserve all SDK-requested files and operations
opaquely: read, write, append, exists, stat, directory listing (including entry
types), mkdir, remove, and rename must conform to the SDK's SessionFs contract.
The host does not interpret compaction artifacts or replace SDK session
behavior. Native compaction uses the SDK defaults.

### 4.6 Configuration and extension compatibility

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
continue to behave as before when the flag is off. Provider/model precedence,
auth behavior, and the built-in-manager-only Copilot preview contract remain as
approved in section 4.8.1.

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

Unsupported capability, invalid configuration, adapter-path validation, backend
configuration, and filesystem/provider failures must be diagnosable without
sensitive payloads. Map them through existing HTTP/MCP error envelopes and
terminal SSE `error`, not a new success-shaped response or an automatic MAF
fallback. Cancellation stays cancellation. Already-dispatched tool effects may
remain after an unsuccessful turn; the feature does not claim transactional or
exactly-once execution.

### 4.8 Preview limits and approved provider contract

The Copilot preview remains local-only and requires a single Functions worker.
Azure Functions hosting, structured-response parity, MCP,
general scoped skills, full system-tool parity, and cross-worker
session overlap are unsupported in this preview unless separately qualified.
Local HTTP SSE, declared delegates, Workflow Sub Agents, and Dynamic Workflow
management use the existing host contracts. Leaves use disposable local
SessionFs trees even when primary sessions select Blob, and never acquire a
persistent session lock. SSE emits its session after native/catalog acceptance
and `done` only after a verified completed turn and disconnect; resume disables
pending native work and verifies completed history before sending another prompt.
The SDK still owns opaque file formats, compaction, and recovery.
Unsupported capabilities fail explicitly without fallback. Configured output caps
are rejected because this path does not yet expose a verified provider generation
cap mapping.

This FRD records intended product behavior and architecture boundaries. It does
not claim implementation completion, real-service qualification, or production
activation for the persistence redesign in section 4.5.

#### 4.8.1 Architecture-approved provider contract

The provider contract preserves MAF behavior and provider/model precedence,
including explicit/autodetected provider selection and authored/per-agent model
merge and `null` semantics. Storage, history, hosting, and tool behavior remain
bounded by the feature-level contracts above until separately qualified.

Decision: OpenAI, Azure OpenAI (API key or Entra), and Foundry project (Entra)
map to the Copilot SDK's singular `ProviderConfig` using the Responses API, with
settings and auth mode frozen at harness selection and no fallback. Copilot
accepts only the built-in `ClientManager`. The provider mappings, credential
handling, and `ClientManager` rules are documented once in
[architecture.md § Bounded Copilot migration preview](../architecture.md#bounded-copilot-migration-preview).
Azure OpenAI intentionally accepts host-only HTTPS custom domains (for example,
APIM); its Entra scope targets public Azure cloud only, so sovereign clouds are
unsupported.

The preview remains local-only and single-worker. Configured output caps are
rejected rather than silently dropped.

## 5. Decisions log

Rows 1-19 are preserved as historical design record. Decisions 20-24 supersede
the active storage and host-recovery implications of decisions 4, 7, 11, 13,
14, and 15. Those earlier rows remain history, not the current persistence
contract or qualification evidence for the redesigned thin adapter boundary.
Decision 25 supersedes only decision 24's persistence-only organizational
restriction; its selected-persistence isolation and the contracts in decisions
20-23 remain unchanged. Decision 27 extends decision 25 with the private
execution-facade shape and preserves the same lazy backend, selection, and
compatibility contracts.

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
| 14 | Recorded host workspace on resume | Fuzzy suffix matching of recorded paths / protocol v4 native envelopes with virtual workspace aliases | Use protocol v4 native envelopes; persist and alias the creating and current worker host workspace paths to virtual `/workspace` for cross-worker restore. Reject protocol v3 preview envelopes, requiring fresh session IDs, and leave MAF unaffected. Dedicated architecture re-review approved this design conditional on explicit human acknowledgement; that acknowledgement is recorded by this decision. | Human (larohra); architecture re-review approved conditional on acknowledgement | 2026-09-30 |
| 15 | #1335 qualification boundary | Treat mocked/local evidence as sufficient / qualify real Blob continuation and compaction while retaining later gates | Accept the sanitized section 4.8 evidence as completing #1335: real Entra Blob protocol tests, replacement-process tool-result continuation and semantic compacted-summary reuse. Do not claim verbatim arbitrary-token retention, Functions hosting, dual-harness end-to-end qualification or production activation; retain those gates in #1357/#1337. | Human (supplied qualification evidence) | 2026-09-30 |
| 16 | MAF compatibility and Copilot provider boundary | Adjust MAF or precedence / preserve MAF exactly and isolate Copilot SDK types | Keep MAF behavior and provider/model precedence byte-for-byte behaviorally unchanged, including provider selection and authored/per-agent merge/`null` semantics. Use one stable singular pure typed target and lazily construct SDK `ProviderConfig` in `_copilot_providers.py`; do not optionally import the SDK in shared/default-off code | Human (larohra) | 2026-09-29 |
| 17 | Copilot provider mappings | Generic/fallback mapping / explicit matrix | Map OpenAI, Azure OpenAI, and Foundry exactly as section 4.8.1 specifies with Responses for all supported providers; reject unsupported providers/settings without fallback | Human (larohra) | 2026-09-29 |
| 18 | Credential lifecycle | Persist credentials / re-supply and refresh | Freeze provider settings at harness selection, re-supply credentials on resume from that provider object, permit overlapping Entra callbacks that acquire per request through Azure Identity, and exclude credentials from persistence, session metadata, launch arguments, and logs while acknowledging native request memory | Human (larohra) | 2026-09-29 |
| 19 | Custom `ClientManager` migration | Adapt custom managers / built-in only on Copilot | Leave MAF unchanged; on Copilot accept only the exact runtime-created built-in manager, treating an explicitly installed `MAFClientManager()` or any other replacement as MAF-only. Reject replacement before app mutation and recheck before execution. `build_chat_client`-only managers remain MAF-only; this is not a future extension hook | Human (larohra) | 2026-09-29 |
| 20 | Copilot session persistence ownership | Host recovery/state protocol / thin SessionFs adapter | The host provides only filesystem operations, containment, metadata, and SDK-shaped errors. The SDK owns continuation, compaction, recovery, file contents, formats, and format compatibility. No host envelope, completed-turn guarantee, rollback logic, native-state checks, handoff markers, tombstones, or recovery/controller protocol. | Human (larohra) | 2026-10-02 |
| 21 | Persistence backend selection | New Copilot-specific setting or environment heuristic / reuse existing storage configuration | Reuse `AzureWebJobsStorage` connection string or `AzureWebJobsStorage__blobServiceUri` with existing storage-specific identity/container behavior. Select Blob when configured, local only when neither is configured, and never fall back from configured Blob failures to local storage. | Human (larohra) | 2026-10-02 |
| 22 | Native identity and path scheme | New opaque hash scheme / shared readable identity | Use `copilot-native/{agent_id}/{session_id}/{sdk_relative_path}`, with `agent_id` supplied by the shared helper and already containing the app correlation key and canonical slug. Retain session-ID validation and path containment without a separate hash scheme or host format version. | Human (larohra) | 2026-10-02 |
| 23 | Session concurrency boundary | Distributed leases/fencing/OS locks / process-local serialization | Match the current MAF boundary: serialize only same-session turns within a Python process, with bounded waiting and no distributed exclusion. Cross-worker overlap is unsupported and caller-owned. | Human (larohra) | 2026-10-02 |
| 24 | Interface separation scope | Broad execution-interface rewrite / persistence-only boundary | Keep the interface split narrowly about persistence. Shared app/configuration/registration routes through the selected harness; MAF owns its existing history provider, Copilot owns SessionFs. Construct/use/close only the selected persistence adapter, without opposite-harness imports, storage initialization, history probes, or cleanup, and without expanding scope into unrelated tool/model/discovery refactors. | Human (larohra) | 2026-10-02 |
| 25 | Harness organizational containment | Persistence-only separation / contain harness-specific execution and persistence together | Keep common app binding/request contracts, cleanup plumbing, and neutral storage settings under `harness/`; place MAF execution/history in `harness/agent_framework/` and Copilot execution/providers/SessionFs in `harness/copilot_sdk/`. Preserve public runner signatures, app-owned resource lifetimes, selected-persistence isolation, and all existing selection, persistence, role, and preview contracts. This supersedes only decision 24's organizational restriction, not its prohibition on unrelated tool/model/discovery redesign. | Human (larohra) | 2026-10-05 |
| 26 | Shared agent identity authority | Retain owner/deployment correlation / consume the current shared site-qualified helper unchanged | Use `_agent_identity.agent_id(slug)` as the sole authority: trimmed, lower-case `WEBSITE_SITE_NAME` or `local`, followed by canonical slug. Keep `copilot-native/{agent_id}/{session_id}/{sdk_relative_path}` and existing validation/containment; do not cache a separate identity prefix, restore owner/deployment inputs, or add migration/version logic. This supersedes decision 22's correlation-key assumption only; SDK ownership, storage selection and app-resource/provider/settings lifetimes remain unchanged. | Human (larohra) | 2026-10-05 |
| 27 | Bound runner composition | Keep harness branches in public runner helpers / introduce a broad lifecycle or history interface / bind one private three-method execution implementation per app context | Keep `AppHarness` as the once-selected app binding and cache one concrete private `AgentRunner` facade per app context. That facade forwards `run_agent`, `run_agent_stream`, and `run_leaf_agent_task` to one selected backend implementation housed in its harness package, with no additional history or lifecycle interface. Public runner exports remain compatibility shims, inactive backend imports stay lazy, unsupported Copilot stream/leaf methods fail before runtime/provider acquisition, and existing deadlines, SSE error boundaries, cleanup, standalone caching, and app-clone isolation remain unchanged. | Human (larohra) | 2026-10-05 |
| 28 | Local role parity and turn acceptance | Reject streaming/leaves or retain host role contracts | Support local SSE, chat delegates, Workflow Sub Agent Activities, and management tools through the bound runner. Preserve isolated disposable leaves, host tool ordering, cancellation/deadlines, and completed-turn event barriers before continuation and successful SSE completion. This narrows decision 20's no-completed-turn-guarantee clause without introducing a file envelope or recovery controller, and supersedes decision 27's stream/leaf rejection only. | Human (merge requirements) | 2026-10-06 |
| 28 | Runtime owner lifetime | Retain failed-client/filesystem registries and process-exit fallback / bounded app-owned client+credential only, with request-owned adapter cleanup | Keep one lazy reused SDK client and shared credential per app binding behind a startup lock. Remove `_failed_client` retention, runtime-owned filesystem retention, request-deferred adapter registries, and `atexit` cleanup. Startup failures do bounded immediate cleanup and report the original error; request-local abort/disconnect/adapter cleanup preserves original failure or cancellation precedence; explicit async shutdown attempts graceful stop then bounded `force_stop`, also attempts credential cleanup, reports cleanup failures immediately, and clears cached handles so stopped/closing resources are never reused. | Human (larohra) | 2026-10-05 |

## 6. Feature-level acceptance and test plan

Acceptance is behavioral, not an assertion that SDK feature names imply parity.
Extend tests mirroring the affected source modules, with configuration scenarios
under `tests/fixtures/config_scenarios/`. Use real SDK/storage/hosting evidence
where mocks cannot establish process, transport, authentication, or durability.

| Area | Acceptance evidence |
| --- | --- |
| Selection/isolation | Exercise unset, `false`, `0`, `true`, `1`, mixed-case/padded text, empty/whitespace-only/invalid values, multiple app contexts, and standalone entry points. Off starts no Copilot process/download/auth/telemetry; on is uniform across all roles and never falls back. |
| Context propagation/lifetime | Construct two same-root app contexts with different frozen storage settings; assert neither changes on environment mutation nor shares a native client. Delayed handlers, delegates and Activities retain their context. Cover explicit/default standalone contexts and normal Durable replay/routing across worker replacement, without persisting harness selection into orchestration history. |
| Unsupported features | Effective inherited/default-on capabilities and unmapped configuration/extensions fail before provider inference or tool effects. Isolated previews of supported capabilities execute real SDK turns. |
| Authoring/API | Existing precedence/null scenarios, tool `None`/empty semantics, routes/auth, response envelopes, structured-output validation/errors, history projection/degradation/errors/bounds, and SSE ordering/cancellation remain compatible. No native or specialist events leak. |
| Models/extensions | Verify supported providers/Entra refresh, model metadata, disabled provider conversation storage, deadlines, output limits, and explicit custom-manager/tool compatibility, including a manager replaced after composition. MAF hooks remain intact off. |
| Tools/MCP/skills | Cover sync/async, Pydantic, both decorator orders, workflow-only tools, approval options, allowlists, HTTP MCP headers/refresh/errors, lazy scoped skills/resources/scripts, and denied ambient capabilities. Assert callable/effect counts and no unexpected interactive approval gate. |
| Delegation/workflows | Prove fresh same-specialist concurrent sessions, catalog/role isolation, no child SSE, parent cancellation and specialist-local errors, Workflow Sub Agent grants/results, existing management/Activity retry/timeout/authorization, and at-least-once semantics. |
| Role storage/trigger execution | Run a non-HTTP trigger with its generated identity, serialization, logging/error behavior, and direct capabilities. Persistent direct state uses the selected harness's storage path only; delegates and Workflow Sub Agents leave no persistent Copilot session tree and dispose ephemeral state. |
| SessionFs file contract | Exercise exact byte preservation and SDK-visible behavior for read, write, append, exists, stat, directory listing with entry types, mkdir, remove, rename, and documented file errors on both local and Blob adapters. Preserve all SDK-requested files opaquely rather than host-specific file whitelists or content interpretation. |
| Identity/path isolation | Verify native paths consume the unchanged shared site-qualified agent ID exactly once, followed by validated session ID and SDK-relative path. Cover trimmed/mixed-case/blank site names, ignored owner/deployment values, local fallback and containment without adding identity hashes or cached prefixes. |
| Backend configuration/errors | Verify Blob selection from existing storage configuration, local selection only when no Blob configuration is present, reuse of existing identity/container behavior, and explicit surfacing of Blob auth/network/configuration failures without fallback to local storage. |
| Persistence boundary isolation | Verify that only the selected harness's persistence implementation is imported, initialized, exercised, and closed. The Copilot path must not probe or clean up MAF history storage, and the MAF path must not initialize Copilot SessionFs. |
| Harness containment | Verify unchanged public runner exports/signatures and shared result, usage, and lock behavior after containing private execution/persistence implementations. App-bound resources remain isolated, standalone defaults remain cached, shutdown visits only acquired owners, one cached backend facade per app binding reuses the selected implementation without per-operation reselection, and no failed-client/filesystem retry registry or process-exit callback remains. |
| Same-process concurrency | Verify bounded waiting and serialization for concurrent turns targeting the same `(agent, session)` within one Python process, while independent sessions remain concurrent. Do not require distributed exclusion, cross-worker ownership, or OS-level locking for this feature. |
| SDK integration boundary | Exercise real SDK callbacks against the adapter and verify that the host does not interpret native session contents, claim recovery semantics, or impose its own compaction/summary protocol. |
| Hosting/telemetry | Demonstrate supported Functions deployment assets, lazy single-client startup, concurrent isolation, bounded cancellation/shutdown, and no reuse of stopped/closing handles. Verify startup-failure and cancellation cleanup precedence, graceful-stop then force-stop shutdown behavior, reported shutdown failures without deferred retry/process-exit guarantees, and usage/correlation/error accounting with sensitive-data-off behavior in host and native telemetry. |

## 7. Docs impact

Keep documentation aligned with the runtime's implemented behavior:

- `docs/architecture.md` — adapter, execution-context, and persistence boundaries
- `docs/copilot-preview-operations.md` — supported shutdown, cleanup, and qualification boundaries for the preview runtime lifetime
- `docs/front-matter-spec.md` — preserved authoring/configuration contracts and
  explicit incompatible settings
- `docs/observability.md` — native telemetry and error-surface expectations
- `docs/workflows.md` / `docs/triggers.md` — only where execution behavior
  changes are user-visible
- `README.md`, `docs/index.md`, `docs/getting-started.md`, and relevant samples
  — only when supported preview behavior becomes user-facing

## 8. Status & sign-off

- **Status:** Finalized for the approved lifecycle amendment, not a claim of
  implementation completion or production qualification.
- **Architecture review:** Reviewed the persistence-only interface and SDK-owned
  session boundary. App-bound harness selection, Durable lifecycle, and
  provider/model contracts previously approved in decisions 8-10 and 16-19
  remain in force. A separate architecture checkpoint on 2026-10-05 returned
  **APPROVE** for decision 25's organizational containment, including canonical
  binding/request contracts, app-owned resource lifetimes, and selected-persistence
  isolation.
- **Human sign-off:** Laveesh Rohra (`larohra`) approved this revised persistence
  design on 2026-10-02. Decisions 20-24 supersede the earlier storage/recovery
  mechanics from decisions 4, 7, 11, 13, 14, and 15 while preserving those rows
  as historical record.
- **Organizational sign-off:** Laveesh Rohra (`larohra`) approved behavior-preserving
  containment of harness-specific execution and persistence on 2026-10-05.
  Decision 25 records that boundary without changing selection, session ownership,
  storage, role, or preview behavior.
- **Identity sign-off:** Laveesh Rohra (`larohra`) approved the unchanged shared
  site-qualified identity authority on 2026-10-05. Decision 26 replaces the older
  owner/deployment assumption without changing SDK session ownership or storage
  configuration and without authorizing existing-data work.
- **Lifecycle sign-off:** A dedicated architecture checkpoint on 2026-10-05
  returned **APPROVE** for decision 28's bounded runtime lifetime contract:
  app-owned lazy client plus shared credential, request-owned adapter cleanup,
  explicit async shutdown only, preserved failure/cancellation precedence, and
  no failed-handle retry registry or process-exit fallback. Laveesh Rohra
  (`larohra`) had already explicitly approved that behavior contract the same day.
