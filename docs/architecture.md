# Azure Functions Agent Runtime architecture

## 1. Overview

`azure-functions-agent-runtime` turns a markdown-first agent project into an `azure.functions.FunctionApp`. The design goal is that you write `.agent.md` files plus a small amount of supporting configuration, and the runtime translates that authoring format into Azure Functions triggers, HTTP routes, MCP surfaces, and tool wiring. At startup, the runtime follows a three-stage pipeline: **discover** project files and inventories, **translate** them into typed runtime objects, and **register** the resulting agents on a Function App. The authoritative implementation of that pipeline lives in `src/azure_functions_agents/app.py:create_function_app()`.

## 2. High-level data flow

```mermaid
flowchart LR
    A["Agent project inputs<br/>*.agent.md<br/>agents.config.yaml<br/>mcp.json<br/>skills/<br/>tools/"] -->|Path --> get_app_root()| B["config/paths.py"]
    B -->|app_root: Path| C["config/loader.py<br/>load_global_config()<br/>load_agent_specs()"]
    A -->|Path --> discover_*()| D["discovery/*<br/>skills + tools + MCP"]
    C -->|GlobalConfig + list[AgentSpec]| E["config/merge.py<br/>compose()"]
    D -->|discovered inventories| E
    E -->|ResolvedAgent| F["config/validation.py<br/>validate_resolved_agent()"]
    F -->|ResolvedAgent| G["registration/capabilities.py<br/>build_capabilities()"]
    G -->|AgentCapabilities| H["registration/triggers.py<br/>registration/endpoints.py"]
    H -->|Decorators applied| I["azure.functions.FunctionApp"]
    J["client_manager.py<br/>ClientManager"] -.->|chat client| K["runner.py<br/>run_agent()<br/>run_agent_stream()"]
    H -.->|handler closures + ResolvedAgent + AgentCapabilities| K
    K -.->|prompt + tools + session| L["Microsoft Agent Framework"]
```

Read left to right: files on disk become typed config, typed config becomes a `ResolvedAgent`, and each resolved agent is registered as Azure Functions bindings plus optional debug endpoints.

A few boundaries are worth calling out explicitly:

- **Discovery is read-only.** These modules inspect the project tree and return inventories; they do not decide what any one agent is allowed to use.
- **Translation is type-driven.** The loader and merge layers convert loose YAML/markdown input into `AgentSpec`, `GlobalConfig`, and then `ResolvedAgent`.
- **Registration is Azure-specific.** This is the first stage that knows about `azure.functions.FunctionApp`, decorators, routes, and trigger bindings.
- **Execution is deferred.** The runner is not part of startup registration; it is called later by handler closures when an HTTP route or trigger actually fires.

## 3. Module map

| Package/module | Role | Key entry points |
| --- | --- | --- |
| `azure_functions_agents/app.py` | Top-level orchestrator that runs the startup pipeline and returns the configured app. | `create_function_app()` |
| `azure_functions_agents/config/paths.py` | Resolves the app root and the optional config/history directory. | `set_app_root()`, `get_app_root()`, `resolve_config_dir()` |
| `azure_functions_agents/config/env.py` | Performs env-var substitution and bool coercion during config loading. | `resolve_env_var()`, `substitute_env_vars_in_text()`, `_to_bool()` |
| `azure_functions_agents/config/schema.py` | Defines the Pydantic models for raw, global, and merged config. | `AgentSpec`, `GlobalConfig`, `ResolvedAgent`, `TriggerSpec`, `DebugConfig` |
| `azure_functions_agents/config/loader.py` | Loads YAML front matter and `agents.config.yaml` into typed models. | `load_agent_specs()`, `load_global_config()` |
| `azure_functions_agents/config/merge.py` | Applies defaults, overrides, and per-agent filters to produce runtime config. | `compose()` |
| `azure_functions_agents/config/validation.py` | Enforces legacy-field rules and post-merge sanity checks. | `validate_agent_frontmatter()`, `validate_global_config_dict()`, `validate_global_mcp_references()`, `validate_resolved_agent()` |
| `azure_functions_agents/discovery/skills.py` | Recursively discovers `skills/*.md`, caches them, and returns per-skill text or a combined block. | `discover_skill_texts()`, `discover_skill_names()`, `discover_skills()` |
| `azure_functions_agents/discovery/tools.py` | Imports `tools/*.py`, finds `FunctionTool` values or wraps plain functions, and caches the result. | `discover_user_tools()` |
| `azure_functions_agents/discovery/mcp.py` | Loads `mcp.json` / `.vscode/mcp.json` and translates server definitions into MAF MCP tool wrappers. | `discover_mcp_servers()` |
| `azure_functions_agents/discovery/builtin_tools.py` | Provides built-in file-reading/search tools behind an allow-list and path-containment checks. | `BUILTIN_TOOLS`, `add_allowed_read_dir()` |
| `azure_functions_agents/registration/capabilities.py` | Applies per-agent MCP/skills/tools filters and packages the final runtime inventory. | `AgentCapabilities`, `build_capabilities()` |
| `azure_functions_agents/registration/_naming.py` | Derives Azure-safe function names and debug slugs from `.agent.md` filenames. | `_safe_function_name()`, `_function_name_from_source()` |
| `azure_functions_agents/registration/_handlers.py` | Builds the callable closures that turn incoming trigger data or HTTP bodies into runner prompts. | `make_agent_handler()`, `make_http_agent_handler()`, `build_sandbox_tools_for_session()` |
| `azure_functions_agents/registration/triggers.py` | Registers each non-main agent trigger, dispatching between built-in, HTTP, and connector trigger paths. | `register_agent()` |
| `azure_functions_agents/registration/endpoints.py` | Registers debug chat UI, REST chat, SSE streaming, and MCP endpoints for agents with debug exposure. | `register_debug_endpoints()` |
| `azure_functions_agents/system_tools/sandbox.py` | Builds the ACA Dynamic Sessions-backed `execute_python` tool for a resolved agent/session. | `create_sandbox_tools()` |
| `azure_functions_agents/system_tools/connectors/*` | Loads Azure API Connections, caches discovered operations, and generates connector-backed `FunctionTool` wrappers. | `cache.py:configure_connector_tools(), get_connector_tools()`; `connectors.py:load_connection()`; `tools.py:generate_tools()`; `arm.py:ArmClient, DataPlaneClient` |
| `azure_functions_agents/runner.py` | Executes prompts through the Microsoft Agent Framework, managing sessions, tools, and streaming. | `run_agent()`, `run_agent_stream()` |
| `azure_functions_agents/client_manager.py` | Defines the pluggable inference-client abstraction and the default MAF-backed implementation. | `ClientManager`, `get_client_manager()`, `set_client_manager()` |
| `azure_functions_agents/_function_tool.py` | Thin local shim around MAF `FunctionTool` creation so project tools can use `@tool`. | `tool()` |
| `azure_functions_agents/_logger.py` | Shared package logger used across discovery, registration, and runtime code. | `logger` |

### How the packages line up

- `config/` answers **"what did the author write?"**
- `discovery/` answers **"what is available in this project folder?"**
- `registration/` answers **"which Azure Functions surfaces should exist for this agent?"**
- `system_tools/` answers **"which runtime-provided tools can be attached on demand?"**
- `runner.py` and `client_manager.py` answer **"once invoked, how does an agent call the model and its tools?"**

### Typical startup trace

When the host imports your app module and calls `create_function_app()`, control usually moves through the codebase in this order:

1. `app.py` resolves the project root.
2. `config/loader.py` reads `agents.config.yaml`.
3. `config/loader.py` reads every `*.agent.md` file and creates `AgentSpec` values.
4. `discovery/tools.py`, `discovery/mcp.py`, and `discovery/skills.py` build the shared inventories for the project.
5. `config/merge.py` turns each `AgentSpec` plus `GlobalConfig` into one `ResolvedAgent`.
6. `config/validation.py` checks each merged object for missing triggers, bad MCP references, and similar config mistakes.
7. `registration/capabilities.py` converts name-based filters into concrete tool lists and `skills_text`.
8. `registration/triggers.py` and `registration/endpoints.py` mutate one `FunctionApp` instance until all agents are registered.

That ordering matters because later modules assume earlier stages have already reduced free-form author input into typed, validated objects. For example, registration code does not re-parse YAML or front matter; it trusts `ResolvedAgent` and `AgentCapabilities`.

## 4. Pipeline stages

The `create_function_app()` docstring in `src/azure_functions_agents/app.py:39-48` is the source of truth. The steps below restate it in module terms.

1. **Resolve app root**
   - **Implemented by:** `src/azure_functions_agents/app.py:36-53`, `src/azure_functions_agents/config/paths.py:15-31`
   - **Input:** optional `app_root: Path | None` plus environment variables such as `AZURE_FUNCTIONS_AGENTS_APP_ROOT` and `AzureWebJobsScriptRoot`
   - **Output:** `resolved_root: Path`
   - **Notes:** this is the root path handed to every later loader/discovery function. `app.py` also calls `_allow_skill_reads()` immediately afterwards so built-in file readers can safely access the project's `skills/` directory.

2. **Load global `agents.config.yaml`**
   - **Implemented by:** `src/azure_functions_agents/config/loader.py:84-108`
   - **Input:** `app_root: Path`
   - **Output:** `GlobalConfig`
   - **Notes:** missing config is valid and becomes `GlobalConfig()`. String values are normalized through `config/env.py`, so env-var references are resolved before the Pydantic model is materialized.

3. **Load all agent markdown files**
   - **Implemented by:** `src/azure_functions_agents/config/loader.py:56-81`, `src/azure_functions_agents/config/loader.py:111-125`
   - **Input:** `app_root: Path`
   - **Output:** `list[AgentSpec]`
   - **Notes:** each file is parsed as YAML front matter plus markdown body. The loader stamps `source_file`, sets `is_main` when the filename is `main.agent.md`, and stores the markdown body in `AgentSpec.instructions`.

4. **Discover runtime inventories from disk**
   - **Implemented by:** `src/azure_functions_agents/app.py:53-59`, `src/azure_functions_agents/discovery/tools.py:20-108`, `src/azure_functions_agents/discovery/mcp.py:80-124`, `src/azure_functions_agents/discovery/skills.py:47-89`, `src/azure_functions_agents/discovery/builtin_tools.py:13-21`, `src/azure_functions_agents/discovery/builtin_tools.py:314`
   - **Input:** `app_root: Path`
   - **Output:** user tools as `list[FunctionTool]`, MCP servers as `dict[str, MCPTool]`, skills as `dict[str, str]`, plus built-ins as `list[FunctionTool]`
   - **Notes:** all three discovery modules cache by resolved app root, so startup pays the disk/import cost once per process. MCP discovery is a translation step too: entries from `mcp.json` become ready-to-use MAF MCP tool objects.

5. **Compose a per-agent runtime view**
   - **Implemented by:** `src/azure_functions_agents/config/merge.py:110-169`
   - **Input:** `AgentSpec`, `GlobalConfig`, `discovered_mcp_names: list[str]`, `discovered_skill_names: list[str]`
   - **Output:** `ResolvedAgent`
   - **Notes:** this is where precedence rules are applied. Model and timeout fall through agent config, global config, environment, and defaults; capability filters turn the global/shared inventories into per-agent allow/deny decisions.

6. **Validate the merged configuration**
   - **Implemented by:** `src/azure_functions_agents/config/validation.py:65-81`, `src/azure_functions_agents/config/validation.py:84-133`
   - **Input:** global MCP references as `list[str]`, discovered MCP/skill names as `list[str]`, and each `ResolvedAgent`
   - **Output:** the same validated `ResolvedAgent` (or an exception that skips registration for that agent)
   - **Notes:** validation deliberately happens twice in the overall pipeline: once for global MCP references, once for each fully merged agent. That split keeps "bad shared config" separate from "bad per-agent overrides."

7. **Build per-agent capabilities**
   - **Implemented by:** `src/azure_functions_agents/registration/capabilities.py:33-78`
   - **Input:** `ResolvedAgent`, discovered user tools, built-in tools, discovered MCP tools, discovered skill texts
   - **Output:** `AgentCapabilities`
   - **Notes:** this stage converts name-based filters into actual runtime objects. After this point, the registration and runner layers do not need to reason about `exclude` lists; they only consume concrete tool lists and the final concatenated `skills_text`.

8. **Create the Azure Functions app container**
   - **Implemented by:** `src/azure_functions_agents/app.py:75`
   - **Input:** startup defaults such as `http_auth_level=func.AuthLevel.FUNCTION`
   - **Output:** `azure.functions.FunctionApp`
   - **Notes:** only one `FunctionApp` is created per startup pass. Every subsequent registration call mutates this object by attaching decorators and handlers.

9. **Register triggers and debug endpoints**
   - **Implemented by:** `src/azure_functions_agents/app.py:76-99`, `src/azure_functions_agents/registration/triggers.py:167-216`, `src/azure_functions_agents/registration/endpoints.py:362-421`, `src/azure_functions_agents/registration/_handlers.py:166-227`
   - **Input:** `FunctionApp`, `ResolvedAgent`, `AgentCapabilities`
   - **Output:** the same `FunctionApp`, now decorated with trigger bindings, HTTP routes, SSE streaming routes, and/or MCP endpoints
   - **Notes:** non-main agents go through `register_agent()` when they have a `trigger`. Any agent with debug exposure enabled also goes through `register_debug_endpoints()`, which can add chat UI, `/chat`, `/chatstream`, and MCP surfaces.

### Where the registration stage hands off to execution

Registration does not run the agent itself. Instead, `registration/_handlers.py` builds closures that call `runner.run_agent()` or `runner.run_agent_stream()`, passing the `ResolvedAgent` instructions plus the already-filtered `AgentCapabilities`; the runner then asks the active `ClientManager` to build a chat client and executes through the Microsoft Agent Framework (`src/azure_functions_agents/runner.py:280-502`, `src/azure_functions_agents/client_manager.py:37-238`).

### Registration paths in practice

- **Main agent with debug enabled:** `create_function_app()` skips `register_agent()` because `main.agent.md` has no normal trigger, but `register_debug_endpoints()` exposes the chat UI, REST, SSE, and MCP surfaces for interactive use.
- **Non-main HTTP agent:** `registration/triggers.py` routes `http_trigger` to `make_http_agent_handler()`, which validates JSON input and optionally validates the model's JSON-shaped response before replying.
- **Non-main built-in trigger:** `registration/triggers.py` calls `make_agent_handler()`, which serializes the trigger payload to JSON, turns it into a prompt, and sends it to `runner.run_agent()`.
- **Connector trigger:** `registration/triggers.py` resolves the dotted connector decorator dynamically and then reuses the same `make_agent_handler()` closure pattern as the built-in trigger path.

### Where connector and sandbox tools enter

- Connector specs are read from `GlobalConfig.system_tools.tools_from_connections`, carried into `ResolvedAgent.connector_specs`, and only turned into actual tools when registration or execution calls `configure_connector_tools(...)` / `get_connector_tools()`.
- Sandbox configuration is read from `GlobalConfig.system_tools.execute_in_sessions`, carried into `ResolvedAgent.sandbox_config`, and turned into per-session tool closures by `build_sandbox_tools_for_session()` right before a request is executed.
- Both are intentionally later-bound: startup computes whether an agent may use them, but the actual tool objects are created as close as possible to runtime invocation.

### What the runner receives from registration

By the time a handler calls `runner.run_agent()` or `runner.run_agent_stream()`, the registration layer has already done most of the policy work:

- `ResolvedAgent.instructions` becomes the per-agent instruction block.
- `ResolvedAgent.timeout` and `ResolvedAgent.model` become execution settings.
- `AgentCapabilities.filtered_user_tools` becomes the concrete user-tool list.
- `AgentCapabilities.filtered_mcp_tools` becomes the concrete MCP-tool list.
- `AgentCapabilities.skills_text` becomes the concatenated skills block.
- `AgentCapabilities.use_connector_tools` decides whether connector tools should be pulled from the shared cache.
- `build_sandbox_tools_for_session()` optionally adds per-session ACA sandbox tools just before the call.

The runner therefore focuses on execution concerns: session history, lock management, final tool assembly order, and streaming/non-streaming response handling.

## 5. Key types

These are the main "passport" objects that move through the pipeline:

- `AgentSpec` — raw parsed front matter plus markdown body for one `.agent.md` file. Defined at `src/azure_functions_agents/config/schema.py:108`.
  - **Created by:** `config/loader.py:_load_agent_spec()`
  - **Consumed by:** `config/merge.py:compose()`
- `GlobalConfig` — parsed `agents.config.yaml`, including shared MCP, system-tool, model, timeout, and tool-filter defaults. Defined at `src/azure_functions_agents/config/schema.py:96`.
  - **Created by:** `config/loader.py:load_global_config()`
  - **Consumed by:** `config/merge.py:compose()` and `config/validation.py:validate_global_mcp_references()`
- `ResolvedAgent` — post-merge per-agent runtime config after defaults, overrides, and filters are applied. Defined at `src/azure_functions_agents/config/schema.py:134`.
  - **Created by:** `config/merge.py:compose()`
  - **Consumed by:** validation, capability building, trigger registration, endpoint registration, and sandbox-tool assembly
- `AgentCapabilities` — final filtered bundle of user tools, MCP tools, skills text, and connector-tool enablement. Defined at `src/azure_functions_agents/registration/capabilities.py:13`.
  - **Created by:** `registration/capabilities.py:build_capabilities()`
  - **Consumed by:** `registration/triggers.py`, `registration/endpoints.py`, and the handler closures they create
- `azure.functions.FunctionApp` — the final Azure Functions app object created in `src/azure_functions_agents/app.py:75` and returned by `create_function_app()` in `src/azure_functions_agents/app.py:36-107`.
  - **Created by:** `app.py:create_function_app()`
  - **Consumed by:** Azure Functions itself after the host imports the module and inspects the registered bindings

### Type hand-off summary

In shorthand, the runtime's startup path is:

`Path` --load--> `GlobalConfig` + `list[AgentSpec]` --compose--> `ResolvedAgent` --filter--> `AgentCapabilities` --register--> `FunctionApp`

At invocation time, the runtime continues with:

`ResolvedAgent` + `AgentCapabilities` + request/trigger payload --handler--> `runner.run_agent()` / `run_agent_stream()` --client manager--> model response

### Why the types are split this way

- `AgentSpec` stays close to the author's source file, including optional fields and front-matter shape.
- `GlobalConfig` stays close to the shared YAML file and does not pretend to be agent-specific.
- `ResolvedAgent` is the "translation boundary" type: after this point the code stops asking where a value came from.
- `AgentCapabilities` is intentionally narrower than `ResolvedAgent`; it contains only execution-ready capability objects and flags.
- `FunctionApp` is external to the package, which is why the runtime creates it late and mutates it only after config translation is complete.

This split keeps parsing, policy, Azure binding registration, and runtime execution loosely coupled. It also makes it easier to extend one layer—such as client selection or connector tooling—without changing the others.

## 6. Extension points

### Custom inference client

To plug in a different chat backend, implement the `ClientManager` interface and register it once with `set_client_manager(...)`; after that, `runner.run_agent()` and `runner.run_agent_stream()` use your implementation for every call. See `src/azure_functions_agents/client_manager.py:37-238` and the README section [Plugging in a custom client manager](../README.md#plugging-in-a-custom-client-manager).

This extension point is deliberately below the registration layer: no trigger or endpoint code needs to change when you swap providers. The `ResolvedAgent.model` value is still the hand-off contract, but your manager decides how to interpret it.

### Custom tools

To add project-specific tools, drop a `.py` file into `tools/` and expose either `@tool`-decorated functions or plain functions that can be auto-wrapped into `FunctionTool` objects. Discovery lives in `src/azure_functions_agents/discovery/tools.py:20-108`, and the local decorator shim is in `src/azure_functions_agents/_function_tool.py:29-95`.

These tools enter the pipeline during discovery, are filtered in `build_capabilities()`, and are finally passed into `runner.run_agent()` alongside built-in, connector, sandbox, and MCP tools. In other words, adding a file under `tools/` affects discovery only; the rest of the pipeline remains unchanged.

### Per-agent capability filtering

Each agent can narrow the shared inventory with front-matter `mcp`, `tools`, and `skills` settings; the runtime applies those filters when it builds `AgentCapabilities`. See `src/azure_functions_agents/registration/capabilities.py:33-78` and the detailed field reference in [`docs/front-matter-spec.md`](front-matter-spec.md).

This design keeps global config declarative: shared config says what exists, while agent front matter says what to exclude or opt out of. That separation is the reason the runtime has both a discovery stage and a capability-filtering stage instead of folding them together.

### Other notable seams

- **Built-in file tools:** `discovery/builtin_tools.py` is treated like a runtime-provided tool pack rather than something authors configure per file.
- **Connector tools:** connectors are global infrastructure, so they are sourced from `agents.config.yaml`, not from individual agent files.
- **Debug endpoints:** debug registration is a separate module so the main trigger-registration path stays focused on Azure Function bindings rather than UI and chat surface concerns.

## 7. Related docs

- **This document intentionally stays at the architecture level.** It explains how modules fit together and what objects move between them, but it does not restate every front-matter field or every supported trigger binding.
- For authoring syntax, defaults, and field-by-field schema details, use the front-matter reference.
- For trigger names, arguments, and examples, use the trigger reference.
- Read those two docs alongside this one: this file explains the runtime's internal translation pipeline, while the others explain the external configuration contract.
- If you are tracing a startup issue, start with this document; if you are writing a new agent file, start with the front-matter spec.
- If you are debugging a missing tool, read Sections 3-6 here first, then check the front-matter spec for filters or opt-outs.
- If you are debugging a missing route or binding, compare Section 4 here with `docs/triggers.md`.

- [`docs/front-matter-spec.md`](front-matter-spec.md) — agent file format and configuration reference
- [`docs/triggers.md`](triggers.md) — supported trigger types and examples
