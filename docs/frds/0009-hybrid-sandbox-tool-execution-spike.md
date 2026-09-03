---
frd: 0009
title: Hybrid ACA Sandbox tool execution spike
status: Finalized
author: larohra
created: 2026-09-02
updated: 2026-09-02
issues: []
pull_requests: []
branch: larohra/hybrid-sandbox-apim-spike
---

# FRD 0009 - Hybrid ACA Sandbox tool execution spike

## 1. Summary

This experimental spike keeps the Microsoft Agent Framework (MAF) model loop in
the Azure Functions worker while moving every local customer executable
capability into one fresh customer-owned ACA Sandbox for each top-level MAF
invocation. Azure OpenAI Responses API traffic and remote MCP traffic remain in
the worker and pass through isolated Azure API Management (APIM) APIs. The work
adds a private environment-gated seam, a strict sandbox tool protocol,
end-to-end telemetry, a deployable sample, and bounded live qualification; it
does not replace or extend the conversation-scoped ACA execution backend.

The evolving experiment record, resource inventory, measurements, failures,
and cleanup procedure live in
[`docs/decisions/0009-hybrid-sandbox-tool-execution-spike.md`](../decisions/0009-hybrid-sandbox-tool-execution-spike.md).

## 2. Motivation / problem

The existing `AcaSandboxExecutionBackend` runs the complete MAF harness in a
conversation-scoped sandbox. This spike answers a different question: can the
model loop, remote MCP clients, and Azure Functions trigger/session behavior
remain in the worker while untrusted customer Python tools and generic
shell/file/search capabilities run in an invocation-scoped ACA Sandbox?

The experiment must prove the security boundary, not merely remote process
execution. In hybrid mode the worker must never import `tools/*.py`; tool
discovery must occur after sandbox creation; every local invocation must be
intercepted; remote MCP must remain worker-side; and cleanup must hold across
normal, timeout, cancellation, disconnect, and worker-crash cases.

## 3. Goals / Non-goals

**Goals**

- Keep the top-level MAF model loop and chat/session handling in Azure Functions.
- Route Azure OpenAI Responses API calls through a streaming APIM AI Gateway.
- Route remote read-only MCP calls from Functions through a separate APIM API.
- Acquire exactly one fresh ACA Sandbox per top-level MAF invocation and share
  it across that invocation's local tool calls only.
- Package application content deterministically with `controller/package.py`,
  then discover/import customer tools only inside the sandbox.
- Build worker-side inert executable stubs from the exact sandbox manifest and
  route them through MAF `FunctionMiddleware` to a narrow
  `ToolExecutionBackend`.
- Provide `run_shell`, `read_file`, `write_file`, and `search_files` through the
  same strict local protocol as customer tools.
- Enforce bounded request/result envelopes, idempotent `call_id`, deadlines,
  output/size caps, serialized initial calls with measured queue wait, and
  explicit stdout/stderr/exit-code/timing results.
- Delete the sandbox after admissions stop and active calls boundedly drain;
  reap invocation-labeled orphans after worker failure.
- Produce one correlated trace waterfall plus low-cardinality counters and
  latency histograms, and a machine-readable benchmark report.
- Deploy and qualify the sample at concurrency 1 and 10; attempt 25 only after
  stable 10-way evidence and quota review.

**Non-goals**

- Production support, a stable public schema/API, or compatibility guarantees
  for the private experimental setting.
- Reusing `AcaSandboxExecutionBackend`, conversation-scoped persistence,
  Dynamic Sessions, Dynamic Workflows, subagents, executable skills, async run
  management, or the built-in `web_request` tool.
- Running the model loop, MCP client, connectors, or model credentials inside
  the sandbox.
- Public sandbox ingress, unrestricted package installation, semantic caching,
  policy retries, or prompt/completion body logging.
- General connector authorization when the selected connector requires an
  interactive grant; a harmless read-only remote MCP endpoint is sufficient.

## 4. Proposed design

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| discover | `execution/aca_composition.py`, new `experimental/hybrid_config.py` | When the private gate is enabled, skip worker import-based customer tool discovery and executable skill discovery. Continue loading remote MCP declarations in the worker. |
| translate | `registration/capabilities.py`, new `experimental/hybrid_protocol.py` | Represent no worker-local executable customer tools at startup. Strictly model the sandbox manifest and tool request/result envelopes. |
| register | `app.py`, `registration/_handlers.py`, `registration/endpoints.py` | Propagate the immutable experimental binding through existing closures without adding a public configuration model. Register a bounded orphan-reaper timer. |
| execute | `runner.py`, `client_manager.py`, new `experimental/hybrid_tools.py`, `experimental/hybrid_executor.py`, `experimental/hybrid_observability.py` | Eagerly acquire/package/start/discover one sandbox before `Agent` construction; build executable fail-closed stubs and middleware; keep MCP worker-side; close the lease in every terminal path. Configure the Azure OpenAI-compatible client for APIM. |
| transport | `transport/ports.py`, `transport/aca_sdk.py`, `controller/package.py` | Reuse the existing provider-neutral ACA handle and six file verbs plus process exec. Use file-plane journals and a persistent sandbox executor; expose no inbound port. |
| sample/IaC | `samples/hybrid-sandbox-apim-spike/`, `eng/scripts/` | Provision/deploy isolated nonproduction resources, configure RBAC/APIM policies, run scenarios and bounded benchmarks, emit JSON evidence, and document cleanup. |

### 4.1 Private experimental surface

The spike is enabled only when
`AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_HYBRID_TOOL_SANDBOX_GROUP_RESOURCE_ID`
contains one existing Sandbox Group ARM resource ID. Related private settings
configure the APIM model endpoint/audience or subscription-key fallback, the
remote MCP APIM endpoint, bounded timeouts/caps, and the orphan grace period.
No `schema.py` or front-matter key changes.

Hybrid startup rejects incompatible surfaces instead of partially enabling
them: configured `session_runtime`, Dynamic Sessions, Dynamic Workflows,
subagents, executable skills, async run management, or enabled `web_request`.
The sample explicitly disables those features as defense in depth.

### 4.2 Invocation lifecycle

`InvocationSandboxLease.acquire()` runs inside the top-level runner and before
MAF `Agent` construction:

1. Resolve the group with `AcaSandboxAdapter.open()`.
2. Create one sandbox from a fail-closed `SandboxCreateRequest` with
   non-sensitive spike, app, and operation labels, no ports, an auto-delete
   backstop, and default-Deny/Full-inspection egress.
3. Obtain the process-cached deterministic application archive from
   `controller/package.py` and deliver it through `SandboxFileTransport`.
4. Deliver the stdlib-only executor, start it through `exec`, and wait for its
   readiness and exact manifest.
5. Read a hybrid tool manifest, distinct from the existing content-binding
   manifest. Each entry carries name, description, and JSON parameter schema.
   Build worker-side `FunctionTool` stubs and a provenance registry from it,
   then construct the MAF `Agent` with `HybridToolMiddleware`.

The lease admits tool calls while the run is live. Closure stops admissions,
waits a bounded interval for active calls, deletes the sandbox, closes the
handle/provider, and records any cleanup failure. Nonstreaming execution wraps
agent build plus `agent.run` in the lease. Streaming enters the lease before
agent construction and releases it from the outermost generator
`finally`/`__aexit__`, outside `_drive_stream`, so disconnect/`aclose()` and
`GeneratorExit` reach the same cleanup path. Auto-delete and a timer-based
label reaper cover worker death. Its minimum orphan age is strictly greater
than the maximum configured top-level run timeout plus bounded drain/delete
allowance, so it cannot reclaim a live invocation by age alone.

The lease is acquired only by the two top-level run paths. Nested delegate,
workflow, and leaf paths never acquire one and are rejected by hybrid startup.
Acquisition is necessarily eager because model-visible schemas exist only
after sandbox discovery; even a tool-free turn pays create/upload/readiness
cost, which the benchmark records.

### 4.3 Tool protocol and executor

The controller and sandbox communicate only through fixed directories on the
ACA file data plane. The controller writes one canonical JSON request file per
`call_id`; the long-running executor atomically claims it and writes one
canonical JSON result. The executor imports tools once after unpacking the
deterministic archive and services multiple calls until shutdown.

The executor is launched as an explicitly detached child through the process
port; the one-shot `exec` call returns after spawning it. Executor readiness,
PID metadata, and the tool manifest are observed and verified over the file
plane, never inferred from the `exec` result.

Requests include protocol version, `call_id`, tool name, JSON arguments,
absolute deadline, and W3C `traceparent` plus a non-sensitive operation
correlation value. Results include success/error status, JSON-safe value,
stdout/stderr, exit code where applicable, and monotonic queue/execution/
serialization timing. Strict parsing rejects duplicate keys, extra fields,
non-finite numbers, path escape, oversized envelopes, expired calls, and
unknown tools. Existing result files make duplicate `call_id` retries
idempotent. Initial execution is serialized by a lease lock; queue wait remains
observable.

Generic tools use a sandbox workspace root. `run_shell` uses a bounded child
process with captured/capped stdout/stderr. File operations remain beneath the
workspace, cap bytes/results, never follow escaping paths, and use atomic
replacement for writes. `search_files` performs bounded glob/text search.

### 4.4 MAF middleware and provenance

The installed branch pins `agent-framework-core` and
`agent-framework-openai` 1.3.0. `FunctionMiddleware.process()` may set
`context.result` and omit `call_next`. Because declaration-only
`FunctionTool`s fail before middleware, sandbox manifest entries become
executable stubs whose handlers always fail closed if middleware propagation
is absent.

The runtime-owned provenance registry records each sandbox-local stub object.
Middleware sandbox-routes only an exact registered local stub object and calls
`call_next` for every other framework function, including MCP functions that
MAF materializes lazily after connecting. Hybrid assembly, rather than
middleware, enforces the unknown-local fail-closed rule: import-based worker
tool discovery is disabled and any non-MCP executable tool not created from
the sandbox manifest is rejected before `Agent` construction. Middleware is
passed to every top-level hybrid `Agent` constructor.

### 4.5 APIM, identity, and egress

The model client uses the Azure OpenAI-compatible Responses API with APIM as
its endpoint. A hybrid-only client factory, separate from the stock Azure
OpenAI builder, supplies an `azure_ad_token_provider` scoped to the custom APIM
audience or the APIM API's configured `api-key` header for the documented
fallback. Preferred authentication is Functions managed identity obtaining a
token for a custom APIM API audience; APIM validates that token, then uses its
own managed identity with `Cognitive Services OpenAI User` on the model
resource. If tenant policy prevents the required app registration, the
documented spike fallback is an APIM subscription key held only by Functions.

The model API policy validates the caller where applicable, sets a nonbinding
high `llm-token-limit`, emits token metrics, authenticates the backend with
managed identity, propagates correlation headers, and forwards with
`buffer-response="false"`. It has no semantic cache, body logging, or retry.
Remote MCP uses a separate APIM API/product/endpoint and read-only operation,
also without body buffering/logging.

Functions, APIM, and the Sandbox Group use separate managed identities. The
Sandbox Group workload identity receives one narrow positive test grant and no
grant on a second resource. Sandbox egress is default Deny with Full
inspection and explicit allow rules only for the positive identity test and
the approved outbound test host. Model and MCP hosts are not sandbox-allowed.
The hybrid lease creates its own minimal environment containing only executor
essentials and the sandbox marker. It does not call the full-run sandbox
profile/environment helper, forward model or telemetry settings, inject model
key placeholders, or add model/telemetry egress rules.

### 4.6 Observability and evidence

One trace links request/session, sandbox create, package upload, executor
startup/readiness, discovery, APIM model calls, tool queue/invoke/transfer, MCP,
and sandbox delete. W3C context crosses APIM and the file protocol.

Histograms cover total request, Functions cold start, model/APIM call,
streaming time-to-first-token, create-to-running, package upload, executor
readiness, discovery, tool queue/execution/transfer, and deletion. Counters
cover requests, model calls, tokens, tool calls, MCP calls, sandbox
creates/deletes/reaped/failures. Metric dimensions are bounded enums only;
session, operation, call, and sandbox IDs appear only in spans/logs.

The stdlib executor does not export spans. Worker-side controller spans project
its returned monotonic queue/execution/serialization timings into the active
trace. The benchmark helper reuses nearest-rank p50/p95/p99 conventions from
`tests/live/aca_deployed_load_support.py`, records throughput/errors/tokens and
cold-start decomposition, and combines client timings with APIM
`TotalTime`/`BackendTime`. A minimal direct-to-model control estimates gateway
overhead without introducing another runtime mode.

### 4.7 Compatibility

The gate is absent by default, preserving current discovery, registration, and
execution behavior. The setting name declares the surface experimental and is
intentionally not represented in public Pydantic configuration. The spike may
be removed or redesigned without deprecation.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Execution split | Entire run in ACA / tools in ACA / all in worker | Keep the MAF loop and remote MCP in Functions; run every local executable tool in ACA. | Human | 2026-09-02 |
| 2 | Sandbox scope | Tool call / top-level run / conversation | One eager fresh sandbox per top-level MAF invocation, shared only by that invocation's local calls. | Human | 2026-09-02 |
| 3 | Runtime seam | Reuse run backend / narrow tool backend / sample-only fork | Add `ToolExecutionBackend` plus `InvocationSandboxLease`; do not reuse `AcaSandboxExecutionBackend`. | Human | 2026-09-02 |
| 4 | Customer tool trust boundary | Worker discovery / sandbox discovery / static manifest | Never import `tools/*.py` in the worker; discover in ACA and construct inert worker stubs from its exact manifest. | Human | 2026-09-02 |
| 5 | Tool transport | Public HTTP / Unix socket / file journal plus exec | Use the existing no-ingress file/process transport with a persistent sandbox-side journal executor. | Agent | 2026-09-02 |
| 6 | MAF interception | Stub handlers / middleware / custom model loop | Use executable fail-closed stubs plus `FunctionMiddleware`; declaration-only tools bypass invocation in pinned MAF. | Human | 2026-09-02 |
| 7 | Tool provenance | Name allowlist / trusted stub registry / route all calls | Route exact runtime-owned stubs to ACA; reject worker-local executables during assembly and let lazily materialized MCP functions call next. | Agent | 2026-09-02 |
| 8 | Experimental API | Public schema / private environment gate / sample fork | Use a clearly named private environment gate; add no stable authoring surface. | Human | 2026-09-02 |
| 9 | Model path | Native Foundry / direct Azure OpenAI / APIM Responses | Use OpenAI-compatible Responses through APIM; direct model traffic exists only as a benchmark control. | Human | 2026-09-02 |
| 10 | Gateway authentication | End-to-end MI / subscription key / model key | Prefer separate Functions/APIM managed identities; allow a documented APIM subscription-key fallback only if tenant policy blocks custom audience setup. | Human | 2026-09-02 |
| 11 | Sandbox identity | No identity / controller identity / dedicated workload UAMI | Expose a dedicated Sandbox Group identity with one narrow positive grant and verify an ungranted-resource denial. | Human | 2026-09-02 |
| 12 | Egress and disabled surfaces | Broad allow / generated allowlist / deny all | Default Deny plus Full inspection and explicit test hosts; disable web request, Dynamic Sessions/Workflows, subagents, skills, persistence, and async run management. | Human | 2026-09-02 |
| 13 | Cleanup | Request finally only / platform TTL only / layered | Stop admissions, boundedly drain, delete in `finally`; add auto-delete and a label-based reaper for crash orphans. | Human | 2026-09-02 |
| 14 | Load bounds | Unbounded / 1 and 10 / 1, 10, and unconditional 25 | Run 1 and 10; attempt 25 only after stable evidence and quota confirmation. | Human | 2026-09-02 |
| 15 | Framework baseline | Requested 1.13.0 / branch pin 1.3.0 / dependency upgrade | Implement against the stacked branch's installed 1.3.0 contract; upgrading MAF is outside this spike unless separately approved. | Agent | 2026-09-02 |
| 16 | Executor launch | Blocking exec / detached exec / create-time full-run bootstrap | Spawn a detached executor through the existing process port and verify readiness/manifest only through the no-ingress file plane. | Agent | 2026-09-02 |
| 17 | Reaper safety | Age only / liveness journal / age beyond run bound | Set orphan age above the maximum run plus drain/delete allowance; invocation cleanup remains primary. | Agent | 2026-09-02 |
| 18 | Model deployment | New gpt-5.4-mini / existing gpt-4.1-mini / existing gpt-5 | Use existing gpt-4.1-mini through an isolated APIM API because new-account deployments require unavailable special entitlement. | Agent | 2026-09-02 |

## 6. Test plan

- [ ] Unit: strict config, manifest, request/result, duplicate-key, deadline,
  envelope cap, idempotency, and path-boundary validation.
- [ ] Unit: worker discovery never imports customer modules while hybrid mode
  is enabled; normal mode remains unchanged.
- [ ] Unit: middleware routes exact local provenance, calls next for remote MCP,
  rejects unknown local executable tools, and stubs fail without middleware.
- [ ] Unit: one lease per top-level nonstreaming/streaming invocation, shared
  sequential and queued parallel local calls, with cleanup on success, timeout,
  cancellation, disconnect, discovery/start failure, and delete failure.
- [ ] Unit: orphan selection/reaping uses bounded age and non-sensitive labels.
- [ ] Unit: low-cardinality metric attributes and complete timing projection.
- [ ] Local integration: sandbox executor custom Python plus shell/file/search,
  idempotency, caps, timeout, and queue serialization against a fake transport.
- [ ] Live: custom tool; sequential shared sandbox; parallel queued calls;
  shell/file/search; allowed/blocked egress; workload MI allowed/denied; MCP
  through APIM; streaming disconnect; timeout; cleanup; janitor.
- [ ] Live benchmark: Functions+sandbox cold, warm Functions+fresh sandbox,
  concurrency 1 and 10, optional 25, direct-model control, APIM timing, and
  machine-readable JSON report.
- [ ] Gate: targeted tests, `ruff`, `mypy`, then the CI-equivalent pytest gate
  without resource-heavy parallelism.

## 7. Docs impact

- [ ] `docs/architecture.md` - mark the private hybrid execution path, module
  boundaries, lifecycle, provenance, and telemetry.
- [ ] `docs/decisions/0009-hybrid-sandbox-tool-execution-spike.md` - durable
  experiment/resource/auth/failure/measurement/cleanup record.
- [ ] `docs/frds/README.md` - index FRD 0009.
- [ ] `samples/hybrid-sandbox-apim-spike/README.md` - deploy, operate,
  benchmark, inspect telemetry, and clean up the sample.
- [ ] `README.md` - no public quickstart change; the surface remains private.
- [ ] `docs/front-matter-spec.md`, `docs/triggers.md` - no change; no public
  schema or trigger surface changes.

## 8. Status & sign-off

- **Architecture review (phase 2):** Independent rubber-duck review completed
  2026-09-02. It identified lazy MCP provenance as blocking and tightened
  executor launch, sandbox environment, APIM client, reaper, manifest, and
  streaming lease contracts; all findings are incorporated above.
- **Human sign-off:** The user directed and authorized the complete hybrid
  boundary, security constraints, E2E matrix, and Azure work on 2026-09-02.
  After the independent review was resolved, this records that direction as
  sign-off for the experimental implementation.
