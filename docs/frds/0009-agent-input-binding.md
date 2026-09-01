---
frd: 0009
title: Python markdown agent binding
status: Finalized
author: hallvictoria
created: 2026-08-11
updated: 2026-08-26
issues: [#1163, #1175, #1284]
pull_requests: []
branch: hallvictoria/agent-binding
---

# FRD 0009 — Python markdown agent binding

## 1. Summary

Add a Python `markdown_agent` decorator that lets an existing Azure Function resolve a
markdown agent definition and receive a fresh, hydrated Microsoft Agent Framework
`Agent`. The customer keeps ordinary Functions and Durable Functions triggers and
application logic; the runtime resolves and caches an immutable hydration blueprint at
indexing time, then constructs and closes a new MAF Agent for every invocation.

This v1 is Python-side dependency injection for Functions and Durable activities,
integrated with the Azure Functions decorator pipeline. `DurableAiApp` additionally
wraps orchestrator contexts with a replay-safe `call_agent()` scheduler backed by one
runtime-owned activity. It is not a host-recognized custom input binding and requires
no .NET host extension or extension-bundle change.

## 2. Motivation / problem

Today `create_function_app()` turns every discovered definition into standalone
agent triggers or endpoints. Existing Function App customers instead need to call an
agent from their own HTTP, queue, timer, or other handler without replacing the
trigger, orchestration, validation, or deterministic business logic they already own.

Although `run_agent()` is public, using it directly requires customer code to parse
and merge the definition and reconstruct its filtered tools, skills, MCP servers,
model, system tools, history, identity, and telemetry. A first-class decorator should
perform that glue consistently with declarative Serverless Agent endpoints.

The Python worker cannot receive a live Python MAF `Agent` object from Functions host
binding metadata. A true host binding could send only serialized metadata and would
require coordinated host-extension and worker-converter work. Python-side injection
therefore provides the requested in-process object without introducing a redundant
host round trip.

## 3. Goals / Non-goals

**Goals**

- Let async Python v2 Functions and customer-owned Durable activities declare a raw
  injected Agent by logical agent name.
- Support both a concise `AiApp.markdown_agent()` API and existing caller-owned
  `FunctionApp` objects through the same free `markdown_agent(app, ...)` implementation.
- Establish `AiApp` as the long-term primary application composition surface, with
  the eventual goal of phasing out `create_function_app()` after its declarative
  behavior is available through `AiApp` and a separately planned migration and
  deprecation cycle is complete.
- Resolve the supported `agent.md` / `.agent.md` convention. For smart bindings,
  recognize required `name` and `description` front matter, the markdown body as
  instructions, and `substitute_variables` only as its parsing control; ignore every
  other per-agent front-matter property.
- Hydrate the app-level model, discovered user and system tools, skills, MCP, and
  per-call history without customer glue.
- Validate binding definitions and discovered assets at indexing time with actionable
  errors.
- Cache compiled definitions and reusable dependencies, never a live MAF `Agent`.
- Give async handlers the entered raw MAF `Agent` so customer code controls sessions,
  middleware, options, tools, and the number and shape of `run()` calls.
- Preserve the active Functions trace context and current app managed-identity
  configuration through model and tool calls.
- Preserve all existing `create_function_app()` behavior and ordinary Functions
  binding metadata.
- Preserve Durable replay determinism by making `context.call_agent()` schedule one
  runtime-owned activity; all definition resolution, hydration, model/tool I/O, and
  response extraction occur outside orchestrator replay.
- Keep customer-owned activities using `DurableAiApp.markdown_agent()` as the advanced
  option for custom payload, result, retry, idempotency, and history contracts.
- Demonstrate HTTP, event-driven, and Durable hybrid handlers in samples and tests.

**Non-goals**

- A host-recognized `agentInput` binding, .NET host extension, extension-bundle entry,
  or Python worker converter in v1.
- Synchronous Function or activity handlers and Durable Entity injection. MAF's Python
  Agent execution, streaming, and owned-resource lifecycle are asynchronous; v1 does
  not add a blocking compatibility facade or process executor.
- Executing model or tool I/O directly in orchestrator replay code.
- Hidden conversation state across `call_agent()` invocations. Each call is stateless;
  applications pass prior results explicitly through Durable inputs and history.
- Structured `call_agent()` results, streaming, generated sub-orchestrators,
  `sub_orchestration`, or direct skill invocation in v1.
- Transparent context wrapping for caller-owned `azure.durable_functions.DFApp`
  instances. They retain the explicit activity plus free `markdown_agent(...)` path.
- Per-agent binding overrides for model, timeout, tools, skills, MCP, system tools,
  subagents, workflows, schemas, triggers, or built-in endpoints. These front-matter
  properties remain available to the declarative `create_function_app()` path but are
  ignored by `markdown_agent`.
- Bound-agent subagent delegation or Dynamic Workflow management tools in v1.
- End-user token exchange, on-behalf-of authentication, or caller-token forwarding.
- Multi-agent orchestration, A2A, new provider policy, or changes to MAF's public API.
- Changes to agent-folder layout, Agent Plugins packaging, or remote-build dependency
  installation; this feature consumes the outputs coordinated by #1163, #1175, and
  #1284.

## 4. Proposed design

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| discover | `composition.py`, existing `config/loader.py`, `discovery/*` | Build one app-root snapshot of binding definitions and app-level tools, skills, and MCP servers. |
| translate | `composition.py` | Project each binding target to required name/description, markdown instructions, and app-level capabilities; ignore other per-agent front matter. |
| register | `bindings.py`, `durable.py`, `app.py` | Add `AiApp`, `DurableAiApp`, and the free decorator; wrap `DurableAiApp` orchestrator contexts and register one reserved internal Agent activity. |
| execute | `hydration.py`, `runner.py`, `client_manager.py` | Build and enter a fresh MAF Agent from a cached blueprint per Function or activity invocation; hidden Durable calls disable persistent history. |

### 4.1 Authoring and public API

New applications may use a thin `FunctionApp` subclass:

```python
import json

from agent_framework import Agent
from azurefunctions.extensions.http.fastapi import Request, Response

from azure_functions_agents import AiApp

app = AiApp()


@app.function_name(name="ProcessOrder")
@app.route(route="orders/{orderId}", methods=["POST"])
@app.markdown_agent(arg_name="order_agent", agent_name="order-fulfillment")
async def process_order(
    req: Request,
    order_agent: Agent,
) -> Response:
    response = await order_agent.run(
        json.dumps(
            {
                "order_id": req.path_params["orderId"],
                "order": await req.json(),
            }
        )
    )
    return Response(content=response.text)
```

  `markdown_agent` must be the innermost decorator, immediately above the handler.
  Python applies it first, so the standard Azure decorators receive the runtime's
  worker-facing wrapper rather than the source handler's injected parameter.

Existing applications keep their app object and Azure SDK decorators:

```python
import json

import azure.functions as func
from agent_framework import Agent
from azurefunctions.extensions.http.fastapi import Request, Response

from azure_functions_agents import markdown_agent

app = func.FunctionApp()


@app.function_name(name="ProcessOrder")
@app.route(route="orders/{orderId}", methods=["POST"])
@markdown_agent(app, arg_name="order_agent", agent_name="order-fulfillment")
async def process_order(
    req: Request,
    order_agent: Agent,
) -> Response:
    response = await order_agent.run(
        json.dumps(
            {
                "order_id": req.path_params["orderId"],
                "order": await req.json(),
            }
        )
    )
    return Response(content=response.text)
```

`agent_name` accepts either the filename stem or normalized slug. Resolution first
compares exact filename stems, then applies the existing slug normalization and looks
up the catalog slug. For example, both `order-fulfillment` and `order_fulfillment`
resolve `order-fulfillment.agent.md`, whose catalog slug is `order_fulfillment`. It
does not resolve the mutable front-matter display `name`. Existing app-wide duplicate
slug validation makes this fallback unambiguous; diagnostics list filename stems and
slugs side by side.

A definition referenced by at least one `markdown_agent` decorator is reachable and may
omit a standalone trigger and built-in endpoints. The binding projection requires only
string `name` and `description` values and treats the markdown body as instructions.
It honors `substitute_variables` to control standard environment substitution in those
recognized fields and the markdown body. All other front-matter keys are ignored without validation or warnings, including
`trigger`, `builtin_endpoints`, model/tool/skill/MCP filters, schemas, workflows, and
subagents. The customer's Function owns triggering, request/response adaptation, and
Durable behavior. Invalid values in ignored keys are silently discarded and cannot
configure the bound Agent; customers validate binding behavior against app-level
configuration. The existing declarative loader continues to recognize and validate
the complete front-matter schema unchanged. Lookup errors explicitly state that
`agent_name` is a filename stem or normalized slug, not the front-matter display name.

`AiApp.markdown_agent()` injects into ordinary Functions, while
`DurableAiApp.markdown_agent()` injects into customer-owned Durable activities.
`DurableAiApp` also augments every ordinarily registered orchestrator context with
`call_agent()` so simple Agent calls do not require a customer-authored activity:

```python
import json

import azure.durable_functions as df

from azure_functions_agents import DurableAgentContext, DurableAiApp

app = DurableAiApp()


@app.orchestration_trigger(context_name="context")
def order_orchestrator(context: DurableAgentContext):
  assessment = yield context.call_agent(
    "order-fulfillment",
    {"order": context.get_input(), "task": "assess risk"},
  )
  return (yield context.call_agent(
    "order-fulfillment",
    {"assessment": assessment, "task": "create a plan"},
    retry_options=df.RetryOptions(
      first_retry_interval_in_milliseconds=5_000,
      max_number_of_attempts=3,
    ),
  ))
```

`DurableAgentContext` is a typed proxy around the SDK-provided
`DurableOrchestrationContext`. Under `TYPE_CHECKING` its base is
`DurableOrchestrationContext`, preserving IDE completion and static signatures. At
runtime its base is an empty stub; it stores the SDK context and delegates missing
attributes through `__getattr__`. This follows the Durable SDK's own context-wrapper
precedent without depending on the SDK context's constructor or runtime inheritance.
Standard methods such as `get_input()`, `call_activity()`, and `create_timer()`
therefore continue to execute on the original context. The proxy adds:

```python
def call_agent(
  self,
  agent_name: str,
  input_: str | JSONValue,
  *,
  retry_options: df.RetryOptions | None = None,
) -> TaskBase: ...
```

The method performs no file, network, model, or tool I/O. It validates a non-empty
agent name and JSON-serializable input, builds a versioned payload, and delegates to
`call_activity()` or `call_activity_with_retry()`. JSON values are canonicalized by a
strict `json.dumps(..., allow_nan=False)` / `json.loads()` round trip before scheduling,
which rejects non-JSON objects and non-finite numbers. The internal activity payload is:

```python
class DurableAgentActivityInput(TypedDict):
  schema_version: Literal[1]
  agent_name: str
  input: JSONValue
  durable_instance_id: str
```

The payload contains no generated UUID, wall-clock value, process state, retry policy,
or resolved blueprint. Given the same orchestrator history and inputs, replay schedules
the same activity name and payload. Agent resolution occurs only when the activity
executes, so `agent_name` may come from orchestration input. Unknown schema versions,
malformed payloads, and unresolved names fail the activity and participate in the
caller's selected Durable retry policy.

Strings are passed to MAF unchanged; other JSON values are encoded as a compact,
deterministic JSON prompt by the activity. The yielded result is the Agent response
text as `str`. Each call uses a fresh Agent and session with persistent history
disabled. An internal `run_blueprint()` history-control parameter defaults to the
current persistent provider for every existing caller; only the hidden activity passes
the opt-out. Applications make continuity explicit by including prior results in later
inputs, as shown above.

`sub_orchestration` is not accepted. Agent names resolve `.agent.md` definitions by
the same filename-stem/slug rules as `markdown_agent`; `.skill.md` is not an authoring
format. Generated sub-orchestration and direct skill-invocation semantics are deferred.

The decorator has no `mode` parameter. App type supplies the intended authoring
context without changing hydration semantics: `AiApp` is the ordinary Function
surface and `DurableAiApp` is the Durable activity surface. Both handlers must be
coroutine functions and receive the fresh raw MAF
`Agent`. Synchronous Functions and activities fail during decorator application with
an actionable error directing the author to use `async def`. This is a MAF protocol
requirement, not only a wrapper implementation choice: MAF Python is async-first.
Non-streaming `Agent.run()` returns an awaitable, streaming returns an async response
stream, and Agent context entry and exit are asynchronous. MAF exposes no blocking
Agent execution or lifecycle protocol for a synchronous Function or activity handler.

Orchestrators remain synchronous generators and never receive a live Agent. The proxy
only creates deterministic Durable scheduling actions. Customers needing control over
activity names, custom result objects, streaming/transcript selection, idempotency, or
specialized execution continue to author an async activity using
`DurableAiApp.markdown_agent()`. Durable Entity injection remains outside v1 because
entity handlers are synchronous and MAF exposes no blocking Agent execution or
lifecycle protocol.

### 4.2 SDK integration

`AiApp` subclasses `azure.functions.FunctionApp`; `DurableAiApp` subclasses
`azure.durable_functions.DFApp`. Both add the same smart decorator, while
`DurableAiApp` also overrides the public `orchestration_trigger()` surface to wrap the
user context before delegating registration to the SDK. Keeping two classes preserves
the current guarantee that non-Durable apps are not `DFApp` instances. Existing app
instances use the free decorator because adding methods by monkey-patch would weaken
typing and global behavior.

`DurableAiApp` preserves the Durable SDK's activity and orchestration decorators while
adding the bound `@app.markdown_agent(...)` method, `DurableAgentContext`, and explicit
`app_root` configuration. The first orchestrator registration installs one Blueprint
containing the reserved `azure_functions_agents_run_markdown_agent` activity; subsequent
orchestrators reuse it. Registration happens synchronously when the first
`@app.orchestration_trigger(...)` decorator is applied, so the activity appears in
`get_functions()` and deployment metadata even when no orchestration has run. It is not
registered merely by constructing a `DurableAiApp` with no orchestrators.

`azure_functions_agents_run_markdown_agent` is reserved. Installation checks already
registered functions, and final function enumeration checks functions decorated later;
a collision raises an error naming the reserved activity and conflicting function.
The activity validates its versioned payload, resolves the target at activity execution
time, calls `run_blueprint()` with persistent history disabled, and returns only
`response.text`. Dynamic agent names can therefore come from orchestration input;
unknown names fail the activity and participate in the selected Durable retry policy.

A caller-owned `azure.durable_functions.DFApp` used with the free
`markdown_agent(app, ...)` decorator retains equivalent explicit activity binding
behavior but does not receive transparent `call_agent()` context wrapping. Non-Durable
applications continue to use `AiApp` or a caller-owned
`azure.functions.FunctionApp`.

The smart decorator accepts a plain callable and returns a wrapped callable. It does
not create, copy, inspect, or mutate `FunctionBuilder` or binding objects. The wrapper
preserves handler metadata with `functools.wraps`, exposes an `inspect.Signature` with
`arg_name` removed to the standard decorators and Python worker, and injects the agent
when called. The source signature remains available through `__wrapped__` for tooling.

Because this depends on Python's bottom-up decorator application, v1 supports one
order only: `markdown_agent` is innermost, then the standard trigger/binding decorator,
then optional `function_name`. Applying it to an Azure `FunctionBuilder` fails at
import with an error showing the supported order. The implementation accepts only a
plain function, coroutine function, or generator function, which rejects a
`FunctionBuilder` without importing or inspecting its private class. There is no SDK
compatibility adapter and no `_configure_function_builder` access.

Both app types use the same async wrapper for required coroutine handlers.

The model timeout enforced by `run_blueprint()` is nested inside the hidden Azure
Functions activity invocation. The Functions host invocation timeout is the outer hard
limit. `RetryOptions` controls whether Durable schedules another complete activity
attempt after failure and the delay between attempts; it does not extend either the
per-attempt model timeout or the host timeout. Each retry constructs a fresh stateless
Agent and repeats the same recorded input.

Runtime probes against the supported `azure-functions>=2.1.0,<3` and
`azure-functions-durable>=1.2.10,<2` ranges verify callable recognition, public
`inspect.signature()` behavior, and binding metadata.

### 4.3 Composition, validation, and caching

`composition.py` extracts shared read-only project loading from `app.py`. Its narrow
interface is:

```python
@dataclass(frozen=True)
class DiscoveryInventory:
  user_tools: tuple[FunctionTool, ...]
  workflow_tools: tuple[WorkflowTool, ...]
  skills: tuple[tuple[str, Path], ...]
  mcp_servers: tuple[tuple[str, MCPServerDefinition], ...]
  failed_loads: tuple[tuple[str, str], ...]

@dataclass(frozen=True)
class ProjectSnapshot:
  app_root: Path
  config: GlobalConfig
  sources: tuple[BindingAgentSource, ...]
  discovery: DiscoveryInventory

def load_project_snapshot(app_root: Path | None) -> ProjectSnapshot: ...
def compose_binding_target(
  snapshot: ProjectSnapshot, agent_name: str
) -> BindingAgentEntry: ...
```

Shared loading:

1. resolves the app root and config;
2. indexes filename stems and normalized slugs without parsing front matter;
3. discovers tools, workflow tools, skills, and MCP servers;
4. parses only the selected target, requiring `name` and `description`, retaining
  markdown instructions, and ignoring other metadata.

`DiscoveryInventory` is an immutable projection of existing `ProjectTools`,
`SkillDiscoveryResult`, and `MCPDiscoveryResult` outputs. It introduces no second
discovery mechanism. Names are retained for skill/MCP diagnostics and deterministic
ordering; all discovery failures are normalized to `(source, reason)` pairs. Because
v1 discovers app-level assets before global tool exclusions and binding definitions
cannot filter capabilities per agent, any tool, skill, or MCP discovery failure rejects
binding registration for the app. This coupling is intentional: continuing with a
partial inventory would silently change the bound Agent's capabilities. The diagnostic
lists each failed source and requires the asset to be fixed or removed.

Full declarative composition remains owned by `create_function_app()` and continues
to use `AgentSpec`, `compose()`, complete validation, and `CatalogEntry` exactly as
today. Binding composition is deliberately separate: it creates
`BindingAgentDefinition` and `BindingAgentEntry` values without merging per-agent
front matter. The two consumers share root resolution and discovery caches, not parsed
definition objects. `agents.config.yaml` remains authoritative for app-level model,
timeout, system-tool, and user-tool defaults; all discovered skills and MCP servers
are enabled because v1 binding definitions have no per-agent filters.

The plain declarative path constructs `AiApp`; the workflow-enabled path constructs
`DurableAiApp`. Each gives its private binding runtime the resolved root and shared
discovery caches while retaining current public return compatibility. The binding
runtime creates and retains its own `ProjectSnapshot` on the first smart decorator,
including when the same app was returned by `create_function_app()`. Full declarative
composition never stores or reuses that binding snapshot. Different app objects own
separate binding snapshots even when their roots match; only existing process-level
discovery caches are shared by resolved root.

Smart-binding composition runs synchronously when each innermost decorator is applied
at module import. It indexes definition paths to enforce global filename-derived slug
uniqueness, resolves and parses the requested target, and builds app-level capabilities
for that target. Invalid YAML or missing/invalid `name` or `description` in an unrelated
definition does not fail binding composition. Those errors in the selected target fail
with its source path and field.

Each app owns a private binding registry keyed by resolved app root and normalized
slug. The registry uses a lock around first compilation so concurrent decorator
registration cannot compile the same target twice. Later decorators reuse the same
immutable `BindingAgentEntry`. There is no composition at handler invocation. Errors
include the requested name, normalized slug, app root, and available filename/slug
pairs.

### 4.4 Hydration and invocation lifetime

The MAF packages move to the latest resolver-compatible releases available on
2026-08-11: `agent-framework-core==1.13.0`, `agent-framework-openai==1.12.0`, and
`agent-framework-foundry==1.10.4`. A pip dry run confirmed this exact set resolves.
Core 1.13 exposes explicit `AgentSession`, unified `Agent.run(..., stream=...)`, and
the Agent async context-manager lifecycle used for client and MCP resources.

Each app owns an immutable `AgentBlueprint` per resolved root and normalized slug. A
blueprint contains the parsed markdown instructions, logical identity, model and
default options, reusable discovered function-tool definitions, skill paths, resolved
MCP server definitions, system-tool configuration, and history-provider factory. It
contains no entered Agent, async exit stack, MCP connection, request-scoped sandbox
tool, or mutable MAF session.

The immutable blueprint is the safe cache boundary. MAF does not explicitly guarantee
that one entered Agent and its owned dependencies are immutable or safe for concurrent
reuse. Multiple invocations of the same Function can overlap; separate `AgentSession`
values isolate conversation history, but do not isolate the Agent's tools, middleware,
clients, or lifecycle. Raw Agent injection also permits invocation code to customize
tools and middleware, so sharing that object could expose mutations to later or
concurrent invocations.

Caching a live Agent would additionally require every invocation-specific dependency
to move outside it. That condition does not hold: Agent construction can include an
invocation-derived resolved ID, sandbox fallback session ID, history provider, mutable
tool lists, MCP wrappers, and HTTP clients. Entering and exiting one cached Agent per
invocation would allow one invocation to close resources still used by another;
entering it once for the process would instead require runtime-owned event-loop startup,
health recovery, synchronization, and reliable asynchronous shutdown. Serializing all
runs behind a per-Agent lock would avoid overlap but introduce head-of-line blocking
and remove same-Function concurrency. The design therefore caches the compiled recipe
and reusable immutable descriptions, then gives each invocation its own entered Agent
and owned resources.

MCP discovery caches resolved immutable server definitions rather than entered tool
wrappers. Each hydration builds fresh MCP tools and HTTP clients because MAF's Agent
context manager enters and closes every MCP tool it owns. The process-wide
`ClientManager`, credential-provider caches, immutable function tools, and skill paths
remain reusable where their existing contracts permit it. A fresh provider chat client,
history provider, web-request tool set, sandbox tool set, and MAF Agent are constructed
for each invocation.

For async Functions and activities, the wrapper constructs the Agent on the current
worker event loop, enters it before calling the customer handler, injects the entered
raw `agent_framework.Agent`, and exits it in `finally` semantics when the handler
returns, raises, or is cancelled. The customer may add or remove tools and middleware,
create `AgentSession` values, call `run()` zero or more times, stream responses, and
choose per-run options. Mutations are invocation-local because no live Agent is reused.
The Agent must not be retained after the handler returns.

Because the runtime no longer intercepts raw `Agent.run()`, async customer code owns
session selection and model-call timeout policy. Calling `run()` without a session uses
MAF's stateless behavior; passing a new `AgentSession` creates an isolated conversation.
The Azure Functions invocation timeout remains the outer bound. MAF model/tool spans
remain nested under the binding invocation span and worker span.

The async wrapper is conceptually:

```python
async def wrapped_handler(*args, **kwargs):
  agent = blueprint.build(invocation_context)
  async with agent:
    kwargs[arg_name] = agent
    return await user_handler(*args, **kwargs)
```

For orchestrators, the customer declares and schedules a normal Durable activity. Its
payload must satisfy the application's own JSON contract, and its result determines
what Durable records in orchestration history. Retry options, idempotency behavior,
activity naming, and any transcript projection are likewise application decisions.
The activity's async handler can use `DurableAiApp.markdown_agent()` to receive a fresh
Agent without moving those Durable semantics into the binding library.

Cancellation of an async handler naturally cancels its current-loop work and still
exits the Agent context. No binding-specific executor or shutdown API is required;
shared client-manager cleanup remains available through `shutdown_client_manager()`.
The obsolete preview names `AiAgent`, `SyncAiAgent`, `shutdown_agent_cache`, and
`shutdown_agent_runtime` are not exported; async handlers import `Agent` directly from
`agent_framework`.

When a Functions `Context` parameter is available, its invocation ID seeds runtime
correlation and invocation-scoped resources; otherwise the runtime generates an ID.

### 4.5 Identity and observability

`AiApp` and the free decorator call the existing idempotent observability bootstrap.
Hydration and the user handler execute inside `agent.binding.invoke <slug>`, nested
under the worker's active Function span. Attributes include agent identity/model,
Function name, invocation ID when available, outcome, and fault domain. MAF model and
tool spans inherit this active context. Prompt and response content remain governed by
the existing sensitive-data setting.

An orchestrator emits no model span during replay. A customer-owned Agent activity
creates the normal `agent.binding.invoke <slug>` span around its handler invocation;
the Durable task/activity trace context provides correlation with the orchestration.

Model, storage, MCP, and system tools continue to use the Function App's configured
managed identity and existing client-ID precedence. Caller bearer tokens are neither
accepted nor forwarded, and v1 makes no end-user delegated-identity guarantee.

### 4.6 V1 validation and limitations

Binding parsing fails only for invalid YAML, missing/non-string `name` or
`description`, duplicate filename-derived slugs, failed discovery, unknown
`agent_name`, invalid decorator order, or an incompatible handler shape. Ignored
front-matter fields never make a binding target invalid.

Functions and activities using `markdown_agent` require coroutine functions. Async
handlers receive raw MAF Agents and support the complete MAF API. Orchestrators and
entities do not use `markdown_agent`; orchestrators call explicit activities instead.

Subagent, workflow, trigger, endpoint, schema, and per-agent capability declarations
are ignored by the binding projection rather than rejected. They continue to affect
the same file when it is also consumed by `create_function_app()`.

### 4.7 Compatibility and migration

For v1, `create_function_app()` remains the compatible zero-code declarative path. It
delegates to the same composition pipeline and returns an enhanced plain or Durable
app while retaining its existing public return compatibility, routes, triggers,
indexing logs, and workflow behavior.

The intended end state is for `AiApp` to become the primary application composition
API and for `create_function_app()` to be phased out. This FRD does not deprecate or
remove `create_function_app()`: `AiApp` must first expose the declarative composition
behavior customers rely on, and a future breaking-change proposal must define the
migration path, deprecation window, release timing, and Durable equivalent before
removal.

Existing `func.FunctionApp()` customers add the package import and free decorator;
they do not replace their app object or standard bindings. Customers starting a new
hybrid app may use `AiApp`; Durable customers may use `DurableAiApp` or the free
decorator with an existing `DFApp`. A customer may also add hybrid handlers directly
to the enhanced plain or Durable app returned by `create_function_app()`. Referencing
a definition already exposed declaratively is allowed: the declarative path uses the
complete front matter, while the binding path uses its minimal projection and cached
blueprint. Direct `run_agent()` callers remain supported and retain their current behavior.

A smart decorator validates its requested target, not every authoring file in the
root. Full reachability validation remains part of `create_function_app()`. Therefore,
a caller-owned app that never calls `create_function_app()` does not validate ignored
fields or reachability for unrelated definitions; those files have no binding effect
until referenced. This is intentional so independent hybrid modules can be imported in
any order without a process-global registration phase.

The MAF dependency update is a package-level compatibility change. Existing
declarative and direct-runner tests must pass against the exact resolved set and its
unified `run(stream=...)` and Agent context-manager APIs before release; compatibility
shims for MAF 1.3 are not part of v1.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Integration layer | Python smart injection / true host binding / phased metadata | Python smart injection in v1; no host extension | Human | 2026-08-11 |
| 2 | Existing app ergonomics | subclass only / monkey-patch / free decorator | `AiApp.agent` plus free `agent(app, ...)` sharing one implementation | Human | 2026-08-11 |
| 3 | Injected type | raw MAF `Agent` / managed facade / both | Fresh raw MAF `Agent` per invocation | Human | 2026-08-11 |
| 4 | Durable scope | activities / direct orchestrator injection / exclude v1 | Exclude all Durable injection scenarios in v1 | Human | 2026-08-11 |
| 5 | Identity | app identity / OBO delegation / claims only | Existing app managed identity and trace context only | Human | 2026-08-11 |
| 6 | Definition lookup | filename identity / display name / path | Existing filename-derived logical identity and slug normalization | Agent | 2026-08-11 |
| 7 | Definition lifetime | cache Agent / cache catalog only / rebuild everything | Cache static composition and capabilities; never cache live Agents | Agent | 2026-08-11 |
| 8 | Binding-only definitions | require endpoint / permit any endpoint-less definition / explicit reachability | Permit no-trigger definitions only when referenced by a smart binding | Agent | 2026-08-11 |
| 9 | Repository isolation | required worktree / current checkout branch | Use local `hallvictoria/agent-binding` branch without a worktree, by explicit request | Human | 2026-08-11 |
| 10 | Decorator integration | mutate FunctionBuilder / support one pure-wrapper order / subclass only | Pure callable wrapper; `agent` must be innermost; no SDK-private state | Agent | 2026-08-11 |
| 11 | Composition timing | full snapshot at app creation / lazy invocation / eager targeted composition | Compile each referenced target eagerly at decoration time and cache per app/root | Agent | 2026-08-11 |
| 12 | Durable app behavior | allow normal functions on DFApp / inspect target metadata / reject DFApp | Reject `DFApp` entirely for v1 smart bindings | Agent | 2026-08-11 |
| 13 | MAF cleanup | close Agent / enter Agent / own created resources | For supported MAF 1.3, use an invocation `AsyncExitStack` only for runtime-created scoped resources; Agent and shared clients have no per-call close | Agent | 2026-08-11 |
| 14 | Unreferenced hybrid definitions | global validation / target-only validation | Caller-owned hybrid apps validate targets only; `create_function_app()` retains full reachability validation | Agent | 2026-08-11 |
| 15 | Durable binding scope | activities only / activities + replay-safe orchestrators / activities + orchestrators + entities | Supersedes #4 and #12: support all three in v1; orchestrators schedule an internal activity, activities use async/sync facades, and entities use a synchronous facade with at-least-once caveats | Human | 2026-08-11 |
| 16 | Binding front matter | complete schema / selected capability fields / name and description only | Binding projection recognizes only required `name` and `description` plus markdown instructions; every other per-agent field is ignored | Human | 2026-08-11 |
| 17 | MAF version | retain 1.3 / adopt latest compatible release | Supersedes #13: use resolver-verified core 1.13.0, OpenAI 1.12.0, and Foundry 1.10.4; use explicit sessions and Agent async lifecycle | Human | 2026-08-11 |
| 18 | Agent lifetime | fresh per invocation / singleton / bounded pool | Supersedes #3 and #7: cache one runtime-owned MAF Agent per app/root/slug and isolate calls with fresh AgentSessions; inject controlled facades rather than mutable raw objects | Human | 2026-08-11 |
| 19 | Cross-mode async ownership | caller event loops / cache per loop / process executor loop | Own cached Agents and their lifecycle on one process async executor; bridge async and sync facades while propagating trace context | Agent | 2026-08-11 |
| 20 | Cached Agent concurrency | concurrent re-entry / per-session lock / per-Agent lease | Serialize complete runs through a per-cache-entry async lock; different Agent entries remain concurrent | Agent | 2026-08-11 |
| 21 | Durable replay identity | generated sequence / mutable counter / history order | Use only deterministic payload fields; Durable activity history position distinguishes repeated calls and supplies recorded results on replay | Agent | 2026-08-11 |
| 22 | Hybrid snapshot ownership | share parsed snapshot / separate snapshots with shared discovery | Full and binding composition own separate parsed snapshots; binding snapshots are per app, while root-keyed discovery caches are shared | Agent | 2026-08-11 |
| 23 | Binding validation scope | validate full project / target only | Binding composition validates its requested minimal projection only; `create_function_app()` independently retains full-project validation | Agent | 2026-08-11 |
| 24 | Agent cache boundary | live Agent / immutable blueprint / no cache | Supersedes #18-#20: cache compiled definitions and reusable dependency descriptions, never a live MAF Agent; hydrate and close a fresh Agent per Function invocation | Human | 2026-08-12 |
| 25 | Customer control | controlled async facade / raw Agent / both | Supersedes the async portion of #15 and #18: async Functions and activities receive the entered raw `agent_framework.Agent`; sync handlers/entities retain an invocation-owned blocking facade and orchestrators retain the replay-safe scheduling proxy | Human | 2026-08-12 |
| 26 | Raw-Agent execution policy | runtime-owned session/timeout / customer-owned / hidden middleware | Async raw-Agent callers own sessions, run options, middleware, streaming, and model-call timeout; generated Durable activity and sync facades retain runtime-managed sessions and timeout | Agent | 2026-08-12 |
| 27 | MCP lifetime | cache live MCP tools / cache resolved definitions / rediscover files | Cache immutable resolved MCP definitions and construct fresh MCP tools/HTTP clients for each Agent context, because MAF enters and closes owned MCP tools | Agent | 2026-08-12 |
| 28 | Revised public surface | retain cache-era names / aliases / clean replacement | Remove preview-only `AiAgent` and `shutdown_agent_cache`; customers annotate async injection with `agent_framework.Agent`, while `shutdown_agent_runtime()` names the remaining sync-executor/client-manager cleanup | Agent | 2026-08-12 |
| 29 | Synchronous handler scope | blocking facade / per-call `asyncio.run()` / async-only | Supersedes the sync and entity portions of #15, #19, #25, and #28: require coroutine Functions and activities, retain only the synchronous replay-safe orchestrator proxy, exclude entity injection, and remove `SyncAiAgent`, `AgentExecutor`, and `shutdown_agent_runtime` | Human | 2026-08-12 |
| 30 | Binding instruction substitution | raw markdown / unconditional substitution / standard per-agent control | Narrowly supersedes #16: apply the standard markdown environment substitution behavior and honor `substitute_variables`; continue ignoring all capability and runtime fields | Agent | 2026-08-14 |
| 31 | Durable orchestrator ownership | generated proxy/activity / explicit customer activity / exclude Durable | Supersedes the orchestrator portions of #15, #21, #25, #26, and #29: v1 supports raw Agent injection in customer-owned async activities only. Orchestrators call those activities explicitly so the application owns naming, payload/results, retries, idempotency, and Durable history contents; defer a standardized proxy until usage evidence justifies library ownership of those semantics | Human | 2026-08-17 |
| 32 | Capability discovery failures | fail app-wide / omit failed assets / scope by selected agent | Fail binding registration app-wide because v1 bindings inherit every discovered app-level capability and have no per-agent filters; silently omitting a failed asset would produce an unexpected partial Agent. Diagnostics identify failed sources and require repair or removal | Agent | 2026-08-17 |
| 33 | Function/activity selection | explicit `mode` / infer from outer decorator metadata / app-type convention | Remove `mode`: `AiApp.agent()` is the ordinary Function surface and `DurableAiApp.agent()` is the Durable activity surface. Both use identical hydration and lifecycle behavior, so an explicit selector adds no runtime value | Human | 2026-08-19 |
| 34 | Decorator name | `agent_input` / `agent` / retain both as aliases | Rename the preview-only decorator to `agent` across the free function and both app classes; do not retain an `agent_input` compatibility alias | Human | 2026-08-19 |
| 35 | Application composition direction | retain `create_function_app()` indefinitely / immediate replacement / phased migration to `AiApp` | Make `AiApp` the long-term primary composition API and eventually phase out `create_function_app()`; preserve it unchanged in v1 and require a separately planned migration and deprecation cycle before removal | Human | 2026-08-19 |
| 36 | Decorator name clarification | retain `agent` / `markdown_agent` / expose both | Supersede #34 and rename the preview-only decorator to `markdown_agent` across the free function and both app classes; retain neither `agent` nor `agent_input` as a compatibility alias | Human | 2026-08-25 |
| 37 | Durable orchestrator convenience | explicit activities only / context scheduler / live Agent proxy | Supersede #31's explicit-activity-only requirement: `DurableAiApp` injects a replay-safe `DurableAgentContext` whose `call_agent()` schedules one runtime-owned activity; existing explicit Agent activities remain supported | Human | 2026-08-26 |
| 38 | Managed Durable call contract | stateful/structured / stateless text / caller-selectable | `call_agent()` is stateless, accepts a string or JSON-safe value, and returns response text; prior results are passed explicitly through Durable history | Human | 2026-08-26 |
| 39 | Managed Durable retries | fixed policy / no retries / optional caller policy | Schedule one activity by default and accept an optional keyword-only `RetryOptions` to use `call_activity_with_retry()` | Human | 2026-08-26 |
| 40 | Durable v1 scope | `DurableAiApp` only / caller-owned `DFApp` adapter / global monkey-patch | Transparently wrap contexts only for `DurableAiApp`; defer caller-owned `DFApp`, `sub_orchestration`, direct skill invocation, structured results, and hidden conversation state | Human | 2026-08-26 |
| 41 | Hidden activity registration | app construction / first orchestrator decoration / first invocation | Register synchronously at the first `DurableAiApp.orchestration_trigger()` decoration, expose it in indexed metadata even when unused, and reuse it for all later orchestrators | Agent | 2026-08-26 |
| 42 | Replay payload identity | resolved blueprint / authored name plus deterministic input / generated call ID | Record a versioned payload containing the authored agent name, canonical JSON input, and Durable instance ID; perform target resolution only in the activity and generate no orchestrator-side identity | Agent | 2026-08-26 |

## 6. Test plan

- [ ] Unit: logical-name resolution, malformed YAML, required name/description,
  duplicate slugs, and ignored trigger/endpoint/model/tool/skill/MCP/schema/workflow/
  subagent fields, including invalid values in ignored fields.
- [ ] Unit: binding hydration uses markdown instructions, app-level model and system
  tools, all discovered user tools/skills/MCP, and session-aware history.
- [ ] Unit: binding markdown instructions substitute `$VAR` and `%VAR%` placeholders
  by default and preserve them when `substitute_variables: false`.
- [ ] Unit: repeated async invocations reuse the same immutable blueprint but receive
  distinct raw Agents, clients, MCP tools, context stacks, and mutable tool lists.
- [ ] Unit: concurrent invocations of the same slug overlap without an Agent lease and
  cannot leak customer mutations or conversation state across invocation boundaries.
- [ ] Unit: async handler success, failure, and cancellation always exit the Agent;
  trace-context propagation and invocation-ID fallback remain covered.
- [ ] Unit: synchronous Function/activity handlers fail during decorator application
  with actionable async-only diagnostics.
- [ ] Indexing: real `FunctionApp.get_functions()` coverage for `AiApp`, caller-owned
  `FunctionApp`, `DurableAiApp`, caller-owned `DFApp`, and `create_function_app()`;
  verify exactly one hidden Agent activity is registered per `DurableAiApp` with an
  orchestrator and none for plain `AiApp`.
- [ ] Indexing: the supported innermost decorator order, actionable rejection of the
  reverse order, worker-facing signature, unchanged trigger/output binding JSON,
  duplicate argument errors, app-type behavior, and handler-shape validation.
- [ ] Indexing: SDK probes run against `azure-functions` 2.1 and the resolved upper
  supported release plus Durable 1.2.10 without importing or mutating
  `FunctionBuilder` internals.
- [ ] Durable: `DurableAgentContext` delegates the standard context API and schedules
  deterministic versioned payloads through normal/retry activity calls; hidden activity
  tests cover stateless fresh Agents, JSON input normalization, text-only results,
  dynamic lookup failures, replay, cancellation, and timeout propagation.
- [ ] Durable: proxy tests cover representative inherited APIs and unknown-attribute
  delegation; payload tests cover schema version, strict JSON canonicalization,
  non-finite numbers, malformed fields, stable instance correlation, and byte-equivalent
  scheduling across replay.
- [ ] Durable: async customer-owned activities still receive fresh raw Agents without a
  mode selector, proving the explicit advanced path remains compatible.
- [ ] Observability: active-parent correlation, nested MAF spans, outcomes, invocation
  attributes, current-loop propagation, Durable correlation/replay, sensitive-data
  gating, and managed-identity client selection.
- [ ] Samples: a standalone `AiApp` demonstrates async HTTP and event-driven handlers,
  while a standalone `DurableAiApp` demonstrates `call_agent()`, optional retries,
  explicit result handoff, and a retained customer-owned preprocessing activity; both
  use a minimal binding definition, tool, skill, and MCP server.
- [ ] E2E: Core Tools indexes and invokes hybrid HTTP and Durable apps; fake clients
  cover normal CI and the official credentialed lane covers model calls where available.
- [ ] Dependency: runner, streaming, MCP lifecycle, and observability behavior pass on
  core 1.13.0, OpenAI 1.12.0, and Foundry 1.10.4 with no remaining MAF 1.3 assumptions.
- [ ] Dependency: the first Phase 3 change updates all three `pyproject.toml` pins and
  a clean resolver install selects the exact approved versions.
- [ ] Regression: an existing multi-agent fixture produces the expected ordered
  Function names and serialized binding dictionaries after the `composition.py`
  extraction; direct runner, trigger, endpoint, workflow, package-import, and sample
  tests remain green.

## 7. Docs impact

- [ ] `docs/architecture.md` — `composition.py` module ownership, unchanged full
  declarative pass, minimal binding projection, blueprint ownership, and explicit
  customer-owned Durable activity boundary.
- [ ] `docs/front-matter-spec.md` — binding projection recognizes only name,
  description, and markdown instructions; all other fields are ignored.
- [ ] `docs/observability.md` — binding invocation spans and Durable activity
  correlation.
- [ ] `docs/workflows.md` — no change; markdown-authored Dynamic Workflows are a
  separate feature. Durable smart-binding guidance lives in `README.md`,
  `docs/architecture.md`, and the hybrid Durable sample.
- [ ] `README.md` — normal and Durable hybrid APIs, Agent lifecycle, and migration from
  `FunctionApp`, `DFApp`, and `create_function_app()`.
- [ ] `samples/README.md`, `samples/hybrid-function-agent/`, and
  `samples/hybrid-durable-agent/` — runnable ordinary and Durable examples.

No `schema.py` change is planned, so generated front-matter reference regeneration is
not expected.

## 8. Status & sign-off

- **Architecture review (phase 2):** Approved. The independent review found
  the prior fresh-Agent/non-Durable design viable and approved it. Human amendments
  then added Durable modes, minimal front matter, updated MAF packages, and cached Agents.
  The amendment review requested cache-concurrency, executor-bridge, replay, return-
  type, version, and entity-retry clarification; those contracts are now recorded.
  A final review then requested concrete discovery inventory fields, snapshot/context
  lifecycle, focused edge tests, and an explicit note that dependency edits occur only
  after sign-off. Those contracts are now recorded. A human amendment on 2026-08-12
  replaced live-Agent caching with cached immutable blueprints and fresh raw Agents;
  the amendment preserves Durable proxies and invocation-owned sync facades.
- **Async-only amendment:** Approved by hallvictoria on 2026-08-12. V1 now follows
  MAF's native async protocol for Functions and activities, removes the blocking sync
  facade/executor, and excludes Durable Entity injection. Its original orchestrator
  proxy scope is superseded by the explicit-activity amendment below.
- **Explicit-activity amendment:** Approved on 2026-08-17. Its requirement that every
  Agent call use a customer-owned activity is superseded by the
  `call_agent()` amendment; explicit activities remain supported as the advanced path.
- **`call_agent()` amendment:** Independent architecture review completed on
  2026-08-26 with verdict **READY FOR HUMAN SIGN-OFF** and no planning blockers.
- **Human sign-off:** Approved by hallvictoria on 2026-08-26. The FRD is finalized for
  implementation.
