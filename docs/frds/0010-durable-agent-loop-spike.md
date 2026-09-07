---
frd: 0010
title: Durable agent loop spike
status: Finalized
author: larohra
created: 2026-09-04
updated: 2026-09-04
issues: []
pull_requests: []
branch: larohra/durable-agent-loop-design
---

# FRD 0010 - Durable agent loop spike

## 1. Summary

**Feasibility verdict:** a long-running reasoning-agent turn can be made
durable as an experimental **step-level runner**, provided a Durable
orchestrator owns the model/tool loop. `AgentMiddleware` may start that
orchestration and stop the local run, but middleware inside an already-running
`agent.run()` cannot add replayable activities to its parent orchestration.
`ChatMiddleware` and `FunctionMiddleware` remain useful boundary adapters and
replay journals; they do not make the live Python call stack durable.

The recommended spike uses a dedicated one-step MAF `Agent` whose chat client
has automatic function invocation disabled. Each `Agent.run()` therefore
performs exactly one APIM-backed inference and returns the LLM's final text or
ordered `function_call` contents without running tool code. Deterministic
orchestrator code schedules one Durable activity per remote tool call or one
single-call sandbox-wave activity per local tool call, appends matching
`function_result` messages, and invokes the next model step. Local filesystem
continuity crosses activities only through immutable workspace artifact refs,
not a live sandbox. Session coordination, turn journals, and final history
commit are durable. Raw prompt, argument, and result content is stored by
reference in customer-owned Blob storage rather than copied into Durable
history. The execution split and tool transport build on
[FRD 0009](0009-hybrid-sandbox-tool-execution-spike.md): model and privileged
remote MCP/connector calls remain worker-side through APIM, while local
customer executable tools run in customer-owned ACA Sandboxes.

The clarified target is a reasoning model that may alternate among many model
steps and tool batches, changing direction after every result. The current
MAF `1.3.*` pin is not sufficient for that target's stateless reasoning replay.
The spike therefore has an explicit prerequisite to qualify and adopt the
latest compatible MAF package set available on 2026-09-04:
`agent-framework-core==1.17.0`, `agent-framework-openai==1.14.2`, and
`agent-framework-foundry==1.12.0`. The provider packages require core
`>=1.17.0`. The latest Durable Extension packages remain preview
`agent-framework-durabletask==1.0.0b260730` and
`agent-framework-azurefunctions==1.0.0b260730`. If this exact upgrade cannot
preserve the runtime's existing behavior, an equivalent provider adapter is the
fallback; the reasoning-first spike does not silently stay on MAF 1.3.

This FRD prepares an architecture review only. It adds no product code, public
schema, deployment, Azure resource, or Azure call.

## 2. Motivation / problem

Today `runner.py` wraps one complete `agent.run()` for non-streaming execution
and one complete streaming `agent.run()` for SSE. MAF owns every inner model
and tool iteration. If the Functions worker fails after a tool side effect but
before the full turn returns, the platform can retry the turn only from its
outer boundary. The model can choose and execute already-completed work again.
The in-process session lock also does not serialize turns across worker
instances.

The existing Dynamic Workflows engine solves a different problem: an agent
authors an explicit coarse-grained DAG and Durable Functions executes its tool,
wait, and leaf-agent nodes in deterministic waves. A user should not need to
author a DAG to make an otherwise normal model/tool turn survive worker
restarts. The spike asks whether the runtime can retain the normal agent
experience while moving the inner loop boundary to Durable checkpoints.

### 2.1 Evidence and constraints

- [`runner.py`](../../src/azure_functions_agents/runner.py) currently awaits
  one full `agent.run()` at the non-streaming boundary and drives one full
  streaming `agent.run()` at the SSE boundary. The current lock is explicitly
  process-local.
- [`workflows/engine.py`](../../src/azure_functions_agents/workflows/engine.py)
  already demonstrates the required Durable rules: sort work deterministically,
  isolate I/O in activities, wait through yielded tasks, use external events
  for cancellation, and keep orchestrator code free of nondeterministic I/O.
- MAF's official
  [middleware documentation](https://learn.microsoft.com/en-us/agent-framework/concepts/agents/middleware/)
  says Python `ChatMiddleware` runs inside the function-invocation loop and
  executes once for **each model call**, including calls that send tool results
  back to the model. This makes it a useful policy, telemetry, or experimental
  replay-cache interception point. It does not turn middleware into a Durable
  scheduler or make the live Python call stack rehydratable.
- Historical `agent-framework-core==1.3.0` inspection found the three native
  middleware categories: `AgentMiddleware` around one complete `Agent.run()`,
  `ChatMiddleware` around each individual model service call, and
  `FunctionMiddleware` around each individual tool call. Current MAF main adds
  Agent Hooks, `MiddlewareBundle`, `MiddlewareFailure`, and
  `AgentLoopMiddleware`; all are present in the target core `1.17.0` baseline.
  They either map back onto the same three seams or wrap whole runs; none is a
  workflow/Durable scheduler. There is no workflow executor middleware.
- MAF 1.3's public
  `require_per_service_call_history_persistence=True` installs
  `PerServiceCallHistoryPersistingMiddleware`, a `ChatMiddleware` that loads
  and saves history around each model call. A fault-injection probe showed why
  this is not turn recovery by itself: after a tool side effect and worker
  cancellation, history contained the user message and assistant
  `function_call`, but not the completed `function_result`; rerunning the same
  turn executed the side effect again.
- MAF's public chat-client
  [`FunctionInvocationConfiguration`](https://learn.microsoft.com/en-us/python/api/agent-framework-core/agent_framework.functioninvocationconfiguration?view=agent-framework-python-latest)
  exposes `enabled`, defaulting to `True`. The upstream implementation in
  [`agent_framework/_tools.py`](https://github.com/microsoft/agent-framework/blob/main/python/packages/core/agent_framework/_tools.py)
  bypasses the function invocation loop and directly returns the underlying
  chat response when that setting is false. Upstream tests assert that
  `client.function_invocation_configuration["enabled"] = False` executes no
  function. This is the viable public seam for receiving model-produced
  function-call contents without running the tools.
- An exact 1.3 fake-client probe through a full `Agent.run()` established the
  historical behavior: this
  seam performs one model call, executes zero tool implementations, returns
  ordered `function_call` contents, and still traverses the Agent and Chat
  middleware/context preparation layers. The same contract must pass on the
  target core `1.17.0`/OpenAI `1.14.2` set before implementation.
- The branch currently pins MAF `1.3.*`; the target package set above is a
  deliberate breaking-risk upgrade, not a loose lower bound. Before product
  implementation, the contract and compatibility suites must prove the exact
  target clients preserve runner, APIM, MCP, middleware, history, telemetry,
  usage, streaming, and tool behavior.
- MAF's ordinary `Message` serialization excludes provider
  `raw_representation`, but explicit `Content` fields (`id`, `call_id`, name,
  arguments, result, annotations, protected reasoning data, and
  `additional_properties`) survive `to_dict()`/`from_dict()`. Exact 1.3 probes
  preserved encrypted-reasoning bytes but its OpenAI adapter did not safely
  replay them statelessly. MAF 1.13 adds automatic
  `reasoning.encrypted_content` capture/replay and background Responses with a
  serializable `OpenAIContinuationToken`; current main adds stricter
  replayability validation. The reasoning-first spike therefore requires a
  qualified MAF upgrade or equivalent adapter.
- Background Responses address failure *inside one long model step*: start the
  provider operation, persist its continuation token, park on a Durable timer,
  then poll the same operation. They complement—not replace—the orchestrator
  checkpoints between model and tool steps.
- The current MAF Durable Extension is turn-durable, not inner-loop durable.
  In
  [`AgentEntity.run()`](https://github.com/microsoft/agent-framework-durable-extension/blob/main/python/packages/durabletask/agent_framework_durabletask/_entities.py),
  the entity appends the request, invokes the complete `agent.run()` through
  `_invoke_agent`, and appends/persists the response only after the run returns.
  A crash mid-turn can therefore repeat completed tool work.
- Declaration-only tools expose a bounded upstream path: exact 1.3 probes using
  `FunctionTool(func=None, input_model=...)` made one `Agent.run()` perform one
  inference, return one or multiple ordered proposed calls with no execution,
  while a separate exact-1.3 continuation probe supplied the prior assistant
  call plus a role=`tool` result message to a fresh next run and received final
  text after one inference. The Durable Extension already persists returned
  function-call and function-result contents, but its public `DurableAIAgent`
  path currently normalizes all inbound `AgentRunInputs` to one text string and
  cannot accept a structured tool-result continuation. A guarded
  `continue_with_tool_results(...)` operation is the minimal upstream change
  needed to reuse that extension as the durable model-step engine.
- MAF Workflow
  [checkpoints](https://learn.microsoft.com/en-us/agent-framework/workflows/checkpoints)
  are created at the end of a superstep, after every executor in that
  superstep completes. They are valuable at executor/workflow boundaries, but
  do not checkpoint the private model/tool iterations inside an executor's
  `agent.run()`.
- [FRD 0009](0009-hybrid-sandbox-tool-execution-spike.md) and
  [`experimental/hybrid_tools.py`](../../src/azure_functions_agents/experimental/hybrid_tools.py)
  establish the APIM/worker/ACA trust split and a sandbox file journal keyed by
  `call_id`. The existing `InvocationSandboxLease` is in-memory,
  create-new/delete-only, and scoped to one whole MAF invocation; it cannot be
  reused directly by Durable activities.
- The generic transport already supports typed `SandboxSessionProvider.attach`
  and `resume`, with `AcaSandboxAdapter._attach_handle` rebuilding a handle
  from persisted sandbox/group/region identity and
  `ExpectedSandboxManifestBinding` enforcing a live manifest handshake.
  Credentials are constructed by each adapter and must never be persisted.
- The current sandbox journal treats an existing result filename as a duplicate
  `call_id`, but does not bind that ID to a canonical request hash. Its
  `asyncio.Lock` serializes only one worker process. Dedupe survives only while
  the same sandbox disk and journal survive.
- This branch resolves `azure-functions-durable==1.6.0`. Its
  `continue_as_new(input_)` API has no pending-event preservation option.
  Approval and callback payloads must be committed as durable facts before an
  external event is raised; the event is only a wake-up hint. Durable Entity
  registration and cross-host behavior are also a Gate-0 proof on this exact
  package/backend combination, not an assumed contract.

## 3. Goals / Non-goals

**Goals**

- Prove that a private, non-streaming HTTP chat starter can run one ordinary
  model/tool turn as a sequence of Durable model and tool activities.
- Support a reasoning model that can execute many model steps, issue sequential
  or parallel tool calls, observe their results, and change direction until it
  returns final text or reaches a budget.
- Durably park and resume one long-running model inference through the
  Responses background/continuation-token protocol rather than holding a
  Function activity open or reissuing the request after a client disconnect.
- Let the reasoning model request human clarification as a structured runtime
  tool call, park the orchestration without holding compute, and resume the same
  logical turn from the matching human answer.
- Checkpoint after every model response and every tool result so recovery
  replays orchestrator code without normally redispatching completed
  activities.
- Serialize turns for one session across Functions instances and atomically
  promote only a completed turn into committed history.
- Preserve the FRD 0009 trust split: APIM-backed model and privileged remote
  MCP/connectors in the worker; local executable tools in customer-owned ACA.
- Use typed, versioned, bounded activity and storage contracts with immutable
  policy, catalog, deployment, and content-integrity bindings.
- Prove serialized MAF `Message`/`Content` fidelity, including encrypted
  reasoning state, across every model checkpoint for one qualified reasoning
  model/provider/API surface.
- Make at-least-once activity semantics and external-side-effect ambiguity
  explicit. Prove supported idempotency paths; never claim exactly once.
- Keep sensitive content out of Durable history, status payloads, dashboard
  custom status, metrics, and default traces.
- Qualify recovery, cancellation, approval waits, deployment compatibility,
  sandbox cleanup, and bounded resource/cost behavior with fault injection.
- Define measurable go/no-go gates before any production design is considered.

**Non-goals**

- Finalizing this FRD or implementing before explicit human architecture
  sign-off.
- A public front-matter/config schema, compatibility promise, or production
  support level.
- Dynamically scheduling Durable activities from `ChatMiddleware`,
  `FunctionMiddleware`, or from inside an existing full `agent.run()`.
- Making arbitrary external tools exactly once. MCP has no universal
  idempotency contract.
- Guessing from natural-language assistant text that the model intended to
  pause. Human waits require a structured runtime-owned request.
- True replayable token streaming. Reliable Redis-backed streaming is a
  separate follow-up.
- Declared triggers, agent-as-MCP starters, subagents, or every connector in the
  first slice. Remote MCP remains in the end-to-end qualification.
- Replacing or merging Dynamic Workflows.
- Running model clients, model credentials, remote MCP clients, or connector
  credentials in the sandbox.
- Trusted ACA snapshot create/restore. The current runtime does not provide the
  required labeled, egress-bound, environment-bound restore contract.
- Unbounded autonomous loops. Bounded multi-hour execution is explicitly in
  scope under the step/tool/token/cost/elapsed-time limits below.

## 4. Proposed design

### 4.1 Feasibility verdict and alternatives

| Alternative | What it provides | Inner-loop verdict |
| --- | --- | --- |
| One activity wrapping full `agent.run()` | Minimal integration and turn-level retry | **Reject.** A completed activity is durable only after the whole turn returns; a mid-turn crash can repeat model calls and tool side effects. |
| MAF Durable Extension entity or MAF Workflow checkpoints | Useful session/orchestration concepts, entity state, executor/superstep recovery | **Reuse concepts, reject as the inner-loop solution.** `AgentEntity.run()` wraps the full run; Workflow checkpoints are superstep boundaries, not private inner model/tool boundaries. |
| Middleware replay cache | `ChatMiddleware` can observe every model call and `FunctionMiddleware` every tool call | **Fallback experiment only.** A retried full run starts at the beginning and correctness depends on the same middleware/tool sequence and deterministic lookup. Middleware cannot yield a Durable activity or rehydrate the MAF stack. |
| MAF Durable Extension plus declaration-only Agent and structured continuation | Reuses durable session entity, structured response codecs, entity-call tasks, Azure Functions registration, and orchestration replay | **Preferred upstream target, unavailable today.** Add structured message transport, pending-call validation, and `continue_with_tool_results`; the current text-only request contract cannot carry role=`tool`/call-ID results. |
| Custom step-level Durable orchestrator using the public MAF one-step seam | One checkpointable activity per model call and tool call, with explicit state and policy | **Recommended private spike/fallback.** Use a dedicated client with `function_invocation_configuration={"enabled": False}` and return model text/function-call contents without auto-invoking tools. |

The model activity should use a one-step `Agent.run()` rather than a raw client
call so the normal instruction, tool-schema, Agent middleware, and supported
context-provider preparation stay centralized. Its client instance is
dedicated to durable inference as a conservative isolation choice: the target
MAF baseline exposes construction helpers and mutable runtime inputs, but a
shared client's invocation mode must never race another concurrent run.
Per-run control is evaluated in the exact target contract tests; the dedicated
client remains the fail-closed default. Disabling invocation is the execution
boundary and applies even to lazily materialized MCP functions. A known-safe
`FunctionMiddleware` guard may intercept without calling
`call_next()`, record a violation, and terminate the loop if any invocation is
attempted. After `Agent.run()` returns, the activity checks that violation flag
and fails rather than accepting the synthetic result. The activity must prove
exactly one underlying model request occurs and no function implementation can
run. Direct `get_response()` remains a narrower fallback if Agent-level setup
cannot be made deterministic.

### 4.1.1 Who decomposes the work

This section uses **request turn** for one complete user-triggered agent run and
**model step** for one individual inference inside that turn. MAF documentation
sometimes calls both a "turn"; the distinction matters because only the latter
is the desired Durable checkpoint boundary.

There is no additional planner prompt and no separate decomposition model.
The model receives the normal conversation, agent instructions, and JSON tool
schemas. Through ordinary model tool-calling semantics it returns either:

- assistant content with no actionable `function_call`, which is the final
  answer for the turn;
- one or more ordered `function_call` contents, which identify the next tools
  and their arguments.

MAF's normal `FunctionInvocationLayer` currently performs a mechanical
classification: scan response contents for actionable calls, map names to
tools, validate arguments, execute a parallel batch, append one assistant
function-call message followed by a role=`tool` function-result message, and
call the model again. The durable runner preserves that protocol but replaces
execution/scheduling with the orchestrator. The model activity returns a
bounded `ModelDecisionEnvelopeV1`; the orchestrator performs deterministic
fan-out/fan-in, and the next model activity receives the prior assistant
function-call message plus ordered tool results. The phrase "answer or which
tools next?" describes the model's existing wire response; it is not a new
instruction added to the prompt.

Approval is not a third model-returned category under the selected
`enabled=False` seam. After the model activity returns function calls, the
orchestrator classifies each against frozen capability/approval policy and
parks before dispatch where required. MAF's `user_input_request=True` marking
exists on the declaration-only/`additional_tools` alternatives, not on the
primary disabled-invocation path.

Clarification is represented by one reserved model-visible tool schema:

```json
{
  "name": "request_human_input",
  "arguments": {
    "question": "Which production region should I investigate?",
    "choices": ["eastus2", "westus3"],
    "allow_free_text": false
  }
}
```

The tool has no customer implementation and never executes in Functions or
ACA. The orchestrator recognizes its reserved provenance, persists a
`HumanInputRequestV1`, publishes `Waiting`, and waits for the answer. The model
is instructed to emit `request_human_input` as the only actionable call in that
model step. If it mixes clarification with other calls, no call executes; the
runtime appends one deterministic protocol-error `function_result` for
**every** call ID in that assistant message, including every clarification
call, and allows one bounded repair model step before failing closed. Two or
more clarification calls are also a rejected mixed batch. The repair consumes
the normal model-step/token budget and may not itself emit another invalid
mixed batch.

The primary disabled-invocation seam bypasses MAF's automatic
`user_input_request`/approval classification, so the orchestrator deliberately
detects this raw `function_call` from `FrozenToolCatalogV1` rather than relying
on `AgentResponse.user_input_requests`. The declaration-only/`additional_tools`
variant remains a comparative MAF-assisted classification path.

After the human responds, the answer re-enters the normal function-calling
protocol as the matching tool result:

```text
assistant:
  function_call call-7: request_human_input(...)

tool:
  function_result call-7:
    {"status":"answered","answer":"westus3","response_id":"response-2"}
```

Using a role=`tool` result preserves the call ID and avoids leaving an orphaned
function call. The next one-step Agent sees that answer alongside all prior
reasoning and tool results and can continue, change direction, or ask another
clarifying question. Model-requested clarification and policy-required approval
share the Durable wait machinery but remain different contracts: clarification
supplies missing information; approval authorizes or rejects one exact
side-effecting call and arguments hash.

The exact encrypted-reasoning/function-call transcript is the preferred resume
path even after a long wait. Before consuming the accepted answer, a
`validate_resume_context_v1` activity checks the frozen
model/deployment/API binding, client-side serialization, content integrity, and
deployment availability. The next model step then attempts exact replay once.
If that call returns a recognized stale-reasoning or retired-deployment
condition, `rehydrate_context_v1` creates a new working context from the
immutable audit summary plus an explicit user-attributed clarification answer,
while the original call/result remain in the audit journal. Any deployment
migration is versioned and policy-approved, never silent. Failure of both exact
replay and controlled rehydration leaves the answer accepted but the run failed
with a specific resumability disposition; it does not repeat prior tool side
effects.

For a large request, the exposed loop can look like:

```text
user request
  -> model step 0 (long reasoning; may run as a background Response)
     -> tool calls A + B
  -> durable activities A + B
  -> model step 1 (sees both results and can change direction)
     -> tool call C
  -> durable activity C
  -> model step 2
     -> final answer
```

Each model step and tool activity is a Durable checkpoint. A model step may
contain extensive provider-internal reasoning, but the runtime does not
checkpoint private chain-of-thought token by token. It persists the complete
response envelope after completion. If one inference itself may exceed the
Function activity window, `start_model_step_v1` starts a background Response
and returns its continuation token; the orchestrator uses Durable timers and
`poll_model_step_v1` activities until the same provider operation completes.

### 4.1.2 Middleware and checkpoint alternatives on MAF 1.17

The semantic seams are validated on target core `1.17.0`. Historical 1.3 probes
remain regression evidence for one-step execution and failure windows, not the
shipping contract.

| Surface | Exact granularity | Durable-loop use |
| --- | --- | --- |
| `AgentMiddleware` | One complete `Agent.run()` | Admission/policy/telemetry or short-circuit to a Durable starter; cannot expose inner checkpoints. |
| `ChatMiddleware` | Every individual inference inside MAF's loop | Model telemetry, policy, and response memoization. It can replace a response, but `call_next()` is an in-process await and its Python stack cannot be rehydrated. |
| `FunctionMiddleware` | Every individual function call | Tool policy, routing, result memoization, or graceful loop termination. It cannot schedule a child activity into its caller's orchestration history. |
| Per-service-call history | Before/after each model inference | Useful audit/session feature; insufficient after a tool completes but before the next model response is saved. |
| Context/History providers | Normally before/after one complete run | Reuse only when their state is explicitly included in the durable model-step contract. |
| Agent Hooks / `MiddlewareBundle` | Mapped to agent/chat/function seams | Complete fail-closed control bundle on 1.17; no new durability boundary. |
| `AgentLoopMiddleware` | Re-invokes whole Agent runs | Useful harness behavior, but each nested run remains nondurable unless driven by the explicit orchestration. |
| MAF Workflow checkpoint | End of a workflow superstep/executor | Coarse durability only because an Agent executor awaits a whole `Agent.run()`. |

A paired chat/function middleware replay journal is technically viable as a
lower-change baseline. In an exact-1.3 fault probe, the first run durably cached
the model decision and tool result before cancellation; retrying the whole
`Agent.run()` replayed both cached entries, performed only the next uncached
model call, and did not repeat the side effect. This is deterministic
fast-forward over an externally persisted journal, not a Durable activity per
step. It remains worth benchmarking against the explicit loop, but it does not
satisfy first-class per-step orchestration, waits, visibility, or cancellation
on its own. It also retains the standard ambiguity window when an external
side effect commits before the FunctionMiddleware journal record; the probe
proved only the injected post-journal failure point, not general exactly-once
behavior.

Two other public 1.3 seams remain useful experiments:

- Making every tool `FunctionTool(func=None)` causes `Agent.run()` to return
  after one inference with proposed calls marked `user_input_request=True`.
  This is clean for an upstream Durable Extension agent, but the declaration
  object cannot later execute and MCP wrappers lose their dispatch closure.
- Listing real tools in
  `FunctionInvocationConfiguration["additional_tools"]` produced the same
  one-inference return behavior in a direct probe while preserving
  `FunctionTool.invoke()`. It is promising, but the configuration is
  client-instance state and lazily materialized MCP inventories complicate an
  exact all-tools invariant. Benchmark it against `enabled=False`; do not make
  it the primary safety boundary until the full inventory is proven.

### 4.1.3 Durable Extension reuse path

The Durable Extension is close to the desired model-step host, but cannot be
consumed unchanged:

- a declaration-only registered Agent makes each entity `Agent.run()` one
  inference, and the entity already serializes returned function-call and
  function-result contents;
- `DurableAIAgent.run()` nevertheless normalizes all inbound
  `AgentRunInputs` to text, joins multiple text messages, and rejects
  function-result-only messages;
- `RunRequest` carries one string/role rather than structured messages, and
  `DurableAgentStateRequest.from_run_request()` constructs one text content
  item; therefore an orchestrator cannot feed a role=`tool`
  `function_result(call_id, result)` back into the existing durable session;
- entity serialization orders operations but does not reserve a whole
  multi-operation turn, so an unrelated user message could interleave between
  decision and tool-result continuation without an active-turn fence; and
- remote MCP tools must be enumerated into a frozen per-run declaration
  manifest, while their actual dispatch remains in orchestrator-owned
  activities; declaration-only entities cannot retain the live MCP closure; and
- the current extension depends on a newer MAF range than this branch's
  `1.3.*` pin, so direct adoption also requires a separately reviewed
  dependency upgrade.

The preferred upstream API is a guarded
`continue_with_tool_results(session, turn_id, expected_step, results)` operation.
It accepts only results matching the entity's latest persisted pending call IDs,
rejects missing/unknown/duplicate/stale calls, preserves provider order, and
then runs the declaration-only Agent for one next inference. The same entity
stores `active_turn_id`, step, pending calls, and deterministic redelivery
receipts. This reuses the extension's session identity, state codecs, entity
task, orchestration replay, registration, and response transport while leaving
actual tools as orchestrator-scheduled activities.

Until that contract exists and its supported MAF range is qualified, the
private blueprint uses the same message/result envelopes and state invariants
so it can later converge rather than fork semantically.

### 4.1.4 AgentMiddleware as starter vs. middleware as scheduler

`AgentMiddleware` itself is only the outer run seam. It can mutate
`AgentContext.messages`, tools/options, `client_kwargs`,
`function_invocation_kwargs`, metadata, and result; it cannot receive each
inner model/tool callback by itself. On the latest MAF baseline, one application
can install a complete `MiddlewareBundle` (or Agent Hooks bundle) containing:

- an Agent middleware for start/admission/final output;
- a Chat middleware or custom `SupportsChatGetResponse` client for each model
  request/response; and
- a Function middleware for each framework-executed tool request/result.

Agent Hooks standardizes `pre_model_call`/`post_model_call` and
`pre_tool_call`/`post_tool_call`, but still implements them on those same Chat
and Function middleware seams. It is a control boundary, not a Durable
scheduler.

An `AgentMiddleware` can validly be the common **starter adapter**:

1. It receives the top-level request-turn `AgentContext`.
2. It writes the immutable start payload, idempotency key, and owner/session
   binding.
3. It starts or deduplicates `durable_agent_turn_orchestrator_v1`.
4. It sets a bounded accepted/run-handle `AgentResponse` and does **not** call
   `call_next()`.

The built-in HTTP endpoint remains the preferable first starter because it can
return a real `202`, status/result/cancel URLs, and Durable client binding
without translating them through an `AgentResponse`. The middleware starter is
useful later for sharing admission across invocation surfaces.

The same middleware cannot directly turn later Chat/Function middleware
callbacks into child activities in the already-started parent history.
Middleware runs inside a worker-local `Agent.run()` frame. It may start a child
orchestration, raise an external event, or await/poll external state, but the
parent activity remains one in-flight at-least-once activity and its Python
stack is lost on worker failure. External events also do not synchronously
return a child activity result into that frame.

A custom client/middleware bridge could implement this protocol:

```text
ChatMiddleware / proxy client
  -> write ModelStepRequested(run_id, step, messages_hash)
  -> signal parent orchestration
parent orchestration
  -> call model activity
  -> call publish_step_result_v1 activity
publish_step_result_v1
  -> write ModelStepCompleted(step, response_ref)
ChatMiddleware / proxy client
  -> poll/read response_ref
  -> set context.result

FunctionMiddleware
  -> write ToolStepRequested(run_id, step, call_id, request_hash)
  -> signal parent orchestration
parent orchestration
  -> call MCP/ACA activity
  -> call publish_step_result_v1 activity
publish_step_result_v1
  -> write ToolStepCompleted(call_id, result_ref)
FunctionMiddleware
  -> poll/read result_ref
  -> set context.result
```

This is technically workable as a durable RPC/replay adapter. The middleware
does not itself call `context.call_activity`; it has no
`DurableOrchestrationContext`. A normal Durable client can only start, signal,
query, or terminate an orchestration. The parent orchestrator receives the
request, yields the real activity, and publishes the reply.
All journal writes are performed by activities/entities; the orchestrator
remains deterministic and performs no direct I/O.

There are two valid reconstructions:

- **Orchestrator-owned loop (recommended):** no long-lived MAF stack crosses a
  checkpoint. The orchestrator schedules a fresh one-step Agent activity,
  records its response, schedules tool activities, records their results, and
  repeats.
- **Middleware replay runner (comparative fallback):** one outer activity
  restarts `Agent.run()` from the beginning after failure; Chat/Function
  middleware inject previously journaled results until it reaches the first
  missing step. This reconstructs logical progress but keeps an outer activity
  live during normal execution, has weaker per-step status/cancellation, and
  still needs idempotency for the external-effect-to-journal window.

A middleware-to-parent request/response bridge can be built only by writing a
request to an external journal/event, letting the orchestrator schedule work,
and having the live middleware poll/read the reply. That holds the outer drive
activity and Python frame for the whole request and therefore cannot exceed the
configured activity timeout. It is a short-turn diagnostic baseline, not a
candidate for the clarified multi-hour reasoning target. If middleware instead
terminates at every boundary and returns the envelope so the parent can yield,
the result is simply the orchestrator-owned explicit loop.

Thus orchestration state absolutely can reconstruct the **logical agent loop**.
It cannot reconstruct the suspended Python instruction pointer, locals, tasks,
network streams, or middleware `call_next()` continuation. The design avoids
needing that call-stack snapshot by making every resumable boundary explicit
data: messages, model decision, pending call IDs, tool results, provider
continuation token, budgets, and policy/catalog hashes.

### 4.2 Pipeline mapping and likely modules

The experimental gate is private and absent by default. No `schema.py`,
front-matter, `agents.config.yaml`, or discovery convention changes in the
spike.

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| discover | Existing `discovery/tools.py`, `discovery/mcp.py`, FRD 0009 hybrid discovery path | No new authoring discovery. Reuse the immutable tool/MCP inventory and sandbox-discovered local manifest; do not import customer tools in the worker. |
| translate | New `experimental/durable_loop_protocol.py`; existing `registration/capabilities.py`, `registration/catalog.py`, `experimental/hybrid_protocol.py` | Freeze versioned run, model-decision, tool, human-input, error, content-ref, policy, and sandbox-wave contracts. Bind agent/catalog/deployment/package hashes before admission. |
| register | `app.py`, `registration/endpoints.py`, `registration/_handlers.py`, likely new `experimental/durable_loop_registration.py` | Under one private env gate, register the non-streaming starter, status/cancel/human-input routes, session coordinator/entity, `durable_agent_turn_orchestrator_v1`, and versioned activities. Registration remains the only Azure-aware startup stage. |
| execute | New `experimental/durable_loop.py`, `experimental/durable_loop_activities.py`; existing `client_manager.py`, `workflows/engine.py` patterns, `experimental/hybrid_*`, `transport/*` | The orchestrator owns the alternating model/tool loop. Activities read/write content refs, compact the model's working context without changing the immutable audit journal, authorize against frozen policy, call APIM model/MCP routes or bounded ACA wave sandboxes, checkpoint results, and atomically commit final history. Existing `runner.py` remains the normal non-durable path. |

`experimental/durable_loop.py` contains deterministic orchestration and
classification only. `experimental/durable_loop_activities.py` owns I/O,
credentials, APIM clients, Blob access, policy revalidation, ACA transport, and
session-store operations. `experimental/durable_loop_protocol.py` contains no
Azure SDK types. Registration may later be folded into existing endpoint
helpers, but the spike should keep its private wiring visibly isolated.

### 4.3 Component architecture

```mermaid
flowchart LR
    C["Caller"] -->|"POST chat + request_id"| S["Private durable chat starter"]
    S -->|"admit/dedupe"| E["Session coordinator entity<br/>one active turn"]
    S -->|"202 run_id, session_id,<br/>status_url, cancel_url"| C
    E --> O["durable_agent_turn_orchestrator_v1"]
    C -->|"GET status / POST cancel"| X["Status + control endpoints"]
    C -->|"POST clarification answer"| U["Human-input endpoint"]
    X --> E
    X --> O
    U -->|"accept answer fact"| E
    U -->|"raise unique external event"| O

    O --> I["turn_init_v1 activity<br/>freeze local tool manifest"]
    O --> M["model_step_v1 activity"]
    O --> K["compact_context_v1 activity"]
    O --> T["remote_tool_call_v1 activities"]
    O --> W["local_tool_wave_v1 activity<br/>one call + one sandbox"]
    O --> A["append_turn_state_v1 activity"]
    O --> F["commit_turn_v1 activity"]

    I --> B["Customer-owned Blob<br/>service encryption + opaque refs"]
    M --> B["Customer-owned Blob<br/>encrypted content + opaque refs"]
    K --> B
    T --> B
    W --> B
    A --> B
    F --> B
    U --> B
    O -->|"bounded refs + metadata only"| D["Customer-owned Durable backend"]

    M --> P["APIM AI Gateway"]
    P --> AOAI["Model deployment"]
    T --> Q["APIM remote MCP/connector APIs"]
    Q --> R["Remote MCP/connectors"]

    I --> ACA["Customer-owned ACA Sandbox Group"]
    W --> ACA
    ACA --> H["Single-call sandbox wave<br/>customer executable tools"]

    I1["Functions controller identity"] --> M
    I1 --> T
    I1 --> B
    I2["APIM identity"] --> AOAI
    I2 --> R
    I3["Sandbox Group identity"] --> H
```

The Durable backend, content Blob account/container, and ACA Sandbox Group are
customer-owned. The Function App controller, APIM, and Sandbox Group use
separate least-privileged identities. The sandbox receives no model, APIM,
Durable, Blob, MCP, or connector credential.

### 4.4 Turn sequence and recovery

```mermaid
sequenceDiagram
    autonumber
    participant C as Caller
    participant H as HTTP starter
    participant U as Human-input endpoint
    participant S as Session coordinator entity
    participant O as durable_agent_turn_orchestrator_v1
    participant I as turn_init_v1 activity
    participant K as compact_context_v1 activity
    participant M as model_step_v1 activity
    participant T as tool_call_v1 activity
    participant J as state_ref_v1 activities
    participant B as Blob refs
    participant A as APIM model/MCP
    participant X as ACA sandbox wave

    C->>H: POST message + request_id
    H->>S: Admit immutable run identity/request hash
    S-->>H: New run or existing deduped run
    H->>O: Start deterministic run_id
    H-->>C: 202 + run_id/session_id/status_url/cancel_url

    O->>I: Discover exact local tool manifest
    I->>X: Create, package, discover
    I->>B: Persist canonical manifest/package/catalog hash
    I->>X: Delete initialization sandbox
    I-->>O: Frozen manifest ref + hash

    loop Bounded model steps
        opt Checkpointed size/token metadata crosses compaction threshold
            O->>K: Compact immutable audit range + current working context
            K->>B: Persist summary, retained groups, source hashes
            K-->>O: WorkingContextRefV1
        end
        O->>M: Schedule model step with working-context/state refs
        M->>B: Read working-context refs
        M->>A: One model request/background poll, immutable storage policy, auto tools disabled
        M->>B: Persist decision content, args, integrity hashes
        M-->>O: Bounded ModelDecisionEnvelopeV1
        Note over O: Deterministically classify final text or ordered tool calls; apply approval policy to calls
        alt request_human_input
            O->>J: Persist HumanInputRequestV1 and unique event name
            J->>B: Store question/schema by opaque ref
            J-->>O: Pending request receipt
            Note over O: Set status Waiting; race answer event, cancel event, and timer
            C->>U: POST answer + submission_id
            U->>J: CAS first valid HumanInputResponseV1
            J->>B: Store answer by opaque ref
            J-->>U: Accepted or idempotent receipt
            U->>O: Raise answer:<generation>:<request_id>
            O->>J: Read authoritative accepted answer
            J-->>O: HumanInputResponseV1
            O->>J: Append role=tool function_result for original call_id
            J-->>O: New CheckpointStateRefV1
        else Local executable call
            O->>T: One single-call sandbox-wave activity
            T->>B: Read previous immutable workspace ref
            T->>X: Create, restore workspace, rediscover and verify manifest
            T->>X: Execute exactly one local call
            T->>B: Persist result + next immutable workspace ref
            T->>X: Delete sandbox
            T-->>O: ToolResultV1 + WorkspaceRefV1
        else Remote MCP/connector
            par Stable ordered safe read-only calls
                O->>T: One activity per call
                T->>A: APIM remote operation
                T->>B: Persist result ref
                T-->>O: ToolResultV1
            end
        end
        O->>J: Append model/tool refs to in-progress turn state
        J->>B: Compare-and-swap immutable state ref
        J-->>O: CheckpointStateRefV1
    end

    O->>S: Atomically commit final response/history ref; clear active turn
    S-->>O: Committed generation

    Note over O,T: On worker restart, orchestrator code replays from the beginning.
    Note over O,T: Completed activity results come from Durable history; semantic work resumes after the last checkpoint.
```

The orchestrator never reads Blob, APIM, ACA, random values, environment
settings, or wall-clock time directly. It uses only its immutable input,
Durable context time/IDs, prior yielded results, stable sorting, Durable timers,
and external events.

### 4.5 Typed/versioned contracts and storage

All contracts are JSON-safe, `extra="forbid"`, size-bounded, and carry an
explicit `schema_version`. Unknown versions fail closed.

| Contract | Required contents |
| --- | --- |
| `DurableRunIdentityV1` | Immutable `run_id`, `session_id`, hashed idempotency `request_id`, request hash, agent slug, agent/catalog/deployment/tool-package/policy hashes, orchestration version, created time, absolute deadlines, budget policy. |
| `ContentRefV1` | Opaque storage reference, SHA-256 integrity hash, byte length, media/content type, encryption/key version metadata, retention class. Never a SAS/token/credential. |
| `MAFMessageBundleV1` | Opaque ref to ordered `Message.to_dict()` envelopes containing text, function calls/results, and protected/encrypted reasoning fields required by the qualified Responses path; MAF/API/model/version binding and canonical bundle hash. A supplemental provider-item ref is allowed only when a conformance test proves the normalized envelope loses required semantics. |
| `WorkingContextRefV1` | Exact bundle sent to the next model step: compaction generation, summary ref, retained atomic message-group refs/boundaries, source audit range/hash, estimated and actual token/byte counts, parent working-context ref, and model/tokenizer/version binding. |
| `ModelOperationV1` | Deterministic local model-step key; provider response ID/serialized continuation token; exact APIM/backend-resource/deployment/API binding; accepted time; provider/run operation deadline; last poll/result-retrieval time; `queued`/`in_progress`/terminal state; whether polling/retrieval consumes or refreshes availability; poll count; and eventual decision-bundle ref. The local key is correlation, not provider idempotency. |
| `ToolManifestV1` | Canonical sandbox-discovered tool names, descriptions, parameter schemas, provenance, package/catalog hash, and manifest hash frozen by `turn_init_v1`. |
| `FrozenToolCatalogV1` | Collision-free model-visible schemas and provenance for sandbox-local tools, remote MCP/connectors, and runtime control tools such as `request_human_input`; immutable catalog/policy hash and routing class per name. |
| `WorkspaceRefV1` | Immutable verified archive of the bounded local workspace after one call, with parent ref, package/manifest binding, integrity hash, byte/file caps, and no credential material. |
| `ModelDecisionEnvelopeV1` | Run/step identity, deterministic model call key, provider response/call ID when returned, model/deployment hash, MAF message-bundle ref, ordered tool-call summaries, usage/cost metadata, finish reason, retry attempt metadata. |
| `ToolRequestV1` | Run/step/call ordinal, provider call ID, runtime deterministic call key, tool name/provenance, canonical request hash, argument ref, policy/catalog/package hashes, deadline, approval ref if required, optional sandbox-wave lease ref. |
| `ToolResultV1` | Matching call key and request hash, status, bounded result/stdout/stderr/artifact refs, provider operation ID where safe, timing, dedupe/reconciliation disposition. |
| `HumanInputRequestV1` | Immutable generation/run/turn/step/request/call IDs, reserved request kind (`clarification`), question ref, bounded choices/response schema, actor policy, unique server-generated event name/nonce, issued/advisory expiry times, record version, and `pending`/`answered`/`timed_out`/`cancelled` terminal state. |
| `HumanInputResponseV1` | Matching request/call/generation IDs, one-time submission ID and body hash, authenticated actor/tenant, answer ref, accepted time, schema-validation result, outbox delivery state, and `accepted`/`consumed`/`orphaned` disposition. |
| `ErrorEnvelopeV1` | Stable bounded error code/classification, retryability, ambiguity classification, failed phase/step/call, sanitized detail ref where allowed; no raw exception content in status/history. |
| `CheckpointStateRefV1` | External in-progress turn-state ref and integrity hash, immutable audit-journal head, current `WorkingContextRefV1`, compaction generation, completed step/call set, checkpointed byte/token usage, next deterministic step, committed session generation, continue-as-new generation. |
| `SandboxWaveLeaseV1` | Opaque lease ID plus persisted sandbox/group/region binding, manifest/package/policy hashes, fencing generation, expiry, selected artifact refs, lifecycle state. Stored outside metrics and never contains credentials. |

`run_id` is server-minted and immutable. A runtime call key is derived
deterministically from the version, run ID, committed model-step index,
call ordinal, committed model-decision hash, and normalized tool identity.
Provider call IDs are recorded separately because they are correlation values,
not stable idempotency keys. Each call key is bound to a canonical request hash
over tool identity, arguments, policy/catalog/package hashes, and relevant
workspace or lease generation. Reuse of a call key with a different request
hash is corruption and fails closed; it must never return the previous result.

Raw user prompts, assembled MAF message bundles, any qualified supplemental
provider items, tool arguments/results, stdout/stderr, approvals, and selected
workspace artifacts live in a dedicated customer-owned Blob account protected
by service encryption at rest, RBAC, network controls, and optional
account-level CMK policy. Durable history, entity state, dashboard custom
status, and activity envelopes contain only bounded refs, integrity hashes,
byte counts, status, and low-cardinality metadata. The commit-once substrate
uses immutable block blobs with `If-None-Match` and ETag compare-and-swap; the
existing append-blob history provider is not used for turn transactions.
Content refs have caps, retention categories, purge support, and no embedded
authorization tokens. Integrity is checked on every read before use.

### 4.6 Session management and final commit

One session coordinator entity serializes turn admission across Functions
instances. It holds only:

- the committed history `ContentRefV1` and generation;
- the last commit receipt (`run_id`, request hash, final-response ref, and
  committed generation) needed to recognize an acknowledged-or-lost retry;
- immutable session/owner/agent policy bindings;
- active `run_id`, request hash, status, deadline, and in-progress state ref;
- a bounded idempotency index from hashed request ID to run/result reference;
- a bounded cancellation/approval/clarification mailbox, first-answer receipts,
  and retention metadata.

The detailed in-progress turn journal is separate from committed history.
Model and tool activities append immutable refs to that journal. Failed,
timed-out, or cancelled turns may retain a short-lived audit/error journal but
never mutate committed history.

Admission is idempotent. Repeating the same session/request ID and request hash
returns the existing run and URLs; the same ID with a different hash fails with
`409 idempotency_conflict`. A different request while a turn is active is
rejected with `409 session_busy` in the first spike rather than silently
queued. The entity/coordinator contract and deterministic orchestration
instance ID must reconcile the ambiguity between admitting and receiving the
start acknowledgement.

`commit_turn_v1` is an idempotent compare-and-set keyed by
`(run_id, expected_generation, request_hash, final_response_ref)`. It validates
the active run, expected committed generation, terminal final response ref,
policy/catalog hashes, and complete call set. The entity then atomically
promotes the completed turn's history ref, records the commit receipt,
increments the committed generation, terminalizes the run, and clears the
active slot. If the commit applied but its acknowledgement was lost, a retry
with the identical key returns the recorded committed generation as success.
Only a different run, request hash, response ref, failed/cancelled state, or
fence at that generation fails closed. The committed response is authoritative
if a provider or activity response is later observed for an uncommitted or
ambiguous attempt.

### 4.7 Activities and tool routing

`turn_init_v1`:

- creates a disposable customer-owned ACA Sandbox without importing customer
  tool modules in the Functions worker;
- delivers the exact package, discovers the local tool schemas in ACA, and
  persists a canonical `ToolManifestV1` plus package/catalog hash;
- deletes the initialization sandbox;
- builds `FrozenToolCatalogV1`, adding the runtime-owned
  `request_human_input` descriptor after failing on any customer/MCP name
  collision; and
- freezes the manifest and combined catalog for the run. Every later local activity rediscovers
  its sandbox manifest and compares the exact hash before execution. Reserved
  control-tool provenance can never route to ACA or MCP.

`compact_context_v1`:

- reads the immutable audit-journal range and current working context selected
  by its input refs, never by querying mutable "latest" state;
- preserves system instructions, the current user objective, pending
  approvals/clarifications, and complete atomic
  reasoning/function-call/function-result groups;
- produces a budgeted summary plus retained recent groups, source range hashes,
  token/byte counts, and a new `WorkingContextRefV1`;
- never modifies or replaces the full audit journal; and
- is itself a Durable activity, so replay reuses the exact summary rather than
  asking the model to summarize again.

`validate_resume_context_v1` and `rehydrate_context_v1`:

- validate long-park content integrity, serialization, frozen deployment/API
  availability, and model/tokenizer compatibility before the next step;
- attempt exact encrypted-reasoning continuation first; and
- on a recognized replay incompatibility, create a new versioned working
  context from the immutable audit/summary plus the human answer, without
  rerunning completed tools or rewriting audit history.

`model_step_v1`:

- rebuilds the APIM model client and credential per activity;
- reads and verifies the exact `WorkingContextRefV1` selected by the
  orchestrator; the complete audit transcript is consumed only by compaction
  and final commit activities;
- authorizes the current immutable agent policy/catalog/deployment hashes;
- uses the same FRD 0009 APIM AI Gateway route with the immutable run storage
  policy (`store=False` by default; `store=True` only after explicit
  retention/privacy approval), no APIM retry, no semantic cache, and no
  prompt/completion body logging;
- constructs a fresh one-step MAF `Agent` with the normal instructions and
  known-safe context preparation and a dedicated chat client configured with
  `function_invocation_configuration={"enabled": False}`;
- allows only an explicit deterministic context-provider allowlist, attaches no
  `AgentSession`/HistoryProvider unless its complete state is part of the
  versioned step contract, and supplies the explicit durable MAF message bundle
  instead, preventing auto-injected/double history persistence;
- uses the qualified latest MAF package set (core `1.17.0`, OpenAI `1.14.2`,
  Foundry `1.12.0`) or a validated equivalent adapter, preserving encrypted
  reasoning required for the next stateless tool-loop step;
- performs one model request, persists the complete normalized MAF response
  messages and raw call arguments by ref, and returns a bounded decision
  envelope;
- may internally use a streaming provider call to measure TTFT, but publishes
  no client token stream and checkpoints only a complete response.

For providers/models using background Responses, the model boundary is split:

- `start_model_step_v1` sends the request with background execution enabled and
  returns either a terminal decision or a persisted continuation token;
- the orchestrator parks on a Durable timer without holding compute;
- `poll_model_step_v1` supplies that token, reads the same provider operation,
  and returns a refreshed token or the terminal decision; and
- cancellation requests provider cancellation when available, while run state
  remains authoritative if provider cancellation races completion.

APIM must expose the complete Responses start/get/cancel surface, preserve
continuation identifiers, avoid body logging and retry, and keep model activity
polls within the same W3C operation. Start, poll, and cancel must remain pinned
to the exact backend resource/deployment that accepted the response; APIM must
not load-balance or fail over a poll to another backend.

Provider retention and privacy are coupled to background execution. With
`store=False`, the provider may retain a background operation only for a short
temporary polling window. Gate 0 measures three separate properties on the
selected Azure deployment:

1. maximum accepted-to-terminal background operation duration;
2. terminal-to-expiry result retrieval window; and
3. whether polling/retrieval is repeatable, one-shot, or refreshes expiry.

The model-step deadline is derived from (1) and the run budget; polling cadence
and recovery margin are derived from (2)-(3). If the provider's stateless
operation duration cannot exceed the Function activity window, `store=False`
does not solve long inference. The customer must then explicitly approve the
provider's `store=True` retention contract or the long-background path is
no-go. A one-shot terminal read is copied to the customer-owned result store
before any response is exposed; loss before that copy is an explicit
unrecoverable/ambiguous model outcome. The private runtime never silently
changes storage mode.

There is one Durable activity instance per model call and per remote tool call.
For local execution, one `local_tool_wave_v1` activity is a wave of exactly one
logical tool call and owns restore, sandbox create, manifest verification,
execution, workspace export, and deletion. The one-call cardinality preserves
the requested checkpoint after each local result. Multi-call waves are a later
latency optimization because their calls cannot be independently retried.

Every activity reauthorizes the request against the immutable run
agent/catalog/policy/deployment hashes and the activity's current registered
tool contract. Removed or changed tools, package drift, policy mismatch,
unexpected provenance, and authorization loss fail closed. The model cannot
grant itself capabilities through prompt output.

Tool routing is:

- **Local executable tool:** a customer-owned single-call ACA wave. The
  activity restores the prior immutable workspace ref, creates and verifies a
  fresh sandbox against the frozen manifest, binds `call_key` to
  `request_hash`, executes one call, exports the next workspace ref, and
  deletes the sandbox before returning.
- **Remote MCP or connector:** a privileged worker-side activity through its
  approved APIM API where applicable. Read-only calls may fan out; writes
  require provider idempotency/outbox policy.
- **Human clarification:** the reserved `request_human_input` call creates no
  tool activity. It persists a pending request, exposes the question through an
  authorized endpoint, waits on a unique external-event name plus
  cancel/timeout timers, and appends the accepted answer as the matching
  `function_result`.
- **Approval-gated tool:** no activity runs until an authenticated external
  approval event matches the run, call key, request hash, policy version,
  actor, expiry, and nonce.

"Read-only" and "safe to parallelize" are explicit operator-authored capability
metadata, not properties the runtime infers from a tool name or description.
Remote calls are serialized by default unless the frozen policy explicitly
allows parallel execution and the activity revalidates that classification.

In the base one-call-sandbox design, no ACA sandbox remains active while the
orchestration waits for a human. If the separately gated session-to-sandbox
optimization is enabled, entering `Waiting` first drains local calls, commits a
workspace checkpoint, releases the active capacity lease, and applies
auto-suspend while retaining the fenced session mapping. The next local call
resumes and revalidates that sandbox or recreates it from the checkpoint.

### 4.8 ACA lifecycle alternatives and recommendation

| Scope | Benefits | Risks | Spike choice |
| --- | --- | --- | --- |
| One sandbox for the full Durable turn | Easiest filesystem continuity; journal survives all steps | Retains capacity/code/disk across model latency and approval waits; depends on long-lived executor readiness and fencing | Not the default. Optional short-turn optimization only after dedicated attach/resume/executor-readiness and durable-fencing qualification. |
| One sandbox per local tool call | Checkpoint granularity matches the tool-call boundary; bounds capacity, retention, credential lifetime, and cleanup | Pays create/restore/export/delete cost for every call; requires immutable workspace artifacts | **Default: one fresh single-call sandbox wave per local call.** |
| Multi-call live-sandbox wave | Lower latency and natural filesystem continuity | The wave, not each call, is the safe retry unit; a crash may redo completed sibling calls | Defer. It can be added only as one Durable wave activity with weaker checkpoint granularity, or with a separately proven durable fence. |
| Recreate from ACA snapshot | Could reduce package/start cost | No proven trusted restore path preserving required labels, egress, environment, manifest, and package bindings | Reject for v1. Use external selected-artifact checkpointing instead. |

The first slice sets local wave cardinality to one and serializes local calls
in stable order. Each `local_tool_wave_v1` activity rebuilds managed identity
credentials, restores the prior verified `WorkspaceRefV1`, creates one fresh
sandbox, rediscovers and compares the exact frozen manifest/package hash,
executes one request-hash-bound call, exports the next immutable workspace
archive, and requests deletion. Tokens and credential objects are never
persisted. Auto-delete and an app/run-labeled reaper are independent backstops.
The next local call restores the prior activity's workspace ref; arbitrary
process state is never carried forward.

The current `InvocationSandboxLease` cannot implement this lifecycle directly:
it always creates a new in-memory lease, owns a process-local queue lock, and
deletes/closes at invocation end. Its protocol, packaging, APIM split, executor,
and transport primitives can be factored or reused, but Durable wave ownership
requires a new persisted lease record, durable fence, request-hash binding, and
attach/resume path.

Attach/resume across workers is technically feasible in the generic transport,
but it is not required by the base single-call design. It remains an
end-to-end gate only for a later retained-sandbox optimization. If
cross-worker attach, strict manifest revalidation, executor readiness, or
fencing fails, that optimization stays disabled. The base fallback remains a
fresh one-call sandbox wave plus explicit external artifact transfer, not ACA
snapshot recreation.

### 4.9 Failure semantics and the exactly-once truth

- Durable replay runs orchestrator generator code from the beginning.
  Completed activity results are supplied from Durable history rather than
  normally redispatched, so semantic work resumes from the last yielded
  checkpoint.
- Durable activities are **at least once**. A worker can complete an external
  side effect and fail before the activity completion acknowledgement reaches
  the Durable backend. Retrying that activity creates an ambiguity window.
- Deterministic call keys plus request hashes and the sandbox journal dedupe
  local calls only while that same journal/disk survives. They do not prove an
  external effect exactly once. The existing result-file journal must be
  hardened to reject the same call ID with changed arguments.
- The existing in-memory queue lock does not coordinate concurrent workers.
  Initial local mutations are orchestrator-serialized. A local activity may
  commit the next workspace ref only when its expected parent ref/generation
  still matches through Blob/entity compare-and-swap.
- There remains a local ambiguity window if a tool makes a side effect after
  execution starts but before the result is durably renamed/checkpointed.
  Unsafe non-idempotent local tools require explicit policy classification,
  an application idempotency key/outbox, or rejection.
- Remote MCP/connectors require end-to-end idempotency keys, a transactional
  outbox with provider reconciliation, or a provider lookup keyed by the
  runtime call key. MCP itself has no universal idempotency guarantee.
- Production must fail closed for tools whose side effects cannot be
  classified/reconciled, or require explicit operator acceptance. It must
  never advertise exactly once.
- A model request may repeat if the provider produced a response but the model
  activity lost it before Durable acknowledgement. This can add cost and
  nondeterminism, but model calls do not directly execute local tools because
  auto-invocation is disabled. The checkpointed, committed response is
  authoritative.
- A background start has a narrower but unavoidable ambiguity: if the provider
  accepts the operation and the worker dies before its response ID/continuation
  token is durably acknowledged, the provider offers no lookup by the runtime's
  deterministic local step key. Retrying may start a second billed inference
  while the first becomes orphaned. Record this separately, attempt
  best-effort cancellation/reaping when an orphan ID is later discovered, and
  never describe the local step key as provider idempotency.
- Cancellation is cooperative. It stops new work and schedules cleanup, but
  cannot undo a side effect already in progress. The reaper covers worker loss
  before cleanup scheduling/acknowledgement.

Retry policies are provenance-specific. Model calls and safe reads use bounded
exponential retries within the run budget. A write activity is retried only
when its idempotency/reconciliation contract permits it; otherwise ambiguity
is surfaced for operator decision. Broad catch-and-continue behavior is
forbidden.

### 4.10 Deterministic control flow, waits, cancellation, and versioning

- Model steps and tool calls receive monotonic integer indexes. Model-emitted
  calls retain response order; stable ordering uses `(step_index, call_ordinal,
  call_key)`, never unordered collection iteration.
- Parallel tool calls use stable fan-out and aligned fan-in. Initial local
  sandbox mutations are serialized. Independently authorized remote calls
  explicitly classified as read-only and parallel-safe may run in parallel up
  to the immutable parallelism cap; all others remain serialized.
- Approval, clarification, and callback endpoints first persist authenticated,
  nonce-bound payloads as compare-and-swap durable facts. They then raise an
  external event only as a wake-up hint. Every orchestration generation rereads
  the durable mailbox before waiting, so a dropped event does not drop accepted
  input.
- Durable external events are one-way and at-least-once. An event raised after
  the orchestration instance exists but before it starts waiting is buffered
  for the matching event name. If the instance does not exist, the event is
  routable; the pinned Python Durable client surfaces `404` for unknown
  instances and `410` for completed/failed instances. Every clarification
  therefore uses a never-reused, server-generated
  `answer:<generation>:<request_id>` event name and a separate authoritative
  answer ledger.
- A parked wait creates exactly one answer event task, one request-scoped
  cancel event task, and one final deadline timer, then races those three with
  `task_any`. The answer outbox retries `raise_event` until delivery succeeds or
  receives terminal `404`/`410`; it does not require periodic orchestrator
  wake-ups. If the final timer wins, one mailbox-read activity reconciles any
  accepted answer before attempting timeout closure.
- The answer endpoint atomically accepts the first valid response. Repeating
  the same submission ID and body is idempotent; a conflicting second answer
  returns `409`; consumed requests return `410`; unknown or unauthorized
  requests return `404`/`403` without raising an event. The endpoint does not
  decide expiry from its wall clock: it accepts or rejects solely through the
  authoritative request-record CAS. The accepted-fact to `raise_event` gap is
  closed by an outbox delivery state or a session-entity inbox that retries the
  wake-up.
- Timeout uses a single-writer `close_human_request_v1` activity that CASes the
  same `pending` record to `timed_out`. Whichever of answer acceptance or close
  wins the CAS is authoritative; `expires_at` is advisory display metadata.
  If `raise_event` later returns `404`/`410`, the outbox stops retrying, marks
  the accepted response `orphaned`, and the endpoint exposes a typed
  `run_terminal` disposition rather than pretending the answer was consumed.
- If timeout, cancellation, and answer race, the authoritative mailbox terminal
  state decides the outcome. A timeout may append a typed
  `{"status":"timed_out"}` result and allow one bounded model step to ask again
  or conclude; it never silently fabricates an answer.
- When answer or cancellation wins, the orchestrator cancels the pending timer.
  External-event tasks have no cancellation requirement; late losing events
  cannot affect another request because generation-scoped event names are never
  reused.
- Each activity has a deadline, bounded attempts, retry classification, and
  remaining run budget. The orchestrator uses Durable context time only.
- The compaction decision is deterministic: it uses only checkpointed
  message-byte counts, provider-reported usage, the frozen tokenizer/model
  contract, compaction generation, and immutable thresholds. When the threshold
  is crossed, the orchestrator schedules `compact_context_v1`; its summary and
  counts return as one recorded activity result and become the sole
  `WorkingContextRefV1` for the next model step.
- Cancellation marks intent in the session coordinator, raises the external
  event, stops scheduling new model/tool work, waits boundedly for in-flight
  work, and schedules wave cleanup. Auto-delete/reaper remain mandatory because
  termination can interrupt cleanup itself.
- On the pinned Durable Functions 1.6.0 API, `continue_as_new` has no
  `save_events` option. It therefore occurs only at a quiescent checkpoint with
  no pending activity/timer task, after mailbox facts have been committed. The
  next generation rereads those facts and carries only
  `CheckpointStateRefV1`, immutable identity/policy hashes, counters, deadlines,
  and budgets; the full turn state remains in external customer-owned storage.
- The orchestrator never calls `continue_as_new` while parked on an unresolved
  external-event task or while a clarification endpoint is accepting that
  generation's event name. It rotates immediately before opening the request
  when needed, or after the event/fact has resolved, the request is terminal,
  and the pending timer is cancelled.
- Orchestrator/activity names, input/output contracts, and schema versions are
  versioned (`*_v1`). In-flight runs stay on compatible code. Deployment
  manifests retain required old handlers until no run references them.
- An immutable deployment/catalog/tool-package hash is checked in every
  activity. Removed or changed tools fail closed rather than dispatching to a
  new implementation. Nondeterministic changes to in-flight orchestrator code
  are prohibited; incompatible behavior ships under a new orchestrator name.

### 4.11 Security and privacy

- Customer ownership is required for the Durable backend/history, content Blob
  storage, and ACA Sandbox Group. Encryption at rest, RBAC, private endpoints,
  network isolation, backup policy, retention, legal hold/purge behavior, and
  customer-managed-key requirements are deployment decisions that must be
  threat-modeled before production.
- The Functions controller identity receives only the exact Durable, Blob,
  APIM, and Sandbox Group operations it needs. APIM has separate backend
  identities. The Sandbox Group identity is distinct and receives only
  approved sandbox workload grants.
- Sandbox egress is deny by default with Full inspection and explicit hosts.
  Model/APIM/Durable/Blob/MCP credentials are never put in the sandbox.
- Durable state is a new sensitive-data surface. Raw content is excluded from
  Durable input/output, entity state, custom status, dashboard, logs, metrics,
  and default traces. Content refs are opaque, bounded, integrity-checked, and
  purged with their session/run retention policy.
- Run, session, sandbox, group, provider call, and lease IDs are internal
  metadata rather than credentials, but they are high-cardinality and can aid
  correlation; redact them from metric dimensions and default user status.
- W3C trace correlation uses trace context and bounded phase/provenance
  dimensions. Sensitive-content telemetry stays off by default.
- Activity authorization treats model text, tool names/arguments, remote MCP
  output, connector output, and restored workspace artifacts as untrusted.
  Prompt/tool-output injection cannot alter immutable policy or select an
  unregistered tool.
- Package and restored-artifact hashes, strict archive/path handling, symlink
  rules, output caps, and manifest binding protect sandbox integrity.
- Cross-tenant owner/session/run bindings, replay authorization, approval actor
  authenticity, callback nonce/expiry, SSRF controls, inspected egress, and
  connector audience validation fail closed.
- Clarification response URLs and request IDs are not bearer secrets. The
  answer endpoint authenticates the principal, authorizes it against the exact
  owner/run/generation/request/call tuple, validates schema/choice/size limits,
  and constructs the event name server-side. Human answers are untrusted model
  input: they may influence reasoning but cannot alter immutable capabilities,
  approval policy, identities, budgets, or tool routing.

### 4.12 Private product/API shape

The spike registers only a built-in, non-streaming HTTP chat path behind a
private environment gate. A representative response is:

```http
HTTP/1.1 202 Accepted
Content-Type: application/json

{
  "run_id": "server-minted-id",
  "session_id": "server-minted-id",
  "status": "Pending",
  "status_url": "/api/experimental/durable-agent-runs/<run_id>",
  "cancel_url": "/api/experimental/durable-agent-runs/<run_id>/cancel"
}
```

The request requires a client `request_id`; duplicate IDs with the same
canonical request hash return the same `run_id`. Authentication/owner binding
matches the existing built-in endpoint policy and is rechecked for status,
cancel, approval, clarification, and callback operations.

When status is `Waiting` for clarification, the authorized status/content
surface returns a pending request:

```json
{
  "request_id": "human-input-3",
  "kind": "clarification",
  "question": "Which production region should I investigate?",
  "choices": ["eastus2", "westus3"],
  "allow_free_text": false,
  "expires_at": "2026-09-05T00:00:00Z",
  "respond_url": "/api/experimental/durable-agent-runs/run-789/input/human-input-3"
}
```

The client submits:

```http
POST /api/experimental/durable-agent-runs/run-789/input/human-input-3
Idempotency-Key: submission-42
Content-Type: application/json

{"answer":"westus3"}
```

The endpoint persists the answer before calling the Durable client's
`raise_event(...)` and returns an accepted/idempotent receipt; it does not wait
synchronously for the next model step. If the answer was accepted but
`raise_event` reports unknown/terminal orchestration (`404`/`410`), delivery is
marked `orphaned` and the endpoint returns a typed `410 run_terminal` response
that preserves the accepted submission receipt; it never reports an
uncommitted 5xx or retries forever.

The starter and management endpoints reuse the async run vocabulary already
defined by `docs/aca-sandbox-session-runtime.md`: `Idempotency-Key` plus body
hash, accepted/provisioning/settling/terminal phases, status/result/cancel
links, `possibly_committed`/`Ambiguous` handling for uncertain writes, and
`410 Gone` after retained state expires. This spike adds durable model/tool
phases without creating a third incompatible run-management state machine.

`Ambiguous` is an error disposition, not a seventh top-level run status. An
uncertain write terminalizes as `status: "Failed"` with a sanitized
`ErrorEnvelopeV1` containing `disposition: "Ambiguous"` and
`possibly_committed: true`. This preserves the six-state status enumeration
while making uncertainty machine-readable and impossible to report as success.

The status contract exposes only:

- `Pending`, `Running`, `Waiting`, `Completed`, `Failed`, or `Cancelled`;
- run/session IDs, created/updated times, current bounded phase and step index;
- completed model/tool counts and aggregate budget usage;
- approval/clarification/callback requirement metadata. Raw question and
  answer content is returned only through an authorized content projection, not
  copied into Durable custom status;
- final response ref retrieval through an authorized content endpoint, or a
  sanitized error code.

Initial clients poll status. Optional durable progress events can update a
bounded status projection, but partial tokens are not replayed as a true model
stream. Reliable Redis streaming is a separate design. Declared triggers and
agent-as-MCP starters are later slices; one read-only remote MCP tool through
APIM is mandatory in the first end-to-end qualification.

### 4.13 Resource and safety budgets

Every run snapshots a server-enforced budget. The initial candidate defaults
are intentionally conservative and may be tightened by measured evidence:

| Budget | Candidate spike cap |
| --- | --- |
| Model steps | 48 maximum; correctness qualification uses at least 16 |
| One background model step | Less than the measured provider accepted-to-terminal maximum and remaining run budget; 2-second initial poll with bounded exponential backoff to 30 seconds; terminal retrieval completes inside its separately measured expiry window |
| Total tool calls | 128 |
| Parallel safe remote reads | 4 |
| Local sandbox mutations | 1 at a time |
| Concurrent app-owned sandboxes | `min(10, maxSandboxCount - reserved_headroom)`; reserve at least 5 group slots and fail configuration when no positive allowance remains |
| Autonomous active execution | 4 hours, still bounded by step/tool/token/cost caps |
| Human/approval waits | 8 per request turn; one open request at a time in v1 |
| Human/approval parked lifetime | 24 hours per request and 7 days per run, then append timeout or cancel and clean up |
| Clarification question/choices/answer | 8 KiB question, 20 choices of 256 characters, 64 KiB answer after schema validation |
| Model token budget | 1,000,000 aggregate input + output tokens for the multi-hour profile; a lower-cost correctness profile remains separate |
| Cost budget | Explicit deployment/model/profile-specific cap; never inferred from token count alone |
| Durable activity attempts | 3 for model/safe reads; writes follow idempotency policy and may be 1 |
| Durable activity envelope | 32 KiB, refs/metadata only |
| Tool argument / result content | 256 KiB / 1 MiB per call |
| Run external content | 32 MiB |
| Working context supplied to one model step | Trigger compaction before 75% of the model context, 4 MiB, or remaining token budget; reject only when the compacted context still cannot fit |
| Local-tool wave | Exactly 1 call and 5 minutes |
| Hosting / activity timeout | Flex Consumption spike with `functionTimeout` 10 minutes; every activity deadline <=8 minutes and every local tool deadline <=5 minutes |
| Human wake-up delivery | One answer event task + one cancel event task + one final timer; outbox retries event delivery with bounded backoff and no orchestration polling loop |
| Human wait history | Measured <=60 Durable history events per request, leaving >=140-event generation headroom |
| Sandbox lifecycle | Delete after wave; 10-minute auto-delete/reaper target |
| Continue-as-new | At most 20 checkpoints or 200 history events per generation |

Caps are checked before scheduling and after every activity. A cap breach
produces a typed terminal failure, stops new work, and still schedules cleanup.
The spike rejects unbounded model loops, tool loops, retries, payloads,
parallelism, spend, and sandbox retention while allowing bounded multi-hour
execution.

Because stateless model steps resend working context, input-token cost grows
with the accumulated transcript and can become quadratic over many steps. The
immutable full audit journal is never compacted or discarded, but
`compact_context_v1` may create a checkpointed working-context summary when
the next step would exceed 75% of the selected model context window, the 4 MiB
bundle cap, or the remaining token budget. Tool-call/reasoning/result groups
remain atomic; source range hashes and the summary response are persisted.
Compaction itself is a budgeted model activity. If a 16-step changing-direction
qualification cannot stay within context, fidelity, and cost gates with this
scheme, the multi-hour target is no-go even if a short loop succeeds.

A customer-owned capacity coordinator grants expiring, fenced app-owned
sandbox slots before `turn_init_v1` or a local wave. Runs above the allowance
park on a Durable timer without holding compute and retry with bounded backoff.
Platform capacity rejection never triggers an unbounded create loop: the run
returns to `Waiting` with a sanitized `sandbox_capacity` phase and terminalizes
as `sandbox_capacity_exhausted` when its run budget expires. Slot release is
idempotent, and the reaper reconciles leaked capacity leases with owned
sandbox inventory.

### 4.14 Observability and qualification metrics

W3C context links run -> model step -> tool call across Functions, Durable
activities, APIM, remote MCP, and the sandbox file protocol. Metrics use bounded
dimensions such as activity kind, tool provenance, outcome, retry class, and
deployment class; content and high-cardinality IDs are excluded by default.

Required measurements:

- total wall latency, active compute, parked/wait time, recovery/replay count,
  recovery catch-up time, checkpoint latency/bytes, continue-as-new count, and
  Durable history events/bytes;
- model/APIM latency, backend latency, TTFT, tokens, cost, attempts, 429s,
  timeouts, and response-loss ambiguity;
- MCP/APIM latency, backend latency, attempts, throttles, and failures;
- human-input requests, wait duration, buffered-before-wait delivery, event
  duplicates, outbox delivery attempts/terminal errors, final-timeout ledger
  reconciliation, answer/timeout/cancel outcomes, stale/conflict submissions,
  and time from accepted answer to next model step;
- tool queue, execution, transfer, attempts, retries, dedupe hits, ambiguity,
  and artifact bytes;
- sandbox capacity queue/rejections, create, optional attach/resume, readiness,
  workspace restore/export-to-Blob, lifecycle policy, delete, reaper, retained
  capacity, and orphan age;
- run/session throughput, concurrent active runs, busy/idempotency conflicts,
  terminal outcomes, errors, and cost.

### 4.15 End-to-end and fault-injection qualification

The correctness profile requires at least five model steps and a scripted
direction change: the model first calls `request_human_input` for the target
region, resumes from the human's role=`tool` answer, requests two local ACA
calls, observes their shared-workspace results, abandons the initial plan,
calls one read-only remote MCP operation through APIM, performs one synthetic
idempotent external write/counter, then returns a final answer grounded in all
results. The multi-hour profile extends that scenario to at least 16 model
steps with checkpointed context compaction. At least one model step runs as a
background Response.

Faults are injected:

- after the model response is persisted but before activity acknowledgement;
- after a background model Response is accepted but before its continuation
  token is acknowledged, and during a later poll;
- after a sandbox side effect but before sandbox result rename and separately
  after rename but before Durable activity acknowledgement;
- after the tool result checkpoint;
- during APIM 429 and timeout responses for model and MCP;
- during sandbox create, workspace restore/export, executor loss, and complete
  sandbox loss; the optional retained-sandbox gate separately injects
  auto-suspend and attach/resume failures;
- on duplicate HTTP requests, duplicate queue/event delivery, duplicate
  approvals/clarification answers, conflicting answer bodies, stale/expired
  request IDs, answer-before-wait buffering, and conflicting request hashes;
- while waiting for clarification: host shutdown, sandbox auto-suspend in the
  optional session-mapping mode, answer/timeout/cancel races, and immediately
  before/after accepted-answer event delivery;
- after answer CAS acceptance but before `raise_event`, after acceptance when
  the orchestration has terminalized or been purged (`404`/`410`), and every
  accept-vs-`close_human_request_v1` interleaving;
- after the maximum configured human park, including exact encrypted-reasoning
  replay, frozen-deployment retirement, and controlled context rehydration;
- during host restart, worker replacement, scale-to-zero, cancellation, and a
  deployment-compatible restart;
- immediately before and after continue-as-new and final session commit.

For each planned seam, kill/restart the worker and assert:

- final output and committed history are correct;
- a completed activity is replayed from history rather than redispatched;
- the synthetic supported write has one committed logical effect;
- ambiguous unsafe writes are not silently reported as success;
- duplicate request IDs resolve to one run;
- failed/cancelled attempts do not change committed history;
- restored workspace artifact hashes match;
- no content appears in Durable/dashboard/status/metric leak scans; and
- final app-owned sandbox inventory reaches zero within the cleanup bound.

### 4.16 Measurable go/no-go gates

| Gate | Go | No-go consequence |
| --- | --- | --- |
| Public MAF one-step seam | Core `1.17.0` + OpenAI `1.14.2` execute exactly one model request, expose ordered function-call contents, and invoke zero tools with auto-invocation disabled in 100% of contract tests. The Agent has no implicit session/history provider, every context provider is allow-listed with state in the step contract, and the provider request is a pure function of the durable message bundle and frozen config. | **Architecture no-go:** do not implement by middleware or private MAF internals. Revisit only with a supported public hook. |
| MAF reasoning baseline | A reviewed upgrade to core `1.17.0`, OpenAI `1.14.2`, Foundry `1.12.0` (or equivalent adapter) preserves the existing runtime contract and provides stateless encrypted-reasoning replay plus background Responses through APIM. | **Architecture no-go:** the clarified reasoning-first spike does not fall back to MAF 1.3/non-reasoning behavior. |
| MAF-envelope transcript fidelity | The selected reasoning-model APIM Responses path passes a >=3-step in-process-versus-durable comparison with item-equivalent reasoning/function-call/function-result transcripts after `Message.to_dict()`/`from_dict()`; invalid/missing encrypted reasoning fails closed. | **Architecture no-go:** use a supplemental provider-item adapter only if it passes the same comparison; never silently lose required content. |
| Background retention and recovery | Measure accepted-to-terminal maximum duration, terminal-result expiry, and repeatable/one-shot/refreshing retrieval semantics separately. Once a response ID/token is acknowledged, worker loss during polling resumes the same backend-bound operation without reissue; a one-shot terminal result is copied once before exposure. A lost start acknowledgement is surfaced as a known duplicate-cost/orphan window. | Use an explicitly approved `store=True` contract if it meets privacy requirements; otherwise **architecture no-go** when stateless operation duration cannot exceed the activity window or terminal retrieval cannot survive the recovery margin. |
| APIM response affinity | Start/get/cancel for one background response remain pinned to the accepting backend resource/deployment and survive ordinary gateway routing; failover never turns a valid response ID into an unexplained 404. | **Architecture no-go** for background mode until affinity is enforceable. |
| Multi-hour context/compaction | A >=16-step scripted reasoning loop changes direction, compacts working context deterministically, preserves atomic reasoning/call/result groups and full audit history, and remains within measured token/cost/context caps. | **Architecture no-go** for the clarified multi-hour target; a short-turn spike alone is insufficient. |
| Durable Extension convergence | A prototype accepts structured tool results, fences the active turn/pending calls, performs one declaration-only Agent inference per entity call across its supported MAF range, and represents remote MCP tools as a frozen declaration manifest with orchestrator-side dispatch. | Use the private model-step blueprint; record the upstream gap rather than weakening semantics. |
| Session substrate | On resolved Durable Functions 1.6.0, entity trigger/call/signal and fencing survive host replacement on every claimed backend; otherwise the deterministic singleton-orchestration + block-Blob lease fallback passes the same tests. | **Architecture no-go** if neither substrate proves one active turn and one commit. |
| Frozen local tool catalog | `turn_init_v1` discovers without worker import; every later sandbox matches the canonical manifest/package/catalog hash or fails closed. | **Architecture no-go.** |
| Durable replay | Across every injected restart seam, all already-acknowledged model/tool activities show zero redispatches and recovery catch-up p95 is <=60 seconds excluding provider latency. | **Architecture no-go** if acknowledged work repeats; otherwise investigate performance before broader use. |
| Session correctness | Twenty-five concurrent cross-process submissions yield at most one active turn; duplicate IDs dedupe 100%; conflicting hashes fail; only the expected generation commits. | **Architecture no-go.** |
| Side-effect truth | Across at least 100 ambiguity-window trials, the synthetic idempotent write has one logical committed effect; unsafe/unreconciled writes return `Failed` with `ErrorEnvelopeV1.disposition="Ambiguous"` and `possibly_committed=true`, never false success. | **Architecture no-go** for general tool enablement if classification cannot be enforced. |
| Commit acknowledgement loss | Killing the worker after the session commit but before activity acknowledgement returns the same commit receipt on retry and leaves the run `Completed` exactly once. | **Architecture no-go.** |
| ACA single-call recovery and capacity | Two sequential create/restore/manifest-check/execute/export/delete activities preserve the exact workspace artifact chain and pass 20/20 live trials, including activity retry from the prior committed ref. At 25 concurrent qualifying sessions, the configured app-owned cap is never exceeded, excess runs wait durably, and no run fails from avoidable group saturation. | **Architecture no-go** for local tools until immutable artifact transfer and bounded admission are reliable. |
| Run-scoped ACA optimization | Dedicated short-turn attach/resume/executor-ready/fencing trials pass 100/100 with bounded retained capacity. | Disable the optimization; this does not block per-wave v1. |
| Privacy | Automated inspection finds zero raw prompts, args, outputs, credentials, tokens, sandbox IDs, Blob paths, SAS values, or authorization-bearing refs in Durable history/dashboard/metrics/default traces. Durable history may contain only opaque content IDs, hashes, sizes, and bounded classifications. | **Architecture no-go.** |
| Bounded history/content | Activity envelopes stay <=32 KiB; generation stays <=200 events; continue-as-new preserves output and budgets; integrity/cap violations fail closed. | **Architecture no-go.** |
| Human clarification | A structured `request_human_input` emitted as the only call parks with no compute; an answer raised before the wait is buffered; duplicate/conflicting/stale submissions follow the first-answer ledger; accept-vs-timeout CAS has one winner; outbox `404`/`410` terminalizes delivery without infinite retry; the accepted answer becomes the matching role=`tool` result and the next model step continues exactly once. | **Architecture no-go** for clarification waits. |
| Human wait history | The exact pinned Durable backend proves the one answer/cancel/timer `task_any` pattern across restart/duplicate delivery and keeps one 24-hour request below 60 history events; no continue-as-new occurs while the request is open. | **Architecture no-go** until the wait is bounded and replay-safe. |
| Max-park reasoning resume | At the configured 24-hour park boundary, exact encrypted-reasoning/call-ID replay succeeds on the frozen deployment, or controlled `rehydrate_context_v1` resumes on an explicitly compatible deployment without repeating completed tools. | **Architecture no-go** for long human waits if both paths fail. |
| Approval/callback continuation | A fact committed immediately before/after a wait boundary is observed exactly once despite lost/duplicate wake-up events; outbox retry or final-timeout reconciliation observes accepted facts; no continue-as-new occurs while the request is open. | **Architecture no-go** for waits/approvals. |
| Hosting timeout | The pinned plan honors the configured 10-minute `functionTimeout`; all activity/tool deadlines terminate within their safety margins under host replacement. | **Architecture no-go** until retry ambiguity is bounded. |
| Cleanup | After normal, failure, cancellation, and worker-loss trials, final app-owned sandbox inventory is zero within 10 minutes in 100% of runs; reaper proves ownership filtering. | **Architecture no-go** until cleanup/capacity is bounded. |
| Cost/latency | Report p50/p95/p99 wall, active/parked, checkpoint, APIM, tool, sandbox, and recovery costs under the fixed workload with no unbounded growth. | No production recommendation; optimize or narrow scope before follow-up. |

Any architecture no-go gate stops the spike from advancing to a public or
production design. It must not be waived by documenting the failure.

### 4.17 Phased implementation plan after sign-off

1. **Gate-0 platform proof:** on the exact lock (`azure-functions-durable`
   1.6.0), prove entity registration/call/signal/fencing and host recovery on
   each claimed backend; prove the singleton-orchestration + block-Blob lease
   fallback before selecting the session substrate.
2. **MAF upgrade and reasoning proof:** qualify core `1.17.0`, OpenAI
   `1.14.2`, and Foundry `1.12.0` against current runner, APIM, MCP, telemetry,
   history, and tool contracts; prove encrypted reasoning replay and background
   continuation through the selected Azure model/API.
3. **Contract and loop simulator:** implement only pure versioned contracts,
   deterministic classification, fake content refs, the pinned MAF one-step
   `Agent.run()` contract, MAF message-envelope equivalence, budgets, and replay
   simulations.
4. **Upstream reuse decision:** prototype structured
   `continue_with_tool_results` and active-turn/pending-call validation against
   the MAF Durable Extension. Adopt it only after dependency compatibility and
   exact one-inference tests pass; otherwise retain the private blueprint with
   equivalent contracts.
5. **Local Durable backend:** run the orchestrator, session coordinator/entity,
   activities, idempotent admission, structured clarification endpoint,
   first-answer ledger/outbox, cancellation, external-event buffering/races,
   and continue-as-new against Azurite or Durable Task Scheduler.
6. **APIM model:** connect one-step `store=False` reasoning-model activities and
   background start/poll/cancel through the AI Gateway; prove no tool
   auto-invocation and collect cost/latency/retry evidence.
7. **Remote MCP:** add one read-only worker-side MCP activity through APIM and
   its authorization/idempotency classification.
8. **ACA single-call recovery:** add `turn_init_v1`, request-hash-bound journal,
   immutable workspace restore/export, one-call create/verify/execute/delete,
   and reaper. Keep multi-call/run-scoped retention disabled unless its
   independent attach/fencing gate passes.
9. **Fault injection:** execute every response-loss, side-effect, restart,
   duplicate, cancel, approval, sandbox-loss, and compatible-deployment seam.
10. **Soak/load:** measure fixed workloads at bounded concurrency and validate
   budgets, continue-as-new, cleanup, throughput, latency, and cost.
11. **Security review:** threat-model storage, identity, egress, approvals,
   injection, SSRF, cross-tenant isolation, replay authorization, artifact
   integrity, retention/purge, and operator ambiguity handling.

Each phase produces evidence for the gates above. No implementation phase
starts until this FRD is finalized by a human.

### 4.18 Relationship to Dynamic Workflows

Dynamic Workflows remain explicit LLM-authored DAGs for coarse, long-running
plans with workflow-safe tools, waits, and leaf agents. The durable agent loop
makes one otherwise normal turn durable at model/tool step boundaries. The
spike does not merge the two engines, translate normal turns into Dynamic
Workflow plans, or recursively run one engine inside the other.

They may share registration patterns, deterministic Durable helpers,
observability conventions, typed content-ref/protocol utilities, and approved
activity authorization helpers. Their orchestrators, state machines, public
contracts, capability policies, and lifecycle semantics remain separate.

### Authoring / API surface

There is no public authoring surface. A private environment gate enables the
experimental registration path for one built-in non-streaming HTTP starter.
Its exact environment-variable name is chosen during implementation and must
clearly include `EXPERIMENTAL`; absence preserves all existing behavior.

### Compatibility

Normal HTTP chat, SSE streaming, declared triggers, agent-as-MCP, Dynamic
Workflows, session runtime, and FRD 0009 hybrid execution remain unchanged when
the gate is absent. The spike does not change `runner.py` semantics; it adds a
parallel private runner. The private route/contract can be removed or changed
without deprecation. Any future public surface requires a new finalized FRD,
schema/docs updates, migration policy, and security review.

Human clarification adds no dependency to the explicit loop:
`azure-functions-durable` already supplies `wait_for_external_event`,
`create_timer`, `task_any`, and the client `raise_event` API. The MAF Durable
Extension's Workflow HITL routes are useful precedent but are not required or
reused for this custom orchestrator.

While the private gate is enabled, durable and legacy in-process turns may not
use the same session ID. The legacy runner's process-local lock cannot
coordinate with a Durable session owner; mixed-mode admission fails closed.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Durability boundary | Whole turn / middleware interception / model and tool steps | Checkpoint every model and tool step; whole-turn retry can repeat completed tool work. | Human | 2026-09-04 |
| 2 | Loop owner | MAF `agent.run()` / middleware / Durable orchestrator | The versioned Durable orchestrator owns the loop because only it can schedule, yield, replay, and resume activities. | Human | 2026-09-04 |
| 3 | MAF execution seam | Private internals / middleware cache / public one-step chat | Disable public chat-client function invocation and consume returned function-call contents; gate against the pinned package before implementation. | Human + Agent | 2026-09-04 |
| 4 | Experimental exposure | Public schema / private env gate / sample fork | Register a private non-streaming HTTP path under an explicit env gate; add no public schema. | Human | 2026-09-04 |
| 5 | Session and content state | MAF session / Durable content payloads / session entity plus Blob refs | Serialize turns with a session entity and keep committed/in-progress content in customer-owned Blob through bounded integrity refs. | Human | 2026-09-04 |
| 6 | Execution trust split | Everything in worker / everything in ACA / FRD 0009 APIM-ACA split | Keep model and privileged MCP/connectors worker-side through APIM; execute local customer code only in customer-owned ACA. | Human | 2026-09-04 |
| 7 | Side-effect guarantee | Claim exactly once / at-least-once plus idempotency / never retry | State at-least-once truth; require request-bound idempotency, outbox/reconciliation, or fail closed for unsafe writes. | Human | 2026-09-04 |
| 8 | Initial client contract | SSE token replay / 202 plus polling / hold HTTP open | Return 202 with run/session/status/cancel URLs; use polling and optional bounded progress, not replayable token streaming. | Human | 2026-09-04 |
| 9 | Sandbox lifetime | Full turn / multi-call live wave / single-call wave / snapshot recreation | Default to one fresh single-call sandbox-wave activity with an immutable workspace ref between calls. Live multi-call/full-turn retention is gated; snapshots are rejected. | Human + Agent | 2026-09-04 |
| 10 | Dynamic Workflows relationship | Merge engines / build on Dynamic Workflows / separate engines with shared helpers | Keep explicit coarse DAG workflows separate from the normal-turn durable loop; share only protocol, registration, and observability helpers. | Human | 2026-09-04 |
| 11 | Review status | Finalize now / In review pending sign-off | Keep `In review`; independent review may prepare the design, but only explicit human sign-off can finalize it. | Human | 2026-09-04 |
| 12 | Model checkpoint representation | MAF messages / provider-native items / service-side thread only | Persist normalized MAF message envelopes, including qualified encrypted reasoning; require semantic equivalence and add provider-native supplements only when proven necessary. | Agent review | 2026-09-04 |
| 13 | Local Durable retry unit | Per-call activities sharing live sandbox / multi-call wave activity / single-call sandbox wave | Use one fresh single-call sandbox-wave activity with immutable workspace refs; defer live multi-call waves. | Agent review | 2026-09-04 |
| 14 | Local tool catalog | Worker import / discover every model step / turn-init manifest pin | Discover once in a disposable ACA sandbox, freeze the manifest hash, and revalidate every execution sandbox. | Agent review | 2026-09-04 |
| 15 | Session substrate | Assume Durable Entity / prove entity with fallback / process lock | Gate exact Durable 1.6 entity behavior; fall back to singleton orchestration plus block-Blob lease/fencing. | Agent review | 2026-09-04 |
| 16 | Continuation events | Trust external event / preserve event history / durable fact plus wake-up | Persist approval/callback facts with CAS; events only wake; continue-as-new only when quiescent. | Agent review | 2026-09-04 |
| 17 | Async state API | New run vocabulary / synchronous response / reuse session-runtime vocabulary | Reuse idempotency, status/result/cancel, ambiguity, expiry, and terminal semantics from the ACA session runtime. | Agent review | 2026-09-04 |
| 18 | Final commit retry | Generation fence only / idempotent commit receipt / separate transaction service | Record the committing run/request/response tuple and return the same receipt when acknowledgement loss retries an already-applied commit. | Agent review | 2026-09-04 |
| 19 | Lost approval wake-up (initial design; superseded by #39) | Event only / short polling / event plus bounded mailbox re-poll | Initially selected event plus timer re-poll; later replaced by one answer/cancel/timeout race and outbox delivery to bound history. | Agent review | 2026-09-04 |
| 20 | Sandbox capacity | Rely on group errors / host concurrency only / fenced app-owned allowance | Admit through a bounded capacity coordinator with reserved group headroom, Durable waiting, expiry, and reaper reconciliation. | Agent review | 2026-09-04 |
| 21 | Ambiguous public outcome | Seventh status / false failure / failed plus disposition | Keep six run statuses; expose uncertainty as `Failed` plus `disposition=Ambiguous` and `possibly_committed=true`. | Agent review | 2026-09-04 |
| 22 | MAF step construction | Raw client / one-step Agent / terminate middleware | Use a dedicated one-step `Agent.run()` with client auto-invocation disabled; retain known-safe Agent/Chat middleware and treat any FunctionMiddleware invocation as a configuration failure. | Agent deep dive | 2026-09-04 |
| 23 | Work decomposition | Extra planner / orchestration planner / ordinary model tool choice | Add no planner. The LLM returns text or ordered function calls using normal tool schemas; the orchestrator mechanically dispatches the returned calls. | Agent deep dive | 2026-09-04 |
| 24 | Durable Extension reuse | Consume unchanged / structured-continuation enhancement / ignore extension | Prefer an upstream `continue_with_tool_results` entity operation; use the private blueprint until structured inputs, pending-call validation, and dependency compatibility exist. | Agent deep dive | 2026-09-04 |
| 25 | Middleware replay journal | Primary scheduler / comparative baseline / reject | Keep it as a fault-tested fast-forward baseline, not the source of first-class Durable step checkpoints. | Agent deep dive | 2026-09-04 |
| 26 | Alternate one-step seam | Declaration-only / `additional_tools` / disabled invocation | Use disabled invocation as the broad fail-safe (including lazy MCP). Evaluate `additional_tools`; reserve declaration-only tools for the upstream structured-continuation path. | Agent deep dive | 2026-09-04 |
| 27 | Reasoning-model baseline | Stay on MAF 1.3 / latest compatible MAF set / custom provider adapter | Target core `1.17.0`, OpenAI `1.14.2`, and Foundry `1.12.0`; use an adapter only if exact upgrade compatibility fails. | Human + Agent | 2026-09-04 |
| 28 | Long individual inference | Hold activity / retry whole model call / background Response | Persist provider continuation tokens and poll the same operation through Durable timers/activities. | Human + Agent | 2026-09-04 |
| 29 | AgentMiddleware role | Own loop / start orchestration / observe only | Permit it as an idempotent starter that stops local execution; the orchestrator, not later middleware callbacks, owns scheduling. | Human + Agent | 2026-09-04 |
| 30 | Recovery model | Serialize Python stack / logical state replay / rerun everything | Reconstruct a fresh one-step Agent from explicit durable messages, calls, results, continuation tokens, budgets, and policy; never require call-stack serialization. | Human + Agent | 2026-09-04 |
| 31 | Background retention | Assume `store=False` indefinitely / explicit `store=True` / measure and gate | Separately measure max operation duration, terminal-result expiry, and retrieval semantics; require explicit customer approval for longer provider storage or fail the long-background path. | Agent review | 2026-09-04 |
| 32 | Background APIM routing | Normal backend pool / sticky backend binding / direct model bypass | Persist and enforce the accepting backend resource/deployment for start/get/cancel; no failover of an existing response ID. | Agent review | 2026-09-04 |
| 33 | Multi-hour context | Resend unbounded transcript / discard history / immutable audit plus checkpointed compaction | Preserve full audit history while generating a bounded working-context summary with atomic reasoning/call/result groups. | Agent review | 2026-09-04 |
| 34 | Latest MAF package set | Loose `>=1.13` / exact current set / stay pinned | Target core `1.17.0`, OpenAI `1.14.2`, and Foundry `1.12.0`; Durable Extension remains `1.0.0b260730`. Advance only after compatibility gates. | Human + Agent | 2026-09-04 |
| 35 | Middleware-to-Durable bridge | Middleware calls activities / event-journal RPC / explicit loop | Middleware may signal and await an external journal, but only the parent orchestrator yields activities. Keep the RPC bridge as a short-turn comparison; use the explicit loop for multi-hour runs. | Human + Agent | 2026-09-04 |
| 36 | Model-requested clarification | Natural-language question / approval mechanism / reserved tool | Expose `request_human_input`; persist the request and return the accepted answer as its matching role=`tool` function result. | Human + Agent | 2026-09-04 |
| 37 | Human-input delivery | External event only / polling only / authoritative fact plus event | Atomically accept the first response, then raise a unique at-least-once external event; buffer early delivery, retry through an outbox, and reconcile the ledger if the final timer wins. | Human + Agent | 2026-09-04 |
| 38 | Clarification dependency | MAF Workflow HITL extension / existing Durable Functions APIs / custom broker | Use the existing `azure-functions-durable` client/event/timer APIs and a custom authenticated endpoint; add no runtime dependency for the explicit loop. | Human + Agent | 2026-09-04 |
| 39 | Human wait topology | Repeated timer re-poll / single event-cancel-timeout race / continue-as-new while waiting | Supersede #19: use one answer event, one cancel event, one final timer, plus outbox retries; never roll over with an open request. | Agent review | 2026-09-04 |
| 40 | Answer vs. timeout race | Endpoint wall clock / orchestration winner / one request-record CAS | `accept_human_response` and `close_human_request_v1` compete on one record; the CAS winner is authoritative and displayed expiry is advisory. | Agent review | 2026-09-04 |
| 41 | Human wake-up terminal errors | Retry forever / discard silently / orphan accepted answer | Treat Durable client `404`/`410` as terminal outbox results, mark the answer orphaned, and expose typed `run_terminal`. | Agent review | 2026-09-04 |
| 42 | Long-park model context | Fail after answer / exact replay only / exact replay plus controlled rehydration | Validate exact encrypted-reasoning replay; on recognized incompatibility rebuild working context from immutable audit without repeating tools. | Agent review | 2026-09-04 |
| 43 | Architecture approval | Continue review / approve private spike / approve production support | Proceed with the private experimental spike under the recorded gates; this approval does not grant production support. | Human | 2026-09-04 |
| 44 | Foundation implementation slice | Simulator only / private Durable foundation / include live transports | Implement exact MAF pins, strict contracts, local replay/HITL/tool fakes, Durable entity/blueprint/routes, and content-free telemetry now; defer APIM, remote MCP, and ACA live adapters to layer 2. | Human | 2026-09-04 |
| 45 | Newly published MAF artifacts | Wait for proxy / local wheel paths / official release assets | Keep exact package pins and lock to the official `python-1.17.0` GitHub release wheels until the repository proxy mirrors them; cap runtime Python support to CI's 3.13-3.14 line. | Agent | 2026-09-04 |
| 46 | Private tool policy | Infer behavior / public schema / fixed private policy file | Require app-root `durable-loop-tools.json` to bind every discovered local/remote tool to provenance and behavior; only remote reads may be parallel-safe. | Human + Agent | 2026-09-07 |
| 47 | Qualification controls | Redeploy per mode / arbitrary fault payload / gated fixed enums | Bind strict `sandbox_profile` and deterministic one-shot `fault_profile` values into the run plan; retained and fault modes each require a separate private gate. | Human | 2026-09-07 |
| 48 | Background start retry | Retry uncertain start / fail ambiguous / provider idempotency guess | Persist `started` before POST; retry explicit throttles only. An uncertain acknowledgement becomes Ambiguous and never issues a second background response. | Agent | 2026-09-07 |
| 49 | Retained sandbox recovery | Trust attach error / inventory-first recreate / fail run | Query exact group inventory first; authoritative absence fences the generation, recreates, and restores the last external checkpoint. | Human + Agent | 2026-09-07 |
| 50 | Human status privacy | Inline question / metadata plus owner detail route / omit status | Keep status content-free and expose question/choices/schema only through an owner-authorized GET on the existing human-input route. | Human | 2026-09-07 |

## 6. Test plan

- [x] Contract: core `1.17.0` + OpenAI `1.14.2` one-step client exposes ordered
  function-call contents and invokes no tool.
- [x] Dependency: core `1.17.0`, OpenAI `1.14.2`, and Foundry `1.12.0`
  pass runner, APIM, MCP, telemetry, history, streaming, usage, and tool
  compatibility suites before reasoning mode is enabled.
- [ ] Contract: the qualified reasoning Responses surface preserves normalized
  MAF encrypted-reasoning/function-call/function-result messages across a
  multi-step durable round trip with item equivalence to the in-process
  control.
- [ ] Background model integration: start/poll/resume uses one provider response
  ID across worker loss after acknowledgement, returns a stable continuation
  token while pending, copies one-shot terminal output before exposure, and
  separately detects/accounts for the lost-start-ack duplicate window.
- [ ] Historical regression: exact MAF 1.3 one-step behavior is retained as a
  comparison only. All one/multiple-call order, disabled-invocation,
  declaration-only, and `additional_tools` assertions must independently pass
  on core `1.17.0`.
- [ ] Contract: the one-step Agent uses an explicit context-provider allowlist,
  has no implicit session/history state, and emits the same provider request
  after reconstructing only its versioned durable inputs.
- [ ] Contract: per-service-call history alone reproduces the tool-result crash
  gap, while paired chat/function replay journaling fast-forwards the same
  deterministic scenario without repeating its side effect.
- [ ] Comparative middleware bridge: latest `MiddlewareBundle` routes model/tool
  requests through a parent-orchestration event journal, proves deterministic
  replay after driver loss, and demonstrates the configured activity-timeout
  ceiling rather than claiming multi-hour support.
- [ ] Upstream compatibility: DurableAIAgent structured tool-result
  continuation preserves roles/call IDs, validates the latest pending-call set,
  fences the whole turn, dedupes redelivery, and supports the extension's MAF
  version range.
- [x] Unit: strict versioned contracts, unknown-version rejection, canonical
  request hashes, deterministic call keys/order, integrity refs, size/budget
  caps, error/ambiguity envelopes, and fail-closed policy drift.
- [x] Unit: pure orchestrator replay, stable fan-out/fan-in, retry selection,
  approval/cancel event races, deadlines, continue-as-new, and cleanup
  scheduling.
- [x] Unit/integration: reserved clarification call validation, mixed-batch
  rejection with one result per call ID, unique event names, early-event
  buffering, first-answer/close CAS interleavings, duplicate/conflict/stale
  handling, answer/timeout/cancel races, outbox wake-up retry and terminal
  `404`/`410`, orphaned-answer receipts, and exact role=`tool` call-ID
  continuation.
- [ ] Integration: a request parked for the configured maximum resumes through
  exact encrypted-reasoning replay or controlled `rehydrate_context_v1`, with
  frozen/retired deployment scenarios and no repeated tool effect.
- [x] Unit/integration: deterministic compaction trigger, immutable source-range
  hashes, atomic reasoning/call/result groups, summary replay, full-audit
  preservation, and compacted-context overflow failure.
- [ ] Unit/integration: cross-process session admission, duplicate/conflicting
  request IDs, one-active-turn invariant, generation fencing, failed/cancelled
  journal isolation, atomic final commit, and idempotent success after
  commit-acknowledgement loss.
- [ ] Local Durable integration: exact Durable Functions 1.6 entity/fallback
  substrate, Azurite or Durable Task Scheduler recovery at each checkpoint,
  quiescent continue-as-new with durable approval facts, and bounded
  history/status payloads.
- [x] Layer-2 adapter contract: deterministic tests cover APIM foreground and
  background start/poll/cancel receipts, exact fixed control routes and
  response-ID header validation, remote MCP initialization/routing and
  ambiguity, per-call workspace waves, retained reuse/loss/recreation,
  capacity fencing, cleanup/reaping, strict private policy/profile parsing,
  activity/commit acknowledgement replay, content-free human status, and
  layer-2 metrics without live Azure or secrets.
- [ ] APIM/model integration: one stateless request per foreground model step;
  background start/get/cancel with measured retention and exact backend
  affinity; no APIM retry/body log; response-loss/orphan accounting,
  tokens/cost/TTFT.
- [ ] MCP integration: worker-side read-only MCP through APIM; idempotency/
  unsafe-write policy tests.
- [ ] ACA integration: `turn_init_v1` manifest pin plus sequential single-call
  create/restore/verify/execute/export/delete activities, request-hash
  validation, immutable workspace chain, no credential persistence,
  bounded capacity admission/waiting, auto-delete, and reaper.
- [ ] Fault injection and E2E: the full matrix in sections 4.15-4.16, including
  the five-step clarification correctness profile, the >=16-step multi-hour/
  compaction profile, two local calls, one remote MCP read, one synthetic
  write, human answer-before-wait and duplicate/race seams,
  background-response loss seams, worker kills, cancellation, compatible
  deployment, final correctness, no duplicate committed effect, no
  acknowledged activity redispatch, no content leak, and zero final sandbox
  inventory.
- [ ] Soak/load/security: bounded concurrency, cost/latency/history/capacity
  measurements and a dedicated threat-model review.

## 7. Docs impact

- [x] `docs/architecture.md` - after implementation, add the private durable
  runner, activity/content-ref boundaries, and its separation from Dynamic
  Workflows.
- [x] `docs/observability.md` - after implementation, document content-free
  durable run/step/call telemetry and metric dimensions.
- [x] `docs/frds/README.md` - index FRD 0010.
- [ ] `docs/front-matter-spec.md` - no spike change; there is no public schema.
- [ ] `docs/triggers.md` - no first-slice change; declared triggers are later.
- [ ] `README.md` - no private-spike quickstart change.

## 8. Status & sign-off

- **Architecture review (phase 2):** Independent rubber-duck review verdict:
  **GO-WITH-CONDITIONS**. The draft now treats normalized MAF transcript
  equivalence and exact Durable 1.6 session behavior as no-go gates, uses
  single-call ACA waves with immutable workspace refs, freezes the sandbox tool
  manifest at turn initialization, makes approval events wake-up hints over
  durable facts, reuses the existing async run vocabulary, and bounds
  cancellation, continuation, timeout, privacy, and cleanup semantics. A final
  post-commit audit required an idempotent final-commit receipt, timer-based
  mailbox recovery for lost approval wake-ups, fenced sandbox-capacity
  admission, and an explicit `Failed` + `Ambiguous` disposition mapping.
  Subsequent upstream deep dives established the three exact MAF middleware
  boundaries, validated the one-step Agent/declaration-only path, retained a
  replay journal as a comparative fallback, and identified structured
  tool-result continuation as the missing Durable Extension API. The user's
  reasoning-first clarification then added the MAF upgrade, background
  continuation/retention/affinity, middleware-starter, logical-reconstruction,
  context-compaction, and durable human-clarification requirements; these
  corrections are recorded in Decisions 18-42.
- **Human sign-off:** larohra, 2026-09-04 — approved through explicit
  implementation request.
- **Layer 1 implementation note:** the private locally testable durable-loop
  foundation is implemented while this FRD remains `Finalized`. The slice pins
  the exact MAF set, adds strict contracts and deterministic fakes, proves the
  real public OpenAI Responses serializer/parser plus one-step Agent seam and
  reasoning-message fidelity, and registers versioned refs-only Durable
  entity/orchestrator/activity and management-route surfaces. Raw prompts,
  instructions, reasoning, arguments, results, and answers are committed to
  integrity-bound content refs before crossing Durable boundaries. Local and
  deterministic Durable harnesses cover replay, admission/start ambiguity,
  idempotent commit, immutable-audit compaction, cancellation facts, mixed-call
  repair, and human first-answer/timeout CAS. The implementation also rejects
  blank/incomplete model outcomes, hashes or externalizes provider identifiers,
  records reasoning-token usage when available, fences cancellation against
  final commit, terminalizes ambiguous tool outcomes without model recovery,
  and restricts model-authored human-response schemas to a bounded local-only
  subset without references or regular expressions. Admission freezes the
  resolved configured model; active-execution budgets exclude authoritative
  parked time; token, cost, and aggregate external-content counters are
  enforced and exposed without content; raw provider call IDs stay in external
  documents rather than Durable envelopes; answer and cancellation wakeups use
  deadline-bound outboxes with history rollover; and the journal requires an
  explicit dedicated Blob origin. Deterministic compaction preserves a
  semantic summary while the immutable audit has its own larger bounded
  envelope without elevating user/tool content to system authority. Every
  activity revalidates the frozen provider/endpoint/API/model target, configured
  per-call argument/result limits wrap dispatch, cost enforcement requires
  explicit frozen pricing rates (otherwise the cap is disabled), terminal
  idempotency receipts remain until their recorded expiry, and external
  protocol documents reject duplicate JSON keys. APIM
  model/background affinity,
  remote MCP, ACA single-call workspace waves, and deployed multi-process/backend
  qualification remain layer 2+ gates; the FRD does not become `Implemented`
  until those deployed layers complete.
- **Layer 2 implementation note:** the private runtime now composes the existing
  APIM client manager, MAF one-step Agent, remote HTTP MCP client, ACA transport,
  hybrid executor, immutable content store, and Durable activity factory. Model
  create remains `${APIM model base}/responses`; background control uses fixed
  `GET /responses` and `POST /responses/cancel` with a validated
  `x-af-response-id`. Start/poll/cancel/tool/capacity/cleanup receipts use keyed
  CAS documents in the dedicated content container, with provider identifiers,
  bodies, encrypted reasoning, arguments/results, retained bindings, and
  workspace archives externalized. `durable-loop-tools.json` supplies exact
  private provenance/behavior policy. Per-call ACA waves explicitly
  create/restore/verify/execute/export/delete; separately gated retained mode
  uses exclusive ownership, generation fencing, attach-manifest verification,
  inventory-first loss recovery, checkpoint restore, final cleanup, timed
  auto-delete, and an owner-filtered reaper. The authenticated starter binds
  strict sandbox/fault profiles into the execution hash, and human status no
  longer exposes question/schema content inline. Deterministic tests cover
  transport and crash-window state machines; final Function App deployment,
  real backend retention/affinity measurement, multi-process Durable
  qualification, live identity/egress verification, load/soak, and the full
  security review remain deployment-layer work.
