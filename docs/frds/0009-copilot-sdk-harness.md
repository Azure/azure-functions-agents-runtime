---
frd: 0009
title: Copilot SDK agent harness
status: Finalized
author: larohra
created: 2026-09-28
updated: 2026-10-16
issues:
  - https://github.com/Azure/azure-functions-bucees-planning/issues/1332
pull_requests: []
branch: null
---

# FRD 0009 - Copilot SDK agent harness

## 1. Summary

Add an internal GitHub Copilot SDK execution path without changing the runtime's
markdown authoring model, Azure Functions registration surfaces, capability
filters, or workflow ownership. Microsoft Agent Framework (MAF) remains the
default production harness. Copilot is a bounded, once-per-app preview selected
only by `AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT`; it never enables per-agent
selection, mixed-harness roles, or fallback after selection.

The design goal is architectural separation, not a public harness plug-in model.
Discovery, merge, validation, and registration remain SDK-neutral. Selected
runners own SDK object creation, provider auth timing, native event translation,
and persistence details. Authored tools remain user-owned code, not a promise of
compatibility with every harness.

## 2. Motivation / problem

The runtime already owns discovery, configuration, capability policy, endpoint
registration, workflow policy, and system-tool boundaries. MAF currently also
owns the agent loop, tool adaptation, chat-client construction, history, and
streaming behavior. Supporting a Copilot path therefore requires more than
swapping `ClientManager`: the execution boundary, capability adaptation,
session/persistence model, and native event handling must all move behind a
selected internal harness boundary.

The runtime must make that change without:

- changing `.agent.md`, `agents.config.yaml`, `mcp.json`, `tools/`, or `skills/`
  authoring;
- re-parsing authored files at execution time;
- turning the host into a second skill loader, session protocol, or MCP policy
  engine; or
- weakening capability authorization or silently dropping authored tools.

## 3. Goals / Non-goals

**Goals**

- Preserve the existing discover -> translate -> register -> execute pipeline,
  with `ResolvedAgent` and `AgentCapabilities` remaining authoritative.
- Keep MAF as the default path; make Copilot an explicit app-level preview.
- Use immutable SDK-neutral descriptors for runtime-owned tools, MCP servers,
  and skill roots, with SDK construction deferred to the selected runner.
- Preserve user-supplied tools without rejecting their SDK origin; let the
  selected SDK and ordinary execution determine compatibility.
- Preserve current authoring and role contracts where supported, and reject
  unsupported Copilot preview behavior explicitly.
- Keep Copilot session persistence as a thin SessionFs boundary: the SDK owns
  session contents, continuation, compaction, and recovery semantics.

**Non-goals**

- A public multi-harness extension framework or per-agent harness selector.
- A host-owned Copilot session format, recovery protocol, compaction policy, or
  skill catalog.
- Host parsing of `SKILL.md` metadata, host-managed skill advertising, or a
  runtime-owned script engine for parity.
- Silent MAF fallback, silent capability drops, cross-SDK tool translation, or
  hidden accommodations for harness-specific callable signatures.

## 4. Proposed design

### 4.1 App-level harness selection

`AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT` is the only selector:

| Value | Selected harness |
| --- | --- |
| Unset, `false`, `0` | MAF |
| `true`, `1` | Copilot preview |
| Any other value, including an empty string | Explicit configuration error |

Selection happens once per app construction and produces a frozen internal
binding (`AppHarness`) that is captured by handlers, delegates, and workflow
closures. Execution entry points receive that bound selection; they do not
re-read environment state, choose a harness per request, or retry under MAF if
Copilot fails. Standalone runner calls use the same resolution boundary.

### 4.2 SDK-neutral capability boundary

Discovery and registration do not import harness SDKs. Runtime-owned capabilities
use immutable SDK-free descriptors:

| Descriptor | Required contents | Excluded from the descriptor |
| --- | --- | --- |
| Tool | name, description, JSON schema, callable, approval mode | SDK tool objects, harness-only keyword passthrough |
| MCP server | name, URL, transport, static headers, tool filter, optional Entra scope/client ID | SDK transport/client objects, live tokens |
| Skill | canonical candidate directory path, directory-name identity | parsed `SKILL.md`, authored description, loaded content |

One concrete runner is cached per bound app context. Its execution, configuration
qualification, default model, history support, and observability policies share
one internal runner contract; there is no separate adapter strategy or forwarding
facade. The frozen app binding captures a construction callable and owns a fresh
guard/cache cell, including for `dataclasses.replace` clones. Creating the runner
does not import an SDK or acquire native processes, credentials, history
providers, or session filesystems. Those remain lazy execution responsibilities,
and unsupported Copilot configuration must still fail before native/provider acquisition.
Direct, streaming, and leaf execution share that binding. Public helper
signatures, deadline timing, SSE error boundaries, and cleanup guarantees stay
unchanged.

`HarnessRequest` carries runtime descriptors plus scalar execution settings.

Selected execution implementations own SDK construction:

- `harness/agent_framework/*` maps descriptors to MAF tools, MCP wrappers, and
  `SkillsProvider.from_paths`.
- `harness/copilot_sdk/*` maps descriptors to Copilot custom tools, MCP server
  configuration, and skill-directory configuration.

The selected runner also owns auth materialization timing, native event
handling, SDK session lifecycle, and SDK-specific result conversion.

### 4.3 Tool authoring contract

The supported runtime authoring surface is intentionally narrow:

- Runtime `@tool(...)` declarations remain supported.
- A `tools/*.py` module may still contribute its first public local function as
  the tool fallback.
- Raw SDK tool objects, SDK decorators, and undecorated direct programmatic
  inputs outside discovery are ignored with a sanitized warning.
- `@tool` accepts only `name`, `description`, `schema`, and `approval_mode`;
  extra keyword arguments are warned and ignored rather than passed through.

### 4.4 Skills boundary

Skill discovery is path-only and shared:

- search each configured root through two child levels;
- stop at directories containing `SKILL.md`;
- use directory basenames as the filter identity; and
- keep full canonical discovered roots only as ownership metadata.

The host does **not** parse `SKILL.md`, project a manual catalog, validate
authored names/descriptions, or claim qualified native skill advertising.
Registration filters candidate paths into immutable approved descriptors.
Each SDK then owns metadata validation, advertising, instruction loading,
duplicate selection, and supported skill-resource/script behavior.

Copilot receives selected paths and disabled directory identities only. MAF
retains its existing `SkillsProvider.from_paths` behavior. An approved parent
does not authorize an excluded nested child; independently supplied overlapping
roots still use most-specific ownership for helper permissions.

### 4.5 MCP and scoped helper behavior

The product MCP surface remains remote HTTP/streamable-HTTP `mcp.json`
configuration plus existing per-agent filtering. No new MCP authoring keys are
introduced.

For `auth.scope`:

- empty/whitespace scope normalizes to `None` after a discovery warning;
- nonempty scope acquires a fresh bearer token at native session create/resume;
- generated `Authorization` overrides any static `Authorization` header; and
- Copilot does not claim mid-turn header refresh parity with MAF.

Configured MCP calls remain noninteractive. Copilot's permission callback may
approve configured MCP requests and scoped skill helper actions only. It does
not become a global approve-all channel.

For native skill helpers, scoped ownership checks remain the source of truth.
Allowed resource reads and approved-script invocations are derived from approved
skill roots; general file reads and general shell commands stay denied.

### 4.6 Execution and result boundaries

The public runner surface stays stable. One cached private concrete runner binds
the already selected harness per app context and implements:

- `run_agent`
- `run_agent_stream`
- `run_leaf_agent_task`
- session-history projection and selected configuration/policy queries

Common contracts stay outside adapters only where they are truly shared:

- `ResolvedAgent` and `AgentCapabilities` remain the product authority.
- `AgentResult` and normalized tool-call accounting remain the product result
  contract.
- Process-local same-session locking and deadline handling remain shared.

Adapter-local behavior stays local:

- MAF usage decoding remains in the MAF adapter.
- Copilot result/event conversion remains in the Copilot adapter.
- Shared code normalizes counts and public result shapes only after those
  adapter-local translations.

Native custom-tool, MCP, and helper evidence comes from the selected adapter's
wrappers or native tool events. Those events must populate existing tool/error
accounting once per native tool call without duplicating wrapper-owned calls.

### 4.7 Persistence and lifecycle boundary

Copilot persistence is a thin SessionFs implementation, not a host session
protocol.

**Storage selection**

- Reuse `AzureWebJobsStorage` or `AzureWebJobsStorage__blobServiceUri`.
- Use Blob when either is configured; use local files only when neither is
  configured.
- Configured Blob failures do not fall back to local files.

**Path scheme**

Store Copilot files under:

`copilot-native/{agent_id}/{session_id}/{sdk_relative_path}`

`agent_id` comes directly from `_agent_identity.agent_id(slug)`; the host does
not invent a second identity layer. Session ID validation and path containment
rules stay shared.

The Copilot preview supports local and Azure Functions execution using the
existing registration surfaces and storage selection. Local HTTP SSE, declared
delegates, Workflow Sub Agents,
Dynamic Workflow management, configured MCP servers, and approved project
skills use the existing host contracts. Workflow-enabled direct roles receive
the packaged `data-driven-workflows` skill through the same approved native
skill-directory path as authored skills; the host keeps the concise workflow
addendum separate and does not read `SKILL.md` into instructions. Leaves use
disposable local SessionFs trees even when primary sessions select Blob, do
not inherit the runtime-only workflow skill, and never acquire a persistent
session lock. SSE emits its session after native/catalog acceptance and `done`
only after a successful, non-interrupted SDK result plus disconnect. If the
SDK reports an interruption or abort during the turn, the host aborts that turn
and returns an error instead of synthesizing success or `done`; the final
disconnect/adapter cleanup still runs. An HTTP client disconnect likewise
cancels only the in-flight turn, aborts it, and leaves the shared native client
usable for later requests. Resume uses SDK-owned continuation without host-side
history scans and rejects live abort/interruption signals observed before
returning a final reply. The SDK still owns opaque file formats, compaction,
and recovery. Unsupported capabilities still fail explicitly when the preview
cannot preserve their semantics. Valid non-empty
`AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT` and
`AZURE_FUNCTIONS_AGENTS_REASONING_SUMMARY` values are captured once in the
immutable Copilot app binding and passed through to every native create/resume
call. Invalid values are explicit configuration errors. The runtime also maps
`agent_configuration.max_output_tokens` into the request-local Copilot
`ProviderConfig.max_output_tokens` override for direct turns, SSE, delegates,
and workflow leaves without mutating the shared app-level provider settings.

**Entrypoint qualification**

Non-HTTP trigger entrypoints use the existing registration, JSON-safe binding
serialization, and shared runner contracts. Each Functions invocation creates
one fresh Copilot session, including a batch delivery. These source-level
contracts do not by themselves qualify a trigger: support claims require a
real Functions host to register the binding, deliver a test event, and verify
the serialized input reaches inference. Track offline unit/component tests,
Core Tools/local-host indexing and binding delivery, and deployed-host
evidence separately. Missing extensions that prevent host indexing are
setup/integration failures, not runtime unsupported-capability verdicts.
Durable activity/orchestration/entity triggers, warm-up triggers,
assistant-skill/MCP triggers, aliases, and dotted connector names remain
outside the supported agent-trigger set under the existing authoring rules.

The intended built-in Debug UI contract is live chat, streaming, and
continuation with the explicit Copilot native session ID. It displays a clear
notice that earlier transcript messages are not restored. The Copilot history
endpoint does not read or project native session files as MAF transcripts; MAF
history behavior remains unchanged. A failed native resume is surfaced as an
error and is never retried as a new session. Debug UI and per-trigger support
claims also require real Functions-host qualification.

The inbound built-in MCP endpoint uses the same bound runner and existing
prompt validation and transport session-ID normalization. Calls without a
transport ID create a native session; the extension-owned `sessionid`
requests continuation, with errors surfaced without retry-as-create (including
when no corresponding native session exists). It is not a public tool
argument for selecting an agent session. Broader endpoint/session API
improvements are outside this contract. The Functions MCP extension owns the transport and
system-key authentication; HTTP endpoint auth settings do not alter that
boundary. Inbound MCP behavior remains unqualified until exercised on a real
Functions host.

**Ownership**

- The SDK owns session contents, continuation, compaction, recovery, and format
  compatibility.
- The host owns filesystem operations, metadata, containment, and SDK-shaped
  errors only.
- Only the selected harness's persistence implementation is imported,
  initialized, and cleaned up.

**Lifecycle**

- Each app binding owns at most one lazy reusable Copilot client and shared
  credential.
- Each request owns its SessionFs adapter.
- Cleanup preserves the original execution or cancellation failure over later
  disconnect, filesystem-close, transport-stop, or credential-cleanup errors.

### 4.8 Compatibility and preview limits

Copilot remains a bounded preview that shares the runtime's authoring,
registration, and policy surfaces while keeping SDK-specific execution local to
the selected harness.

Current supported direction:

- same authoring/config surfaces as MAF;
- once-per-app preview selection;
- direct-role execution with runtime tool/MCP/skill descriptors and user-supplied tools;
- SDK-owned session files behind SessionFs; and
- no silent weakening of policy or validation.

Still outside this FRD's supported preview contract:

- per-agent harness selection or mixed-harness execution in one app;
- host-owned skill metadata/catalog behavior;
- silent compatibility shims for unsupported tool callables or tool options;
- silent provider/client-manager fallback; and
- claims that unqualified native advertising/loading behavior is production-ready.

The approved harness-boundary cleanup removes public custom chat-client
injection. Shared configuration retains portable model/provider settings, while
MAF owns concrete client construction, narrow `ExperimentalWarning` suppression
for `create_harness_agent()`/`FileSystemAgentFileStore` and
`FileHistoryProvider`, instrumentation setup, and history projection. The
common runtime retains neutral observability, registration, and request
behavior. The retired
`agent_configuration.agent_framework.compaction.max_context_window_tokens`
field warns and is ignored; MAF uses its native model-aware default. Failed
requests always return caller-supplied session IDs, and return generated IDs
only after resumability is confirmed, consistently across response surfaces.
`shutdown_runtime()` is the public async cleanup entry point.

### 4.9 Telemetry addendum

This addendum extends the runtime telemetry contract for the Copilot execution
path. The shared run-log sensitive-data gate applies to both harnesses.

- Reuse the active turn's existing session-event subscription and custom-tool
  wrapper with the existing OpenTelemetry provider and Application Insights
  destination. Do not add a subscription, settings, sampling, caps, exporters,
  flush/shutdown calls, or create/resume telemetry; keep
  `continue_pending_work=False`.
- Runtime-owned invocation telemetry is harness-neutral: both MAF and Copilot
  use `agent.run {slug}` spans and the shared `af.agent.*` attributes, with
  `af.agent.harness=maf|copilot` identifying the selected app-level harness. The
  runtime does not create a synthetic harness span and does not add
  Copilot-only GenAI invocation fields when the shared fields carry the same
  stable facts.
- Keep `ASSISTANT_USAGE` as a custom `af.agent.model.usage` event on the owning
  Copilot invocation span. Reuse standard `gen_ai.usage.*` fields only where
  the reported SDK semantics match exactly, keep the existing once-only
  eight-field usage recorder and counters unchanged, and defer inference spans
  and standard GenAI metrics until the runtime has reliable evidence for exact
  model boundaries and attribution.
- Export only allowlisted telemetry. Standard identity, conversation, and tool
  fields come only from stable runtime state, and `ENABLE_SENSITIVE_DATA` remains
  the only content opt-in for prompt/request/response and tool content.
  Non-HTTP and HTTP run-log payloads share the same metadata-by-default shape:
  response content is logged only when sensitive-data capture is enabled, while
  raw tool-call records follow the selected harness policy (MAF permitted,
  Copilot excluded). Credentials, authorization headers, raw envelopes,
  reasoning text, and skill content/paths are never exported.
  **Carve-out:** per Decision 24, runtime-owned Copilot ingress, delegate,
  leaf, and custom-tool spans still record caught Python exception messages and
  stack traces regardless of `ENABLE_SENSITIVE_DATA`, so arbitrary exception
  text may contain sensitive content if application code includes it.
  Content-free diagnostics and best-effort telemetry cleanup must not change
  results, errors, cancellation, or SSE termination.
- Coverage is turn-scoped. Do not fabricate model spans or missing timing,
  claim complete native hierarchy, or promise recovery of lost events.

## 5. Decisions log

This cleanup intentionally consolidates earlier iterative rows into the durable
final contract approved for PR 245. Superseded explorations, review back-and-forth,
and temporary proposals are omitted here; the FRD retains only the decisions that
still govern the feature.

| # | Decision | Options considered | Choice | Decided by | Date |
| --- | --- | --- | --- | --- | --- |
| 1 | Harness selection boundary | per-agent selection / app-level binding | Resolve once per app with `AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT`; MAF stays default, Copilot stays preview, and there is no runtime fallback after selection. | Human (larohra) | 2026-09-28 |
| 2 | Runtime containment model | public plug-in framework / internal harness boundary | Keep public authoring and runner surfaces stable; move SDK-specific execution, auth timing, and persistence behind internal adapters only. | Human (larohra) | 2026-09-28 |
| 3 | Capability contract | harness objects in discovery/registration / SDK-neutral descriptors | Use immutable tool, MCP, and skill descriptors through discovery, merge, validation, registration, and `HarnessRequest`; map them only at the selected adapter boundary. | Human (larohra) | 2026-10-01 |
| 4 | Skill ownership | host metadata/catalog / SDK-owned loading and advertising | Discovery is path-only. The host does not parse `SKILL.md` or maintain a manual catalog; SDKs own metadata validation, advertising, and instruction loading. | Human (larohra) | 2026-10-06 |
| 5 | Tool authoring surface | raw SDK tools / broad `@tool(**kwargs)` passthrough / runtime-owned portable surface | Support runtime `@tool` plus the existing first-public-function fallback from `tools/`; ignore raw SDK tools with a warning; keep only `name`, `description`, `schema`, and `approval_mode`; log and ignore unsupported extra kwargs. | Human (larohra) | 2026-10-06 |
| 6 | Harness-specific callable compatibility | hidden callable shims / explicit rejection | Do not accommodate `FunctionInvocationContext` or similar harness-specific signatures. Normal schema generation and SDK validation remain authoritative. | Human (larohra) | 2026-10-06 |
| 7 | MCP and helper auth boundary | host-managed parity layer / adapter-owned session auth | Preserve existing MCP authoring and filters. Materialize bearer auth at Copilot create/resume between turns, not mid-turn; scoped helper permissions remain ownership-based and default-deny outside approved roots. | Human (larohra) | 2026-10-02 |
| 8 | Persistence boundary | host session protocol / thin SessionFs adapter | Reuse existing storage settings, use `copilot-native/{agent_id}/{session_id}/{sdk_relative_path}`, and keep contents/continuation/compaction/recovery fully SDK-owned. | Human (larohra) | 2026-10-02 |
| 9 | Execution/result normalization | shared cross-SDK behavior / adapter-local conversion with shared public result contract | Keep MAF usage decoding local to MAF, Copilot result conversion local to Copilot, and normalize only the public `AgentResult`/tool-call accounting contract in shared code. | Human (larohra) | 2026-10-06 |
| 10 | Failure and cleanup precedence | deferred retry registries / immediate bounded cleanup preserving original failure | Keep one app-owned native client plus request-owned SessionFs adapters, and preserve the original execution or cancellation error over later transport, disconnect, filesystem, or credential cleanup failures. | Human (larohra) | 2026-10-05 |
| 11 | Local role parity and turn acceptance | reject streaming/leaves / retain host role contracts | Support local SSE, chat delegates, Workflow Sub Agent Activities, workflow-management tools, and the same filtered MCP/skill inventories through the bound runner. Workflow-enabled direct roles also keep the packaged `data-driven-workflows` skill as an approved native skill directory while leaves retain project-only skills. Preserve isolated disposable leaves, host tool ordering, cancellation/deadlines, and completed-turn event barriers before continuation and successful SSE completion. | Human (larohra) | 2026-10-06 |
| 12 | Turn acceptance follow-up after SDK contract review | host history scans rejecting any prior aborted turn / request-local live interruption check with SDK-owned resume-waiting | Grounded in the SDK-contract follow-up in this PR, supersede only row 11's turn-acceptance clause: keep `continue_pending_work=False`; delegate resume/idle waiting to the SDK without host history scans or rejecting prior aborted history; reject only a live abort/interruption observed during the current request before a successful result/`done`; cancellation still aborts and rethrows; SSE still emits no success/`done` on interruption. | Human (larohra) | 2026-10-07 |
| 13 | Debug UI history under Copilot | unsupported UI / live-only / native continuation with MAF transcript replay | Support live chat, streaming, and explicit native-session continuation. Show that prior transcript messages are not restored; do not project Copilot native files into MAF history; surface resume failures without creating a replacement session. MAF history stays unchanged. | Human (larohra) | 2026-10-07 |
| 14 | Inbound built-in MCP endpoint | reject under Copilot / reuse bound runner | Enable the existing MCP handler with prompt validation, native continuation, honest errors without fallback, and Functions-extension-owned system-key authentication. Keep real-host qualification separate from offline adapter evidence. | Human (larohra, relayed by coordinator) | 2026-10-07 |
| 15 | SSE backpressure | cancel execution / drop oldest buffered events and complete the run | Keep at most 128 queued events; when a slow client falls behind, drop the oldest queued events, report the dropped count with `stream_truncated`, and send the complete final message before `done`. Client disconnect still cancels execution. | Human (larohra) | 2026-10-07 |
| 16 | Client customization | public custom client-manager injection / built-in construction | Remove public `ClientManager`, `MAFClientManager`, getter, and setter APIs; preserve normal model/provider configuration and end custom chat-client injection. | Human (larohra) | 2026-10-07 |
| 17 | Backend-specific responsibilities | common-module implementations / selected-harness ownership | Keep concrete MAF client creation, MAF warnings and instrumentation, and SDK history projection inside the selected MAF harness; retain neutral shared registration and observability helpers. | Human (larohra) | 2026-10-07 |
| 18 | Legacy compaction field | honor field / warn and ignore | Remove the MAF-specific field from the public contract, warn when legacy global or per-agent config contains it, and use MAF's native model-aware default. | Human (larohra) | 2026-10-07 |
| 19 | Failed-turn session IDs | echo generated ID on any failure / echo only resumable generated IDs | Always return caller-supplied IDs; return generated IDs only when the runtime confirms the session is resumable, consistently across HTTP, chat, and SSE surfaces. | Human (larohra) | 2026-10-07 |
| 20 | Public shutdown API | keep manager-specific name / use neutral runtime name | Remove `shutdown_client_manager()` and expose `shutdown_runtime()` for acquired harness-owned resources. | Human (larohra) | 2026-10-07 |

### Approved telemetry addendum decisions

These decisions govern the telemetry addendum only; they do not revise the
previously approved harness decisions above.

| # | Decision | Options considered | Choice | Decided by | Date |
| --- | --- | --- | --- | --- | --- |
| 21 | GenAI-semantics scope | new Copilot telemetry follows GenAI semantics / whole-runtime blanket compliance | Have the new Copilot runtime-owned invocation and tool telemetry follow the selected GenAI semantic-conventions snapshot only where the runtime has exact matching values; keep MAF unchanged and avoid any duplicate canonical wrappers. | Human (larohra) | 2026-10-13 |
| 22 | Canonical invocation and tool boundaries | keep custom `agent.run` naming / adopt Copilot canonical GenAI names | Use `invoke_agent {name}` for Copilot invocation spans and `execute_tool {name}` for Copilot tool spans, preserving the existing session-event subscription, explicit host context, wrapper ownership, and matched native-timing rules. | Human (larohra) | 2026-10-13 |
| 23 | Stable fields, usage, and privacy | infer missing provider/model semantics / emit only proven fields | Emit standard identity, conversation, provider, and tool fields only from stable runtime state; keep usage as the existing custom event plus exact-match `gen_ai.usage.*` fields; defer inference spans and standard metrics; keep allowlisted telemetry and never include full `tool_calls` records in Copilot run logs. | Human (larohra) | 2026-10-13 |
| 24 | Copilot exception diagnostics | keep Copilot exception details suppressed/content-free / match installed MAF exception capture at runtime-owned boundaries | Disable automatic OTel exception/status recording on Copilot runtime-owned ingress, delegate, leaf, and custom-tool spans, then explicitly capture actual caught Python failures once per span with OpenTelemetry exception events, `error.type`, `ERROR` status, and `af.fault_domain` regardless of `ENABLE_SENSITIVE_DATA`; keep prompt/request/response gating unchanged, preserve cancellation as nonfailure, and keep native-only SDK tool failures as structured statuses without fabricated exception events. | Human (larohra) | 2026-10-14 |
| 25 | Harness-neutral runtime telemetry surface | divergent Copilot `invoke_agent` invocation naming / shared runtime invocation contract | Supersede the divergent invocation naming from the earlier telemetry addendum: runtime-owned invocation spans use `agent.run {slug}` and shared `af.agent.*` fields for both harnesses, with `af.agent.harness=maf\|copilot` as a span attribute. Keep adapter-owned tool and usage projection truthful without fabricating unavailable model spans, timing, provider, or native SDK hierarchy. | Human (larohra) | 2026-10-14 |
| 26 | Non-HTTP run-log content gating | keep non-HTTP raw response/tool logging unchanged / share the HTTP run-log privacy switch | Use the shared run-log payload builder for non-HTTP and HTTP entrypoints: metadata is logged by default, response content is logged only when `ENABLE_SENSITIVE_DATA=true`, raw tool-call records remain allowed for MAF when enabled, and Copilot run logs continue to exclude raw tool-call records. | Human (larohra) | 2026-10-14 |

### Approved runner simplification

These decisions supersede earlier restrictions only where stated. Authentication,
capability authorization, privacy, and cleanup contracts remain unchanged.

| # | Decision | Options considered | Choice | Decided by | Date |
| --- | --- | --- | --- | --- | --- |
| 28 | Bound-runner responsibilities | separate adapter strategy and forwarding facade / one app-bound runner contract | Use one concrete runner per immutable app binding for execution, pre-registration qualification, defaults, history support, and observability policy. Runner creation is resource-free; SDK imports and acquired resources stay lazy. Same-root apps and binding clones retain independent runners and native owners, and shutdown preserves existing error/cancellation precedence. | Human (larohra) | 2026-10-14 |
| 29 | Harness discriminator namespace | `af.harness` / shared `af.agent.*` namespace | Refine the harness-neutral telemetry decision to use `af.agent.harness=maf\|copilot`; invocation names, other attributes, exception policy, privacy, and truthful tool/usage evidence stay unchanged. | Human (larohra) | 2026-10-14 |

### Approved hosted/worker amendment

| # | Decision | Options considered | Choice | Decided by | Date |
| --- | --- | --- | --- | --- | --- |
| 30 | Hosted worker/storage contract amendment | keep local-only/single-worker FRD guard / mirror the existing MAF storage and process-local concurrency contract | Remove the local-only and single-worker FRD restriction. Keep MAF as the default harness, preserve the existing storage-selection and no-fallback-on-configured-error contract, keep the existing registered surfaces enabled, and describe same-session overlap across workers/instances as the same process-local coordination limit already present on the MAF path rather than as a Copilot-only carve-out. | Human (larohra) | 2026-10-14 |
| 31 | Copilot ignored reasoning/output settings | keep rejecting Copilot reasoning/output-limit overrides / warn and ignore the settings | Keep other unsupported-capability checks unchanged, but treat `AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT`, `AZURE_FUNCTIONS_AGENTS_REASONING_SUMMARY`, and `agent_configuration.max_output_tokens` as warned no-op settings on the Copilot path. Copilot keeps SDK/provider defaults for reasoning behavior, and `max_output_tokens` is not enforced because the pinned SDK/runtime still lacks a verified provider generation-cap mapping. | Human (larohra) | 2026-10-15 |
| 32 | Copilot native reasoning/output amendment | preserve the warned no-op contract / pass through verified SDK-native settings | Supersede row 31 after verifying the pinned `github-copilot-sdk` exposes these settings natively. Freeze valid `AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT` (`low`, `medium`, `high`, `xhigh`, `max`) and `AZURE_FUNCTIONS_AGENTS_REASONING_SUMMARY` (`none`, `concise`, `detailed`) once in the Copilot app binding, fail fast on invalid values, and pass them through to native create/resume calls. Map `agent_configuration.max_output_tokens` into the request-local Copilot `ProviderConfig.max_output_tokens` override without mutating the shared app-level provider configuration. MAF remains unchanged. | Human (larohra) | 2026-10-16 |
## 6. Feature-level acceptance and test plan

| Area | Required proof |
| --- | --- |
| Harness selection | Resolve once per app, propagate to handlers/delegates/workflows, and reject invalid selector values without fallback. |
| Neutral capability boundary | Prove discovery/registration outputs and `HarnessRequest` contain only immutable SDK-free descriptors; enforce import boundaries so MAF imports stay in MAF adapters and Copilot imports stay in Copilot adapters. |
| Tool authoring | Cover runtime `@tool`, first-public-function fallback, raw SDK tool rejection warnings, ignored extra kwargs, and explicit failure for unsupported callable signatures. |
| Skills | Cover depth-2 path discovery, basename exclusions, path-only discovery, ownership for overlapping roots, and the absence of host `SKILL.md` parsing or manual catalog generation. |
| MCP/auth/helpers | Cover existing per-agent/per-server filters, blank-scope normalization, generated bearer precedence, native MCP/helper event accounting, and scoped helper permission denials. |
| Persistence | Exercise SessionFs read/write/append/stat/list/mkdir/remove/rename behavior on local and Blob backends, selected-harness-only initialization, and shared `agent_id` path routing. |
| Lifecycle/errors | Verify request cleanup ordering and that execution/cancellation failures remain authoritative over later disconnect, filesystem, transport, or credential cleanup errors. |
| Debug UI/history | Verify live chat/streaming and native continuation, display the no-transcript-restore notice, preserve MAF transcript replay, and surface native resume failures without retry-as-create. |
| Non-HTTP entrypoints | Exercise each existing binding serializer and shared-handler contract offline; separately qualify each trigger on a real Functions host with actual binding delivery before marking it supported. Resource-blocked triggers remain unqualified, not unsupported. |
| Inbound built-in MCP | Verify preview acceptance and bound-handler dispatch, prompt validation, omitted/provided/normalized session IDs, and errors without replacement sessions. Real-host transport and system-key auth qualification remains separate. |
| Harness cleanup | Verify resource-free runner creation/qualification, per-binding and clone isolation, concurrent runner caching, selected-harness-only provider/warning/observability/history ownership, legacy compaction warning/default behavior, public shutdown, and failed-turn session IDs on HTTP, chat, and SSE. |
| Copilot telemetry | Cover direct/SSE, delegates, workflow leaves, non-HTTP and UI ingress; usage deduplication/accounting, tool ownership and ordering, incomplete timing, cancellation/error cleanup, both privacy modes, and unchanged MAF behavior. |

## 7. Docs impact

- `docs/architecture.md` documents the harness boundary, neutral capability
  model, selected-harness persistence split, and Copilot telemetry ownership.
- `docs/front-matter-spec.md` documents unchanged authoring surfaces and calls
  out that Copilot reuses the same MCP/skill filtering fields.
- The cleanup amendment removes public custom-client injection, documents the
  legacy compaction warning, and updates shutdown and failed-turn session-ID
  behavior across the runtime docs.
- `README.md` and operational docs may describe the bounded preview and storage
  namespace, but should defer detailed operational behavior to dedicated preview
  documentation rather than expanding this FRD.
- `docs/observability.md` and `docs/copilot-preview-operations.md` document
  telemetry semantics, limits, privacy, and operator setup; align
  `README.md`, `docs/index.md`, and `docs/getting-started.md` if preview
  observability expectations change.

## 8. Status & sign-off

- **Status:** Finalized
- **Human sign-off:** @larohra approved the feature contract on 2026-09-28 and
  approved the consolidated capability, skills, tool-authoring, and lifecycle
  simplifications through 2026-10-06.
- **Telemetry addendum sign-off:** larohra approved the revised Copilot
  telemetry addendum on 2026-10-13. The addendum decisions above govern
  Copilot telemetry except where a later row explicitly names shared runtime
  behavior. On
  2026-10-14 larohra approved the harness-neutral runtime invocation and
  `af.agent.harness` attribute contract, which supersedes the earlier divergent
  Copilot invocation naming while preserving truthful tool and usage telemetry.
  The same day, larohra approved applying the shared sensitive-data run-log
  payload contract to non-HTTP entrypoints for both harnesses.
- **Hosted/worker amendment sign-off:** larohra approved this amendment to the
  existing finalized FRD on 2026-10-14 without creating a new FRD.
- **Ignored-settings amendment sign-off:** larohra approved this scoped
  amendment on 2026-10-15 without creating a new FRD, then approved this
  SDK-verified native-settings amendment on 2026-10-16 without creating a new
  FRD.
- **Scope note:** This FRD records the durable feature contract only. It does
  not preserve superseded iteration history, review churn, or qualification
  narratives that no longer change the product boundary.
- **Harness-boundary cleanup sign-off:** Laveesh Rohra (`larohra`) approved the
  cleanup amendment on 2026-10-07. Independent architecture review returned
  **READY** on 2026-10-07; implementation may proceed under this amended
  contract.
- **Runner simplification sign-off:** larohra approved the bound-runner
  simplification on 2026-10-14. The shared harness discriminator is
  `af.agent.harness`. Existing authorization, lazy acquisition, isolation,
  telemetry privacy, and cleanup guarantees remain required.
