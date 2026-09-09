---
frd: "0009"
title: A2A server with optional Durable execution
status: Finalized for P3
author: tsuyoshiushio / Copilot
created: 2026-09-08
updated: 2026-09-08
issues: []
pull_requests: []
branch: tsuyoshiushio-a2a-frd-and-design
---

# FRD 0009 - A2A server with optional Durable execution

## 1. Summary

Add an opt-in Agent2Agent (A2A) server surface to markdown-authored agents using
Microsoft Agent Framework (MAF)'s A2A library. Deliver a simple non-streaming
server first, a deliberately limited basic SSE server next, and durable,
distributed task execution afterward. Separately remove the runtime's existing
mandatory Durable dependency for applications that use neither workflows nor
durable A2A. P3 is authorized for implementation; every later slice remains a **design
proposal, not an available feature**.

The [proposed architecture](../design/a2a-server.md) defines integration and
correctness boundaries; the [implementation plan](../design/a2a-implementation-plan.md)
defines independently reviewable PRs and capability gates. Existing behavior
remains documented in [architecture.md](../architecture.md).

## 2. Motivation / problem

The current chat SSE endpoint streams one POST response; it is not an A2A task
service. Its process-local session locks and conversation history cannot provide
cross-worker task lookup, cancellation, or subscriptions. Copying a connection
backplane would route requests but would not solve task execution recovery.

At inspected baseline `08b9d762d95f451ce926b89c166a14a2d345d433`,
`azure-functions-durable==2.0.0b2` is mandatory even for plain chat applications,
and eager imports prevent simply moving that requirement to an extra. The pinned
MAF packages' locked dependency closures do not themselves require Durable.
Decoupling must address installation, imports, startup, and registration together.

Users need an incremental adoption path without accidentally treating a
single-process SSE prototype as a production distributed service. Reviewers need
separate changes for dependency migration, protocol semantics, execution, and
distributed delivery rather than a single large feature PR.

## 3. Goals / Non-goals

**Goals**

| ID | Requirement and observable acceptance criterion |
| --- | --- |
| R1 | Opt-in authoring follows discover -> translate -> register -> execute. Existing configurations produce the same routes; `builtin_endpoints: true` does not silently expose A2A. |
| R2 | Use the MAF A2A integration through public extension points, with one tested protocol/version tuple. AgentCard, request validation, result/event serialization, and errors match that tuple; no hand-written competing protocol stack. |
| R3 | Simple mode completes an authorized text request with one direct A2A `Message`, without Task persistence, SSE, or Durable execution. Initial package/import coupling remains until P1/P2. Unsupported task operations fail explicitly; `returnImmediately` has no effect for a direct Message and does not promise detached execution. |
| R4 | Basic SSE emits legal A2A domain events, not chat `data:` payloads. Incremental output is Task-first with ordered artifact updates; a message-only stream contains exactly one Message. Local-only limitations are explicit. |
| R5 | Existing chat wire output, public runner APIs, tool authorization, model options, session handling, deadlines, usage records, and cleanup remain unchanged. |
| R6 | At P2, fresh base, base+monitor, and simple/basic A2A installations do not install or import Durable, and register no Durable bindings when workflows are disabled. Earlier P3/P5 samples retain the baseline mandatory dependency; isolation is not their prerequisite. Enabling a feature without its extra fails at startup with an actionable error. |
| R7 | Existing workflow applications retain behavior after installing the documented workflow extra. Workflows and durable A2A coexist with one `DFApp`, independently named registrations, and no duplicated workflow engine. |
| R8 | Durable admission returns stable task/context identities and eventually schedules accepted work despite disconnect or scheduling failures. Context-less retries deduplicate by owner + agent + client message ID before server IDs are generated; conflicting payloads fail. |
| R9 | An authorized request on any worker can get/list/subscribe/cancel a durable task. Subscribe starts with the current snapshot at sequence N, then events after N without a race gap; multiple subscribers receive the same ordered events independently. |
| R10 | Execution ownership, retries, continuation, and cancellation have explicit state rules. Stale attempts cannot publish; cancellation is cooperative; retryable activities do not imply exactly-once tool side effects. |
| R11 | Every operation enforces owner/agent/context scope, bounded input and output, retention, and resource limits. Tenant ID or possession of a task ID alone is not authorization. |
| R12 | Each PR carries its own tests, bounds, and affected docs. Basic SSE requires an enforced local-only startup guard or stays test-only; durable config/cards remain internal through P9b and become public only after P10 qualification. |

**Non-goals**

Do not replace chat/UI/MCP endpoints or the workflow plan engine; add remote A2A
agents as tools; implement push notifications, gRPC, or REST in the initial
chain; guarantee full historic replay, unlimited SSE lifetime, exactly-once LLM
execution, or exactly-once external side effects. Do not deploy experimental
infrastructure as part of this design work. A2A Task identity is not a Durable
instance ID and is not a chat session ID.

## 4. Proposed design

| Pipeline stage | Existing / proposed modules | Change |
| --- | --- | --- |
| Discover | `discovery/*` | Keep read-only inventories. A2A reuses the existing agent/tool/skill inventory; no new discovery format. |
| Translate | `config/schema.py`, `merge.py`, `validation.py` | Proposed typed A2A options and strict mode validation; compose auth and capabilities before app mutation. |
| Register | `app.py`, `registration/capabilities.py`, `registration/a2a.py` (new), `_auth.py` | Freeze mode-specific capabilities; register per-agent card and JSON-RPC routes. Select `DFApp` only for enabled workflows or durable A2A. |
| Execute | `runner.py`, `a2a/adapter.py` (new) | Reuse MAF execution and runtime policy. Introduce a private typed event seam when basic SSE consumes it; retain existing chat serialization. |
| Durable execution | `a2a/durable.py`, `a2a/store.py` (new) | Separate task lifecycle from HTTP and use a shared task projection plus ordered journal. Azure binding/registration code remains in registration or the composition root. |

Names for new internal modules are proposed ownership boundaries, not promises of
new public Python APIs. The recommended integration is the published Alpha
`agent-framework-hosting-a2a==1.0.0a260730` with
`agent-framework-hosting==1.0.0a260730` and candidate `a2a-sdk==1.1.2`.
Use its public conversion helpers with an app-owned SDK `RequestHandler`, not the
opinionated MAF `A2AExecutor`. The [library gate](../design/a2a-server.md#2-library-and-protocol-compatibility-gate)
covers Alpha approval, native models, optional HTTP extras, and the Functions
response bridge. Detailed storage selection is a separate pre-durable gate.

### Authoring / API surface

Implemented P3 example:

```yaml
builtin_endpoints:
  chat_api: true
  a2a:
    mode: simple
    url: https://agents.example.com/agents/incident-triage/a2a
  http_auth: entra
```

`a2a` is absent/disabled by default. P3 accepts only `mode: simple`; `basic`
requires the P5b local-only startup guard (otherwise test-only). `durable` is not
accepted as public configuration or advertised in cards until P10; earlier slices
use internal harnesses/experimental configuration. `builtin_endpoints: true`
keeps its current chat/UI/MCP meaning. An explicit
A2A declaration inherits the resolved built-in HTTP auth policy; no secret,
connection string, task hub, or executor routing ID goes into front matter.
An A2A-only agent must satisfy the endpoint-or-trigger validation rule.

Proposed per-agent routes, relative to the configured Functions route prefix:
`agents/{slug}/a2a` (JSON-RPC POST) and
`agents/{slug}/.well-known/agent-card.json` (GET). The per-agent base avoids an
ambiguous root card in a multi-agent app, but is **not domain-root well-known
auto-discovery**: clients configure the explicit card URL or SDK `card_path`.
A root singleton/routing convention is deferred. Published card URLs use a trusted
external base URL, not an untrusted forwarded Host header. Card discovery/auth and
SDK route mounting are part of the first server acceptance gate.

Target semantics are A2A specification v1.0.1, **wire version `1.0`**. Never
advertise 1.0 based on a Python package's version number. A 0.3 SDK surface requires
an explicitly approved compatibility profile, not mixed method names or fields.
See the design's version gate.

### Compatibility

The final simple/basic A2A installation must not require Durable unless the same
app enables workflows. This is P2's acceptance gate after the P3/P5 working
samples: those earlier slices retain existing mandatory Durable installation and
eager imports but do not use Durable execution/bindings when workflows are off.
Proposed extras are `[workflows]`, `[a2a]`, and `[a2a-durable]`; the last includes
the dependencies needed for both A2A and Durable execution. `[monitor]` remains
independent. These names are proposals until dependency review.

Moving an existing mandatory dependency is a packaging compatibility change:
update workflow samples, E2E requirements, development installs, and migration
guidance in the same PR. Do not silently disable workflows when Durable is absent.
Preserve supported public workflow helper imports using lazy exports/type-safe
boundaries where necessary; invoke an actionable missing-extra error only when
Durable functionality is actually requested.

### Delivery sequence

The [plan](../design/a2a-implementation-plan.md) prioritizes **D0 -> P3 -> P4 ->
P5a -> P5b -> P1 -> P2 -> P6a onward**, retaining existing PR IDs for reference.
After the FRD, P3 delivers a working simple A2A implementation/sample for design
validation and discussion; record feedback before the P4 event seam and P5a/P5b
local lifecycle/SSE work. P1/P2 import cleanup and dependency migration follow,
then P6a/P6b/P6c atomic storage/admission/managed polling, P7 recovery,
P8 distributed readers/SSE, P9a cancellation, P9b continuation, and P10 qualification.
Atomic publication belongs to P6a; bounds ship with each affected surface.
Internal tested components do not create unused public APIs or early card claims.

## 5. Decisions log

Entries marked Agent are recommendations, not human architecture approval.

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Scope of this work | Implement / design only | Documentation only; no production code, dependency change, commit, push, or PR | Human, relayed by coordinating session | 2026-09-08 |
| 2 | Delivery order | All-at-once / incremental | Non-SSE simple server, basic SSE, then durable/distributed capabilities; reviewer-friendly PRs | Human, relayed by coordinating session | 2026-09-08 |
| 3 | A2A integration | Custom protocol implementation / MAF A2A | Use MAF A2A library; verify its server and SDK boundaries before implementation | Human, relayed by coordinating session | 2026-09-08 |
| 4 | Durable installation | Mandatory / optional | Optional when neither workflows nor durable A2A is enabled | Human, relayed by coordinating session | 2026-09-08 |
| 5 | Initial protocol binding | JSON-RPC / REST / both | JSON-RPC first; exact supported version determined by the library gate | Agent | 2026-09-08 |
| 6 | Simple response model | Invent transient Task / direct Message | Direct single Message; no fabricated persistence or nonblocking acceptance | Agent | 2026-09-08 |
| 7 | Distributed transport | Owner-routed backplane / shared state and events | Any-worker readers over shared projection/journal; execution separate from HTTP | Agent | 2026-09-08 |
| 8 | Replay promise | Full token history / current snapshot plus ordered continuation | Snapshot plus continuation; retention-defined journal, no full historic replay promise | Agent | 2026-09-08 |
| 9 | Storage baseline | Table+Blob / another transactional store | Tentatively Table+Blob; transaction, admission, fencing, and retention gate before implementation | Agent | 2026-09-08 |
| 10 | Authoring and extras | New top-level format / existing built-in config | Extend typed built-in options; propose workflows/a2a/a2a-durable extras; keep old shorthand unchanged | Agent | 2026-09-08 |
| 11 | MAF server integration | `A2AExecutor` / hosting conversion helpers | Recommend published Alpha hosting-a2a helpers plus app-owned SDK RequestHandler to preserve runtime policy; reviewer acceptance and wheel/HTTP bridge gate required | Agent, source review by coordinating session | 2026-09-08 |
| 12 | Direct Message nonblocking flag | Reject `returnImmediately=true` / spec no-effect behavior | Honor the specification: flag has no effect for direct Message or streaming; do not invent asynchronous Task support | Agent, protocol correction from coordinating session | 2026-09-08 |
| 13 | Review granularity and release gates | Large lifecycle PRs / bounded sub-PRs | Mandatory P5a/b, P6a/b/c, P9a/b split; enforce local-only basic mode; durable public gate at P10; safety bounds accompany first use | Agent architecture review, coordinating session | 2026-09-08 |
| 14 | Prioritize executable design feedback | Dependency cleanup first / simple server and SSE first | D0 -> P3 working implementation/sample and discussion -> P4 -> P5a/b -> P1/P2 -> durable stages. Preserve IDs; defer Durable-free install/import acceptance to P2, not the initial sample | Human, explicit user direction; sequence approval only, not full FRD sign-off | 2026-09-08 |
| 15 | Publish the first design review PR | Keep local drafts / open the bottom PR | Publish only the English FRD, architecture, plan, and index as a signed draft PR on main. Japanese copies stay local; P3 and native stack registration follow separately. This supersedes decision 1's publication restriction, not the implementation/sign-off gate | Human, relayed by coordinating session | 2026-09-08 |
| 16 | Authorize product implementation | Approve P3 only / approve the full staged design | Implement and publish only P3: the non-streaming A2A server, dependency extra, tests, docs, and runnable sample. This supersedes Decision 1's no-code/no-PR restriction for P3 only; it does not approve P4/P5 streaming or any distributed/durable design. | Human, explicit user authorization | 2026-09-08 |
| 17 | Close the P3 library gate | Substitute newer packages / use both MAF A2A integrations / use the proposed tuple | Add `[a2a]` with `agent-framework-hosting-a2a==1.0.0a260730`, `agent-framework-hosting==1.0.0a260730`, and `a2a-sdk[http-server]==1.1.2`; retain the existing MAF core/openai/foundry pins and mandatory Durable dependency. The narrow HTTP extra supplies Starlette/SSE-Starlette for the SDK's public `create_jsonrpc_routes` hook; do not add `[fastapi]` or `[all]`. | Human specified the conditional tuple; Agent verified published metadata/wheels and `pip check` against core 1.13 | 2026-09-08 |
| 18 | Fix the P3 protocol profile | A2A 0.3 compatibility / A2A 1.0 / mixed aliases | Support native A2A wire `1.0` JSON-RPC `SendMessage` only. A missing version means 0.3 and is rejected. Reject 0.3 aliases, streaming, Task get/list/cancel/subscribe, and supplied `taskId`; both `returnImmediately` values complete normally with one direct Message. | Human, explicit P3 protocol direction | 2026-09-08 |
| 19 | Scope simple-mode conversation context | Reject all context / use raw context as runner session / derive a safe scoped session | Accept optional `contextId`; generate one when absent, echo it on the response Message, and derive the runner session ID from a hash of auth scope + agent slug + context ID. Never use client IDs as filenames. Function/admin/anonymous are documented single-trust-domain scopes; Entra uses the validated tenant plus object/client identity. Generate a new response `messageId`; JSON-RPC `id` is independently echoed by the SDK envelope. | Agent, closing P3 architecture review blocker | 2026-09-08 |
| 20 | Define card publication and authentication | Infer Host / anonymous card / same auth and configured URL | Require `builtin_endpoints.a2a.url` as the trusted external JSON-RPC URL (absolute HTTPS, with HTTP allowed only for loopback development); never infer it from request Host/forwarding headers. Serve the per-agent card at `agents/{slug}/.well-known/agent-card.json` with the same resolved `http_auth` as JSON-RPC. Advertise JSON-RPC 1.0, one agent-derived A2A skill, text/plain input/output, streaming false, push notifications false, and the matching function-key or Entra security scheme. Materialize the immutable card lazily on its async GET route through the public MAF adapter. Clients must configure this card URL or SDK `card_path`; no domain-root discovery is implied. | Agent, closing P3 architecture review blocker | 2026-09-08 |
| 21 | Set P3 resource and error bounds | Configurable/unbounded / fixed conservative P3 limits | Per agent, allow at most 32 in-flight A2A executions. Bound raw JSON request bodies to 256 KiB, Messages to 16 Parts, each text Part to 32 KiB, aggregate input text to 64 KiB, and direct response text to 256 KiB. Reject file/data/structured Parts. Use SDK JSON-RPC errors with the request ID when parsing reached an envelope; use HTTP 400/413 only when no valid bounded envelope can be dispatched. Do not return reasoning, tool arguments, tool results, or arbitrary metadata. | Agent, closing P3 architecture review blocker | 2026-09-08 |
| 22 | Preserve app/runtime composition | Dedicated A2A agent invocation / existing runner and app policy | Register A2A only for an explicit object declaration; `builtin_endpoints: true` remains chat/UI/MCP only. Reuse the non-streaming runner with its tools, skills, policies, timeouts, sessions, and workflow integration. Simple A2A alone keeps a plain `FunctionApp`; workflows plus A2A share the existing single `DFApp` and unchanged workflow bindings. | Human direction, confirmed by architecture review | 2026-09-08 |

## 6. Test plan

Every implementing PR includes tests. P3 now covers the simple server items
below; later lifecycle and distributed cases remain assigned to their plan
slices.

- [x] Extend app, route, registration, auth, and config-scenario coverage
  and config-scenario fixtures for route opt-in, auth inheritance, validation,
  missing extras, and workflows/A2A coexistence.
- [x] Add clean-wheel environment coverage for the P3 A2A extra on Python 3.13/3.14.
  Later slices still cover base, monitor, durable A2A,
  workflows, and combined extras; assert installed closure, imported modules,
  successful startup, and actual generated binding metadata.
- [x] Add P3 protocol fixtures using the selected MAF/SDK tuple for card
  discovery, 1.0 negotiation, direct Message, errors, unsupported task
  methods/input parts, no-effect direct-Message `returnImmediately`, bounds, and
  JSON-RPC correlation. Task-first SSE and event ordering remain P5 work.
- [ ] Keep `test_runner_streaming.py`, `test_runner_usage.py`,
  `test_runner_harness.py`, and delegation tests as characterization gates for
  the private event refactor, including timeout/cancel/GeneratorExit cleanup.
- [ ] Add backend contract and multi-process tests for atomic publication,
  admission outbox recovery, duplicate IDs, ownership fencing, subscription races,
  two subscribers, cancellation races, restart, interrupted continuation, scoped
  listing, retention expiry, and slow readers.
- [x] Run existing lint/type/test gates for P3 and a deterministic real-host E2E
  covering Agent Card fetch and a direct Message over HTTP. Later slices retain
  their own host E2E coverage
  for binding annotations and streaming lifetime; mocks alone cannot prove these.

## 7. Docs impact

P3 updates the generated reference, authoring spec, architecture/module map,
README, docs landing/onboarding pages, and runnable samples index. Trigger and
workflow docs remain unchanged because P3 adds neither a trigger nor a new
workflow behavior. Later implementation PRs update their relevant surfaces.
Schema-changing PRs regenerate `docs/front-matter-reference.md` and run the
`update-schema-docs` skill. Extras migration updates sample and contributor
installation instructions together with package metadata.

## 8. Status & sign-off

- **Architecture review (phase 2):** Coordinating session reviewed all three
  documents; requested smaller lifecycle slices, deterministic release gates,
  early atomicity/bounds, context-less dedup, and explicit card discovery.
  Revisions incorporated; protocol/library compatibility,
  concrete storage transaction design, identity policy, and local SSE release
  restrictions are explicit review gates in the companion design. A separate P3
  review on 2026-09-08 confirmed the discover/translate/register/execute
  boundaries and required Decisions 17-22 before product code.
- **Human sign-off:** P3 only was explicitly authorized on 2026-09-08. P4/P5,
  distributed execution, and the durable design remain in review and require
  separate human authorization.
- **Implementation:** P3 is authorized to proceed. No later capability or
  production-readiness claim is approved by this status.
