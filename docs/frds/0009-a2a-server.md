---
frd: "0009"
title: A2A server (simple profile)
status: Finalized
author: tsuyoshiushio / Copilot
created: 2026-09-08
updated: 2026-09-25
issues: [#211]
pull_requests: [#208, #209]
branch: tsuyoshiushio-a2a-frd-and-design
---

# FRD 0009 - A2A server (simple profile)

## 1. Summary

Add an experimental, opt-in Agent2Agent (A2A) server endpoint to
markdown-authored agents. An agent that declares `builtin_endpoints.a2a`
publishes a per-agent Agent Card and accepts A2A 1.0 JSON-RPC `SendMessage`
requests. The runtime runs the agent through its existing non-streaming runner
and returns one direct A2A `Message`. The protocol layer uses the Microsoft Agent
Framework (MAF) A2A hosting helpers and the A2A Python SDK.

This FRD covers only the `simple` profile. Streaming, Tasks, and durable
execution are planned for later work (see [Planned work](#planned-work)). They
are not part of this FRD and are not available.

## 2. Motivation / problem

Agents built with this runtime can be called through the chat API or as an MCP
tool. Other agent platforms use A2A to call remote agents. Today, an A2A client
cannot call a markdown-authored agent without a custom HTTP adapter.

## 3. Goals / Non-goals

**Goals**

| ID | Requirement |
| --- | --- |
| G1 | A2A is opt-in per agent. Existing configurations register the same routes as before. `builtin_endpoints: true` does not enable A2A. |
| G2 | Use the public MAF A2A hosting helpers and SDK routes. Do not write a separate protocol implementation. |
| G3 | An authorized text request completes with one direct A2A `Message`. There is no Task, streaming, or Durable execution. |
| G4 | Reuse the existing runner, tools, skills, policies, timeouts, sessions, and auth. Existing chat, UI, and MCP behavior does not change. |
| G5 | Unsupported operations fail with an explicit A2A error. Fixed limits protect the endpoint. |

**Non-goals**

- Remote A2A agents as tools of a local agent.
- Replacing the chat, UI, or MCP endpoints.
- Production readiness. The endpoint is experimental.

### Planned work

These items are planned for later work. Each needs its own FRD, or an update to
this FRD, before implementation:

- Streaming responses (`SendStreamingMessage`, SSE).
- A2A Task lifecycle (get, list, cancel, subscribe).
- Durable and distributed Task execution.
- Optional Durable dependency for apps that do not use workflows.
- REST and gRPC bindings, and push notifications.

## 4. Proposed design

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| Discover | `discovery/*` | No change. A2A uses the existing agent, tool, and skill inventory. |
| Translate | `config/schema.py`, `config/merge.py`, `config/validation.py` | Add typed `builtin_endpoints.a2a` (`mode`, `url`). Validate the URL. Resolve `http_auth` for A2A routes. |
| Register | `registration/endpoints.py`, `registration/a2a.py` (new), `registration/_auth.py` | Register the card and JSON-RPC routes for each agent that declares `a2a`. `registration/a2a.py` also contains the SDK `RequestHandler` for the simple profile. |
| Execute | `runner.py` | No change. The handler calls the existing non-streaming runner. |

### Authoring / API surface

```yaml
builtin_endpoints:
  a2a:
    mode: simple        # optional; only "simple" is accepted
    url: https://agents.example.com/agents/incident-triage/a2a
  http_auth: entra      # function (default) | admin | anonymous | entra
```

- `a2a` is disabled when absent. It requires the object form.
- `url` is required. It is the trusted external JSON-RPC URL that the Agent Card
  publishes. It must be absolute HTTPS, without credentials, query, or fragment.
  HTTP is accepted only for loopback hosts. The path must end with
  `/agents/{slug}/a2a`. The runtime does not infer it from request headers.
- An A2A-only agent satisfies the "endpoint or trigger" validation rule.

Routes, relative to the Functions route prefix. Both use the resolved
`http_auth` policy:

| Route | Method | Purpose |
| --- | --- | --- |
| `agents/{slug}/.well-known/agent-card.json` | GET | Per-agent Agent Card. Clients configure this URL. There is no domain-root discovery. |
| `agents/{slug}/a2a` | POST | A2A 1.0 JSON-RPC `SendMessage`. |

### Protocol profile

- Wire version `1.0` only. A request without `A2A-Version: 1.0` is rejected,
  because an absent header means 0.3.
- Only `SendMessage` is supported. Other methods return
  `UnsupportedOperationError`.
- Input is a user `Message` with text Parts only. `taskId`,
  `referenceTaskIds`, extensions, push configuration, and file or data Parts are
  rejected.
- The result is one `Message` with a new `messageId` and the same `contextId`.
  `returnImmediately` is accepted with either value and has no effect, as the
  specification defines for a direct Message.
- The Agent Card advertises JSON-RPC 1.0, text/plain input and output, one skill
  from the agent name and description, `streaming: false`,
  `pushNotifications: false`, and the matching function-key or Entra security
  scheme.

### Conversation context

The optional `contextId` continues a conversation. The server generates one when
it is absent. The runner session ID is a SHA-256 hash of the auth scope, agent
slug, and `contextId`, so client input never becomes a file name. Function,
admin, and anonymous modes share one app trust scope. Entra uses the validated
tenant and object or client identity.

### Limits

Fixed limits for each agent: 32 executions in flight, 256 KiB request body,
16 Parts, 32 KiB for each text Part, 64 KiB total input text, and 256 KiB
response text. When a JSON-RPC envelope is available, errors use SDK JSON-RPC
errors with the request ID. Otherwise, the endpoint returns HTTP 400 or 413.
Responses contain only assistant text. Reasoning, tool arguments, tool results,
and runtime metadata are not returned.

### Compatibility

- New optional extra `[a2a]`: `agent-framework-hosting-a2a==1.0.0a260730`,
  `agent-framework-hosting==1.0.0a260730`, and
  `a2a-sdk[http-server]==1.1.2`. The existing MAF core, OpenAI, and Foundry pins
  do not change. If an agent declares `a2a` without the extra, startup fails
  with an installation message.
- `azure-functions-durable` stays a mandatory dependency. A2A alone uses a plain
  `FunctionApp`. With workflows, A2A shares the existing `DFApp`.
- Local development requires Azure Functions Core Tools 4.14.0 or later. On
  Windows with Python 3.13, Core Tools 4.10.0 stops with `0xC0000005` during
  indexing because of a Protobuf conflict with the worker (#211).

## 5. Decisions log

Agent entries are recommendations. The Human approval of the simple profile on
2026-09-08 covers the implementation that follows them.

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Delivery order | All at once / incremental | Start with a non-streaming simple server. Streaming and durable execution come later in separate work. | Human | 2026-09-08 |
| 2 | A2A integration | Custom protocol implementation / MAF A2A | Use the MAF A2A library. | Human | 2026-09-08 |
| 3 | MAF server integration | `A2AExecutor` / hosting conversion helpers | Use `agent-framework-hosting-a2a` helpers with an app-owned SDK `RequestHandler`, so that runtime policy stays in the runtime. | Agent | 2026-09-08 |
| 4 | Protocol binding | JSON-RPC / REST / both | JSON-RPC only. | Agent | 2026-09-08 |
| 5 | Response model | Transient Task / direct Message | One direct Message. Do not create a Task that is not stored. | Agent | 2026-09-08 |
| 6 | Library versions | Newer packages / proposed tuple | Add `[a2a]` with hosting-a2a and hosting `1.0.0a260730` and `a2a-sdk[http-server]==1.1.2`. Keep the existing MAF pins and the mandatory Durable dependency. | Human; Agent verified wheels and `pip check` | 2026-09-08 |
| 7 | Protocol version | 0.3 / 1.0 / both | A2A 1.0 `SendMessage` only. Reject a missing version header, 0.3 names, streaming, Task methods, and `taskId`. | Human | 2026-09-08 |
| 8 | Conversation context | Reject context / raw context as session / scoped hash | Accept `contextId` and derive the runner session from a hash of auth scope, agent slug, and `contextId`. | Agent | 2026-09-08 |
| 9 | Card URL and auth | Infer from Host / configured URL | Require `a2a.url`. Serve the per-agent card with the same `http_auth` as JSON-RPC. | Agent | 2026-09-08 |
| 10 | Limits | Configurable / fixed | Fixed limits as listed in §4. | Agent | 2026-09-08 |
| 11 | Composition | Separate agent invocation / existing runner | Use the existing non-streaming runner. `builtin_endpoints: true` does not enable A2A. | Human | 2026-09-08 |
| 12 | Client validation | Upgrade runtime MAF / raw HTTP only / separate client environment | The sample uses MAF `A2AAgent` (`agent-framework-a2a==1.0.0b260821`, core 1.15+) in a separate virtual environment. The server keeps core 1.13. A raw HTTP client is also kept. | Human | 2026-09-08 |
| 13 | Handler placement | New `a2a/` package / `registration/a2a.py` | Keep the `RequestHandler` in `registration/a2a.py`. There is no second consumer yet. | Agent recommendation; accepted by Human | 2026-09-25 |
| 14 | Local Core Tools | Pin Protobuf / worker setting / require Core Tools 4.14.0+ | Document Core Tools 4.14.0+. Do not ship pins or worker settings as a workaround. Track the worker defect in #211. | Human | 2026-09-25 |
| 15 | FRD scope | Full staged design / implemented profile only | This FRD covers only the simple profile. Later work is listed as planned, without detail. | Human | 2026-09-25 |

## 6. Test plan

- [x] Unit: config schema, merge, and validation for `a2a` (URL rules, opt-in, auth inheritance).
- [x] Fixture scenario: `tests/fixtures/config_scenarios/20_a2a_simple/`.
- [x] Registration: routes, auth level, missing extra, and workflows with A2A.
- [x] Protocol: Agent Card, version check, direct Message, `contextId`,
  `returnImmediately`, unsupported methods and Parts, limits, and JSON-RPC ID
  correlation.
- [x] CI: clean wheel install of `[a2a]` on Python 3.13 and 3.14.
- [x] E2E: real Functions host. MAF `A2AAgent` and a raw HTTP client fetch the
  card and send a message.

## 7. Docs impact

- `docs/front-matter-spec.md`, `docs/front-matter-reference.md` (generated):
  `builtin_endpoints.a2a`.
- `docs/architecture.md`: module map and registration handoff.
- `README.md`, `docs/index.md`, `docs/getting-started.md`, `samples/README.md`:
  A2A entry points.
- `samples/a2a-incident-triage/`: runnable sample.
- `docs/triggers.md`: no change. A2A is not a trigger.

## 8. Status & sign-off

- **Architecture review:** The pipeline boundaries (discover, translate,
  register, execute) were reviewed on 2026-09-08.
- **Human sign-off:** The simple profile was approved for implementation on
  2026-09-08. The FRD scope was reduced to the simple profile on 2026-09-25.
