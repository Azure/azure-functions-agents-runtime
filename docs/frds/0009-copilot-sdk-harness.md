---
frd: 0009
title: Copilot SDK agent harness
status: In review
author: larohra
created: 2026-09-28
updated: 2026-09-28
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
Resolve the value once per app and carry the immutable selection through
registration, direct execution, delegation, and workflow execution. Standalone
`run_agent`, `run_agent_stream`, and leaf-runner entry points use the same
app-scoped resolution, not independent module-level environment reads. Changing
the environment does not switch an already initialized app.

With the flag off, there is no Copilot native-process launch, runtime download,
authentication, or telemetry bootstrap. Existing MAF client/tool extensions and
MAF observability remain intact. With it on, missing SDK assets, unsupported
configuration, or runtime failures are errors, never reasons to retry under MAF.
All roles in an app use the selected harness; a Copilot coordinator cannot use a
MAF specialist or Workflow Sub Agent.

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
lazily initialized, process-long SDK client per Functions worker, reused across
invocations with isolated sessions. Concurrent initialization must not launch
duplicate runtimes. Request cancellation must not close the shared client or
poison unrelated sessions; worker shutdown must release its client/process.
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
filesystem implementation. Reuse configured Functions storage/identity settings
where applicable; do not fall back to ephemeral disk on an Azure storage error
or missing Azure storage configuration. The concrete local-versus-deployed
selection rule must be defined, not inferred from a failed Blob connection.

Persistent native state applies to direct runs, including the fresh IDs used by
non-HTTP triggers, whose current MAF path also writes history. This preserves
that behavior but costs a native file tree, not one JSONL transcript, per trigger
invocation. Retention remains customer-controlled; no automatic retention limit
or new retention API is introduced. Retain/delete a session as a complete unit,
never prune live compaction references. Delegates and Workflow Sub Agents instead
use fresh ephemeral native storage, disposed at call end; they do not populate
the persistent namespace or require its completed-turn storage barrier.

Use a separate, versioned native namespace keyed by `(agent_slug, session_id)`.
Validate both identity components and contain every native relative path within
that namespace. Persist **all** SDK-owned files: journals, metadata, workspace
files, compaction checkpoints/references, and any referenced content. Do not
whitelist only `events.jsonl`, edit journal records, or serialize native state as
MAF `Message` JSONL.

The following are proposed storage contracts, not claims about the demonstrated
Blob adapter:

| Concern | Required contract |
| --- | --- |
| Ownership | One active owner per agent/session across workers, acquired before restore or inference and held through completion. Use storage-backed ownership with renewal and fencing, not only an `asyncio.Lock`. Contention waits within the request deadline or fails explicitly; independent sessions remain concurrent. |
| Lost ownership | Fence stale writes, deny new dispatch, and cancel the affected turn; never let an old owner publish completion after a replacement owner proceeds. Cancellation cannot undo an already-started external effect. |
| Filesystem operations | Qualify every operation used by the selected native runtime, including append, replacement, rename, delete, listing, and missing-file behavior. Reads see acknowledged writes. Rename emulation must be crash-recoverable and protected from concurrent readers/writers; Blob is not assumed to provide POSIX rename. |
| Acknowledgment | A successful persistent SessionFs mutation acknowledges durable storage, not a queued upload. A successful persistent turn requires acknowledged native state and all references, including background compaction writes. The exact SDK quiescence/flush signal and storage barrier must be established. |
| Completion | Record that a turn is active before submitting its prompt; publish completed-turn metadata only after native completion and the storage barrier. Return non-streaming success or SSE `done` only afterward. Deltas before `done` are provisional. This metadata identifies safe continuation, not per-model/tool checkpoints or a recovery controller. |
| Failure/interruption | Storage/ownership failures abort rather than become an ordinary model-visible tool result that permits continued inference. A session left active, corrupt, missing referenced state, or in an unsupported format must fail explicitly on restore, not reset, partially restore, or automatically replay tools. |
| Restore | A clean worker reopens the complete acknowledged native state and accepts the next user prompt without host transcript injection. No automatic continuation of pending work, no empty `send_messages`, and no exactly-once guarantee. |

Native compaction alone owns triggering, summarization, and context
transformation. Native compaction references and their targets are part of the
same durability obligation as conversation history. Acceptance requires a real
compact -> complete -> replace both processes -> restore -> follow-up sequence
showing reuse of the saved summary without a replacement compaction LLM call.
Separate passing compaction and uncompacted-restore tests do not prove this.

### 4.6 Compatibility and history break

Existing MAF files and blobs remain untouched. There is no automatic conversion,
import, dual-write, or merge between formats. A continuation request whose
identity has only incompatible harness history must fail explicitly instead of
silently starting an empty conversation; a fresh session ID starts a new native
conversation. Switching back to MAF can continue existing MAF history only,
never the turns recorded under Copilot. Copilot-only conversations require a new
MAF session ID. An older binary unaware of the native namespace cannot enforce
that guard, so rollback to it requires fresh IDs rather than an assumption of
history continuity. Cross-namespace detection must not parse or modify MAF JSONL.

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

### 4.8 Evidence limits and open architecture decisions

The supplied 2026-09-25 assessment used SDK revision
`4001c1da7d832c51bad1d38619c1a082af390efb`, runtime `1.0.84-5`, protocol 3.
It reported real local-file and Azure Blob native restores with single-writer
rename recovery, and host-named custom delegation with overlapping same-specialist
calls and no specialist-stream leakage. These establish feasibility, not the
contracts above. Native `custom_agents`/`task` delegation did not match the host
contract. A Functions-shaped Windows transport comparison favored external stdio;
it was not an Azure Functions deployment qualification.

The Blob evidence did not establish distributed ownership or concurrent-reader
safe rename. A storage-error path allowed inference before acknowledgment.
Compacted cold restore, Functions hosting, MCP, scoped skills, public streaming/
structured-response parity, and content-safe telemetry remain unqualified.
The assessment's mid-turn recovery experiments do not add those capabilities to
this feature's scope.

| Open decision | Required resolution before architecture finalization |
| --- | --- |
| SDK/runtime and hosting contract | Select version/protocol/assets, core-versus-optional packaging and dependency coexistence with MAF, provider/Entra mappings, and provider `store=false` enforcement. Define worker lifecycle and target Functions hosting constraints, including Linux/Flex not qualified by the Windows evidence. Do not promote the evidence SHA to a production pin by assumption. |
| Native storage protocol | Specify ownership/fencing, rename/read consistency, completion metadata, SDK storage-error barriers, and acknowledgment/quiescence. Define local-versus-deployed storage selection, complete-session retention safety, corruption/version detection, and interrupted-session behavior against concrete SDK operations. |
| Native continuation and presentation | Establish a supported native history projection, compaction/reference preservation and cold-restore evidence, and metadata-only incompatible-history detection/rollback behavior. Rendering history must not become a second execution-state authority. |
| Configuration compatibility | Decide the enforceable portable output limit and treatment/replacement of MAF-specific compaction configuration, preserving recursive inheritance/null behavior without claiming equivalent algorithms. |
| Extension compatibility | Define the supported custom `ClientManager` contract and MAF `FunctionTool` conversion boundary, including authored decorator kwargs, approval semantics, unsupported hooks/options, and construction-time validation. Preserve MAF extensions with the flag off. |

## 5. Decisions log

Dates below record decisions supplied or proposed for this specification;
scope approval is not full architecture sign-off.

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

## 6. Feature-level acceptance and test plan

Acceptance is behavioral, not an assertion that SDK feature names imply parity.
Extend tests mirroring the affected source modules, with configuration scenarios
under `tests/fixtures/config_scenarios/`. Use real SDK/storage/hosting evidence
where mocks cannot establish process, transport, authentication, or durability.

| Area | Acceptance evidence |
| --- | --- |
| Selection/isolation | Exercise unset, `false`, `0`, `true`, `1`, mixed-case/padded text, empty/whitespace-only/invalid values, multiple app contexts, and standalone entry points. Off starts no Copilot process/download/auth/telemetry; on is uniform across all roles and never falls back. |
| Unsupported features | Effective inherited/default-on capabilities and unmapped configuration/extensions fail before provider inference or tool effects. Isolated previews of supported capabilities execute real SDK turns. |
| Authoring/API | Existing precedence/null scenarios, tool `None`/empty semantics, routes/auth, response envelopes, structured-output validation/errors, history projection/degradation/errors/bounds, and SSE ordering/cancellation remain compatible. No native or specialist events leak. |
| Models/extensions | Verify supported providers/Entra refresh, model metadata, disabled provider conversation storage, deadlines, output limits, and explicit custom-manager/tool compatibility, including a manager replaced after composition. MAF hooks remain intact off. |
| Tools/MCP/skills | Cover sync/async, Pydantic, both decorator orders, workflow-only tools, approval options, allowlists, HTTP MCP headers/refresh/errors, lazy scoped skills/resources/scripts, and denied ambient capabilities. Assert callable/effect counts and no unexpected interactive approval gate. |
| Delegation/workflows | Prove fresh same-specialist concurrent sessions, catalog/role isolation, no child SSE, parent cancellation and specialist-local errors, Workflow Sub Agent grants/results, existing management/Activity retry/timeout/authorization, and at-least-once semantics. |
| Role storage/trigger execution | Run a non-HTTP trigger with its generated identity, serialization, logging/error behavior, and direct capabilities. Persistent direct state is isolated; delegates/Workflow Sub Agents leave no persistent native tree and dispose ephemeral state. |
| System tools | Exercise `web_request` defaults/exclusion/SSRF/budgets/errors and real ACA `execute_python` scoping/results without substituting local execution. |
| Completed-turn restore | On Blob and local storage, complete a real tool-using turn, replace Python and native processes, and continue by the same agent/session identity without restating prior values. Inspect outbound provider context/state reuse, not just a plausible answer. |
| Compacted restore | Force native compaction, acknowledge complete state, replace both processes, restore in a clean worker, and prove saved-summary/reference reuse without another compaction LLM call. |
| Storage failures/concurrency | Fault-inject append/replace/rename/read/ack errors, partial references, corrupt/unsupported state, crashes before completion, lease loss, stale writers, and two-worker same-session contention. No silent reset, continued inference after a storage barrier failure, or false success/`done`; independent sessions still progress. |
| History break | MAF bytes remain unchanged. Incompatible IDs fail explicitly; native and MAF namespaces never cross-read as execution state. Verify documented rollback behavior and native history rendering without importing MAF messages. |
| Hosting/telemetry | Demonstrate supported Functions deployment assets, lazy single-client startup, concurrent isolation, bounded cancellation/shutdown, and no orphan native process. Verify usage/correlation/error accounting and sensitive-data-off behavior in host and native telemetry. |

## 7. Docs impact

This specification and its FRD index entry describe proposed behavior only.
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

- **Status:** In review; a feature specification, not a claim of implementation.
- **Architecture review:** Dedicated agent review completed on 2026-09-28;
  contract clarifications incorporated (decision 8). Section 4.8 remains open;
  section 6 defines behavioral evidence, not completed qualification.
- **Human sign-off:** Feature scope and preview behavior were supplied as agreed
  requirements. Full architecture approval has not been recorded.
- **Finalization:** Resolve the open architecture decisions and record explicit
  human sign-off before marking this feature specification Finalized.
