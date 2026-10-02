---
frd: 0009
title: Copilot SDK agent harness
status: In review
author: larohra
created: 2026-09-28
updated: 2026-10-02
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
setting and custom-manager compatibility, requires the unresolved support choices in section 4.8.

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
Entra auth inputs, and per-agent filtering, with the accepted refresh limit in
section 4.3.2. SDK support for additional MCP transports does not expand the
product's supported authoring surface. Skills stay
limited to resolved paths and harness-supported skill behavior; workflow-only
runtime guidance must not become a global skill. Native ambient skills,
shell/file tools, planning, memory, search, or native `task` delegation must not
bypass this capability set. Native `builtin:skill` is allowed only for selected
approved skill directories.
Host-authorized autonomous execution must not acquire an interactive SDK
approval gate. Preserve authored approval semantics where supported; reject
unmapped approval options instead of granting blanket native permissions.

Keep the host `web_request` implementation's default, disable/exclude behavior,
SSRF controls, budgets, and error shape. Keep ACA Dynamic Sessions
`execute_python` endpoint/authentication/session scoping and result/error behavior;
do not replace remote execution with a local SDK shell or code interpreter.

#### 4.3.1 Harness-neutral capability interface

A capability descriptor is the runtime's SDK-free description of something an
agent may use. Discovery and registration produce only immutable capability
descriptors. They do not produce MAF objects or Copilot SDK objects. Discovery
stays read-only.

The descriptor set has three shapes:

- **Tool descriptor.** Name, description, JSON input schema, a sync or async
  callable, and policy fields that affect execution, including approval
  requirements. It is immutable after registration filters it for a role.
- **MCP server descriptor.** Server name, URL, transport, repr-hidden static
  headers, a tool filter of all, none, or a finite set, and optional Entra scope
  and client ID. It does not contain an SDK client, live token, or wrapper tool.
- **Skill descriptor.** Skill name, description, approved path, and metadata
  needed by the selected SDK. It does not eagerly inspect or load every resource
  or script and contains no provider state.

The ordinary runtime `@tool` authoring seam stays stable. Authors keep using
runtime `@tool` syntax, schema validation, sync or async callables, and current
invocation semantics. Tool discovery records neutral runtime tool metadata. The
MAF adapter maps that metadata to MAF `FunctionTool`; the Copilot adapter maps it
to Copilot tools. Authors do not rewrite tools with SDK-specific decorators.
Raw MAF `FunctionTool` subclasses and MAF-only `@tool` keyword arguments remain
isolated in the MAF compatibility layer. Unmapped extensions fail explicitly on
Copilot instead of being silently flattened.

`HarnessRequest` carries only these descriptors and other scalar execution
settings. It does not carry `agent_framework` or Copilot SDK types. Registration
remains the authority for per-agent filtering. The runner receives the filtered
request and passes it to the selected adapter.

Each harness has one adapter that maps descriptors to its SDK:

- The MAF adapter builds `FunctionTool`, `MCPStreamableHTTPTool`, and MAF
  `SkillsProvider.from_paths` configuration without changing MAF behavior.
- The Copilot adapter builds Copilot custom tools, MCP server configuration, and
  Copilot skill directory/enable/exclusion configuration.

Only the MAF adapter may import `agent_framework`. Only the Copilot adapter may
import Copilot SDK modules. Existing public MAF-typed authoring surfaces, such as
the public `FunctionTool` and `tool` exports, stay supported through a MAF
compatibility layer while they exist. Their migration remains an explicit open
item. Issue #1334 calls out that MAF `FunctionTool` extension compatibility
needs a separate decision.

Skill discovery and filtering stay shared. Discovery keeps the existing
malformed-frontmatter logging and skip behavior, name validation, duplicate
checks, and per-agent exclusions. The runtime passes approved skill paths and
metadata to the selected adapter. The harness SDK owns how skills advertise
instructions, load content, access resources, or execute supported skill
mechanisms. Do not create a runtime-owned skill loader, prompt engine, three-tool
abstraction, or script executor only to force parity between SDKs.

This interface maps to the pipeline as follows:

| Stage | Modules | Contract |
| --- | --- | --- |
| discover | `discovery/tools.py`, `discovery/mcp.py`, `discovery/skills.py`, `_function_tool.py` compatibility layer | Read project files and imports, validate existing authoring rules, and emit neutral inventories. Do not create harness SDK objects. |
| translate | `config/schema.py`, `config/merge.py`, `config/validation.py` | Keep typed config composition and validation independent of harness SDK types. |
| register | `registration/capabilities.py`, `registration/catalog.py`, `_harness.py` | Filter neutral inventories into immutable role capabilities and build `HarnessRequest` with descriptors only. |
| execute | MAF adapter, Copilot adapter, `runner.py`, `_copilot.py` | Map descriptors to the selected SDK at the execution boundary and preserve the same runtime contract. |

Phase-out acceptance is structural for the neutral core. Removing MAF from that
core means deleting the MAF adapter and its dependency. Discovery, registration,
`HarnessRequest`, runner contracts, and Copilot execution do not change. Retiring
public legacy MAF-specific extension surfaces still needs an explicit migration.
The design does not promise that customer MAF subclasses work after the MAF
adapter is removed. Add an import-boundary test that fails if `agent_framework`
appears outside the MAF adapter and the named MAF-compat public surfaces, or if
Copilot imports appear outside the Copilot adapter.

#### 4.3.2 Issue #1336 MCP and skills compatibility

This amendment proposes Model Context Protocol (MCP) and scoped-skill
compatibility. MCP uses the existing remote HTTP/streamable-HTTP transports.
The local-only preview limit applies to Azure Functions app hosting, not to
MCP URLs. Scoped skills are project skill folders exposed only when
configuration allows them. MAF remains the default; Copilot remains an
app-level, default-off preview with no automatic fallback.

MCP scope is MAF parity only, with the accepted between-turn auth limit below.
Bring any further gap to human review rather than adding host policy.

**Discovery and filtering.** Keep existing `mcp.json` authoring and discovery.
Preserve existing discovery warnings, skipped entries, and `failed_loads`
reporting. Discovery emits the immutable SDK-free MCP server descriptors
from section 4.3.1. Registration preserves the existing per-agent `mcp: false`
and `mcp.exclude` filters; `HarnessRequest` carries only selected descriptors.

Per-server `tools` already exists in MAF; it is not a new field:

- Omitted, or any list containing `"*"`: map to `["*"]` for all tools.
- `[]`: no tools.
- Any other list: the existing exact-name allowlist.

Map the selected remote HTTP/streamable-HTTP servers and their filters to public
SDK `mcpServers` configuration. Use normal SDK connection, initialization, and
tool loading.

**MCP lifecycle and auth.** Supply the filtered configuration at public session
create/resume. Reuse the existing per-session lock and per-turn
create/resume/disconnect lifecycle. Resume the same native session ID and
history after a completed turn. Fresh static headers replace the old ones.

For a server without `auth`, pass static headers unchanged, including
`Authorization`. Empty or whitespace-only `auth.scope` keeps the existing
warning and static-only behavior, without a token attempt. For nonempty scope,
use the authored scope and existing credential selection: absent or unresolved
`client_id` uses the default credential; a resolved client ID uses the client-ID
credential. Acquire a fresh token before create/resume. Generated
`Authorization` overrides static `Authorization`. Never log headers or tokens.

Copilot receives static headers only at create/resume, with no mid-turn refresh.
This is the accepted limit relative to MAF's per-request token refresh.
Propagate token-acquisition, create/resume, connection, and tool errors through
ordinary SDK and runtime error/result paths. Do not add automatic retries of
possibly side-effecting calls or fall back to stale credentials, dropped
capabilities, SDK OAuth, or a new empty session.

**MCP approvals.** Ordinary configured calls remain noninteractive, matching
MAF's existing `never_require` default. The SDK's `on_permission_request`
parameter is optional; omission is not autoapproval. Permission requests still
need a consumer decision.

Use the callback already needed for skill helpers on create and resume. For
ordinary configured MCP requests (`kind == "mcp"`), the proposed minimal mapping
delegates to `PermissionHandler.approve_all`, the SDK's approve-once helper.
Native `mcpServers.tools` enforces the authored filter. All other request kinds
retain the agreed skill-helper handling and default deny. Do not install a
global approve-all handler or broaden shell/read/edit access. Managed-approval
limits remain open in section 8.

**Skills adaptation.** `discovery/skills.py` recursively indexes valid `SKILL.md`
files by name. `registration/capabilities.py` applies the existing `skills: false`
and `skills.exclude` frontmatter selections to produce the approved `(name, path)`
inventory, including independently enabled nested skills. Add no authoring keys.
Keep canonical discovered roots, including excluded roots, as read-only
target-ownership metadata; they grant no access.

The adapter defines how skills run for its harness. MAF uses public
`SkillsProvider.from_paths` over the approved paths. Its current resource/script
recursion includes nested skill directories under an approved parent. Preserve
that flag-off baseline; it does not enforce the stronger Copilot subtree policy.
The MAF nested-exclusion gap is separate from this amendment.

Copilot registers only individual approved skill directory paths and explicit
`disabled_skills` names from the same frontmatter selections, using supported
public configuration and native `builtin:skill`. Do not register an unfiltered
ancestor root or copy skills into a collection folder.

Each resource or script target belongs to the most-specific canonical discovered
skill root containing it. Permit a target only when its owning skill is in the
agent's approved inventory. An enabled parent cannot authorize an excluded nested
child. An independently enabled child keeps its own grant when its ancestor is
excluded. Resolve canonical paths before ownership checks; reject traversal,
symlink or alias escapes, out-of-root targets, missing or ambiguous ownership,
and string-prefix overlaps that are not path containment. No broad ancestor
grant is allowed. `skills: false` exposes no skill or helper capabilities.

Malformed skill frontmatter remains logged and skipped like MAF. Existing
missing, invalid, and duplicate skill-name validation remains unchanged. Do not
add a global skill/provider cache; all provider state is per-role and per-run.
Supported skill execution is owned by the selected SDK's configured
capabilities. Do not add a host script runner, new execution limits, or interactive
approval gates in this amendment. Skills remain trusted deployment-owned code, not
an OS sandbox. Untrusted or adversarially mutable skill trees are unsupported.

Native instruction loading is established. Resource access and script execution
through native skills use targeted native helpers. For enabled skills, the
intended Linux Python-worker allowlist is `builtin:skill`, `builtin:view`, and
`builtin:bash`; `powershell` is not enabled on this target. The host owns the
permission policy for those helpers. Use `on_permission_request` on create and
resume with default deny. Return `ApproveOnce` only for approved resource files
in their permitted owning skill tree or validated approved skill-script commands.
Reject general Bash commands, including commands from an approved skill directory
or after a skill is loaded. Never use approve-all, session-wide cached grants,
or a callback bypass.

Native `view` may read only approved resource files in their owning skill tree.
Native `bash` remains visible, and a validated approved script invocation may run
from any turn because the SDK does not attest skill origin. This does not permit
general Bash execution. Validate stable permission-request data, including request
kind, full command text and arguments, and canonical target ownership. A loaded
skill, model or caller intent, working directory (`cwd`), `toolCallId`,
`possiblePaths` alone, or `allowedTools` metadata cannot grant arbitrary Bash.
Skill frontmatter and event metadata cannot expand these grants.

Support only narrow literal approved-script invocation forms with validated
arguments, not broad shell prefixes or a generic shell parser/executor. Deny
unknown, ambiguous, or compound command forms. Do not grant blanket command
chaining, substitution, pipeline, or redirection permission.

Once approved, a skill script runs with host privileges. This policy restricts
which native helper actions may start; it does not sandbox the script's internal
effects. Supported command forms and arguments need implementation coverage under
this contract. They are not a new authoring surface or another user policy
question.

The native skill schema takes a skill name and does not enumerate approved names.
Give the model the approved skill names and descriptions automatically. Prefer a
supported SDK prompt or catalog presentation. If replace-mode integration needs
it, a tiny adapter projection of already validated names and descriptions is an
acceptable fallback. That fallback is not a loader or prompt engine.

The disabled SDK built-ins list reflects SDK 1.0.14. The runtime may expose more
later. For enabled skills on Linux, `builtin:skill`, `builtin:view`, and
`builtin:bash` are the targeted allowlist exceptions. Skill loading is limited to
approved names. Visible `view` and `bash` helpers permit only approved skill
resource reads and validated approved-script actions. General Bash execution and
other file reads are denied by the callback, not represented as separate generic
tools. Other built-ins remain excluded by the custom-tool allowlist unless
separately approved. Disabled built-ins: shell/files
(`powershell`, `create`, `edit`, `grep`, `glob`), network
(`web_fetch`), agents (`task`, `read_agent`, `write_agent`, `list_agents`),
interaction/planning (`ask_user`,
`task_complete`, `exit_plan_mode`, `send_inbox`, `context_board`), tool search,
and infinite sessions.

Add structural tests that capability copies cannot mutate catalog leaves and
ordinary project skill roots do not receive workflow-only guidance unless the
existing role contract already adds it.

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

Only one turn may execute at a time for a given agent/session. Concurrent requests
must wait within their deadline or fail clearly. A failed startup that neither
began a turn nor damaged session state must leave the previous conversation
usable. If an interrupted turn cannot be continued safely, return an explicit
error rather than silently resetting the conversation or replaying work.

The storage guarantees below support that behavior; they are not claims about
the demonstrated Blob adapter. Marker states, write ordering, and cleanup
mechanics are implementation details, not prescribed by this specification.

| Concern | Required contract |
| --- | --- |
| Ownership | Enforce single-active-turn isolation across workers, not only within one process. Ownership protection must prevent stale owners from mutating session state; independent sessions remain concurrent. |
| Lost ownership | Fence stale writes, deny new dispatch, and cancel the affected turn; never let an old owner publish completion after a replacement owner proceeds. Cancellation cannot undo an already-started external effect. |
| Filesystem operations | Qualify every operation used by the selected native runtime, including append, replacement, rename, delete, listing, and missing-file behavior. Reads see acknowledged writes. Rename emulation must be crash-recoverable and protected from concurrent readers/writers; Blob is not assumed to provide POSIX rename. |
| Acknowledgment | A successful persistent SessionFs mutation acknowledges durable storage, not a queued upload. A successful persistent turn requires acknowledged native state and all references, including background compaction writes. The exact SDK quiescence/flush signal and storage barrier must be established. |
| Completion | Non-streaming success or SSE `done` means the turn finished and the native state required for continuation is durably acknowledged. Earlier stream deltas are provisional. This is a completed-turn guarantee, not per-model/tool checkpoints or a recovery controller. |
| Failure/interruption | Storage/ownership failures abort rather than become an ordinary model-visible tool result that permits continued inference. Uncertain turn progress, corruption, missing referenced state, or an unsupported format must fail explicitly on restore, not reset, partially restore, or automatically replay tools. A known-safe startup failure must not invalidate the last completed conversation. |
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

Native `skill`, `view`, and `bash` invocations, including permission-denied calls,
feed `AgentResult.tool_calls`, tool/error counts, and existing telemetry through
the generic public SDK `tool.execution_start` and `tool.execution_complete`
events. Account for each `toolCallId` exactly once, not once per event. Do not
duplicate custom calls already captured by wrappers or count `skill.invoked`
metadata as another generic tool call. Preserve the existing public `tool_start`,
`tool_end`, and `error` meanings, sanitized results, and sensitive-data/redaction
policy. Never expose raw native envelopes or add a public logging interface.

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

### 4.8 Preview support limits

The Copilot preview is local-only and requires a single Functions worker. Azure
Functions hosting, multi-worker execution, Blob-backed native session storage,
MAF history import, public streaming/structured-response parity, MCP, scoped
skills, delegation, workflows, system tools, and interrupted-turn recovery are
unsupported in this preview. Unsupported capabilities fail explicitly without
fallback. Configured output caps are rejected because the pinned SDK/native
runtime does not expose a provider generation cap for this path.

Native session storage remains SDK-owned. The host verifies completed turns
before resume and returns explicit errors for missing, corrupt, interrupted, or
incompatible native history; it does not reset conversations silently or claim
transactional/exactly-once execution.

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

Entries retain earlier proposals and later human decisions. The original feature
and provider sign-offs are separate from the amendment sign-off in section 8.

The MAF-parity-only scope supersedes earlier MCP host-policy, provenance/catalog
approval checks, explicit MAF approval overrides, and post-create staging
proposals. The thin SDK-owned skills contract supersedes the earlier shared
runner, MAF-dependent Copilot skills, common three-tool abstraction, runtime
skill engine, and stricter discovery proposals. Linux Bash replaces the earlier
PowerShell target under the same scoped helper policy. The neutral descriptor
contract, nested skill ownership, unchanged role contracts, and minimal flag-off
validation supersede the older conflicting proposals. Section 4.3 defines the
current requirements.

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
| 12 | Feature specification sign-off | Keep In review / finalize the agreed feature contracts | Finalized after approving the safe startup failure behavior; section 4.8 remains an explicit record of unresolved implementation choices and required evidence, not a claim of parity or production readiness | Human (larohra) | 2026-09-28 |
| 13 | MAF compatibility and Copilot provider boundary | Adjust MAF or precedence / preserve MAF exactly and isolate Copilot SDK types | Keep MAF behavior and provider/model precedence byte-for-byte behaviorally unchanged, including provider selection and authored/per-agent merge/`null` semantics. Use one stable singular pure typed target and lazily construct SDK `ProviderConfig` in `_copilot_providers.py`; do not optionally import the SDK in shared/default-off code | Human (larohra) | 2026-09-29 |
| 14 | Copilot provider mappings | Generic/fallback mapping / explicit matrix | Map OpenAI, Azure OpenAI, and Foundry exactly as section 4.8.1 specifies with Responses for all supported providers; reject unsupported providers/settings without fallback | Human (larohra) | 2026-09-29 |
| 15 | Credential lifecycle | Persist credentials / re-supply and refresh | Freeze provider settings at harness selection, re-supply credentials on resume from that provider object, permit overlapping Entra callbacks that acquire per request through Azure Identity, and exclude credentials from persistence, session metadata, launch arguments, and logs while acknowledging native request memory | Human (larohra) | 2026-09-29 |
| 16 | Custom `ClientManager` migration | Adapt custom managers / built-in only on Copilot | Leave MAF unchanged; on Copilot accept only the exact runtime-created built-in manager, treating an explicitly installed `MAFClientManager()` or any other replacement as MAF-only. Reject replacement before app mutation and recheck before execution. `build_chat_client`-only managers remain MAF-only; this is not a future extension hook | Human (larohra) | 2026-09-29 |
| 17 | Issue #1336 version/API seam | Upgrade or loosen versions / retain the version pin and public experimental staging | Retain `github-copilot-sdk` 1.0.14, native 1.0.85, and protocol 3; the proposed post-create refresh/start sequence is superseded by the between-turn header refresh decision | Agent proposal; lifecycle superseded by the between-turn header refresh decision | 2026-09-29 |
| 18 | MCP authority and safety | SDK discovery/ambient permissions / host-filtered descriptors and verified catalog | Keep registration authoritative, pass only filtered neutral descriptors, verify server/tool provenance before prompt, and deny ambient, unattributed, mismatched, or policy-flagged requests. The proposed dynamic-header broker is superseded by the between-turn header refresh decision. The older descriptor shape is superseded by the harness-neutral capability interface. The remaining mapping awaits review. | Agent proposal; partially superseded, otherwise pending human sign-off | 2026-09-29 |
| 19 | Skills and script execution | SDK-native skills or exposed tools without execution / exact public-MAF adaptation and shared runner | Historical proposal to adapt exact per-role roots into the custom-tool seam. Its shared runner, Copilot dependency on MAF `FileSkillsSource`/`SkillsProvider`, and stricter discovery correction are superseded by the preserve-existing-skill-behavior and harness-neutral capability-interface decisions; the remaining adaptation details await fresh review. | Agent proposal; partially superseded by later human decisions | 2026-09-29 |
| 20 | Compatibility role boundary | Enable workflow/delegate roles opportunistically / direct supported runs only | Remove MCP/skills rejection only for direct Copilot runs; retain delegation, Dynamic Workflow, and Workflow Sub Agent rejection without bypasses, with structural non-leakage tests | Agent proposal/pending human sign-off | 2026-09-29 |
| 21 | MAF skill behavior during Copilot compatibility work | Change shared discovery/execution behavior / preserve existing MAF behavior | Preserve current malformed-frontmatter logging-and-skip behavior in both harnesses; keep existing name validation; preserve `load_skill`, `read_skill_resource`, and `run_skill_script` behavior without adding a host runner, new limits, or approval gates. The earlier MAF-tool reuse wording is superseded by the harness-neutral capability interface. | Human | 2026-09-29 |
| 22 | MCP tool approvals | Interactive approval / explicit autonomous policy | Configured MCP tools require no interactive user approval; set MAF `approval_mode="never_require"` and use a Copilot callback to approve only catalog-verified configured MCP calls, rejecting other requests. Validate behavior on the pinned SDK/native pair; its v1.0.14 source documents managed approval for Shell/Read/Edit/Domain, not MCP. | Human | 2026-09-29 |
| 23 | Authenticated MCP continuity between turns | Mid-turn dynamic refresh or new session / detach and resume the same native session with fresh static headers | Proceed with public non-destructive disconnect/resume of the same native session ID and history after a completed turn; obtain fresh Entra headers as needed before the next create/resume. No mid-turn replacement, automatic side-effect retry, stale-token/drop-tools fallback, or claim of full MAF parity. | Human (larohra) | 2026-09-30 |
| 24 | Empty MCP auth scope | Reject or use a Copilot-only policy / preserve MAF behavior | Preserve MAF behavior: warn and use authored static headers (or no headers) when `auth.scope` is empty; valid-scope token acquisition failures remain explicit errors. Preserve unresolved/missing client-ID fallback to the default credential and existing generated-Authorization precedence. | Human (larohra) | 2026-09-30 |
| 25 | Harness-neutral capability interface | Share MAF types across harnesses / neutral descriptors with one adapter per SDK | Use immutable SDK-free tool, MCP server, and skill descriptors through discovery, registration, and `HarnessRequest`; map them only inside one adapter per harness so MAF can be removed by deleting the MAF adapter and dependency. | Human (larohra) | 2026-10-01 |
| 26 | MCP filter and readiness parity | Extra host-side catalog/readiness attestation / match MAF filtering and connection behavior | Preserve existing authored filters: per-agent disable/exclude and per-server all, none, or named tool allowlist. Do not require a separate host preflight proving every configured tool is present; rely on normal SDK connection, initialization, tool loading, and explicit surfaced errors. | Human (larohra) | 2026-10-01 |
| 27 | Remote MCP static-header boundary | Disallow remote MCP or add dynamic-header machinery / allow remote URLs with static create-resume headers | Remote HTTP/streamable-HTTP MCP URLs are allowed by the intended design. Accept static headers and Entra tokens supplied at create/resume between completed turns, with no mid-turn refresh, automatic retry of side-effecting calls, proxy, private API, or dynamic-header broker. | Human (larohra) | 2026-10-01 |
| 28 | Amendment role scope | Specify direct-only support and workflow/delegate rejection / leave roles to separate work | Do not add new role restrictions or new workflow/delegate support claims in this amendment. Neutral descriptors are reusable by callers under their existing role contracts. | Human (larohra) | 2026-10-01 |
| 29 | Flag-off validation | Exhaustive new regression suite / ordinary or minimal check | Preserve flag-off MAF behavior. Existing coverage or a minimal focused check is acceptable; this is not an open design decision. | Human (larohra) | 2026-10-01 |
| 30 | Runtime `@tool` SDK mapping | Author SDK-specific decorators / map runtime tools through adapters | Keep ordinary runtime `@tool` authoring, schema validation, and sync/async invocation stable. Discovery emits neutral tool metadata; the MAF adapter constructs MAF `FunctionTool`, and the Copilot adapter constructs Copilot tools. MAF-specific subclasses and keyword arguments stay in the MAF compatibility layer and fail explicitly on Copilot when unmapped. | Human (larohra) | 2026-10-01 |
| 31 | Thin SDK-owned skills integration | Runtime-owned skill engine / harness interface with SDK-owned skill behavior | Keep shared discovery, validation, exclusions, and approved skill paths/metadata. Let each harness adapter map those paths into its SDK: MAF through public `SkillsProvider.from_paths`, Copilot through supported public skill directory/enable/exclusion configuration and native `builtin:skill`. Do not create a runtime-owned loader, prompt engine, three-tool abstraction, or script executor for parity. | Human (larohra) | 2026-10-02 |
| 32 | Native skill helper permissions | Hide helper tools or broadly trust them / visible helpers with approve-once scoped actions | Keep native skill helpers visible when needed, but approve only valid resource reads under approved skill trees and validated approved skill-script invocations. Reject general PowerShell and other generic helper use. There is no skill-origin attestation or sandbox guarantee once a script is approved. | Human (larohra) | 2026-10-02 |
| 33 | Explicit frontmatter skill exclusions and nested ownership | Name-only exclusion / owning-skill subtree enforcement | Apply existing `skills: false` and `skills.exclude` selections to individual approved paths, explicit `disabled_skills` names, and canonical most-specific target ownership. An enabled parent cannot authorize an excluded nested child; an independently enabled child keeps its own grant under an excluded parent. Preserve the flag-off MAF baseline, which does not guarantee nested subtree exclusions. | Human (larohra) | 2026-10-02 |
| 34 | Linux native skill helper target | Windows PowerShell / Linux Bash | Use `builtin:skill`, `builtin:view`, and `builtin:bash` for enabled skills on the intended Linux Python worker, with PowerShell disabled. This supersedes the earlier Windows PowerShell helper target, not the default-deny approve-once policy for approved resource reads and validated approved-script invocations. Linux execution remains untested; scripts have host privileges, not a sandbox. | Human (larohra) | 2026-10-02 |
| 35 | MCP parity-only scope | Add host MCP policy / preserve current MAF behavior | Keep existing MAF MCP behavior and the accepted between-turn header limit. Add no host MCP policy. Raise any further gap for joint review before changing scope. | Human (larohra) | 2026-10-02 |

## 6. Feature-level acceptance and test plan

Acceptance is behavioral, not an assertion that SDK feature names imply parity.
Extend tests mirroring the affected source modules, with configuration scenarios
under `tests/fixtures/config_scenarios/`. Use real SDK/storage/hosting evidence
where mocks cannot establish process, transport, authentication, or durability.

| Area | Acceptance evidence |
| --- | --- |
| Selection/isolation | Exercise unset, `false`, `0`, `true`, `1`, mixed-case/padded text, empty/whitespace-only/invalid values, multiple app contexts, and standalone entry points. Off starts no Copilot process/download/auth/telemetry; on is uniform across all roles and never falls back. Existing coverage or a minimal focused flag-off check is sufficient for this amendment; no new exhaustive suite is required. |
| Context propagation/lifetime | Construct two apps with different flag snapshots, including the same root; delayed handlers, delegates, and Activity calls retain their own context after environment changes. Cover explicit/default standalone contexts. On worker replacement, completed Activity results replay normally and newly executed/redelivered Activities use the serving app's context, without adding harness state to Durable history or changing its scheduling/version-routing rules. |
| Unsupported features | Effective inherited/default-on capabilities and unmapped configuration/extensions fail before provider inference or tool effects. Isolated previews of supported capabilities execute real SDK turns. |
| Authoring/API | Existing precedence/null scenarios, tool `None`/empty semantics, routes/auth, response envelopes, structured-output validation/errors, history projection/degradation/errors/bounds, and SSE ordering/cancellation remain compatible. No native or specialist events leak. |
| Models/extensions | Verify supported providers/Entra refresh, model metadata, disabled provider conversation storage, deadlines, output limits, and explicit custom-manager/tool compatibility, including a manager replaced after composition. MAF hooks remain intact off. |
| Tools | Cover ordinary runtime `@tool` mapping to neutral metadata, sync/async, Pydantic, both decorator orders, workflow-only tools, approval options, and denied ambient capabilities. Assert MAF maps neutral tools to `FunctionTool`, Copilot maps them to Copilot tools, callable/effect counts remain stable, and no unexpected interactive approval gate appears. Unmapped MAF-only extensions fail explicitly on Copilot. |
| Import boundary / neutral interface | Structurally assert `HarnessRequest` and discovery/registration outputs contain only immutable SDK-free descriptors. Assert `agent_framework` imports are limited to the MAF adapter and named MAF-compat public surfaces, and Copilot imports are limited to the Copilot adapter. Delete-or-stub the MAF adapter in a smoke test to prove discovery, registration, runner contracts, and Copilot descriptor mapping do not change. |
| MCP compatibility | Exercise remote HTTP/streamable-HTTP mapping at public create/resume and existing per-agent disable/exclude filters. Test omitted `tools`, any list containing `"*"`, `[]`, and exact-name allowlists through native `mcpServers.tools`. Preserve discovery warnings, skipped entries, and `failed_loads`. Complete an actual MCP call and turn, then resume the same native session through the existing lock/disconnect lifecycle and make another call with fresh headers. Prove prior user/tool/assistant history reaches the resumed model and new headers replace old ones. Verify static headers without auth; empty/whitespace scope warnings without token acquisition; default credentials for missing/unresolved client IDs; resolved client-ID selection; and generated `Authorization` precedence. Token, connection, initialization, and tool errors follow ordinary SDK/runtime error-result propagation. Verify ordinary configured MCP calls use the MCP-only SDK approve-once branch without interaction. Shell/read/edit requests must retain the scoped skill-helper policy and default deny. Assert no host-added side-effect retry, stale-token, OAuth, dropped-capability, or empty-session fallback. |
| Skills compatibility | Exercise canonical `(name, path)` inventory construction and existing `skills: false`/`skills.exclude` selections, including valid nested skills. Build approved skill descriptors plus read-only discovered-root ownership metadata. Verify MAF maps approved paths through public `SkillsProvider.from_paths` without changing its existing nested resource/script behavior. Copilot registers only individual approved paths and explicit `disabled_skills` names, with no broad roots or collection-folder copies. For Copilot, an approved parent plus an excluded nested child must deny child registration, resource reads, and scripts; an excluded parent plus an independently enabled nested child must allow the child through its own inventory, not its parent. Resolve each target to its most-specific canonical discovered root; reject traversal, symlink/alias escapes, out-of-root targets, ambiguous ownership, and string-prefix overlaps. `skills: false` exposes no skill/helper capabilities. Cover malformed-frontmatter logging-and-skip, existing name validation and duplicate errors, and role isolation. Verify no runtime-owned skill loader, prompt engine, three-tool abstraction, host script runner, or new approval gate is introduced. Verify approved names/descriptions reach the model without duplicate author prompts. These nested cases are acceptance requirements, not established live evidence; the live exclusion case covered siblings only. |
| Native skill helpers | Verify the Linux allowlist is `builtin:skill`, `builtin:view`, and `builtin:bash` where skills are enabled, with PowerShell disabled. Create and resume both install the same default-deny `on_permission_request` policy. Return `ApproveOnce` only for approved resource files in their permitted owning skill tree or validated approved-script commands. Exercise narrow literal script forms and arguments; deny unknown, ambiguous, and compound forms, broad shell prefixes, chaining, substitution, pipelines, and redirection. Reject arbitrary Bash before and after skill loading, regardless of intent, `cwd`, `toolCallId`, `possiblePaths` alone, or `allowedTools` metadata. A validated approved script may run from any turn; it runs with host privileges without skill-origin attestation or an OS sandbox. No approve-all, session-wide grants, or callback bypass is allowed. Linux execution remains to be exercised during implementation; existing Windows PowerShell evidence does not prove it. |
| Native tool accounting | Through public SDK `tool.execution_start` and `tool.execution_complete` events, verify allowed and permission-denied native `skill`, `view`, and `bash` calls appear exactly once per `toolCallId` in `AgentResult.tool_calls`, tool/error counts, and existing telemetry. Verify custom calls already captured by wrappers are not duplicated and `skill.invoked` metadata is not a second generic call. Preserve public `tool_start`/`tool_end`/`error` meanings, sanitized results, and sensitive-data/redaction policy without raw native envelopes or new public logging interfaces. Existing deadline/cancellation behavior and already-dispatched-effect limits remain unchanged. |
| Compatibility/role isolation | Verify flag-off MCP/tool behavior and unchanged MAF skill discovery/script behavior without Copilot startup. A minimal focused flag-off check is acceptable for this amendment. Structurally prove per-run provider state, capability-copy/catalog-leaf non-mutation, project-skill retention, and no unintended `data-driven-workflows` leakage. |
| Delegation/workflows | Prove fresh same-specialist concurrent sessions, catalog/role isolation, no child SSE, parent cancellation and specialist-local errors, Workflow Sub Agent grants/results, existing management/Activity retry/timeout/authorization, and at-least-once semantics. |
| Role storage/trigger execution | Run a non-HTTP trigger with its generated identity, serialization, logging/error behavior, and direct capabilities. Persistent direct state is isolated; delegates/Workflow Sub Agents leave no persistent native tree and dispose ephemeral state. |
| System tools | Exercise `web_request` defaults/exclusion/SSRF/budgets/errors and real ACA `execute_python` scoping/results without substituting local execution. |
| Completed-turn restore | On Blob and local storage, complete a real tool-using turn, replace Python and native processes, and continue by the same agent/session identity without restating prior values. Inspect outbound provider context/state reuse, not just a plausible answer. |
| Compacted restore | Force native compaction, acknowledge complete state, replace both processes, restore in a clean worker, and prove saved-summary/reference reuse without another compaction LLM call. |
| Storage failures/concurrency | Fault-inject append/replace/rename/read/ack errors, partial references, corrupt/unsupported state, crashes before completion, lease loss, stale writers, and two-worker same-session contention. For known pre-turn create/resume, validation, or startup-deadline failures that leave native state valid, verify no inference/tool effects and a successful retry on the same session ID. Uncertain submission or unsafe state must still fail explicitly. No silent reset, continued inference after a storage barrier failure, or false success/`done`; independent sessions still progress. |
| History break | MAF bytes remain unchanged. Incompatible IDs fail explicitly; native and MAF namespaces never cross-read as execution state. Verify documented rollback behavior and native history rendering without importing MAF messages. |
| Hosting/telemetry | Demonstrate supported Functions deployment assets, lazy single-client startup, concurrent isolation, bounded cancellation/shutdown, and no orphan native process. Verify usage/correlation/error accounting and sensitive-data-off behavior in host and native telemetry. |

## 7. Docs impact

This specification and its FRD index entry describe intended behavior, not
shipped implementation.
Implementation documentation must change with the behavior it documents:
`docs/architecture.md` for the adapter/lifecycle/storage boundaries and module
map entries for the MAF adapter, Copilot adapter, and neutral capability modules;
`docs/front-matter-spec.md` for preserved contracts and explicit incompatible
settings; `docs/observability.md` for native telemetry; `docs/workflows.md` and
`docs/triggers.md` where execution/error behavior changes; and `README.md`,
`docs/index.md`, `docs/getting-started.md`, and relevant samples for supported
preview use and the history break. Update the current MAF-only statements in
`AGENTS.md` when the implementation changes that invariant. Any schema change
requires regenerating the configuration reference and synchronizing examples;
this document does not introduce an unimplemented schema or rewrite runtime docs.

Documentation for issue #1336 compatibility must update `README.md`,
`docs/architecture.md`, `docs/front-matter-spec.md`, the preview compatibility
matrix, and the local Copilot preview sample. Document existing MCP authoring
and filters, remote HTTP/streamable-HTTP mapping, same-session create/resume
and between-turn credential replacement, static-header limits, existing auth
and error behavior, and minimal unattended approval. Also document thin
SDK-owned skill integration, trusted-code/non-sandbox boundary, preserved MAF
skill discovery/filtering and nested resource/script baseline, explicit
frontmatter disabled names and most-specific nested target ownership, Linux
`builtin:skill`/`builtin:view`/`builtin:bash` mapping with default-deny approve-once
resource/script actions, native tool-call/error accounting through sanitized
public SDK events, ordinary runtime `@tool` mapping, and flag-off behavior.
Distinguish Windows-only live helper evidence from untested Linux Bash execution.
The sample must be copy/paste complete for setup, request, expected failure, and cleanup.

## 8. Status & sign-off

- **Status:** In review. The MCP/skills amendment needs review of this revision
  and full human sign-off before product implementation. The parent migration is
  not complete or production-qualified.
- **Original sign-offs:** Laveesh Rohra (`larohra`) approved the behavior-focused
  session contract on 2026-09-28 and the Copilot provider contracts on 2026-09-29.
  Those sign-offs do not approve the MCP/skills amendment.
- **Architecture review:** The 2026-09-28 review remains historical for the
  original app-bound selection, Durable lifecycle, and safe startup behavior.
  The 2026-10-02 amendment review found nested-skill ownership and native tool
  accounting gaps. This text addresses both and retains the Linux Bash target.
  No independent re-review of those corrections or this revision has occurred.
- **Amendment direction:** The approved filter/auth boundaries, harness-neutral
  interface, ordinary runtime `@tool` mapping, and thin SDK-owned skills remain
  in sections 4.3.1-4.3.2. Nested exclusions, scoped helper permissions, the Linux
  Bash target, existing role contracts, and minimal flag-off validation are
  unchanged. The latest human direction limits MCP to MAF parity and asks that
  further gaps come back for review. This is not full amendment sign-off or
  approval of the proposed SDK helper mapping.
- **Open question: managed approvals.** The SDK's standard helper raises when
  managed settings are enabled and returns no approval when a request requires
  managed approval. These source-level limits are not a demonstrated failure in
  our configured runtime. Their effect on unattended MCP calls needs human
  review; no new host policy, bypass, or preview restriction is proposed.
- **Open question: reading user code.** Should native `view` read project code
  outside approved skill resources? The current MAF setup has no general file
  reader by default. This would expand scope and needs separate human approval.
  The operative `view` policy remains skill-resource-only.
- **Remaining gates:** Review and full amendment sign-off are required before
  returning to `Finalized` or implementing. Section 4.8 records preview support
  limits; section 6 defines acceptance, not achieved results. Linux helper
  execution remains unverified.
