# Durable Agent Loop: execution-mode feasibility and leadership plan

Date: 2026-09-14

Status: Research complete, incorporating the independent Whole agent in ACA
analysis. This is an architecture/prototype recommendation, not an approved
production FRD or a claim that the proposed modes are implemented.

Source baseline: `Azure/azure-functions-agents-runtime`, commit
`217abc251e55f52b4ce820bafc66a095655aaee6`, the DTS-backed leadership spike,
examined in the dedicated `larohra/durable-without-aca` worktree.
No Azure resources or data were created for this feasibility analysis.

## 1. Leadership decision

**The idea is worth proposing, but as one durable runtime with a simple default
and an optional isolation capability, not three equal execution engines.**

- **Launch default: option 1, Functions model steps and trusted application-tool
  activities.** Target business/service agents that call APIs, MCP servers,
  connectors, databases, and customer-deployed bounded functions.
- **Second capability: option 3, a customer-provided ACA Sandbox Group for
  executable tools.** Keep model access and durable orchestration in Functions.
  This is the natural extension for shell/code/workspace workloads that need
  isolation and retained files.
- **Defer option 2, whole-agent ACA, from the general-purpose launch.** Revisit
  when a customer needs the model-side SDK, custom agent code, dependencies,
  or agent-side networking inside its sandbox policy boundary. Do not imply
  that wrapping an existing complete agent run preserves every tool checkpoint.

This sequencing prioritizes the requirement discussed in this conversation:
**each tool call should be its own activity checkpoint**. The whole-agent
analysis identifies a different valid second-stage priority: if compatibility
with ordinary MAF, skills/subagents, native packages, and agent-local files is
more valuable than per-tool checkpoints, an explicit whole-agent ACA preview
can deliver more developer-experience value than hybrid. That is an intentional
durability/trust tradeoff, not a disagreement about technical feasibility.

If leadership permits only one route, **choose option 1 for the stated
minimal-setup/adoption objective**, with an explicit trusted-tools scope.
If arbitrary model-generated shell/code and per-session compute isolation are
mandatory at launch, choose **option 3 instead**. These are different product
promises; direct Functions execution cannot honestly provide both.

"Most lucrative" here means the strongest expected adoption and
engineering/operational return, not demonstrated revenue or profit. There is
no demand, pricing, utilization, or support-cost study establishing financial
ROI. The commercial hypothesis is that most service agents should not pay a
sandbox setup tax, while execution-heavy customers can opt into it.

## 2. Four distinctions that make the proposal viable

### Logical sessions are not compute sessions

Option 1 does **not** need to give up session management. DTS plus a small
session entity and external content can retain committed conversation,
reasoning continuation, active-turn admission, HITL, timers, and checkpoints.
What it gives up is worker affinity and implicitly retained local filesystem,
interpreter, caches, subprocesses, or sockets.

The application still has to implement and operate these logical-session
contracts. DTS schedules work and persists history; it does not infer owner
authorization, message storage, tool idempotency, or filesystem continuity.

### One user turn can contain many model/tool steps

For all fine-grained modes, preserve this structure:

```text
Authenticated ingress
    -> session admission and generation fence
    -> Durable orchestrator in Functions, with history in DTS
        -> one model-step activity
        -> separate activity for each proposed tool call
        -> append recorded results
        -> next model-step activity
        -> final session commit

HITL and retry waits -> durable events/timers, not a parked worker
Large/sensitive bodies and workspace artifacts -> external content store
```

Each model step reconstructs a fresh one-step Agent from recorded messages,
with automatic function execution disabled. The checkpoint is an acknowledged
activity result, not a snapshot of a Python call stack.

Moving an opaque `agent.run()` into either Functions or ACA gives only a
coarse activity checkpoint. Its internal model calls and tools do not become
separate DTS activities merely because middleware can observe them.

### Trusted deployed code is different from arbitrary executable input

Running trusted customer-deployed Python in the customer's Function App is
normal Functions usage. A constrained application function can invoke an
approved command or HTTP operation when it has appropriate validation and
authorization. This is not the same as accepting an arbitrary LLM-generated
shell script, arbitrary package installation, or arbitrary URL with ambient
app privileges.

Direct activities share the app's identities, secrets, network reach, files,
CPU, memory, and failure domain. A subprocess, directory name, or different
user-assigned identity selected by the SDK does not establish a per-tool
security boundary. An async timeout is not a hard CPU/memory limit or proof
that subprocesses have stopped.

### Managed provisioning is not absence of infrastructure

"No extra setup" is a feasible user experience if the product provisions and
wires the resources. It is not a property supplied by the Python library alone.
Model authorization/quota, deployment and role-assignment permissions, region
availability, data retention, cost, and network policies still need an owner.

This plan assumes a customer application/deployment trust boundary. Running
unrelated customers' uploaded executable code in a shared privileged Function
App would be a materially different, unproven hosted-service architecture.

## 3. Comparison of the requested routes

| Dimension | 1. Functions-only | 2. Whole agent in customer ACA | 3. Functions agent, ACA tools |
| --- | --- | --- | --- |
| Compute placement | Model-step and tool activity bodies in Functions; service tools invoke their remote backends. | Agent/model-side code and executable tools in ACA; durable admission, timers, history, and activity adapters still need a control plane outside the sandbox. | Model-step and authorized service activities in Functions; executable local tools in ACA. |
| Checkpoint granularity | Native model-step/tool activity boundaries. | Opaque whole-run mode checkpoints completed turns or explicit supported segment yields; proposed start/adopt/poll controls the run. Equivalent per-tool durability requires a stepwise sandbox protocol or a qualified resumable framework integration. | Same fine-grained boundaries as option 1; tool activity dispatches to sandbox. |
| Logical conversation/session | Entity plus external context, independent of worker. | Same durable logical-session contract remains necessary. Sandbox lifetime is not its source of truth. | Same as option 1. |
| Files | No implicit cross-activity workspace. Use explicit objects/artifacts; optionally reconstruct a bounded ZIP workspace. | Retained sandbox disk supports workspace-oriented agents; external checkpoint needed for replacement. | Retained sandbox workspace for tools; model receives selected outputs/refs, not implicit local disk. |
| Process continuity | None promised between activities. | Warm process can continue while alive; observed Disk suspend required executor restart. No general live-process snapshot guarantee. | Same sandbox limitation for tool processes; model state is explicit. |
| Privilege boundary | All app-resident tool code can use the app's privileges. Suitable only for trusted code and validated operations. | Sandbox separates agent/tool execution from Functions, but agent and tools inside that sandbox may share model/network authority. | Stronger separation of privileged model controller from executable tool code, if credentials and deployment bytes are kept out of the sandbox. |
| Customer policy reach | Function App identity and network policy. | Can govern agent-side and tool-side sandbox egress/runtime within supported service features. Does not eliminate external controller or provider data handling. | Sandbox policy governs executable tools, not model-side Functions or worker-side connectors. |
| Package management | Immutable deployment/build dependencies; no shared mutable per-session venv. | Versioned compatible image/bundle and runtime bootstrap; custom dependencies possible within supported sandbox constraints. | Functions dependencies and a separate tool bundle/runtime must be compatible and versioned. |
| Cold/warm performance | Avoids sandbox provisioning and remote executor handoff. Still pays activity dispatch, content I/O, app cold starts, provider/service latency. | Cold sandbox/bootstrap cost; warm in-sandbox calls may be efficient. Stepwise mode still pays remote handoffs; opaque mode trades durability for fewer handoffs. | Additional tool handoff; retained execution amortizes create cost. Remote service calls need not pay the sandbox tax. |
| Scale/failure domain | Functions autoscaling; shared worker resources need concurrency limits. Long or CPU-heavy tools can interfere with the agent/controller. | Functions control plane plus sandbox capacity, admission, lifecycle and reaper. Model waits may occupy sandbox capacity unless explicitly released. | Same dual-capacity problem, with potentially less sandbox occupancy because model/HITL waits need not hold active tool compute. |
| Customer setup | Lowest, if provisioned by the platform. Still needs authorized model and deployment access. | Group plus identity, model/connector access, runtime/package compatibility, networking, capacity and policy validation. | Group plus tool identity, networking, package/runtime and capacity validation; model setup stays in Functions. |
| Platform complexity | Lowest execution-lane count, but durable runtime still needs production hardening. | Highest scope if matching fine-grained durability and generic agent compatibility. Opaque mode is narrower but weakens the promise. | More lifecycle code than option 1; substantial spike reuse, one common orchestration protocol. |
| Best fit | Business/API/workflow agents and trusted application functions. | Entire agent runtime must be under customer sandbox policies, or an existing framework needs a whole-run host and accepts coarser recovery. | Code/workspace agents requiring tool isolation without putting model credentials beside executable tools. |
| Launch recommendation | Default. | Defer as a specialized profile. | Optional isolation capability, after default contracts are stable. |

Whole-agent placement is not inherently inferior: it is the right boundary
when the customer requires **all agent-side compute** inside the controlled
runtime. Hybrid does not meet that requirement. Conversely, whole-agent ACA
does not automatically separate powerful agent credentials from untrusted
tools sharing its process or sandbox.

### The correct whole-agent handoff, if option 2 is selected later

The independent whole-agent analysis recommends:

```text
Entity admits turn
    -> short idempotent start-or-adopt activity
    -> ACA executes the ordinary MAF agent segment
    -> durable timers and short poll activities observe the same attempt
    -> final / supported HITL / budget boundary yields a quiescent segment
    -> controller persists external conversation/workspace checkpoint
    -> entity CAS commits the authoritative checkpoint or final turn
```

Do not hold a Function activity open for the whole agent loop. A host restart
should adopt the same live sandbox attempt, not automatically launch another.
If the sandbox is lost, resume from the last committed external segment
checkpoint only after reconciling the intervening effect frontier.

The existing full-agent backend already has start/status/events/cancel and a
harness with atomic local conversation/workspace commit. The Durable bridge,
external conversation checkpoint, supported HITL/budget yields, effect journal,
and generation/cancellation fencing are new work. Current sandbox-local
conversation alone does not survive complete sandbox deletion.

Normal MAF execution compatibility does not mean arbitrary MAF state can be
checkpointed. Require registered checkpoint adapters for custom context or
subagent state, or restrict yields to supported root-agent quiescent boundaries.
Unknown mutating effects must block automatic restart and new turns pending
reconciliation; the collaborator calls this proposed outcome
`RECONCILIATION_REQUIRED`.

A trusted external journal may help re-use cached model/tool results while
recovering a segment, but it is not DTS's per-tool activity history. Missing
model decisions followed by a downstream side effect are particularly unsafe:
a new model response must not reinterpret an already-executed mutation.

An all-in-ACA **stepwise** variant is technically plausible: DTS still schedules
one model activity and every tool, each targeting the retained sandbox. It
retains fine-grained checkpoints but loses the opaque-agent compatibility
advantage and keeps the extra remote placement/credential complexity. Defer it
unless a customer explicitly requires both per-step durability and all
agent-side execution inside its group.

## 4. State, isolation, and consistency contract shared by the modes

| State | Required treatment |
| --- | --- |
| Committed conversation and encrypted reasoning | Immutable external documents, refs in entity/history, owner-authorized reads, frozen provider/model/API/SDK compatibility. Rebuild clients and messages on resume. |
| In-progress turn, HITL, timers, cancellation | Deterministic orchestration and bounded entity/receipt metadata; large checkpoints and question/answer bodies external. No worker must remain alive during a durable wait. |
| Files | Explicit artifact/object API by default. A reconstructed workspace is an optional bounded contract, not the Functions working directory or the deployed application ZIP. |
| Tool code and packages | Immutable deployment or sandbox package/image, content/version pinned per run. Activity replay after deployment must not silently execute changed code. |
| Environment and credentials | Reacquire from approved runtime identity/configuration; do not place tokens, connection strings, or raw environment dictionaries in Durable history or checkpoints. |
| Memory, subprocesses, handles, sockets, caches | Recreate or use application-specific continuation protocols. Neither DTS nor file archives preserve them. Retained disk is not a live-process checkpoint. |

Use an authorized **application/environment + owner + agent + session**
namespace for admission, content refs, workspace refs and execution mappings.
Bind run ID, turn generation, execution profile, code/package version,
catalog/policy hash and request hash to each activity request. An identifier
hash is not authentication or authorization.

One active mutating turn per logical session should advance the expected
generation once. Parallelism is allowed only for operations explicitly
classified safe for it. Session fencing must reach the external writer or
executor: entity serialization alone cannot stop a stale process writing a
shared filesystem or calling a remote service.

For side effects, reuse a recorded completion when available; use a backend
idempotency key or transactional effect-plus-receipt where supported. After
an unresolved "effect may have happened" window, return an explicit ambiguous
outcome rather than repeat an unsafe write or claim success. A Blob receipt
written before/after a remote effect narrows the window but is not a
distributed transaction with that backend.

For Functions workspaces, a correct adapter needs per-attempt scratch
directories, safe extraction, absolute rooted paths or a child process's
working directory, immutable checkpoint upload, expected-parent commit,
and cleanup. Do not change process-global `cwd` for concurrent tools.
Avoid adding workspace checkpoint cost to every service-only activity.

For hybrid, do not import sandbox-designated customer code for discovery in
the privileged worker. Use a declarations-only validated manifest or isolated
discovery. Do not upload the whole deployed application ZIP if it includes
settings, credentials, controller-only code or unnecessary packages.

For whole-agent ACA, keep DTS/content-store/group-management credentials in
the controller. The collaborator proposes APIM-governed model/MCP access using
the sandbox workload identity plus short-lived, operation-scoped capabilities
that bind session, turn, segment, attempt, generation and cancellation epoch.
This requires a new broker/gateway policy implementation and qualification.
It should not be described as already present. Removing model keys from the
guest does not remove its model authority, and a shared group identity alone
cannot distinguish a current attempt from an orphaned old one.

## 5. What is reusable, and what is not implemented yet

All repository paths below are relative to the examined worktree.

| Source and symbol | Evidence and feasibility consequence |
| --- | --- |
| `docs\frds\0010-durable-agent-loop-spike.md`, sections 4.13-4.17 | Existing budgets, one-step reasoning contract, history/HITL rules, fault seams and go/no-go gates are reusable requirements, not automatically satisfied by a new execution mode. |
| `docs\frds\0011-focused-durable-chat-demo.md` | Demonstrates the private durable loop and execution evidence. Versioned orchestrators and provenance-specific activity names must stay replay-compatible. |
| `src\azure_functions_agents\experimental\durable_loop_activities.py`, `MafOneStepModelProvider` | Already accepts the generic `ClientManager`, rebuilds a declarations-only Agent, disables tool invocation and replays explicit MAF messages. Supports a Functions-first design. Does not prove arbitrary SDK/context-provider compatibility. |
| `src\azure_functions_agents\experimental\durable_loop_registration.py`, `_default_activity_runtime` | Currently hard-requires `HybridApimClientManager`, `ApimMafResponsesProvider` and the concrete execution plane. Functions-only is not enabled merely by deleting the ACA setting. |
| `src\azure_functions_agents\experimental\durable_loop_execution.py`, `DurableExecutionPlaneRouter`, `build_durable_execution_plane` | Current builder requires APIM MCP and ACA configuration; LOCAL provenance routes to ACA. Refactor placement independently from provenance while preserving the frozen binding. |
| `src\azure_functions_agents\experimental\durable_loop_tools.py`, `DurableToolDispatchPort`, `DurableToolCatalogPort` | Useful common provider seams. `RegistryToolDispatcher` uses `InMemoryToolEffectLedger` and is prototype/test infrastructure, not a production direct-execution lane. |
| `src\azure_functions_agents\experimental\durable_loop_mcp.py`, `DurableRemoteMcpLane.dispatch` | Persistent started/result receipts and explicit ambiguous mutations are reusable patterns for direct adapters. Do not claim all existing writes have transactional exactly-once semantics. |
| `src\azure_functions_agents\discovery\tools.py`, `discover_project_tools`, `_tool_module` | Existing trusted tool discovery imports Python modules. Can support direct trusted tools; must not be reused unmodified to inspect untrusted/sandbox-only packages in Functions. |
| `src\azure_functions_agents\experimental\durable_loop_registration.py`, `_provenance_tool_activity_name`, `apply_session_entity_operation` | v3 LOCAL calls currently select a sandbox-named activity. Keep old versions; introduce a new placement-aware contract rather than reinterpret old histories. Reuse admission/commit/HITL logic. |
| `src\azure_functions_agents\experimental\durable_loop_http.py`, run metadata creation | Freezes agent/model/tool bindings, but the current entity key uses owner hash plus session ID. A public multi-agent design must explicitly include the application/agent namespace and migration rules. |
| `src\azure_functions_agents\experimental\durable_loop_config.py`, `validate_durable_loop_application` | Private mode rejects `session_runtime`, declared triggers, subagents, Dynamic Sessions, Dynamic Workflows and executable skills. The whole-agent and durable flags cannot just be combined, nor can the spike be advertised as full runtime parity. |
| `src\azure_functions_agents\execution\aca_sandbox.py`, `AcaSandboxExecutionBackend`; `harness\__main__.py`, `harness\atomic_commit.py` (collaborator trace) | Existing full-agent start/status/events/cancel, run-journal adoption, ordinary MAF execution and local atomic commit are reusable. They are not yet integrated with the Durable entity or external segment checkpoints. |
| `samples\durable-agent-loop-spike\infra\main.bicep` and `infra\modules\*.bicep` | Private, environment-specific composition includes shared APIM, ACA, model, storage, DTS, identities and monitoring. A managed default needs new conditional composition and least-privilege review, not deployment of this template unchanged. |

APIM is a current **spike dependency**, not a requirement imposed by DTS.
Removing it from the default requires a qualified model provider path and
remote-service authentication/error/telemetry behavior. Background response
start/poll/cancel must remain pinned to the accepting backend; do not replace
the APIM provider with the foreground adapter and claim equivalent long-running
reasoning support without qualification.

## 6. Minimal setup and customer-owned sandbox onboarding

| Resource or responsibility | Functions-only default | Additional work for optional sandbox execution |
| --- | --- | --- |
| Functions host and immutable app deployment | Required; platform can provision the chosen hosting plan and deployment storage. | Still required for the durable control plane and activity adapters. |
| DTS scheduler/task hub | Required for this proposal; use an app/environment trust boundary, not a task hub per session. | Same hub and orchestration contract. Sandboxes need not become DTS workers. |
| Host storage and external content | Required. Separate host/deployment/content uses logically; storage-account consolidation is a policy/least-privilege decision. | Add package/artifact/lease/receipt storage where not already present. |
| Model endpoint, quota, identity | Reuse an approved provider or provision one with authorization. Model calls are not free or permissionless. | Hybrid keeps this in Functions; whole-agent must provide appropriately scoped model access inside ACA or through a broker. |
| Tool/connector credentials | Scoped to approved service operations; do not persist tokens in Durable DTOs. | Give tools only their required identity/capabilities. A group ID alone does not authorize controller access. |
| Monitoring, retention, purge, budgets | Platform defaults plus customer policy. DTS history retention is not a complete conversation-retention strategy. | Add capacity, create/attach/resume, workspace, stale-generation, cleanup and orphan metrics. |
| Networking and regional capacity | Functions, DTS, storage, and model connectivity must all work in the chosen region/policy. | Validate sandbox service availability, network reach, registry/package access, capacity and supported lifecycle/runtime features. |

The desired customer experience can be: deploy the agent with defaults; for
isolated executable tools, explicitly select that execution policy, select an
authorized existing Sandbox Group, and complete a guided
compatibility/permission check. A group is an available capability, not an
instruction to relocate all execution. The platform owns the runtime protocol
and lifecycle logistics. The customer owns its group policies, capacity
decisions and permissions.

Use explicit deployment-level execution policy, frozen per admitted run.
The exact configuration schema is not designed here. A configured-but-invalid
or unavailable sandbox **must fail closed**, not silently fall back to
privileged Functions execution. Customer sandbox failure must not cause the
runtime to rewrite the customer's policies or increase its own privileges.

Microsoft's current identity guidance makes task-hub-scoped RBAC possible;
worker-only services can use the Durable Task Worker role. The same app also
starting/controlling runs may require the broader data-contributor role.
Assigning a narrower SDK client inside the same Function App is not a
privilege boundary between its activities.

## 7. Implementation and feasibility gates

This is a proposed sequence for a follow-on approved prototype/feature, not
work performed by this research session.

| Phase | Deliverable | Exit condition |
| --- | --- | --- |
| 0. Product contract | Agree trusted-tools default, execution isolation promise, supported provider/MAF/runtime surface, and logical-session semantics. | Leadership understands that zero sandbox setup does not mean arbitrary-code isolation or implicit local workspace continuity. |
| 1. Common execution contract | Preserve the one-step orchestrator, external content/receipts, authorization, budgets, and versioned placement binding. Define executor capability requirements separately from tool provenance. | Existing histories keep their meaning; unavailable/mismatched execution profiles fail closed. |
| 2. Functions-only vertical slice | Trusted local tool adapter; persistent receipt handling; qualified model/service paths without mandatory ACA/APIM; minimal provisioning. | Real model -> direct tool -> next model -> final commit works across worker replacement with zero sandbox calls. No production use of in-memory receipts. |
| 3. Default-mode hardening | Identity isolation between apps, per-owner authorization, message/argument bounds, packaging, synchronous-tool/concurrency behavior, timeouts, cancellation and deployment compatibility. | Fault and load cases below pass; supported authoring surface and non-goals are documented. |
| 4. Optional hybrid provider | Reuse ACA tool execution, artifact chain, capacity fencing and lifecycle management; add BYO group preflight and strict no-fallback policy. | Same logical traces under direct and sandbox placement where tool semantics match; zero sandbox-tool imports in privileged workers; bounded cleanup and recoverable workspace replacement. |
| 5. Whole-agent decision gate | Validate a concrete customer requirement not met by hybrid; choose opaque-turn versus stepwise semantics explicitly. | Approve only with measured benefit and an honest checkpoint contract. Full agent isolation is valuable only if its runtime/auth/network constraints are actually met. |

For any production feature, follow the repository's full FRD, architecture
sign-off, implementation, testing and documentation lifecycle. Existing
spike evidence does not waive those gates.

### Required correctness and recovery cases

- One user turn with multiple model steps and multiple tool calls; automatic
  tool invocation remains disabled in the fine-grained modes.
- Worker loss after a persisted model result, tool result, append checkpoint,
  and final commit; acknowledged work is reused, not reissued.
- Loss during a tool effect: a supported idempotent write has one logical
  effect, while an unreconcilable write exposes ambiguity. Test at least the
  100 ambiguity-window trials called for in FRD 0010.
- Concurrent submissions to the same session, including the FRD's 25-submit
  case, plus different owner/agent/session combinations: one active mutation,
  one expected-generation commit, and no cross-scope result or artifact reads.
- HITL before-wait delivery, duplicate/stale/conflicting answers,
  answer/timeout/cancel races, host replacement while parked, and
  continue-as-new boundaries.
- No worker-local state dependency: next tool/model step runs in a different
  process; credentials are reacquired; an explicit artifact restores exactly
  or fails rather than silently starting empty.
- Deployment/model/package mismatch fails or follows an explicitly qualified
  migration/rehydration path; encrypted reasoning and call IDs are preserved.
- Direct mode creates no sandbox and does not require APIM for a supported
  direct provider. Required-sandbox mode does not fall back on group denial,
  quota exhaustion, policy mismatch, bootstrap failure or timeout.
- Required-sandbox code is not imported in Functions during discovery or
  execution; sandbox receives neither controller credentials nor unrelated
  application files.
- Capacity waits, cancellation, stale executors, Disk resume and missing
  sandbox replacement are bounded. Group inventory/cleanup reflects owned
  resources only; do not delete customer-owned unrelated sandboxes.
- Default history/status/metrics contain refs and bounded metadata, not raw
  prompts, arguments, output, tokens, authorization-bearing URLs or secrets.

### Benchmark before making a speed or savings promise

Use the same deployment region, model/backend, tool input/output sizes, package
version and concurrency. Compare no-op, typed HTTP, bounded file workspace,
and representative code workloads. Separate warm app, cold app, warm retained
sandbox, cold sandbox, resumed disk, and replacement-from-Blob cases.

Measure the end-to-end user turn as well as activity dispatch/queue,
model/backend, receipt/content I/O, tool body, sandbox admission/bootstrap,
artifact transfer, active compute, parked duration, recovery and cleanup.
Record sample counts and actual traces, then report p50/p95/p99 where the
sample size supports them. Include concurrency 1 and 10, and a bounded
saturation case with the configured capacity headroom.

The pass condition for the performance claim is a reproducible improvement
for the stated workload without weakening its durability/isolation contract.
Set a public latency objective only after that baseline. A local ZIP timing
or sandbox-only execution span is not a proxy for an entire Durable tool call.

## 8. What the existing measurements support

No matched Functions-only versus whole-agent versus hybrid benchmark has been
run. These are existing ACA measurements from distinct windows, not results
for the proposed new modes.

| Measurement window | Relevant observations | Decision use |
| --- | --- | --- |
| Early isolated per-call probes | Create p50 1,056.2 ms; tool 176.5 ms; export 77.3 ms; explicit delete 5,873.3 ms. | Per-call provisioning has real overhead. Delete belongs to the measured lifecycle, not automatically every product critical path. |
| Early retained probe | One create 2,232.9 ms; three tool calls p50 99.0 ms with preserved workspace. | Retention amortizes create cost. Three calls do not establish production tails or SLA. |
| Early C10 create wave | Create p50 4,432.5 ms / p95 4,713.5 ms; 10/10 successful; 12.014 s wall. | Capacity/concurrency affect create latency; do not substitute isolated p50 for load behavior. |
| Later App Insights values supplied by creator | Create p50/p95 915/1,924 ms; execute 56/258 ms; workspace write 20/140 ms, read 14/118 ms; cleanup 6,910/6,971 ms. Initial sample averages: capacity wait about 85 ms, export about 180 ms, restore about 157 ms. | Useful component baselines for hybrid, not direct-Functions or whole-agent timings. |
| Leadership `latest-metrics.json` window | Create 906/1,924 ms; execute 50/175 ms; model request 1,779.04/4,164.0944 ms; Durable model step 2,388.701/5,031.8039 ms. | Model latency is already a substantial part of the workload. Avoid promising dramatic end-to-end savings from removing only executor overhead. |

Do not sum component percentiles into a turn percentile, subtract unpaired
component medians to claim measured overhead, or interpret client video/polling
timing as a service SLA. Retained attach/resume/executor restart was not
isolated as a percentile. Whole-agent cold bootstrap includes different code
and dependencies; tool-only sandbox measurements do not measure it.

The earlier local workspace experiments demonstrate explicit file/data
reconstruction, not a real DTS/MAF fault-recovery integration. They show that
archive/extract CPU and file count can matter even without a network:
8 MiB/128 files had median archive 85.900 ms and extract 217.578 ms;
31 MiB/496 files had 333.247 ms and 834.115 ms, respectively. These are
Windows-local component measurements with only three and two samples.

Method caveats for the earlier evidence:

- Fields named `checkpoint_plus_restore_p50` in the earlier summary are
  sums of component medians, not independently measured end-to-end percentiles.
- The 8,192-file stress workload omits directory entries from its benchmark
  archive; the actual exporter counts them against its 8,192-member cap.
  It is metadata-stress evidence, not proof the case fits the runtime contract.
- The process-loss script uses controlled process exit and a synthetic module
  for runtime-only package loss. Some process-state observations are modeled
  assertions, not abrupt cloud-crash measurements.
- Azure Blob transfer ranges in those artifacts are estimates only. No Azure
  storage benchmark was run by this session.

Cost needs the same discipline. Current DTS documentation bills scheduler
and compute separately; Consumption bills actions including activity
dispatch and processing the result, while Dedicated uses capacity units.
Fine-grained checkpoints add actions, content operations and replay work.
Functions-only avoids ACA lifecycle/capacity charges, but not model tokens,
Functions compute, DTS, storage or telemetry. A model request still occupies
activity compute while awaited unless a supported start/poll/timer pattern is
used. Hybrid/whole-agent add sandbox compute and retained-capacity/storage
costs according to the actual service/lifecycle configuration.

For a cost comparison, price observed scheduler actions, Functions active
compute and any warm baseline, sandbox active/retained usage, storage
operations/bytes, model tokens, network and telemetry using the target region
and SKU. No financial savings percentage is established by this evidence.

## 9. Evidence and limitations

Local source is the implementation evidence. FRD 0010 describes candidate
defaults and gates; it is not proof every proposed public surface is shipped.
For example, current Function App settings and recorded live execution may
differ from earlier FRD candidate timeouts.

Public Microsoft references reviewed for this analysis:

- [DTS billing](https://learn.microsoft.com/en-us/azure/durable-task/scheduler/durable-task-scheduler-billing):
  scheduler/compute separation, action accounting, SKU capacity and retention.
- [DTS managed identity](https://learn.microsoft.com/en-us/azure/durable-task/scheduler/durable-task-scheduler-identity):
  app/worker roles, managed identity setup and task-hub-scoped RBAC.
- [Functions hosting, timeouts and scaling](https://learn.microsoft.com/en-us/azure/azure-functions/functions-scale):
  plan-specific limits, cold starts and shared scaling of Durable triggers.
- [Durable Functions performance and scale](https://learn.microsoft.com/en-us/azure/durable-task/durable-functions/durable-functions-perf-and-scale):
  worker placement, concurrency, replay/caching and provider-specific behavior.
- [App Service and Functions managed identity](https://learn.microsoft.com/en-us/azure/app-service/overview-managed-identity):
  identity attachment to the app/slot rather than individual tool activities.

Service limits, regional support and preview APIs must be requalified for
the intended public launch. Functions hosted on Container Apps, ACA Dynamic
Sessions pools, and the spike's ACA Sandbox Groups are not interchangeable
resource or lifecycle contracts.

Prior session artifacts, unchanged:

- `durable-without-aca.md` and `comparison-table.md`: the earlier A/B/C/D
  state-storage and continuity analysis, not this three-mode product decision.
- `measured-simulated-results.json`,
  `workspace-checkpoint-benchmark-results.json`,
  `durable-workspace-simulation-results.json`: historical local/ACA evidence;
  read with the measurement qualifications above.
- `durable_workspace_simulation.py` and `workspace_checkpoint_benchmark.py`:
  local illustrative prototypes, not production executors.

Leadership metrics source:
`C:\Users\larohra\.copilot\session-state\26545db2-8bd8-4a0c-a578-10af56815eed\files\durable-loop-leadership-demo\evidence\latest-metrics.json`.

## 10. Joint conclusion and remaining product choice

The independent analyses agree on the primary decision: **one product,
Functions/DTS trusted-tool default, explicit pinned placement, and no
three-mode co-equal GA commitment**. Both select Functions-only if only one
route is funded for the minimal-setup objective.

The optional ACA sequencing depends on which requirement leadership values:

| Requirement after the default launch | Prefer |
| --- | --- |
| Keep every tool as a DTS activity and separate executable code from model/connector authority | Hybrid (option 3). This report's recommendation for the current conversation's checkpointing requirement. |
| Preserve ordinary agent/SDK extensibility and rich in-sandbox agent workspaces, accepting coarser recovery and shared guest authority | Explicit whole-agent ACA advanced preview (option 2). The collaborator's preferred optional mode for compatibility/UX return. |
| Both all agent-side execution inside customer policy and per-tool DTS checkpointing | A separately qualified all-in-ACA stepwise profile; no first-launch commitment. |

Do not sell whole-agent placement as a free safety upgrade or hybrid as full
agent-runtime isolation. Their trust and recovery boundaries are different.
The practical leadership pitch is:

> Durable agent execution with no mandatory sandbox setup: trusted service
> agents run on Functions, with checkpointed model/tool activities and logical
> session continuity. Customers needing isolated executable tools can opt into
> their own sandbox group without changing the durable control plane.

Collaboration evidence, read rather than re-traced in this worktree:

- `C:\Users\larohra\.copilot\session-state\b2b53e19-0d76-496d-9e2a-a762fb769c8b\files\whole-agent-aca-leadership-analysis.md`
- `C:\Users\larohra\.copilot\session-state\b2b53e19-0d76-496d-9e2a-a762fb769c8b\files\whole-agent-durable-failure-matrix.md`

The collaborator's detailed design also remains in
`whole-agent-in-aca-durable-loop.md` in that directory. Its proposed protocol
and acceptance gates are not implementation evidence. For reused local
benchmark values, the measurement qualifications in section 8 of this report
take precedence over historical "checkpoint + restore p50" shorthand.
