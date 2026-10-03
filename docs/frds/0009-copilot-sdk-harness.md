---
frd: 0009
title: Copilot SDK agent harness
status: Finalized
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
previews with explicit capability checks. For Copilot-owned sessions, the host
provides only a thin filesystem adapter and storage selection boundary; the SDK
owns continuation, compaction, recovery, file contents, and format compatibility.

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
surfaces while narrowing the host's persistence responsibility.

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
Some current capability payloads are MAF objects. Section 4.3.1 defines their
planned SDK-neutral representation and per-harness adaptation. The persistence
interface in section 4.5 is narrower: it routes storage without changing shared
tool definitions or discovery contracts.

| Pipeline stage | Existing modules | Required responsibility |
| --- | --- | --- |
| discover | `discovery/tools.py`, `discovery/mcp.py`, `discovery/skills.py`, `_function_tool.py` | Keep project inventories and discovery rules; separate framework wrapping from author intent. Do not run inference or launch the native runtime during discovery. |
| translate | `config/schema.py`, `config/merge.py`, `config/validation.py` | Preserve typed composition, inheritance/null semantics, and effective capability validation. Interpret no new harness selector in agent files. |
| compose/register | `app.py`, `registration/capabilities.py`, `registration/catalog.py`, `registration/_handlers.py`, `registration/endpoints.py`, `registration/triggers.py` | Resolve the app's preview choice before harness-specific bootstrap; validate the complete catalog before FunctionApp mutation; pass resolved values to lazy handlers. Keep Azure registration and inbound authorization here. |
| execute | `runner.py`, `client_manager.py`, internal Copilot adapter | Create/resume sessions, bind approved tools, enforce deadlines, and translate events/results. `ClientManager` remains provider access, not the agent loop, tool dispatcher, or session manager. |
| persist | `_agent_identity.py`, `_history_identity.py`, `_session_id.py`, separate native SessionFs adapter; existing `_blob_history.py`/`_file_history.py` on the MAF path | Route persistence only through the selected harness. Preserve validation and path-containment rules, reuse the shared readable agent ID for native paths, and treat Copilot session bytes as opaque SDK-owned files. |
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

This amendment defines Model Context Protocol (MCP) and scoped-skill
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
MAF's existing `never_require` default. The adapter installs its internal
`on_permission_request` callback on create and resume. This is not user-facing
configuration.

For ordinary configured MCP requests (`kind == "mcp"`), the callback delegates
to `PermissionHandler.approve_all`, the SDK's approve-once helper.
Native `mcpServers.tools` enforces the authored filter. All other request kinds
retain the agreed skill-helper handling and default deny. Do not install a
global approve-all handler or broaden shell/read/edit access. The helper's
managed-approval limits still apply; section 8 records the qualification limits.

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
`builtin:bash`. The host owns the
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
separately approved. Disabled built-ins: files
(`create`, `edit`, `grep`, `glob`), network
(`web_fetch`), agents (`task`, `read_agent`, `write_agent`, `list_agents`),
interaction/planning (`ask_user`,
`task_complete`, `exit_plan_mode`, `send_inbox`, `context_board`), tool search,
and infinite sessions.

Add structural tests that capability copies cannot mutate catalog leaves and
ordinary project skill roots do not receive workflow-only guidance unless the
existing role contract already adds it.

### 4.4 Native runtime lifetime

The proposed default is the external native Rust runtime over stdio, with one
lazily initialized, process-long SDK client per frozen app/storage context in a
Functions worker, reused across invocations with isolated sessions. Concurrent
initialization must not launch duplicate runtimes for the same context.
Request cancellation must not close the shared client or poison unrelated
sessions; worker shutdown must release its client/process.
Initialization, process-local session-lock waits, execution, and storage
operations are bounded by the request deadline.

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
includes the app correlation key and canonical agent slug. The correlation key
joins the trimmed, available `WEBSITE_OWNER_NAME` and `WEBSITE_DEPLOYMENT_ID`
(or `WEBSITE_SITE_NAME` fallback) with `/`, lowercases them, and uses `local`
when no platform metadata is available. Do not add another app-identity segment
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

Unsupported capability, invalid configuration, adapter-path validation, backend
configuration, and filesystem/provider failures must be diagnosable without
sensitive payloads. Map them through existing HTTP/MCP error envelopes and
terminal SSE `error`, not a new success-shaped response or an automatic MAF
fallback. Cancellation stays cancellation. Already-dispatched tool effects may
remain after an unsuccessful turn; the feature does not claim transactional or
exactly-once execution.

### 4.8 Preview limits and approved provider contract

The Copilot preview remains local-only and requires a single Functions worker.
Azure Functions hosting, public streaming/structured-response parity, MCP,
scoped skills, delegation, workflows, full system-tool parity, and cross-worker
session overlap are unsupported in this preview unless separately qualified.
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

The SDK-owned persistence boundary supersedes earlier completed-turn durability,
lease-fenced envelopes, workspace aliases, host rollback/recovery, and native
format-version guard proposals. Earlier qualification evidence belongs to those
superseded designs, not to the redesigned thin adapter. Section 4.5 defines the
current persistence contract; its interface separation does not replace the
planned capability adaptation in section 4.3.1.

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
| 36 | MCP-only SDK helper branch | Global approve-all / SDK helper restricted to MCP requests | Use the SDK standard `PermissionHandler.approve_all` approve-once helper only for `request.kind == "mcp"` in the shared callback. All other request kinds retain scoped skill checks and default deny; no global approve-all. | Human (larohra) | 2026-10-02 |
| 37 | Full MCP/skills amendment sign-off | Keep In review / finalize with comment cleanups | Finalize the full amendment with the internal MCP-only SDK approve-once branch and Linux helper wording clarified. Scoped skills, SDK managed-approval limits, and skill-resource-only `view` are unchanged. | Human (larohra) | 2026-10-02 |
| 38 | Native session delivery and persistence | Shared MAF JSONL or fragmented native Blob tree / one isolated lease-fenced envelope | Propose sections 4.5/4.9's encoded identities, frozen context, single-object local/Blob SessionFs, serialized/latching callbacks, safe pre-handoff rollback, uncertain post-handoff state, metadata-only history guards, tombstone deletion and native compaction after #241. Dedicated architecture-agent re-review APPROVED these mechanics on 2026-09-29 after two REVISE reviews; qualification was still open at this decision and is completed by the later #1335 qualification decision. Human-approved contracts/status are unchanged. | Agent proposal; architecture-agent approval | 2026-09-29 |
| 39 | Recorded host workspace on resume | Fuzzy suffix matching of recorded paths / protocol v4 native envelopes with virtual workspace aliases | Use protocol v4 native envelopes; persist and alias the creating and current worker host workspace paths to virtual `/workspace` for cross-worker restore. Reject protocol v3 preview envelopes, requiring fresh session IDs, and leave MAF unaffected. Dedicated architecture re-review approved this design conditional on explicit human acknowledgement; that acknowledgement is recorded by this decision. | Human (larohra); architecture re-review approved conditional on acknowledgement | 2026-09-30 |
| 40 | #1335 qualification boundary | Treat mocked/local evidence as sufficient / qualify real Blob continuation and compaction while retaining later gates | Accept the sanitized section 4.8 evidence as completing #1335: real Entra Blob protocol tests, replacement-process tool-result continuation and semantic compacted-summary reuse. Do not claim verbatim arbitrary-token retention, Functions hosting, dual-harness end-to-end qualification or production activation; retain those gates in #1357/#1337. | Human (supplied qualification evidence) | 2026-09-30 |
| 41 | Copilot session persistence ownership | Host recovery/state protocol / thin SessionFs adapter | The host provides only filesystem operations, containment, metadata, and SDK-shaped errors. The SDK owns continuation, compaction, recovery, file contents, formats, and format compatibility. No host envelope, completed-turn guarantee, rollback logic, native-state checks, handoff markers, tombstones, or recovery/controller protocol. | Human (larohra) | 2026-10-02 |
| 42 | Persistence backend selection | New Copilot-specific setting or environment heuristic / reuse existing storage configuration | Reuse `AzureWebJobsStorage` connection string or `AzureWebJobsStorage__blobServiceUri` with existing storage-specific identity/container behavior. Select Blob when configured, local only when neither is configured, and never fall back from configured Blob failures to local storage. | Human (larohra) | 2026-10-02 |
| 43 | Native identity and path scheme | New opaque hash scheme / shared readable identity | Use `copilot-native/{agent_id}/{session_id}/{sdk_relative_path}`, with `agent_id` supplied by the shared helper and already containing the app correlation key and canonical slug. Retain session-ID validation and path containment without a separate hash scheme or host format version. | Human (larohra) | 2026-10-02 |
| 44 | Session concurrency boundary | Distributed leases/fencing/OS locks / process-local serialization | Match the current MAF boundary: serialize only same-session turns within a Python process, with bounded waiting and no distributed exclusion. Cross-worker overlap is unsupported and caller-owned. | Human (larohra) | 2026-10-02 |
| 45 | Interface separation scope | Broad execution-interface rewrite / persistence-only boundary | Keep the interface split narrowly about persistence. Shared app/configuration/registration routes through the selected harness; MAF owns its existing history provider, Copilot owns SessionFs. Construct/use/close only the selected persistence adapter, without opposite-harness imports, storage initialization, history probes, or cleanup, and without expanding scope into unrelated tool/model/discovery refactors. | Human (larohra) | 2026-10-02 |

## 6. Feature-level acceptance and test plan

Acceptance is behavioral, not an assertion that SDK feature names imply parity.
Extend tests mirroring the affected source modules, with configuration scenarios
under `tests/fixtures/config_scenarios/`. Use real SDK/storage/hosting evidence
where mocks cannot establish process, transport, authentication, or durability.

| Area | Acceptance evidence |
| --- | --- |
| Selection/isolation | Exercise unset, `false`, `0`, `true`, `1`, mixed-case/padded text, empty/whitespace-only/invalid values, multiple app contexts, and standalone entry points. Off starts no Copilot process/download/auth/telemetry; on is uniform across all roles and never falls back. Existing coverage or a minimal focused flag-off check is sufficient for this amendment; no new exhaustive suite is required. |
| Context propagation/lifetime | Construct two same-root app contexts with different flag snapshots and frozen storage settings; assert neither changes on environment mutation nor shares a native client. Delayed handlers, delegates, and Activity calls retain their own context. Cover explicit/default standalone contexts. On worker replacement, completed Activity results replay normally and newly executed/redelivered Activities use the serving app's context, without adding harness state to Durable history or changing its scheduling/version-routing rules. |
| Unsupported features | Effective inherited/default-on capabilities and unmapped configuration/extensions fail before provider inference or tool effects. Isolated previews of supported capabilities execute real SDK turns. |
| Authoring/API | Existing precedence/null scenarios, tool `None`/empty semantics, routes/auth, response envelopes, structured-output validation/errors, history projection/degradation/errors/bounds, and SSE ordering/cancellation remain compatible. No native or specialist events leak. |
| Models/extensions | Verify supported providers/Entra refresh, model metadata, disabled provider conversation storage, deadlines, output limits, and explicit custom-manager/tool compatibility, including a manager replaced after composition. MAF hooks remain intact off. |
| Tools | Cover ordinary runtime `@tool` mapping to neutral metadata, sync/async, Pydantic, both decorator orders, workflow-only tools, approval options, and denied ambient capabilities. Assert MAF maps neutral tools to `FunctionTool`, Copilot maps them to Copilot tools, callable/effect counts remain stable, and no unexpected interactive approval gate appears. Unmapped MAF-only extensions fail explicitly on Copilot. |
| Import boundary / neutral interface | Structurally assert `HarnessRequest` and discovery/registration outputs contain only immutable SDK-free descriptors. Assert `agent_framework` imports are limited to the MAF adapter and named MAF-compat public surfaces, and Copilot imports are limited to the Copilot adapter. Delete-or-stub the MAF adapter in a smoke test to prove discovery, registration, runner contracts, and Copilot descriptor mapping do not change. |
| MCP compatibility | Exercise remote HTTP/streamable-HTTP mapping at public create/resume and existing per-agent disable/exclude filters. Test omitted `tools`, any list containing `"*"`, `[]`, and exact-name allowlists through native `mcpServers.tools`. Preserve discovery warnings, skipped entries, and `failed_loads`. Complete an actual MCP call and turn, then resume the same native session through the existing lock/disconnect lifecycle and make another call with fresh headers. Prove prior user/tool/assistant history reaches the resumed model and new headers replace old ones. Verify static headers without auth; empty/whitespace scope warnings without token acquisition; default credentials for missing/unresolved client IDs; resolved client-ID selection; and generated `Authorization` precedence. Token, connection, initialization, and tool errors follow ordinary SDK/runtime error-result propagation. Verify ordinary configured MCP calls use the MCP-only SDK approve-once branch without interaction. Shell/read/edit requests must retain the scoped skill-helper policy and default deny. Assert no host-added side-effect retry, stale-token, OAuth, dropped-capability, or empty-session fallback. |
| Skills compatibility | Exercise canonical `(name, path)` inventory construction and existing `skills: false`/`skills.exclude` selections, including valid nested skills. Build approved skill descriptors plus read-only discovered-root ownership metadata. Verify MAF maps approved paths through public `SkillsProvider.from_paths` without changing its existing nested resource/script behavior. Copilot registers only individual approved paths and explicit `disabled_skills` names, with no broad roots or collection-folder copies. For Copilot, an approved parent plus an excluded nested child must deny child registration, resource reads, and scripts; an excluded parent plus an independently enabled nested child must allow the child through its own inventory, not its parent. Resolve each target to its most-specific canonical discovered root; reject traversal, symlink/alias escapes, out-of-root targets, ambiguous ownership, and string-prefix overlaps. `skills: false` exposes no skill/helper capabilities. Cover malformed-frontmatter logging-and-skip, existing name validation and duplicate errors, and role isolation. Verify no runtime-owned skill loader, prompt engine, three-tool abstraction, host script runner, or new approval gate is introduced. Verify approved names/descriptions reach the model without duplicate author prompts. These nested cases are acceptance requirements, not established live evidence; the live exclusion case covered siblings only. |
| Native skill helpers | Verify the Linux allowlist is `builtin:skill`, `builtin:view`, and `builtin:bash` where skills are enabled. Create and resume both install the same default-deny `on_permission_request` policy. Return `ApproveOnce` only for approved resource files in their permitted owning skill tree or validated approved-script commands. Exercise narrow literal script forms and arguments; deny unknown, ambiguous, and compound forms, broad shell prefixes, chaining, substitution, pipelines, and redirection. Reject arbitrary Bash before and after skill loading, regardless of intent, `cwd`, `toolCallId`, `possiblePaths` alone, or `allowedTools` metadata. A validated approved script may run from any turn; it runs with host privileges without skill-origin attestation or an OS sandbox. No approve-all, session-wide grants, or callback bypass is allowed. Linux execution remains to be exercised during implementation. |
| Native tool accounting | Through public SDK `tool.execution_start` and `tool.execution_complete` events, verify allowed and permission-denied native `skill`, `view`, and `bash` calls appear exactly once per `toolCallId` in `AgentResult.tool_calls`, tool/error counts, and existing telemetry. Verify custom calls already captured by wrappers are not duplicated and `skill.invoked` metadata is not a second generic call. Preserve public `tool_start`/`tool_end`/`error` meanings, sanitized results, and sensitive-data/redaction policy without raw native envelopes or new public logging interfaces. Existing deadline/cancellation behavior and already-dispatched-effect limits remain unchanged. |
| Compatibility/role isolation | Verify flag-off MCP/tool behavior and unchanged MAF skill discovery/script behavior without Copilot startup. A minimal focused flag-off check is acceptable for this amendment. Structurally prove per-run provider state, capability-copy/catalog-leaf non-mutation, project-skill retention, and no unintended `data-driven-workflows` leakage. |
| Delegation/workflows | Prove fresh same-specialist concurrent sessions, catalog/role isolation, no child SSE, parent cancellation and specialist-local errors, Workflow Sub Agent grants/results, existing management/Activity retry/timeout/authorization, and at-least-once semantics. |
| Role storage/trigger execution | Run a non-HTTP trigger with its generated identity, serialization, logging/error behavior, and direct capabilities. Persistent direct state uses the selected harness's storage path only; delegates and Workflow Sub Agents leave no persistent Copilot session tree and dispose ephemeral state. |
| SessionFs file contract | Exercise exact byte preservation and SDK-visible behavior for read, write, append, exists, stat, directory listing with entry types, mkdir, remove, rename, and documented file errors on both local and Blob adapters. Preserve all SDK-requested files opaquely rather than host-specific file whitelists or content interpretation. |
| Identity/path isolation | Verify native paths reuse the shared agent ID, including its app correlation key exactly once, followed by validated session ID and SDK-relative path. Cover partial/local identity fallbacks and containment enforcement without introducing separate identity hashes. |
| Backend configuration/errors | Verify Blob selection from existing storage configuration, local selection only when no Blob configuration is present, reuse of existing identity/container behavior, and explicit surfacing of Blob auth/network/configuration failures without fallback to local storage. |
| Persistence boundary isolation | Verify that only the selected harness's persistence implementation is imported, initialized, exercised, and closed. The Copilot path must not probe or clean up MAF history storage, and the MAF path must not initialize Copilot SessionFs. |
| Same-process concurrency | Verify bounded waiting and serialization for concurrent turns targeting the same `(agent, session)` within one Python process, while independent sessions remain concurrent. Do not require distributed exclusion, cross-worker ownership, or OS-level locking for this feature. |
| SDK integration boundary | Exercise real SDK callbacks against the adapter and verify that the host does not interpret native session contents, claim recovery semantics, or impose its own compaction/summary protocol. |
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
preview use and the SDK-owned persistence boundary. Update the current MAF-only statements in
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
Do not claim Linux helper qualification before it is exercised.
The sample must be copy/paste complete for setup, request, expected failure, and cleanup.

## 8. Status & sign-off

- **Status:** Finalized. The full MCP/skills amendment is design-approved.
  Finalization does not mean implementation or production qualification; the
  parent migration remains incomplete.
- **Original sign-offs:** Laveesh Rohra (`larohra`) approved the behavior-focused
  session contract on 2026-09-28 and the Copilot provider contracts on 2026-09-29.
  The amendment sign-off below is separate.
- **Persistence review and sign-off:** The persistence-only interface and
  SDK-owned session boundary were architecture-reviewed. Laveesh Rohra
  (`larohra`) approved the revised persistence design on 2026-10-02. It supersedes
  earlier storage/recovery mechanics while retaining their historical decisions.
  App-bound selection, Durable lifecycle, and provider/model contracts remain
  in force. This storage split leaves MAF with its existing history provider and
  Copilot with SessionFs; it does not implement the planned capability interface
  in section 4.3.1.
- **Architecture review:** The 2026-09-28 review remains historical for the
  original app-bound selection, Durable lifecycle, and safe startup behavior.
  The 2026-10-02 amendment review found nested-skill ownership and native tool
  accounting gaps. This text addresses both and retains the Linux Bash target.
  No independent re-review of those corrections or this revision has occurred.
- **Amendment sign-off:** Laveesh Rohra (`larohra`) approved the full MCP/skills
  amendment on 2026-10-02 with the callback and Linux-helper wording cleanups.
  The approved contract remains in sections 4.3.1-4.3.2: MAF-parity-only MCP,
  an internal MCP-only SDK approve-once branch, harness-neutral capabilities,
  ordinary runtime `@tool` mapping, and thin SDK-owned scoped skills. Nested
  ownership, Linux helper restrictions, existing role contracts, and minimal
  flag-off validation are unchanged. Further MCP gaps require human review.
- **SDK managed-approval limits:** The SDK's standard helper raises when
  managed settings are enabled and returns no approval when a request requires
  managed approval. Those limits remain effective; no bypass, workaround, or
  new managed policy is authorized. The affected unattended MCP scenarios remain
  unqualified, not demonstrated configured-runtime failures.
- **Scope boundary:** Native `view` may read approved skill resources only.
  General project-code reading is outside the agreed scope and has no human
  approval.
- **Remaining qualification:** Section 4.8 records current preview support
  limits; section 6 defines acceptance, not achieved results. Implementation
  must meet those requirements. Linux helper execution and managed-approval
  scenarios remain unverified.
