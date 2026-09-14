# Releases

This page explains what changed for customers in each published version of Azure Functions Agents Runtime. It complements the automatically generated [GitHub release list](https://github.com/Azure/azure-functions-agents-runtime/releases), which remains the source for release artifacts and complete commit comparisons.

!!! warning "Public preview"
    All versions listed here are previews. Features and configuration may change before general availability.

## Unreleased

!!! warning "Not yet available"
    Changes listed here are staged for a future package release and are not yet available in a
    published version. Content may change before publication.

No customer-facing changes are currently staged.

## At a glance

| Version | Released | Highlights |
| --- | --- | --- |
| [0.1.0b14](#010b14) | September 8, 2026 | Harness-based execution, stable trigger payloads, Durable Functions 2.x, clearer workflow names |
| [0.1.0b13](#010b13) | August 25, 2026 | Multi-owner workflow isolation, conditional workflow control, documentation site |
| [0.1.0b12](#010b12) | August 11, 2026 | Session resume and history in the chat UI, token-usage logging |
| [0.1.0b11](#010b11) | August 5, 2026 | Flexible agent names, Dynamic Workflow sub-agents |
| [0.1.0b10](#010b10) | July 28, 2026 | Dynamic Workflows for markdown triggers, MCP update |
| [0.1.0b9](#010b9) | July 21, 2026 | Endpoint authentication, multi-agent delegation |
| [0.1.0b8](#010b8) | July 21, 2026 | Non-HTTP trigger serialization fix |
| [0.1.0b7](#010b7) | July 16, 2026 | Experimental Dynamic Workflows, built-in web request tool |
| [0.1.0b6](#010b6) | July 9, 2026 | OpenTelemetry observability |
| [0.1.0b5](#010b5) | July 7, 2026 | Safer ACA Dynamic Sessions authentication |
| [0.1.0b4](#010b4) | June 30, 2026 | Agent discovery under `agents/` |
| [0.1.0b3](#010b3) | June 5, 2026 | Quieter MAF logs and release maintenance |
| [0.1.0b1](#010b1) | June 2, 2026 | First public beta of the markdown-first MAF runtime |
| [0.0.0a2](#000a2) | May 29, 2026 | Early public-preview configuration and endpoint alignment |
| [0.0.0.dev6](#000dev6) | May 28, 2026 | Initial development snapshot |

There was no published `0.1.0b2` release.

---

## 0.1.0b14

**Released September 8, 2026** · [GitHub release](https://github.com/Azure/azure-functions-agents-runtime/releases/tag/v0.1.0b14) · [PyPI](https://pypi.org/project/azurefunctions-agents-runtime/0.1.0b14/)

### Features

- **Consistent harness-based agent execution.** Direct agents, delegated agents, and workflow sub-agents now run through the Microsoft Agent Framework harness. Existing instructions, tools, skills, streaming, sessions, and observability continue to work. Optional global or per-agent `agent_configuration` settings can limit output tokens and compact model-facing conversation history when it reaches a configured token budget. No configuration is required to use the harness. ([#148](https://github.com/Azure/azure-functions-agents-runtime/pull/148))
- **Meaningful Dynamic Workflow names in Durable Task Scheduler.** The DTS dashboard now labels orchestrations with the agent name, tool activities with the workflow tool name, and sub-agent activities with the agent slug. Registered Azure Function names and workflow behavior are unchanged. ([#201](https://github.com/Azure/azure-functions-agents-runtime/pull/201))

### Bug fixes

- **Stable non-HTTP trigger payloads across Azure Functions SDK versions.** Explicit adapters now preserve the runtime's established payload shape for known trigger bindings, including `body_encoding` and binary bodies. This prevents newer `azure-functions` serializers from silently changing data delivered to agents. ([#184](https://github.com/Azure/azure-functions-agents-runtime/pull/184))
- **Read-only skills work without an unavailable approval prompt.** Loading a skill and reading its resources no longer ends a streaming turn before an assistant response. Running a skill script still requires approval and remains blocked until an approval surface is available. ([#191](https://github.com/Azure/azure-functions-agents-runtime/pull/191))

### Compatibility and maintenance

- **Dependency compatibility.** The harness integration updates the runtime's Microsoft Agent Framework dependencies, and the runtime now depends on `azure-functions-durable==2.0.0b2`. No `.agent.md` or runtime configuration migration is required, but applications that directly pin or import MAF or Durable Functions must reconcile the new versions and revalidate those integrations. ([#148](https://github.com/Azure/azure-functions-agents-runtime/pull/148), [#189](https://github.com/Azure/azure-functions-agents-runtime/pull/189))
- The Dynamic Workflows guide now includes animated comparisons of standard agent-loop and workflow execution. ([#180](https://github.com/Azure/azure-functions-agents-runtime/pull/180))
- Contributor guidance now explains how to split large features into reviewable pull requests. This does not change runtime behavior. ([#192](https://github.com/Azure/azure-functions-agents-runtime/pull/192))

### Upgrade notes

- Rebuild the application environment so the new Microsoft Agent Framework and Durable Functions dependency versions are installed.
- Existing agent files and runtime configuration require no changes. Add `agent_configuration` only when explicit output or context-window limits are needed.

[Compare 0.1.0b13...0.1.0b14](https://github.com/Azure/azure-functions-agents-runtime/compare/v0.1.0b13...v0.1.0b14)

---

## 0.1.0b13

**Released August 25, 2026** · [GitHub release](https://github.com/Azure/azure-functions-agents-runtime/releases/tag/v0.1.0b13) · [PyPI](https://pypi.org/project/azurefunctions-agents-runtime/0.1.0b13/)

### Features and improvements

- Dynamic Workflows gained multi-owner ownership and isolation, allowing workflow access to be scoped to the owning agent. ([#151](https://github.com/Azure/azure-functions-agents-runtime/pull/151))
- Dynamic Workflows gained data-driven control flow for conditional execution. ([#163](https://github.com/Azure/azure-functions-agents-runtime/pull/163))
- Agent metadata can be represented for inventory-as-code scenarios. ([#165](https://github.com/Azure/azure-functions-agents-runtime/pull/165))
- This MkDocs documentation site launched on GitHub Pages. ([#156](https://github.com/Azure/azure-functions-agents-runtime/pull/156))

### Maintenance

- Official package builds stopped including the test suite, and the release cadence was updated. Runtime behavior was not changed by this work. ([#172](https://github.com/Azure/azure-functions-agents-runtime/pull/172))

[Compare 0.1.0b12...0.1.0b13](https://github.com/Azure/azure-functions-agents-runtime/compare/v0.1.0b12...v0.1.0b13)

---

## 0.1.0b12

**Released August 11, 2026** · [GitHub release](https://github.com/Azure/azure-functions-agents-runtime/releases/tag/v0.1.0b12) · [PyPI](https://pypi.org/project/azurefunctions-agents-runtime/0.1.0b12/)

### Features and improvements

- The built-in chat UI can display and copy the current session ID, resume a session, list recent sessions, and replay conversation history. ([#143](https://github.com/Azure/azure-functions-agents-runtime/pull/143))
- Runtime logs include model token usage for monitoring consumption and investigating cost. ([#147](https://github.com/Azure/azure-functions-agents-runtime/pull/147))

[Compare 0.1.0b11...0.1.0b12](https://github.com/Azure/azure-functions-agents-runtime/compare/v0.1.0b11...v0.1.0b12)

---

## 0.1.0b11

**Released August 5, 2026** · [GitHub release](https://github.com/Azure/azure-functions-agents-runtime/releases/tag/v0.1.0b11) · [PyPI](https://pypi.org/project/azurefunctions-agents-runtime/0.1.0b11/)

### Features

- Agent display names can differ from their source filename or normalized slug, making customer-facing names more flexible without changing routing identity. ([#94](https://github.com/Azure/azure-functions-agents-runtime/pull/94))
- Dynamic Workflows can invoke workflow sub-agents, enabling specialized agents to participate in durable orchestration. ([#117](https://github.com/Azure/azure-functions-agents-runtime/pull/117))

[Compare 0.1.0b10...0.1.0b11](https://github.com/Azure/azure-functions-agents-runtime/compare/v0.1.0b10...v0.1.0b11)

---

## 0.1.0b10

**Released July 28, 2026** · [GitHub release](https://github.com/Azure/azure-functions-agents-runtime/releases/tag/v0.1.0b10) · [PyPI](https://pypi.org/project/azurefunctions-agents-runtime/0.1.0b10/)

### Features and improvements

- Markdown-declared triggers can start Dynamic Workflows. ([#112](https://github.com/Azure/azure-functions-agents-runtime/pull/112))
- Application Insights Live Metrics is disabled when Microsoft Entra environment-variable authentication is configured, avoiding an incompatible telemetry path. ([#118](https://github.com/Azure/azure-functions-agents-runtime/pull/118))

### Security and maintenance

- The MCP dependency was updated to address CVE-2026-59950, and compatible dependency ranges were loosened so patch updates can be consumed. ([#114](https://github.com/Azure/azure-functions-agents-runtime/pull/114))
- End-to-end coverage, release scheduling, and validation for `feature/*` integration branches were improved. These changes primarily affect maintainers. ([#101](https://github.com/Azure/azure-functions-agents-runtime/pull/101), [#116](https://github.com/Azure/azure-functions-agents-runtime/pull/116), [#89](https://github.com/Azure/azure-functions-agents-runtime/pull/89), [#125](https://github.com/Azure/azure-functions-agents-runtime/pull/125))

[Compare 0.1.0b9...0.1.0b10](https://github.com/Azure/azure-functions-agents-runtime/compare/v0.1.0b9...v0.1.0b10)

---

## 0.1.0b9

**Released July 21, 2026** · [GitHub release](https://github.com/Azure/azure-functions-agents-runtime/releases/tag/v0.1.0b9) · [PyPI](https://pypi.org/project/azurefunctions-agents-runtime/0.1.0b9/)

### Features

- Built-in and HTTP-trigger endpoints can enforce inbound authentication instead of relying only on surrounding infrastructure. ([#100](https://github.com/Azure/azure-functions-agents-runtime/pull/100))
- Agents can expose other agents as tools for multi-agent delegation. ([#102](https://github.com/Azure/azure-functions-agents-runtime/pull/102))

[Compare 0.1.0b8...0.1.0b9](https://github.com/Azure/azure-functions-agents-runtime/compare/v0.1.0b8...v0.1.0b9)

---

## 0.1.0b8

**Released July 21, 2026** · [GitHub release](https://github.com/Azure/azure-functions-agents-runtime/releases/tag/v0.1.0b8) · [PyPI](https://pypi.org/project/azurefunctions-agents-runtime/0.1.0b8/)

### Bug fixes

- Azure Functions binding objects from non-HTTP triggers are serialized consistently before being passed to an agent. This fixed failures and unexpected payloads across the supported trigger types. ([#105](https://github.com/Azure/azure-functions-agents-runtime/pull/105))

[Compare 0.1.0b7...0.1.0b8](https://github.com/Azure/azure-functions-agents-runtime/compare/v0.1.0b7...v0.1.0b8)

---

## 0.1.0b7

**Released July 16, 2026** · [GitHub release](https://github.com/Azure/azure-functions-agents-runtime/releases/tag/v0.1.0b7) · [PyPI](https://pypi.org/project/azurefunctions-agents-runtime/0.1.0b7/)

### Features

- **Experimental Dynamic Workflows** introduced durable orchestration of agent tools and tasks. ([#77](https://github.com/Azure/azure-functions-agents-runtime/pull/77))
- The built-in `web_request` system tool lets an agent make controlled outbound HTTP requests. ([#96](https://github.com/Azure/azure-functions-agents-runtime/pull/96))

### Bug fixes and documentation

- Error logging was expanded to make failed agent loads and executions easier to diagnose. ([#82](https://github.com/Azure/azure-functions-agents-runtime/pull/82))
- Function signatures now include the injected `client` parameter only when it is needed. ([#93](https://github.com/Azure/azure-functions-agents-runtime/pull/93))
- Durable type annotations and workflow endpoint handlers were corrected and simplified. ([#97](https://github.com/Azure/azure-functions-agents-runtime/pull/97), [#98](https://github.com/Azure/azure-functions-agents-runtime/pull/98))
- A generated front-matter reference and the first end-to-end test suite were added. ([#86](https://github.com/Azure/azure-functions-agents-runtime/pull/86), [#95](https://github.com/Azure/azure-functions-agents-runtime/pull/95))

[Compare 0.1.0b6...0.1.0b7](https://github.com/Azure/azure-functions-agents-runtime/compare/v0.1.0b6...v0.1.0b7)

---

## 0.1.0b6

**Released July 9, 2026** · [GitHub release](https://github.com/Azure/azure-functions-agents-runtime/releases/tag/v0.1.0b6) · [PyPI](https://pypi.org/project/azurefunctions-agents-runtime/0.1.0b6/)

### Features

- OpenTelemetry integration added runtime tracing and metrics for agent, model, and tool activity, with export to Application Insights when configured. ([#79](https://github.com/Azure/azure-functions-agents-runtime/pull/79))

[Compare 0.1.0b5...0.1.0b6](https://github.com/Azure/azure-functions-agents-runtime/compare/v0.1.0b5...v0.1.0b6)

---

## 0.1.0b5

**Released July 7, 2026** · [GitHub release](https://github.com/Azure/azure-functions-agents-runtime/releases/tag/v0.1.0b5) · [PyPI](https://pypi.org/project/azurefunctions-agents-runtime/0.1.0b5/)

### Security improvement

- The ACA Dynamic Sessions endpoint host is validated before the runtime attaches a managed identity token, preventing credentials from being sent to an unexpected host. ([#83](https://github.com/Azure/azure-functions-agents-runtime/pull/83))

[Compare 0.1.0b4...0.1.0b5](https://github.com/Azure/azure-functions-agents-runtime/compare/v0.1.0b4...v0.1.0b5)

---

## 0.1.0b4

**Released June 30, 2026** · [GitHub release](https://github.com/Azure/azure-functions-agents-runtime/releases/tag/v0.1.0b4) · [PyPI](https://pypi.org/project/azurefunctions-agents-runtime/0.1.0b4/)

### Features and documentation

- The runtime can discover agent definitions in an `agents/` directory, making applications with many agents easier to organize. ([#74](https://github.com/Azure/azure-functions-agents-runtime/pull/74))
- Documentation added nested skill-directory examples. ([#76](https://github.com/Azure/azure-functions-agents-runtime/pull/76))

### Maintenance

- `aiohttp` and `starlette` received dependency updates. ([#61](https://github.com/Azure/azure-functions-agents-runtime/pull/61), [#66](https://github.com/Azure/azure-functions-agents-runtime/pull/66))
- The repository added its feature-development, worktree, and FRD process. This does not change runtime behavior. ([#71](https://github.com/Azure/azure-functions-agents-runtime/pull/71))

[Compare 0.1.0b3...0.1.0b4](https://github.com/Azure/azure-functions-agents-runtime/compare/release0.1.0b3...v0.1.0b4)

---

## 0.1.0b3

**Released June 5, 2026** · [GitHub release](https://github.com/Azure/azure-functions-agents-runtime/releases/tag/release0.1.0b3) · [PyPI](https://pypi.org/project/azurefunctions-agents-runtime/0.1.0b3/)

### Improvements and maintenance

- Microsoft Agent Framework experimental warnings are suppressed so application logs focus on actionable runtime information; agent metadata logging was consolidated. ([#60](https://github.com/Azure/azure-functions-agents-runtime/pull/60))
- The package release pipeline was refactored. ([#58](https://github.com/Azure/azure-functions-agents-runtime/pull/58))

[Compare 0.1.0b1...0.1.0b3](https://github.com/Azure/azure-functions-agents-runtime/compare/0.1.0b1...release0.1.0b3)

---

## 0.1.0b1

**Released June 2, 2026** · [GitHub release](https://github.com/Azure/azure-functions-agents-runtime/releases/tag/0.1.0b1) · [PyPI](https://pypi.org/project/azurefunctions-agents-runtime/0.1.0b1/)

### Initial beta capabilities

- A markdown-first runtime built on Microsoft Agent Framework, with front-matter and global YAML configuration translated into Azure Functions. ([#6](https://github.com/Azure/azure-functions-agents-runtime/pull/6), [#7](https://github.com/Azure/azure-functions-agents-runtime/pull/7), [#9](https://github.com/Azure/azure-functions-agents-runtime/pull/9), [#23](https://github.com/Azure/azure-functions-agents-runtime/pull/23))
- MCP support with Azure identity authentication, connector triggers, reasoning streaming, Microsoft Foundry samples, and an Outlook reply-agent sample. ([#20](https://github.com/Azure/azure-functions-agents-runtime/pull/20), [#28](https://github.com/Azure/azure-functions-agents-runtime/pull/28), [#41](https://github.com/Azure/azure-functions-agents-runtime/pull/41), [#45](https://github.com/Azure/azure-functions-agents-runtime/pull/45))
- Unified built-in agent endpoints and removal of the earlier `run_copilot` public API. ([#43](https://github.com/Azure/azure-functions-agents-runtime/pull/43), [#48](https://github.com/Azure/azure-functions-agents-runtime/pull/48))

### Quality, security, and release engineering

- CI linting and test reliability were improved, and a partner release pipeline was introduced. ([#17](https://github.com/Azure/azure-functions-agents-runtime/pull/17), [#25](https://github.com/Azure/azure-functions-agents-runtime/pull/25), [#26](https://github.com/Azure/azure-functions-agents-runtime/pull/26), [#31](https://github.com/Azure/azure-functions-agents-runtime/pull/31), [#44](https://github.com/Azure/azure-functions-agents-runtime/pull/44), [#52](https://github.com/Azure/azure-functions-agents-runtime/pull/52), [#54](https://github.com/Azure/azure-functions-agents-runtime/pull/54))
- The repository added its security policy and updated test dependencies. ([#13](https://github.com/Azure/azure-functions-agents-runtime/pull/13), [#29](https://github.com/Azure/azure-functions-agents-runtime/pull/29))

[Compare 0.0.0.dev1...0.1.0b1](https://github.com/Azure/azure-functions-agents-runtime/compare/0.0.0dev1...0.1.0b1)

---

## 0.0.0a2

**Released May 29, 2026** · [GitHub release](https://github.com/Azure/azure-functions-agents-runtime/releases/tag/0.0.0a2) · [PyPI](https://pypi.org/project/azurefunctions-agents-runtime/0.0.0a2/)

This early alpha aligned the quickstart and samples on Microsoft Foundry, updated public-preview configuration names, unified built-in endpoints, removed the legacy `run_copilot` API, and stabilized package publishing. ([#41](https://github.com/Azure/azure-functions-agents-runtime/pull/41), [#43](https://github.com/Azure/azure-functions-agents-runtime/pull/43), [#44](https://github.com/Azure/azure-functions-agents-runtime/pull/44), [#45](https://github.com/Azure/azure-functions-agents-runtime/pull/45), [#48](https://github.com/Azure/azure-functions-agents-runtime/pull/48), [#52](https://github.com/Azure/azure-functions-agents-runtime/pull/52), [#54](https://github.com/Azure/azure-functions-agents-runtime/pull/54))

[Compare 0.0.0.dev6...0.0.0a2](https://github.com/Azure/azure-functions-agents-runtime/compare/0.0.0.dev6...0.0.0a2)

---

## 0.0.0.dev6

**Released May 28, 2026** · [GitHub release](https://github.com/Azure/azure-functions-agents-runtime/releases/tag/0.0.0.dev6) · [PyPI](https://pypi.org/project/azurefunctions-agents-runtime/0.0.0.dev6/)

The initial development snapshot established the Azure Functions agent runtime, migrated its early implementation to Microsoft Agent Framework, introduced markdown/YAML translation and MCP support, and added the first CI and release infrastructure. See the [GitHub release](https://github.com/Azure/azure-functions-agents-runtime/releases/tag/0.0.0.dev6) for the complete foundational PR list.
