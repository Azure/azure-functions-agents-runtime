---
frd: 0010
title: Pluggable Python Agent extensions
status: Finalized
author: hallvictoria
created: 2026-09-02
updated: 2026-09-08
issues: []
pull_requests: []
branch: hallvictoria/agent-binding
---

# FRD 0010 - Pluggable Python Agent extensions

## 1. Summary

Replace FRD 0009's runtime-owned markdown Agent binding with a pluggable,
cross-repository architecture. The `azure-functions` SDK exposes lightweight
`FunctionApp.markdown_agent()`, `AIApp`, and `DurableAIApp` interfaces. A new
`azurefunctions-agents-extension-base` distribution owns framework-neutral
provider discovery, markdown resolution, function-parameter injection, and
optional Durable orchestration. A second new distribution,
`azurefunctions-agents-extension-agent-framework`, owns Microsoft Agent Framework
(MAF) types, configuration, Agent construction, and execution.

The two extensions are implemented in `azure-functions-python-extensions` and
are fully independent of `azurefunctions-agents-runtime`. The current runtime
implementation is a behavioral reference, not a dependency. This FRD
supersedes the ownership, authoring, and dependency design in FRD 0009.

## 2. Motivation / problem

Existing Function Apps should be able to add a markdown-defined Agent to an
ordinary HTTP, queue, timer, or other handler without replacing their
`FunctionApp` object or adopting the declarative Agent runtime. At the same
time, the core Functions SDK must not depend on one Agent framework, and a MAF
customer should not install the larger Hosted Skills runtime, its configuration
system, workflow engine, storage integrations, or unrelated tools.

FRD 0009 placed the implementation in `azurefunctions-agents-runtime`. That
couples the lightweight binding to a product with a broader authoring format and
dependency graph. It also does not establish a reusable contract for future
Copilot, LangGraph, PydanticAI, or other provider packages.

The revised design separates the stable Functions-facing interface, generic
binding mechanics, and provider-specific Agent implementation. Each provider
can evolve its accepted options and injected type independently while sharing
the same Functions and Durable lifecycle rules.

## 3. Goals / Non-goals

**Goals**

- Add lightweight, provider-neutral `markdown_agent`, `AIApp`, and
  `DurableAIApp` interfaces to `azure-functions`.
- Let an existing `func.FunctionApp` use native
  `@app.markdown_agent(provider="agent_framework", ...)` syntax.
- Introduce an independently installable framework-neutral base extension.
- Introduce an independently installable MAF provider extension with typed
  convenience APIs and injected MAF `Agent` values.
- Make provider selection explicit and pin one provider per Function App.
- Discover provider adapters without changing `azure-functions` or the base
  package when future provider distributions are published.
- Treat an exact `.agent.md` file as raw Agent instructions without front matter
  or `agents.config.yaml` support.
- Construct and close a fresh Agent and client for every Function invocation.
- Keep direct runtime dependencies to the minimum needed by each distribution.
- Make Durable support opt-in through
  `azurefunctions-agents-extension-agent-framework[durable]`.
- Keep all core, base, and framework root imports usable when
  `azure-functions-durable` is absent.
- Preserve Durable replay determinism by running Agents only in an activity.
- Automatically discover Agent Skills and HTTP MCP server definitions from the
  shared app root and make every discovered capability available to every Agent
  binding in that app.
- Keep discovered capability definitions framework-neutral and construct all
  live provider resources inside Function invocations or Durable activities.
- Keep `azurefunctions-agents-runtime` and the new extensions fully independent,
  accepting limited implementation duplication.

**Non-goals**

- Supporting any `.agent.md` front matter or `agents.config.yaml` fields.
- Supporting `.claude.md`, bare aliases, display-name or slug lookup, environment
  substitution, runtime model selection, or runtime configuration merging.
- Importing Python tools from project folders, runtime system tools, persistent
  history, built-in endpoints, or workflows.
- Depending on or importing `azurefunctions-agents-runtime` from either new
  extension.
- Bundling OpenAI, Foundry, or another MAF chat-client integration in v1.
- Automatically selecting a provider when multiple providers are installed.
- Adding a host-recognized input binding, worker converter, or .NET host
  extension. Injection remains an in-process Python decorator behavior.
- Running Agent I/O in orchestrator replay code, injecting Agents into
  synchronous handlers, or adding Durable Entity support.
- Implementing Copilot, LangGraph, PydanticAI, or other future providers in this
  change.

## 4. Proposed design

This feature no longer participates in the Agent runtime's discover, translate,
register, and execute pipeline. It defines a smaller independent pipeline in the
extensions repository:

| Stage | Owner | Responsibility |
| --- | --- | --- |
| select | `azure-functions`, `agents-base` | Select an explicit provider per Agent binding and load each adapter lazily. |
| discover | `agents-base` | Discover immutable Skill and MCP definitions from the shared app root. |
| resolve | `agents-base` | Resolve one exact `.agent.md` path, read its raw UTF-8 instructions, and filter discovered capabilities. |
| decorate | `agents-base` | Validate the handler and hide its injected parameter from the worker-visible signature. |
| construct | provider package | Translate selected definitions and construct fresh provider capability and Agent contexts. |
| execute | provider package | Inject the entered Agent, call the handler or Durable activity, and close owned resources. |

### 4.1 Distribution and namespace layout

The core SDK retains its existing `azure.functions` namespace. The extensions
repository adds two independently built wheels following its namespace-package
conventions:

```text
azurefunctions-agents-extension-base
  azurefunctions.extensions.agents.base

azurefunctions-agents-extension-agent-framework
  azurefunctions.extensions.agents.framework
```

The unique leaf packages avoid two wheels owning the same concrete Python
package. The existing `azurefunctions-extensions-base` distribution does not
collide with the new `azurefunctions-agents-extension-base` distribution;
their normalized distribution names and concrete leaf namespaces are distinct.
`agents-framework` depends on `agents-base`; neither depends on the existing
Agent runtime.

The base extension directly requires the bridge-compatible `azure-functions`
version. It discovers safely contained Skill directory paths without parsing
provider-owned `SKILL.md` content, so it needs no frontmatter dependency. The
framework extension directly requires the base extension and
`agent-framework-core`; MAF owns Skill format parsing and validation. The
framework requires a customer supplied `client_factory`, so it does not need
`agent-framework-openai`, `agent-framework-foundry`, storage, or runtime
observability dependencies. HTTP MCP support and Entra authentication
dependencies are installed through the framework package's `[mcp]` extra.
Durable remains independently selectable and may be combined with the MCP
profile.

### 4.2 Core SDK interface

The core SDK adds the generic surface:

```python
app = func.FunctionApp()


@app.route(route="orders/{orderId}", methods=["POST"])
@app.markdown_agent(
    provider="agent_framework",
    arg_name="order_agent",
    agent_name="order-fulfillment",
    client_factory=create_chat_client,
)
async def process_order(req: func.HttpRequest, order_agent: object):
    ...
```

`FunctionApp.markdown_agent()` accepts an explicit `provider` and generic
keyword arguments. It lazily imports `azurefunctions.extensions.agents.base`,
passes the exact app and arguments unchanged, and returns the provider-created
callable decorator. It adds no Azure binding metadata and does not use the
Durable FunctionBuilder replacement path.

`func.AIApp(provider=..., app_root=..., **provider_defaults)` is a lightweight
`FunctionApp` subclass that configures the app provider and its defaults. Its
`markdown_agent()` override always uses that provider. Plain
`FunctionApp.markdown_agent()` continues to require an explicit provider; its
first use pins that provider to the app, and later uses must match it.

`func.DurableAIApp` subclasses `AIApp`, not the optional Durable SDK's `DFApp`.
This is viable because `FunctionApp` already exposes `orchestration_trigger`,
`activity_trigger`, and the other Durable decorators through its lazy Durable
Blueprint bridge. `DurableAIApp.__init__()` asks base to configure Durable Agent
support; that call, rather than class definition or module import, validates the
selected provider and imports the base Durable module. The class therefore
remains exportable without Durable installed, while construction produces an
actionable provider-specific installation error. Its overridden
`orchestration_trigger()` asks base to wrap the customer generator/context and
then delegates registration to `FunctionApp.orchestration_trigger()`.

```python
class DurableAIApp(AIApp):
  def __init__(self, *, provider: str, **kwargs: object) -> None:
    super().__init__(provider=provider, **kwargs)
    _load_agents_base().configure_durable_app(self)

  def orchestration_trigger(self, **kwargs: object):
    return _load_agents_base().durable_orchestration_trigger(
      self,
      sdk_decorator=super().orchestration_trigger,
      **kwargs,
    )
```

Core imports no provider SDK and performs no module-level Durable import.

Core distinguishes absence of the base/provider package from an exception
raised after that package loads. It does not hide provider bugs behind a generic
installation message.

### 4.3 Base extension contract

`agents-base` discovers adapters from entry-point group
`azurefunctions.extensions.agents.providers`. The entry-point name is the
provider ID. The MAF package declares:

```toml
[project.entry-points."azurefunctions.extensions.agents.providers"]
agent_framework = "azurefunctions.extensions.agents.framework.provider:create_provider"
```

The loaded object is a zero-argument factory returning an `AgentProvider`
protocol implementation. Loading the entry point imports provider code; calling
the factory must not perform customer configuration or network I/O. The protocol
supplies:

- `provider_id` and provider-specific distribution/install guidance;
- declared support for neutral capability kinds such as `skills` and `mcp`;
- `compile_binding(instructions, agent_name, options, annotation, capabilities)`,
  which validates provider options, the injected annotation, and selected
  immutable capability definitions and returns an immutable provider recipe;
- `open_agent(recipe, invocation)`, an async context that creates, enters,
  returns, and closes a fresh provider Agent;
- `run_agent(recipe, prompt, invocation) -> str`, used only by a Durable
  activity to create an Agent, run it, and serialize its response.

Provider discovery enumerates the complete entry-point group once. Zero matches
for an explicit ID raise `LookupError` with the provider installation guidance;
multiple matches for the same ID raise `RuntimeError` listing the contributing
distributions. Exceptions while loading or calling a matched provider entry
point propagate with their original cause and are not rewritten as absence.

App state is stored in a process-wide, lock-protected `WeakKeyDictionary` keyed
by the exact app instance. Each app state owns one immutable app root, one
provider adapter, immutable app defaults, and one Durable recipe cache. The
first provider configured or used pins the app; a later different provider is
rejected. Missing, unknown, and duplicate entry-point providers produce the
deterministic errors above. Binding-level options may override the pinned
provider's app defaults for that binding.

The app root is immutable and app-scoped because markdown lookup is an app
concern. `AIApp` and `DurableAIApp` may receive an explicit `app_root` during
construction. Decorators expose no `app_root`; a plain `FunctionApp` resolves
its root from `AzureWebJobsScriptRoot` or the current directory when its first
Agent decorator is applied.

`AIApp` construction establishes its immutable provider and configures that
provider's defaults. Framework typed wrappers always select MAF and expose only
`client_factory` and explicit Python `tools`; advanced MAF Agent construction
options are deferred beyond V1. The generic SDK bridge remains open to options
defined by other provider packages.

The base decorator requires an undecorated async function and validates that
`arg_name` names a positional-or-keyword or keyword-only parameter. It removes
that parameter from the worker-visible signature, then reconstructs the source
call and injects the entered Agent for each invocation. Cleanup is guaranteed
for successful calls, errors, cancellation, and timeout. Immutable instructions
and validated options/capability definitions may be cached; live Agents,
clients, credentials, tokens, HTTP clients, and MCP tools may not be cached.

### 4.4 Markdown authoring

`agent_name="order-fulfillment"` may resolve exactly one of:

```text
<app_root>/order-fulfillment.agent.md
<app_root>/agents/order-fulfillment.agent.md
```

If both exist, resolution fails as ambiguous. Path separators, absolute paths,
and traversal segments in `agent_name` are rejected. When the shared app root
is first established, precedence is an explicit app, provider-configuration, or
decorator value, then `AzureWebJobsScriptRoot`, then the current working
directory.

The complete UTF-8 file contents are passed unchanged as provider instructions.
Files without front matter are valid. Text delimited by `---`, including valid
or invalid YAML-looking text, remains ordinary instructions. There is no YAML
parsing or validation, metadata requirement, substitution, or secondary
configuration file.

`agent_name` must be a non-empty filename component: it may not be `.` or `..`,
contain `/` or `\\`, be absolute on either Windows or POSIX, or contain a NUL
character. Base resolves the app root first, resolves each candidate, and
requires the result to remain under the resolved app root. Symlinks that escape
the app root are rejected; symlinks within it are followed. Directory entries
are compared to the expected filename with case-sensitive string equality on
every platform so behavior does not change when deployed from Windows to Linux.
Both candidate locations are checked during decorator application. If both
exist, decoration raises `ValueError` naming both paths; full paths cannot be
used as an escape hatch because separators are forbidden.

### 4.5 Microsoft Agent Framework provider

The framework package exports typed provider conveniences named
`markdown_agent`, `AIApp`, and `DurableAIApp`. They pin the provider ID to
`agent_framework`, expose MAF-compatible type hints for `client_factory` and
`tools`, and delegate generic mechanics to core/base. Provider overrides and
advanced Agent construction options are not exposed by these typed V1 APIs.

Provider IDs remain strings because provider packages are discovered through
open-ended entry points. A closed SDK enum would require an SDK release for
each new provider. Each provider package instead exports its canonical ID as a
public string constant; the framework package exports
`AGENT_FRAMEWORK_PROVIDER_ID`.

V1 requires a typed zero-argument client factory:

```python
type ClientFactory = Callable[[], BaseChatClient[Any]]
```

It creates a fresh MAF chat client for each invocation. The adapter creates an
`agent_framework.Agent[Any]` using the raw instructions, `agent_name`, client,
typed tools, and a deliberately small documented set of Agent options. The
Agent async context enters and closes context-managed clients, so a shared
client instance is not accepted by the primary API. The extension invokes the
factory once per Agent creation and does not catch or reinterpret factory
exceptions. Customers may implement their own pooling behind the factory only
if every returned client independently satisfies the MAF lifecycle contract.

The initial framework release pins `agent-framework-core==1.13.0`, the version
against which the Agent and client lifecycle is validated. Widening that range
requires compatibility tests against both the new floor and ceiling.

The handler receives the entered raw MAF `Agent` and controls its `run()` calls,
messages, sessions, and response handling. No model call is made automatically
for ordinary Function injection.

### 4.6 Skills and MCP capabilities

The base extension automatically discovers capabilities from the shared app
root:

```text
skills/<skill-name>/SKILL.md
mcp.json
```

Skill discovery validates only filesystem shape and path containment, then
records an immutable Skill directory path without reading `SKILL.md`. Each
provider owns Skill format parsing, metadata validation, and duplicate-name
handling. The MAF provider delegates those responsibilities to MAF's
`SkillsProvider`/`FileSkillsSource`; base discovery does not execute scripts or
construct provider objects.
MCP discovery validates an immutable `servers` object containing HTTP or
streamable-HTTP URL definitions, optional allowed tool names, static headers,
and optional Entra `scope`/`client_id` settings. V1 rejects stdio/local process
servers. Definitions retain environment references rather than resolved
secrets. Resolution, credentials, tokens, HTTP clients, and tools are created
only for an invocation.

Every discovered Skill and MCP server is available to every Agent binding in
the app. V1 exposes no capability booleans, selectors, include/exclude lists, or
per-agent policy. Placing a valid definition under `skills/` or in `mcp.json`
is the application author's explicit grant of that capability to all Agents in
the app. Python `tools=` remains separate and explicit because it passes
code-defined tools directly to providers such as MAF. The base package passes
the complete neutral capability snapshot to each provider. A provider is
rejected when the snapshot is non-empty and it does not declare support for a
discovered capability.

Discovery runs once when app state is first configured and is serialized by the
app-state lock. Results are immutable tuples kept on that app state rather than
in a process-global path cache, so separate FunctionApp instances cannot leak
configuration to each other. Discovery order is stable by portable relative
path and then declared name. Malformed frontmatter or JSON, duplicate names,
unsupported transports, invalid paths, and unresolved required values fail app
configuration instead of silently dropping capabilities. The pre-release
provider SPI takes the new capability bundle directly and has no legacy
signature fallback.

The Agent Framework provider translates all discovered skill paths into a MAF
`SkillsProvider` and all discovered MCP definitions into MAF MCP tools. It merges MCP
tools with explicitly supplied Python `tools`. Each invocation constructs and
closes fresh capability resources and the Agent using one async exit stack,
including error, timeout, and cancellation paths. Other providers may translate
the same definitions differently; selecting capabilities for a provider that
does not declare support fails during binding compilation.

The `.agent.md` contract remains unchanged: the complete file is raw
instructions. YAML-looking `skills` or `mcp` fields in that file are never
parsed and cannot select capabilities. This avoids coupling the lightweight
binding to the runtime's complete frontmatter and configuration model.

### 4.7 Optional Durable support

The public installation profile is:

```text
azurefunctions-agents-extension-agent-framework[durable]
```

It depends on `azurefunctions-agents-extension-base[durable]`, whose Durable
extra adds `azure-functions-durable>=2.0.0b2`. Durable imports live only in
isolated Durable modules and call paths. These imports must succeed without it:

```python
import azure.functions
import azurefunctions.extensions.agents.base
import azurefunctions.extensions.agents.framework
```

`DurableAIApp` wraps orchestration contexts with a `DurableAgentContext` bound
to the app's provider. `call_agent(agent_name, input_,
retry_options=...)` always uses that provider. It recursively accepts only JSON
primitives, string-keyed objects, and arrays;
rejects NaN and infinities; and canonicalizes accepted input through a
sorted-key, compact-separator JSON encode/decode round trip. It schedules one
hidden activity with a deterministic payload. In the activity, string input
remains unchanged as the prompt and all other input is encoded as the same
sorted-key compact JSON string. The orchestrator performs no file, client,
Agent, model, or tool I/O. Durable recipe caching is scoped to the app.

The exact activity object contains only JSON values:

```json
{
  "schema_version": 1,
  "agent_name": "orders",
  "input": {"order_id": "42"},
  "durable_instance_id": "instance-id"
}
```

This contract is being finalized before the first public release. The temporary
schema-v2 payload used on the feature branch was never shipped, so V1 accepts
only the schema-v1 shape above and provides no compatibility bridge for
prerelease orchestration instances.

The provider and its non-JSON values, such as client factories, are
configured by `DurableAIApp` construction and reconstructed in each worker
process when the customer's app module is imported. V1 does not allow a Durable
orchestrator or ordinary markdown Agent binding to select a different provider.
Every worker process reconstructs this state by importing the customer's app
module before handling activities. Provider loading, markdown reads, recipe
compilation, and model I/O occur only in the activity and therefore do not form
part of orchestrator replay determinism. Providers need not make compiled
recipes serializable; they must preserve the existing activity execution and
lifecycle contract for the deployment version being run.

The activity resolves the markdown file, asks the selected provider adapter to
create a fresh Agent, client, Skills provider, and MCP tools, normalizes the JSON
input to a prompt, awaits the MAF Agent, and returns `response.text` as `str`.
Durable calls use the complete app-local capability snapshot; V1 adds no
per-call or per-agent capability selector. Capability definitions, paths,
provider IDs, configuration, and secrets are absent from the schema-v1
orchestration payload.
All discovery, credential acquisition, networking, and model/tool execution
occurs in the hidden activity. Persistent hidden state is not introduced.
Reserved activity names and repeated function enumeration are validated
explicitly.

### 4.8 Compatibility and runtime ownership

FRD 0009 is superseded. Repository history confirms its APIs and FRD are absent
from `origin/main`; therefore the runtime-owned `bindings.py`, binding-only
composition/hydration/Durable modules, exports, tests, and hybrid samples are
removed from the feature branch without a public deprecation period.

The declarative runtime remains unchanged and continues to own its full
front-matter/config format, Hosted Skills, discovery, registration, workflows,
history, and dependency graph. The new extensions may duplicate small,
well-tested wrapper and Durable payload logic but must not import runtime code.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Implementation owner | Existing Agent runtime / core SDK / new extension | New independent extension packages | Human | 2026-09-02 |
| 2 | Core SDK responsibility | Full implementation / generic interface / no API | Generic lightweight interface only | Human | 2026-09-02 |
| 3 | Shared provider architecture | Provider duplication / framework-neutral base / runtime core | `azurefunctions-extensions-agents-base` | Human | 2026-09-02 |
| 4 | MAF distribution name | Singular `agent-framework` / plural `agents-framework` | `azurefunctions-extensions-agents-framework` | Human | 2026-09-02 |
| 5 | Provider selection | Auto-select / activation import / explicit | Explicit provider, one per app (superseded by Decision 19) | Human | 2026-09-02 |
| 6 | Provider extensibility | Core conditionals / package entry points / manual monkey-patching | Standard package entry points | Human + Agent | 2026-09-02 |
| 7 | Markdown lookup | Explicit path / legacy discovery / exact convention | Exact `<agent_name>.agent.md` in root or `agents/` | Human | 2026-09-02 |
| 8 | Markdown contents | Front matter / body only / whole raw file | Whole file is unchanged instructions | Human | 2026-09-02 |
| 9 | MAF client support | Bundle OpenAI / provider extras / supplied client | Required typed per-invocation `client_factory` | Human | 2026-09-02 |
| 10 | Durable dependency | Required / separate distribution / optional extra | Framework `[durable]` extra | Human | 2026-09-02 |
| 11 | Durable import behavior | Conditional exports / import-safe stable surface | Stable core export; fail only when Durable is used | Human | 2026-09-02 |
| 12 | Runtime reuse | Runtime dependency / shared source / independent implementation | Fully independent; duplication allowed | Human | 2026-09-02 |
| 13 | Core Durable implementation | `DFApp` subclass / factory / import-safe `AIApp` subclass | `AIApp` subclass using existing lazy Durable decorators | Agent + Human | 2026-09-02 |
| 14 | Provider discovery contract | Core conditionals / manual registration / entry points | `azurefunctions.extensions.agents.providers`; entry-point value is a zero-argument adapter factory | Agent + Human | 2026-09-02 |
| 15 | App provider state | App attribute / thread-local / weak registry | Lock-protected weak-key registry; pin at construction or first decorator | Agent + Human | 2026-09-02 |
| 16 | Markdown path safety | Platform-native lookup / normalized lookup / portable strict lookup | Portable case-sensitive filename matching; reject separators and escaping symlinks | Agent + Human | 2026-09-02 |
| 17 | MAF compatibility | Exact pin / broad major range | Pin `agent-framework-core==1.13.0` initially | Agent + Human | 2026-09-02 |
| 18 | Client factory signature | Zero-argument / invocation-aware | `Callable[[], BaseChatClient[Any]]`; exceptions propagate | Agent + Human | 2026-09-02 |
| 19 | Provider scope (superseded by Decision 35) | One provider per app / provider per binding with optional app default | Multiple providers per app; `AIApp` supplies a default, while each binding may override it | Human | 2026-09-03 |
| 20 | V1 Durable provider scope | Additional provider registration / per-call provider / app default only | Durable calls always use the `DurableAIApp` default; no `configure_agent_provider()` API | Human | 2026-09-03 |
| 21 | Provider default freeze (superseded by Decision 35) | Mutable defaults / replace defaults / freeze independently at first use | Freeze per provider when first used by a binding or established as the app default | Agent | 2026-09-03 |
| 22 | Durable determinism boundary | Serialize compiled recipes / serialize provider ID / derive default from app state | Serialize JSON input only; reconstruct the default provider from startup state and compile inside the activity | Agent + Human | 2026-09-03 |
| 23 | Provider ID authoring (partially superseded by Decision 35) | Closed SDK enum / raw strings only / provider-package constants with string escape hatch | Keep provider parameters open as strings; each provider package exports its canonical ID constant, and its typed decorators default to that value while allowing per-binding overrides | Human + Agent | 2026-09-03 |
| 24 | Capability discovery owner | Base-neutral definitions / framework-only discovery / separate capability SPI | Base discovers immutable neutral definitions; providers translate and own live resources | Human + Agent | 2026-09-03 |
| 25 | Capability authoring (superseded by Decision 32) | Parse `.agent.md` / Python selectors / app-wide only | Keep markdown raw; automatic app-root discovery with app defaults and per-binding Python selectors | Human | 2026-09-03 |
| 26 | Durable capabilities | Defer / serialize selections / app defaults in activity | Support Skills and MCP immediately through the app-local snapshot; keep payload schema v1 and construct live resources only in the activity | Human | 2026-09-03 |
| 27 | MCP transport | HTTP and stdio / HTTP only / customer-built tools only | Discover HTTP/streamable-HTTP servers in V1; reject local stdio processes | Human + Agent | 2026-09-03 |
| 28 | Capability dependencies (superseded by Decision 33) | Mandatory / provider extras / runtime dependency | Add provider `[mcp]` and `[capabilities]` extras; retain independent `[durable]`; never depend on the Agent runtime | Agent | 2026-09-03 |
| 29 | Discovery availability and cache (partially superseded by Decision 34) | Extra-dependent/global path cache / mandatory neutral discovery/app-local snapshot | Base always discovers using mandatory frontmatter parsing and stores one immutable snapshot per app | Agent, architecture review | 2026-09-03 |
| 30 | Selector inheritance (superseded by Decision 32) | Binding defaults always `True` / omitted inherits app / merge app and binding selectors | Omitted binding selectors inherit app defaults; an explicit selector replaces that capability default | Agent, architecture review | 2026-09-03 |
| 31 | Provider SPI transition | Legacy fallback / signature introspection / clean pre-release break | Pass the neutral capability bundle directly with no compatibility shim because the SPI is unreleased | Agent, architecture review | 2026-09-03 |
| 32 | V1 capability UI | App/binding selectors / automatic all / frontmatter policy | Remove all Skills/MCP API parameters and selectors; every valid app-root definition is available to every Agent, while `tools=` remains explicit | Human | 2026-09-03 |
| 33 | Capability extra naming | Keep `[capabilities]` alias / `[mcp]` only / make MCP dependencies mandatory | Remove the redundant `[capabilities]` alias; Skills need no extra and optional MCP dependencies remain under `[mcp]` | Human | 2026-09-03 |
| 34 | Skill format validation owner | Base parses metadata / provider parses format / no validation | Base discovers safe Skill directory paths only; each provider owns format parsing and validation, and `python-frontmatter` is removed | Human | 2026-09-03 |
| 35 | Provider scope simplification | Multiple providers per app / one provider per app | Pin one provider per app; plain `FunctionApp` pins on first Agent use and typed app wrappers pin at construction | Human | 2026-09-03 |
| 36 | Typed MAF options | Broad Agent option surface / `client_factory` and `tools` only | Expose only `client_factory` and explicit Python `tools` in V1 typed MAF APIs | Human | 2026-09-03 |
| 37 | App-root scope | App and decorator roots / app-scoped root only | Remove `app_root` from decorators; configure it only on `AIApp` or `DurableAIApp`, while plain apps use environment/current-directory resolution | Human | 2026-09-03 |
| 38 | Public app class casing | `AiApp` / `AIApp` | Use `AIApp` and `DurableAIApp` consistently in the SDK and provider packages | Human | 2026-09-03 |
| 39 | Extension distribution names | Existing plural extension names / singular extension names aligned with Agent branding | Use `azurefunctions-agents-extension-base` and `azurefunctions-agents-extension-agent-framework`; supersedes Decisions 3 and 4 for naming only | Human | 2026-09-08 |

## 6. Test plan

- [ ] Core SDK works without either Agent extension installed and reports
  actionable missing-base/provider errors only when Agent APIs are used.
- [ ] Core delegates the exact app and generic arguments without importing a
  provider or Durable at package import time.
- [ ] Base fake-provider tests cover entry-point discovery, provider conflicts,
  first-use provider pinning, app-scoped defaults, exact markdown lookup,
  traversal/ambiguity, signature hiding, option precedence, fresh contexts,
  cleanup, cancellation, and weak references.
- [ ] Base capability tests cover Skill path discovery without content parsing,
  valid/malformed MCP definitions, traversal and symlink escape, deterministic
  ordering, universal binding availability, unsupported providers, and
  immutable-only caches.
- [ ] Framework tests use fake MAF clients and cover the exact V1 typed option
  surface, annotation
  validation, raw instructions, fresh Agent/client construction, lifecycle,
  result propagation, and package entry-point metadata without network calls.
- [ ] Framework capability tests cover provider-owned Skill format validation,
  MAF translation, explicit-tool merging, fresh resources per invocation, and
  cleanup on success/error/cancellation.
- [ ] Non-Durable clean-environment tests prove all three root imports work and
  `azure.durable_functions` is absent from `sys.modules`.
- [ ] Durable tests cover missing-extra guidance, hidden activity registration,
  deterministic payloads, app-provider routing, JSON validation, retries,
  dynamic Agent names, response text extraction, replay safety, collisions, and
  repeated enumeration.
- [ ] Durable capability tests prove schema v1 is unchanged, discovery and
  networking never run in the orchestrator, the complete app-local snapshot is
  applied in the activity, and live MCP resources are invocation-owned and
  closed.
- [ ] Built-wheel tests inspect contents and METADATA and resolve base,
  framework, and framework `[durable]` profiles independently.
- [ ] Cross-repository samples register and execute using both generic core and
  typed provider APIs.
- [ ] All repositories pass their lint, type, unit, packaging, and documentation
  gates on their supported Python versions.

## 7. Docs impact

- [ ] `azure-functions` API reference and release notes: generic provider-aware
  interfaces and missing-extension behavior.
- [ ] `azurefunctions-agents-extension-base/README.md`: provider SPI, lifecycle,
  authoring, and optional Durable contract.
- [ ] `azurefunctions-agents-extension-agent-framework/README.md`: installation,
  `client_factory`, typed APIs, automatic Skills/MCP discovery, raw markdown
  rules, capability extras, and `[durable]`.
- [ ] `azure-functions-python-extensions/README.md`: list both new packages.
- [ ] Runtime `docs/architecture.md`, README, FRD index, and FRD 0009: remove
  runtime binding ownership and link to the superseding design.
- [ ] Move hybrid samples to the framework extension package with no front
  matter or `agents.config.yaml`.
- [ ] No runtime schema or generated front-matter reference changes.

## 8. Status & sign-off

- **Architecture review (phase 2):** Independent review completed 2026-09-02.
  It found no architectural blocker and requested concrete Durable, provider
  SPI/pinning, markdown path, versioning, and client-factory contracts. Those
  contracts are incorporated above. Its reported package-name collision was
  dismissed because the existing and proposed normalized distribution names
  and concrete leaf namespaces are distinct.
- **Human sign-off:** hallvictoria, 2026-09-02. Approved Decisions 13-18 and
  authorized implementation; status set to `Finalized`. On 2026-09-03,
  hallvictoria explicitly requested implementation of per-binding provider
  selection. Later on 2026-09-03, hallvictoria restricted V1 Durable calls to
  the app default provider and approved removal of additional-provider startup
  configuration, as summarized in Decisions 20-22. On 2026-09-03,
  hallvictoria prioritized Skills and MCP support and selected automatic
  app-root discovery, per-binding include/exclude filtering, and immediate
  Durable support through app defaults (Decisions 24-27), then directed
  implementation to start. An independent architecture review on 2026-09-03
  clarified dependency availability, selector inheritance, cache ownership,
  fail-fast validation, and the pre-release SPI transition in Decisions 28-31.
  Later on 2026-09-03, hallvictoria simplified the V1 capability UI by removing
  all Skills/MCP selectors and making app-root discovery universally available
  within the app, superseding Decisions 25 and 30 with Decision 32. On the same
  date, hallvictoria removed the redundant `[capabilities]` extra and assigned
  Skill format parsing and validation to providers (Decisions 33-34). Later on
  2026-09-03, hallvictoria simplified V1 to one provider per app, reduced the
  typed MAF surface to `client_factory` and `tools`, and made `app_root`
  app-scoped (Decisions 35-37).
