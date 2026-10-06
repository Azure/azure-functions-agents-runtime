---
frd: 0010
title: HostedSkill binding
status: Finalized
author: victoriahall
created: 2026-10-05
updated: 2026-10-05
issues:
  - https://github.com/Azure/azure-functions-bucees-planning/issues/1362
pull_requests: []
branch: hallvictoria/hosted-skill-binding
---

# FRD 0010 - HostedSkill binding

## 1. Summary

Add an opt-in `@app.hosted_skill` decorator to the Function App returned by
`create_function_app()`. The decorator injects a runtime-owned `HostedSkill`
object into an ordinary asynchronous Azure Function handler, allowing
deterministic application code to invoke an existing markdown-defined agent by
identity slug. `HostedSkill` exposes explicit `run()` and `stream()` methods,
reuses the runtime's existing discovery, frontmatter, capability, provider,
session, and execution contracts, and does not expose the current Microsoft
Agent Framework (MAF) implementation.

The feature ships in `azurefunctions-agents-runtime`. It is not a new extension
package, Functions host binding type, worker converter, or harness provider.

## 2. Motivation / problem

Today `create_function_app()` turns every `.agent.md` file into runtime-owned
triggers or built-in endpoints. An application that already has deterministic
Functions cannot selectively call one of those agents from the middle of its
own handler without bypassing the resolved catalog and directly reconstructing
runner arguments. Doing so duplicates configuration, couples application code
to runtime internals, and risks changing tool filters, provider precedence,
session identity, or response validation.

The Agent Binding feature in `azure-functions-python-extensions` demonstrates
the useful handler-injection shape, but it injects a provider SDK object through
separate extension packages. Hosted Skills needs a different contract: the
binding is owned by this runtime, selects an already composed `.agent.md`
definition, and injects a stable facade over whichever harness the runtime
supports. Existing deterministic behavior must remain unchanged unless a
handler explicitly opts in.

The current Hosted Skills contract does not define a Foundry connection object
in frontmatter. Provider selection and authentication are environment-owned;
the `.agent.md` and `agents.config.yaml` inputs contribute agent behavior and
the requested model. The binding must preserve that division rather than add a
second schema or duplicate connection settings in the decorator.

## 3. Goals / Non-goals

**Goals**

- Add `@app.hosted_skill(arg_name=..., agent_name=...)` to both normal and
  Durable app objects returned by `create_function_app()`.
- Inject a harness-neutral `HostedSkill` into an ordinary asynchronous Function
  while hiding the runtime-managed parameter from worker indexing.
- Resolve `agent_name` against the existing immutable `AgentCatalog` by identity
  slug and reuse the selected agent's resolved instructions and capabilities.
- Expose explicit `run()` and structured `stream()` methods with caller-visible
  session continuation.
- Preserve the selected agent's model, timeout, agent configuration, filtered
  user tools, MCP servers, skills, sandbox, `web_request`, response contract,
  history, usage accounting, and observability.
- Let a `.agent.md` file exist solely as an internal HostedSkill target without
  registering a trigger or built-in endpoint.
- Fail with actionable diagnostics for unknown agents, invalid decorator use,
  missing provider configuration, unsupported capabilities, malformed existing
  frontmatter, and invalid structured responses.
- Preserve every existing trigger, endpoint, and SSE wire contract when no
  HostedSkill decorator is used.
- Document that ownership, versioning, and release follow
  `azurefunctions-agents-runtime`.

**Non-goals**

- A new package in `azure-functions-python-extensions`, provider entry point,
  Functions host binding, worker converter, or generated binding metadata.
- Exposing raw MAF, Copilot SDK, LangGraph, or other harness objects or options.
- A new `.agent.md`, `agents.config.yaml`, provider, connection, or
  authentication schema.
- File-path declarations or binding-level overrides for model, provider,
  timeout, tools, skills, MCP, connection, or instructions.
- Callable-object shorthand such as `await skill(...)`; all operations use
  explicit `skill.<method>` APIs.
- Structured request input or `input_schema` enforcement. Version 1 accepts a
  string prompt.
- Chat-time Sub Agent delegation, Dynamic Workflow management, or Copilot
  preview execution through HostedSkill in version 1.
- Changing deterministic Functions that do not apply the decorator.

## 4. Proposed design

The binding compiles from the same immutable `AgentCatalog` used by delegation
and registration. It does not independently read or parse an agent file. This
keeps the existing discover -> translate -> compose -> register -> execute
pipeline authoritative and avoids a second interpretation of frontmatter.

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| discover | `config/loader.py`, `discovery/*` | No new discovery format. Existing agent files, project tools, MCP servers, and skills remain the complete inputs. |
| translate | `config/merge.py`, `config/validation.py` | Reuse `ResolvedAgent`. Permit a fully validated agent with no external surface so it can be an inert internal catalog entry. |
| compose | `app.py`, `registration/catalog.py`, `_hosted_skill_app.py` | Build the existing complete catalog before app mutation, then initialize an enhanced FunctionApp or DFApp with that catalog and the frozen app harness. |
| register | `_hosted_skill_app.py`, existing Azure registration modules | Add a Python decorator that wraps an application handler and hides its injected parameter. Register no new host binding type. Existing automatic surfaces are unchanged. |
| execute | `hosted_skill.py`, `runner.py`, `streaming_events.py`, `response_contract.py`, `client_manager.py` | Map facade calls to the existing runner inputs, expose typed stream events, enforce the response contract, and preflight provider configuration without constructing a client. |

### 4.1 Authoring and selection

The author first creates the runtime app, then applies `hosted_skill` as the
innermost decorator on an asynchronous handler:

```python
from azurefunctions.extensions.http.fastapi import Request, Response

from azure_functions_agents import HostedSkill, create_function_app

app = create_function_app()


@app.route(route="orders/prepare", methods=["POST"])
@app.hosted_skill(arg_name="skill", agent_name="order-preprocessor")
async def prepare_order(
  req: Request,
    skill: HostedSkill,
) -> Response:
  result = await skill.run((await req.body()).decode("utf-8"))
  return Response(result.content, media_type="text/plain")
```

`agent_name` is the existing identity slug derived from the discovered agent
filename. For example, `order-preprocessor.agent.md` is selected with
`agent_name="order-preprocessor"`. The decorator accepts no path and no agent
configuration overrides. An unknown slug fails when the decorator is applied.

The decorated function must be declared with `async def`, and the named
parameter must be positional-or-keyword or keyword-only. The decorator resolves
the parameter annotation when possible and requires `HostedSkill`; it removes
that parameter from `__signature__` before an outer trigger decorator indexes
the function. It uses `functools.wraps()` so `__wrapped__` and ordinary handler
metadata still identify the original function, then assigns the reduced worker
signature to the wrapper. Outer decorators therefore inspect the wrapper without
the runtime-managed parameter. Passing that hidden parameter from the worker or
application code is an error. The wrapper otherwise preserves argument binding,
defaults, variadic arguments, return values, exceptions, and cancellation.

The app creates a fresh facade for each Function invocation. A facade retains
only the selected immutable catalog entry and app execution context; it does not
retain a live MAF Agent, open MCP connection, model client, or session resource.
Each `run()` or `stream()` call creates and cleans up its own execution resources
through the runner. The facade must not be retained and used after its Function
invocation completes. A handler that exits before calling either method has no
HostedSkill resource to clean up. Method calls are independent and safe under
concurrent Function invocations; retaining the facade in module-level state is
unsupported even though the facade itself contains no live harness object.

### 4.2 App types and catalog lifetime

A shared internal mixin provides the decorator. Public concrete
`HostedSkillFunctionApp` and `HostedSkillDFApp` classes combine that mixin with
`azure.functions.FunctionApp` and `azure.durable_functions.DFApp`, respectively.
They remain instances of their corresponding SDK base and preserve its indexing
and registration behavior. `create_function_app()` returns the union of those
two concrete types, so static analysis exposes `.hosted_skill` regardless of
which existing workflow-policy condition selects the Durable variant. It does
not use a wrapper container or multiple inheritance between the two SDK app
types.

The app receives the complete immutable `AgentCatalog` only after pass 1
composition succeeds. Decorator application looks up and compiles a selected
entry once. Per-invocation facades reuse that compiled description while all
live execution state remains per call.

An agent with neither a trigger nor a built-in endpoint is a valid inert catalog
entry. It still goes through frontmatter parsing, global merge, duplicate-slug
checks, reference validation, capability filtering, harness validation, and
catalog freezing. It registers no Function unless application code later
selects it with `@app.hosted_skill` or another supported internal reference.
This relaxes the current external-surface requirement for all discovered agents
because decorators execute only after `create_function_app()` returns; startup
cannot know which entries later decorators will select. Composition therefore
removes that requirement rather than adding a false
`is_selected_by_hosted_skill` marker; decorator lookup is the later proof that a
particular inert entry is used.

### 4.3 HostedSkill object API

The version 1 public surface is:

```python
class HostedSkill:
    async def run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
    ) -> AgentResult: ...

    def stream(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
    ) -> AsyncIterator[HostedSkillEvent]: ...
```

`HostedSkill` has no `__call__` method. A blank or non-string prompt is rejected
before provider or tool effects. Session IDs use the runtime's existing safe
validation. Omission creates a fresh caller-visible ID; supplying the returned
ID on a later call resumes the selected agent's conversation through the
existing `(agent_slug, session_id)` history contract. Calls using the same
agent/session retain the existing serialization and timeout behavior.

HostedSkill resolves or validates the public session ID before constructing
per-session capabilities. It passes that exact ID both to
`build_sandbox_tools_for_session()` and to the runner, so ACA Dynamic Sessions
state and persisted conversation history cannot drift into different identity
buckets. A caller-provided ID is also the returned `AgentResult.session_id`.

`run()` returns the existing `AgentResult`. It forwards the selected entry's
resolved instructions, model, timeout, `agent_configuration`, filtered user
tools, MCP tools, skill paths, and `web_request` tools. It creates ACA Dynamic
Sessions tools after resolving the run's session ID so `execute_python` uses
that same public identity. It passes shallow copies of mutable tool, MCP, and
skill-path lists so neither the runner nor an SDK can mutate the catalog's
capability lists. HostedSkill uses the same process-wide ACA credential and HTTP
connection pools as existing agent surfaces; the sandbox module continues to
own those shared resources while each method call owns only its bound tool
closure and stream lifecycle.

If `response_example` or `response_schema` is configured, HostedSkill prepends
the same response-format instructions used by HTTP handlers and validates the
completed output with the same fenced-JSON extraction and JSON Schema rules.
Invalid output raises `HostedSkillResponseError`; it does not return a partially
valid `AgentResult`.

### 4.4 Structured streaming

`stream()` returns harness-neutral `HostedSkillEvent` values rather than
HTTP/SSE strings. A frozen event type and finite `HostedSkillEventKind` describe
the existing public vocabulary:

- `session` with the resolved session ID;
- `delta`, `message`, or `intermediate` with content;
- `tool_start` with call ID, name, and complete arguments;
- `tool_end` with call ID, name, and result;
- terminal `done` or `error`.

The runner owns one structured event iterator. Existing `run_agent_stream()`
becomes an adapter that serializes those events to its unchanged
`data: <JSON>\n\n` contract. Existing event ordering, fragmented tool-argument
coalescing, deadlines, stream finalization, cancellation, usage recording,
tool-error accounting, spans, and unknown-content handling remain authoritative.

The structured iterator buffers function-call argument chunks just as the
current runner does: it emits `tool_start` when the accumulated argument value
is complete JSON, or immediately before the corresponding result/end of stream
if completion cannot be detected sooner. Consumers receive one coalesced start,
not raw MAF fragments. `session` is first; content, reasoning, and tool events
then retain provider order; exactly one terminal `done` or `error` is last.

An error event is terminal and is never followed by `done`. Consumer
cancellation propagates rather than becoming an error event. If a response
schema is configured, HostedSkill accumulates only final assistant text from
`delta` and `message` events, excluding reasoning and tool results. Validation
runs once after the underlying stream completes normally and before forwarding
its `done` event, using the same complete fenced-JSON extraction and schema
validation as `run()`. It cannot retract already consumed deltas; validation
failure emits a non-sensitive response-contract error and suppresses `done`.
Consumers that need the final JSON must likewise accumulate text events.

When a consumer breaks iteration, cancels, or closes the generator, a
`try`/`finally` boundary closes the core iterator. Generator exit and task
cancellation reach the runner's existing MAF finalization path, which settles
usage and provider callbacks; MCP and other context-managed execution resources
are released by the same agent/session teardown. Cancellation is not converted
to a model-visible or HostedSkill error event.

### 4.5 Provider and Foundry contract

The decorator runs a side-effect-free provider preflight for the selected
HostedSkill. `ClientManager` gains
`validate_provider_settings(model: str | None) -> None`, whose default is a
no-op, so a custom manager remains responsible for its own provider contract.
HostedSkill always calls the active manager's hook. A custom implementation may
raise an actionable configuration error; if it inherits the no-op, validation
is intentionally deferred to that manager's client-build/runtime behavior.
`MAFClientManager` implements the hook by reusing its existing provider and
model resolution without constructing a chat client, credential, or network
connection.

For the built-in Foundry provider, `FOUNDRY_PROJECT_ENDPOINT` is required and
must be nonblank. Authentication continues through the runtime's existing
`DefaultAzureCredential` construction at execution. No connection value is
accepted by `.agent.md` or the decorator. Model precedence remains requested
resolved model -> provider-specific environment -> runtime model -> provider
default. Consequently, `FOUNDRY_MODEL` is supported and recommended but is not
required when another resolved value or the existing default applies.
Foundry preflight resolves that model through the same helper and succeeds when
resolution reaches the default.

Diagnostics identify the missing setting or corrective action without echoing
endpoint values, credentials, tokens, or other secrets. Explicit provider
selection and existing autodetection order do not change: an explicit
`AZURE_FUNCTIONS_AGENTS_PROVIDER` wins; otherwise Azure OpenAI endpoint,
Foundry project endpoint, then OpenAI key are checked in that order.

### 4.6 Version 1 capability boundary

Version 1 supports the default MAF harness and the selected entry's direct-role
filtered user tools, MCP servers, project skills, sandbox, and `web_request`.
It rejects the following at decorator application rather than silently dropping
them:

- an entry with chat-time `subagents`;
- an entry with Dynamic Workflows enabled;
- an app using the bounded Copilot preview harness.

The decorator is a method on an already constructed enhanced app and reads that
app's frozen harness directly. Copilot rejection is therefore a constant-time
check with no SDK import or process startup.

The selected agent may still define its own trigger or built-in endpoint; those
surfaces coexist and keep their existing behavior. `input_schema` is not applied
to HostedSkill because its version 1 input is already a typed string prompt. If
the same agent defines `input_schema` for an HTTP surface, that surface still
validates request bodies while HostedSkill explicitly ignores the field.
Other valid frontmatter is reused where it affects direct execution and ignored
only where it exclusively configures an independently registered surface, such
as trigger arguments or endpoint authentication.

### 4.7 Errors, lifecycle, and observability

Configuration and decorator errors fail during app import with the selected
slug and an actionable field or API diagnostic. Runtime inference, MCP, tool,
history, timeout, and response-contract failures preserve their existing
exception or terminal-stream behavior. Cancellation always propagates and must
close in-flight streaming resources through the core iterator's finalizer. No
live model, MCP, sandbox, or session resource exists merely because a facade was
injected, so a handler that never invokes it requires no special cleanup.

HostedSkill execution uses the existing agent run and token-usage telemetry and
adds a non-sensitive execution-surface attribute identifying `hosted_skill`.
Content remains governed by the existing sensitive-data setting. Logs and spans
must not include provider secrets, endpoint credentials, raw connection values,
or content unless existing policy explicitly permits it.

### 4.8 Packaging and compatibility

The public classes and enhanced app types are delivered, versioned, and released
with `azurefunctions-agents-runtime`. There is no dependency from this runtime
to `azure-functions-python-extensions`, and no new distribution, entry point,
extension bundle requirement, or Functions host metadata.

Existing calls to `create_function_app()` remain source-compatible and return a
subclass of the same normal or Durable app base used today. Existing registered
functions, routes, bindings, and metadata are unchanged when the decorator is
unused. The deliberate compatibility change is that a valid endpoint-less
agent file no longer fails startup and instead becomes an inert catalog entry.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Owning package | Runtime / new extension package / provider package | Implement and release directly in `azurefunctions-agents-runtime`; no new package or host binding | Human | 2026-10-05 |
| 2 | Declaration shape | App method / standalone decorator / both | `@app.hosted_skill(arg_name=..., agent_name=...)` on apps returned by `create_function_app()` | Human | 2026-10-05 |
| 3 | Agent selection | Identity slug / explicit file path / both | Select the existing composed catalog entry by identity slug only | Human | 2026-10-05 |
| 4 | Invocation API | Explicit methods / callable shortcut / both | Explicit `run()` and `stream()` methods only; `HostedSkill` is not callable | Human | 2026-10-05 |
| 5 | One-shot result and sessions | `AgentResult` with explicit session / text with sticky invocation session / both | Return `AgentResult`; omitted session starts fresh and explicit session resumes | Human | 2026-10-05 |
| 6 | Streaming API | Typed events / text deltas / SSE strings | Yield structured `HostedSkillEvent`; retain SSE only as an HTTP adapter | Human | 2026-10-05 |
| 7 | Reused configuration | Minimal tools only / all direct execution settings / binding overrides | Reuse model, timeout, agent configuration, filtered tools/MCP/skills, system tools, and response contract; add no overrides | Human | 2026-10-05 |
| 8 | Internal-only agents | Require existing surface / allow inert entries / predeclare in factory | Permit endpoint-less discovered agents as fully validated inert catalog entries | Human | 2026-10-05 |
| 9 | Version 1 advanced capabilities | Include / reject / silently omit | Reject Sub Agent delegation, Dynamic Workflows, and Copilot preview at decorator application | Human | 2026-10-05 |
| 10 | Foundry model contract | Require endpoint and explicit model / preserve runtime fallback / defer mismatch | Require the endpoint and preserve existing model precedence/default; correct docs that overstate `FOUNDRY_MODEL` as mandatory | Human | 2026-10-05 |
| 11 | Provider validation timing | App composition / decorator application / first run | Preflight the selected HostedSkill when its decorator is applied, without constructing a client | Human | 2026-10-05 |
| 12 | Live object lifecycle | Cache live harness object / per-app live object / facade with per-call resources | Inject a fresh facade per Function invocation and create live execution resources per method call | Agent | 2026-10-05 |
| 13 | Enhanced app typing | Wrapper / protocol-only return / concrete SDK subclasses | Return public `HostedSkillFunctionApp` or `HostedSkillDFApp` subclasses sharing one mixin | Agent | 2026-10-05 |
| 14 | Tool-call streaming | Raw argument fragments / coalesced starts | Preserve current coalescing and emit complete `tool_start` arguments when detectable | Agent | 2026-10-05 |
| 15 | Shared catalog safety | Pass mutable lists directly / copy per call | Shallow-copy mutable capability lists before handing them to the runner or SDK | Agent | 2026-10-05 |

## 6. Test plan

- [ ] Unit: app mixin and decorator cover normal and Durable app types,
  decorator order, async requirement, annotation and parameter validation,
  hidden worker signature, argument forwarding, caller-injection rejection,
  fresh facade identity, handler returns, exceptions, cancellation, SDK-base
  `isinstance` compatibility, and static return typing.
- [ ] Unit: HostedSkill forwards resolved instructions, model, timeout,
  configuration, filtered user tools, MCP, skills, `web_request`, and
  session-bound sandbox tools without mutating the catalog.
- [ ] Unit: `run()` returns `AgentResult`; HostedSkill is not callable; omitted
  sessions are fresh; explicit sessions resume; invalid IDs and prompts fail
  before effects; sandbox and history receive the same ID; catalog capability
  lists remain unchanged after execution.
- [ ] Unit: response example/schema instructions and completed-output validation
  are shared with HTTP handlers while HTTP status and payload compatibility stay
  unchanged.
- [ ] Unit: structured streaming covers event ordering, fragmented tool
  arguments, results, reasoning, timeout, provider/build failure, terminal
  errors, schema failure, consumer cancellation, generator close, resource
  finalization, usage, and spans; existing SSE bytes remain unchanged.
- [ ] Unit: decorator application rejects unknown slugs, subagents, workflows,
  Copilot preview, and missing built-in provider settings with safe diagnostics.
- [ ] Unit: Foundry preflight requires a project endpoint, permits existing
  model fallback, constructs no client or credential, and delegates validation
  to custom managers.
- [ ] Integration: `create_function_app()` catalogs an endpoint-less agent
  without registering a surface and preserves all existing auto-registration.
- [ ] Integration: a deterministic route invokes an injected HostedSkill backed
  by an existing `.agent.md` without repeating agent or connection settings.
- [ ] Integration: decorated and undecorated handlers coexist in one app without
  changing each other's signatures, registration, or execution, and concurrent
  HostedSkill invocations do not mutate shared catalog state.
- [ ] End to end: a Core Tools app with configured Foundry proves deterministic
  behavior, one-shot execution, explicit session continuation, and structured
  streaming; it registers only ordinary Functions bindings.
- [ ] Regression: existing runner streaming, endpoint, registration, delegation,
  workflow, harness, package-export, and app-composition tests remain green.

No configuration scenario fixture is required because discovery and
frontmatter interpretation do not change. If implementation changes how an
authoring file is interpreted, add a mirrored scenario before that change.

## 7. Docs impact

- [ ] `docs/architecture.md` - add the enhanced app/decorator registration path,
  catalog-backed facade, structured stream core, and package ownership.
- [ ] `docs/front-matter-spec.md` - document identity-slug selection,
  endpoint-less internal agents, reused versus surface-only fields, and version
  1 limitations.
- [ ] `docs/getting-started.md` - add hybrid usage and align Foundry model wording
  with current runtime fallback.
- [ ] `README.md` - add the concise HostedSkill API and deterministic/agentic
  composition example.
- [ ] `samples/README.md` and `samples/hybrid-hosted-skill/README.md` - index and
  explain a representative sample with no duplicated settings.
- [ ] `docs/triggers.md` - no change; HostedSkill is not a trigger type.
- [ ] `docs/front-matter-reference.md` - no change; no schema fields are added.

## 8. Status & sign-off

- **Architecture review:** Independent review completed 2026-10-05. The draft
  was clarified for concrete app typing, provider preflight, coalesced stream
  events, session/sandbox identity, response validation, shallow catalog copies,
  signature hiding, harness access, and cleanup. The review's suggested
  per-decorator composition marker was rejected because decorators necessarily
  run after composition; the external-surface requirement is intentionally
  removed for inert entries. Separate subclass feasibility was verified against
  the installed Functions and Durable Functions SDKs. A focused second pass
  found no remaining blockers and marked the design ready for human sign-off.
- **Human sign-off:** victoriahall, 2026-10-05. Approved for implementation.