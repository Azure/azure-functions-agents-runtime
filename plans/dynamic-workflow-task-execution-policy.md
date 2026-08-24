# Implement bounded Dynamic Workflow task execution policy

This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`,
`Decision Log`, and `Outcomes & Retrospective` must be kept current as work proceeds.
Maintain this document in accordance with `PLANS.md` at the repository root.

## Purpose / Big Picture

After this change, a Dynamic Workflow author can put a bounded execution policy on a
`tool` or `sub_agent` task. A policy can enforce an Activity-attempt timeout, retry
explicitly retryable failures with deterministic Durable timers, and convert an exhausted
execution failure into a dependency-satisfying result when `continue_on_error` is true.
Python tool authors can declare authoritative timeout and retry behavior on
`@workflow_tool`, including `max_attempts=1` to prohibit an LLM-authored DAG from retrying
a side-effecting handler. Handlers can read a stable idempotency key and attempt context so
external side effects can be deduplicated under Durable Functions' at-least-once delivery.

The behavior is visible without reading implementation details. Schema tests accept and
reject the documented policies, engine tests show a transient handler failing twice and
succeeding on the third attempt with two deterministic timers, status output uses
`schema_version: 3` while retrying, and a continued `for_each` failure produces an ordered
partial result that unlocks an explicitly conditioned downstream task. Existing plans
without `execution` and without decorator policy metadata retain their current serialized
payloads, scheduler path, and status versions.

## Progress

- [x] (2026-08-24 00:10Z) Read the updated `AGENTS.md`, all of `PLANS.md`,
  `docs/architecture.md` sections 2 and 3, and the complete FRD extension.
- [x] (2026-08-24 00:12Z) Completed independent schema/metadata and Durable-engine
  implementation mapping against the current source and tests.
- [x] (2026-08-24 00:18Z) Recorded human approval as FRD Decision 80 and finalized the
  task execution policy extension.
- [x] (2026-08-24 00:30Z) Completed a final independent architecture review and resolved
  policy-aware routing, status v3, backoff rounding, error bounds, cancellation, Sub Agent
  timeout, async dual-decorator, and sync-timeout concurrency ambiguities in Decision 81.
- [x] (2026-08-24 00:40Z) Created this self-contained implementation plan.
- [x] (2026-08-24 00:43Z) Established a focused baseline: 226 workflow schema,
  discovery, registry, engine, and UI tests passed in 4.14 seconds.
- [x] (2026-08-24 01:10Z) Implemented public policy models and errors, decorator
  metadata, async discovery, immutable registry metadata, authoritative precedence,
  start-time resolution, Durable serialization, and exports. The focused Milestone 1
  suite passed 245 tests; ruff and strict mypy passed.
- [x] (2026-08-24 01:30Z) Implemented Activity-owned sync/async deadlines, outcome
  envelopes, sanitization, idempotency context, and policy-aware Sub Agent execution.
  The focused Activity/context suite passed 56 tests; ruff and strict mypy passed.
- [x] (2026-08-24 02:15Z) Implemented persisted retry state, precomputed backoff
  timers, continued-failure behavior, cancellation, and policy-aware scheduler
  routing. The focused workflow suite passed 325 tests; ruff and strict mypy passed.
- [x] (2026-08-24 02:40Z) Implemented and tested status schema version 3, UI
  rendering, and Activity-delivery telemetry. The expanded focused suite passed
  367 tests; ruff and strict mypy passed.
- [x] (2026-08-24 03:15Z) Passed the independent testing review after adding a
  deterministic incident-triage policy plan plus coverage for revocation between
  attempts, duplicate delivery, and one-time `when` evaluation. The targeted sample
  and engine suite passed 110 tests.
- [x] (2026-08-24 03:45Z) Updated architecture and user documentation across
  `docs/workflows.md`, `docs/architecture.md`, `docs/front-matter-spec.md`,
  `docs/index.md`, and `README.md`; regenerated the unchanged front-matter reference,
  invoked the schema-doc synchronization workflow, and passed `mkdocs build --strict`.
  `docs/getting-started.md` did not change because execution policy is optional and
  does not alter the minimal quickstart.
- [x] (2026-08-24 04:00Z) Passed the complete repository gate: Ruff reported no
  findings, strict mypy reported no issues across 42 source files, and the
  CI-equivalent coverage run passed 1,153 tests with 52 E2E tests deselected
  after final-review regressions were added.
- [ ] Complete the final branch review and push the implementation branch.

## Surprises & Discoveries

- Observation: The current engine already has two schedulers. `_run_static_workflow()` uses
  wave-based execution and a status string; `_run_dynamic_workflow()` uses typed persisted
  state and deterministic helper phases for `when` and `for_each`. Policy-aware plans must
  extend the dynamic scheduler rather than add a third scheduler.
  Evidence: `src/azure_functions_agents/workflows/engine.py` defines
  `_run_static_workflow()` at line 143 and `_run_dynamic_workflow()` at line 892.

- Observation: Async workflow handlers are deliberately rejected today even though the
  Activity entry point is asynchronous.
  Evidence: `tests/test_workflow_registry.py:test_register_workflow_tool_rejects_async_handler`
  captures the current behavior and must be replaced by acceptance tests.

- Observation: A timed-out synchronous Python handler cannot be forcibly stopped. Returning
  a timeout outcome releases the Durable Activity slot while the worker thread may still run.
  Evidence: FRD Decision 81 now distinguishes `MAX_PARALLELISM`, which bounds dispatched
  Durable attempts, from operating-system threads that survive a wrapper deadline.

- Observation: Durable orchestrator cancellation cannot signal an Activity already running
  on another worker. Only the Activity's own deadline can cooperatively cancel an async
  handler.
  Evidence: FRD Decision 81 removes the previous unimplementable active-Activity cancellation
  claim and requires late results to be ignored.

- Observation: The focused pre-change workflow baseline is fast enough to run after every
  scheduler milestone.
  Evidence: The five-module command in `Concrete Steps` completed with `226 passed in
  4.14s`.

- Observation: Existing Sub Agent timeouts are floats in seconds and may not resolve to an
  exact millisecond. Rejecting those values during app composition would break policy-free
  applications.
  Evidence: `WorkflowPlanPolicy` now stores a floor-normalized millisecond bound, while the
  execution resolver applies the new minimum only when a task is policy-aware.

- Observation: A handler may raise Python `TimeoutError` independently of the wrapper-owned
  deadline. Classifying every such exception as a retryable attempt timeout can duplicate
  side effects.
  Evidence: The Activity wrapper now checks `asyncio.Timeout.expired()` and treats an
  unexpired handler `TimeoutError` as terminal `execution_unknown`.

- Observation: Durable `task_all` can complete on the first failed child rather than after
  every dispatched sibling reaches a terminal state. Applying its children immediately can
  mistake pending results for handler-contract failures.
  Evidence: The structured scheduler now races individual dispatched tasks and drains the
  complete wave before applying outcomes in stable node/instance order.

- Observation: Activity-boundary exceptions are indistinguishable from infrastructure
  failures after Durable serialization. Authorization and malformed-input failures therefore
  need explicit nonretryable outcomes inside policy-aware Activities.
  Evidence: Policy-aware authorization and input-contract failures now return strict,
  sanitized outcomes; only an invocation with no runtime outcome enters infrastructure retry.

- Observation: Policy-aware status must count blocked non-iterated nodes before their
  scheduler instances exist, while unexpanded `for_each` nodes have no executable units yet.
  Evidence: Version 3 derives its pending/materialized base from persisted normal tasks and
  adds only actually materialized iteration instances; version 2 keeps its prior accounting.

- Observation: Workflow task telemetry belongs inside the Activity delivery rather than the
  orchestrator.
  Evidence: Policy-aware Activities emit one bounded start/completion pair and span per actual
  delivery, including cancellation, while replayed scheduler decisions emit no metrics.

- Observation: The existing incident-triage sample is the smallest user-visible surface that
  already exercises the structured scheduler and `for_each`.
  Evidence: `scripts/policy-demo-plan.json` now deterministically combines two transient
  failures then success, exhausted async timeout, continued per-item failure, and conditioned
  recovery without provider faults.

- Observation: The first final branch review found that policy-aware fail-fast envelopes
  omitted `attempts`/`kind`, a resolved Sub Agent timeout below one second could persist an
  engine-invalid policy, and the README retry example omitted the required maximum delay.
  Evidence: Regression tests now require fail-fast diagnostics and reject subsecond specialist
  timeouts at submission; the public example constructs a complete bounded backoff policy.

- Observation: Re-review found that terminal fail-fast instances retained their pre-result
  `running` state in status v3, leaving the terminal counts inconsistent with the failed
  output.
  Evidence: Fail-fast instances now enter terminal `failed` state before status publication;
  v3 counts and the built-in UI expose a matching `failed` bucket.

- Observation: A subsequent review found that multiple failures drained from one wave updated
  every instance state but only the selected terminal failure's logical node state.
  Evidence: Each fail-fast outcome now marks its logical node failed before the deterministic
  lowest-ordered failure publishes terminal status; a two-node same-wave regression covers it.

## Decision Log

- Decision: Use the presence of authored `execution`, even an empty object, or any
  decorator timeout/retry declaration as the policy-aware switch.
  Rationale: Presence is stable before defaults resolve and unambiguously selects the
  structured scheduler, bounded timeout admission, and status schema version 3.
  Date/Author: 2026-08-23, Agent architecture review; recorded as FRD Decision 81.

- Decision: Resolve `timeout` and `retry` independently, with a declared decorator field
  authoritative over the corresponding DAG field. Select `retry` as one whole object rather
  than merging nested fields.
  Rationale: Tool authors know whether side effects are retry-safe. Omission delegates that
  field to the DAG; explicit `max_attempts=1` forbids automatic DAG retry.
  Date/Author: 2026-08-20, Human (TsuyoshiUshio); FRD Decision 68.

- Decision: Precompute retry delays at submission with `Decimal` created from the canonical
  multiplier string, `ROUND_FLOOR` to integer milliseconds, and a final maximum-delay cap.
  Persist the entire delay sequence with the effective policy.
  Rationale: The orchestrator must never depend on platform floating-point exponentiation
  during replay, and admission must use the exact same delays that execution uses.
  Date/Author: 2026-08-23, Agent architecture review; FRD Decision 81.

- Decision: Keep policy-free payloads and scheduler behavior unchanged instead of
  retroactively applying the new ten-minute tool deadline.
  Rationale: Existing workflow tools may intentionally run under only the Functions host
  limit; changing them would be an undocumented compatibility break.
  Date/Author: 2026-08-19, Agent architecture review; FRD Decisions 68, 71, and 79.

- Decision: Return sanitized application outcomes from capability-bearing Activities, but
  keep authorization and contract failures non-continuable. Treat an Activity invocation
  exception with no returned outcome as infrastructure failure in the orchestrator.
  Rationale: Selective retry requires the orchestrator to distinguish expected handler
  outcomes from Durable delivery/worker failures while capability revocation must fail
  closed.
  Date/Author: 2026-08-19, Agent architecture review; FRD Decisions 70, 73, 74, and 78.

## Outcomes & Retrospective

Planning and Milestones 1-3 are complete. Authors can construct and export strict retry,
backoff, execution, and classified-error types; `@workflow_tool` preserves authoritative
declarations for sync and async handlers; and `start_workflow` persists an effective policy
only for policy-aware tasks. Policy-aware Activities now publish frozen attempt context to
sync, async, and sync-returning-awaitable handlers, enforce one Activity-owned deadline, and
return sanitized outcomes. Policy-free task inputs and Activity behavior remain unchanged.
The structured scheduler routes every policy-aware plan, uses independent Durable backoff
timers and stable idempotency across attempts, applies same-wave outcomes deterministically,
and supports exact continued-failure results for normal and `for_each` tasks. Status v3 now
reports executable-unit counts and bounded attempt state, the built-in UI renders all three
status versions, and Activity telemetry records actual deliveries without replay metrics.
The independent testing checkpoint now passes, including the deterministic sample mode and
cross-attempt authorization/redelivery regressions. Architecture, workflow authoring,
front-matter cross-reference, landing-page, README, and deterministic sample documentation now
describe the implemented contract, and the strict MkDocs build passes. Ruff, strict mypy, and
the complete CI-equivalent non-E2E coverage suite pass. Only final branch review and publication
remain.

## Context and Orientation

This repository turns markdown agent declarations and project files into an Azure Functions
application. Dynamic Workflows use Azure Durable Functions, whose orchestrator is a
deterministic generator that replays its history. An Activity is a separately delivered
function invocation used for non-deterministic or side-effecting work. Activities are
delivered at least once: a worker can complete an external side effect but fail before
Durable records completion, causing redelivery. Therefore retry support must expose
idempotency information and must not claim exactly-once execution.

`src/azure_functions_agents/_function_tool.py` defines `@tool`, `@workflow_tool`,
`WorkflowToolMetadata`, and `WorkflowTool`. Discovery in
`src/azure_functions_agents/discovery/tools.py` imports project `tools/*.py` modules and
returns normal tools and workflow Activity candidates. Discovery is read-only: it records
metadata but does not apply an agent's policy.

`src/azure_functions_agents/workflows/registry.py` freezes discovered handlers into
`WorkflowToolEntry` values and a `WorkflowHandlerCatalog`.
`src/azure_functions_agents/workflows/integration.py` builds the complete handler catalog
and immutable per-agent `WorkflowPlanPolicy` catalogs before Azure registration. This is
where decorator declarations become immutable registration metadata; registration must not
re-parse markdown or YAML.

`src/azure_functions_agents/workflows/schema.py` defines the Pydantic `WorkflowTask` and
`WorkflowPlan`, typed persisted payload contracts, plan validation, template resolution,
condition evaluation, and `plan_to_activity_inputs()`. Add the public retry/backoff/execution
models here unless implementation shows that a smaller dependency-free public module avoids
an import cycle. An effective policy is a start-time, validated, JSON-safe object containing
the chosen timeout in integer milliseconds, the chosen retry policy, the precomputed
integer-millisecond retry delay sequence, and task-local `continue_on_error`. Public authored
models must not expose internal persisted-only fields.

`src/azure_functions_agents/workflows/engine.py` owns the Durable blueprint. The static
scheduler `_run_static_workflow()` handles plans without `when`, `for_each`, or execution
policy and must remain byte-for-byte compatible in observable payload and status behavior.
The structured scheduler `_run_dynamic_workflow()` owns deterministic materialization,
dispatch, result application, cancellation restoration, and status. Its state uses
`TypedDict` contracts rather than Pydantic models because orchestrator replay consumes
already-validated JSON.

The structured scheduler materializes one executable unit for a normal task and one unit per
source position for a `for_each` task. Each unit must own independent attempt state,
effective policy, last failure, next retry deadline, and idempotency key. `when` is evaluated
once before the first attempt. Retry must reuse frozen resolved arguments, target, and
idempotency key.

`src/azure_functions_agents/workflows/context.py` currently owns workflow/session identity
and Durable instance IDs. Add the public immutable `WorkflowTaskContext` and a `ContextVar`
accessor here. The key format is
`af-wf-task-v1:<sha256(length-delimited workflow_id, node_instance_id)>`. Length-delimited
means hashing each UTF-8 value's byte length followed by its bytes, preventing ambiguous
concatenations. Attempt number is deliberately excluded so retries and redelivery share a
deduplication key.

`src/azure_functions_agents/runner.py:run_leaf_agent_task()` executes a stateless Workflow
Sub Agent. A policy-aware Sub Agent timeout is at most
`min(resolved_agent_timeout, PT10M)`; an explicit task value above that bound is rejected and
a shorter value is allowed. A policy-free Sub Agent keeps its existing unclamped resolved
timeout.

`src/azure_functions_agents/workflows/tools.py` validates a submitted plan under the
captured owner policy and starts Durable orchestration. Effective policy resolution must
happen before start, while the immutable handler catalog and resolved Sub Agent catalog are
available. Persist the result so replay never reads changed decorator metadata. Every
Activity attempt still reauthorizes against the currently deployed owner policy.

`src/azure_functions_agents/public/index.html` renders workflow status. Existing string
status is schema version 1 and dynamic object status is schema version 2. Version 3 is
version 2 plus execution states and fields. Consumers must continue accepting all three.

The authoritative behavior is recorded in
`docs/frds/0004-dynamic-workflows.md`, Decisions 67 through 81. The most important fixed
bounds are one through five attempts, an effective policy-aware timeout from one second
through ten minutes, an initial delay through five minutes, a multiplier from 1.0 through
10.0, a maximum delay through fifteen minutes and not below the initial delay, and at most
one hour of configured attempt deadlines plus delays.

## Plan of Work

### Milestone 1: Public contracts, metadata, and start-time resolution

Add strict public models `WorkflowRetryBackoff`, `WorkflowRetryPolicy`, and
`WorkflowTaskExecution` in `src/azure_functions_agents/workflows/schema.py`. Preserve
Pydantic's existing `extra="forbid"` behavior. Reuse the module's ISO-8601 parsing helpers,
but require authored policy durations to resolve to whole milliseconds. Validate all fixed
bounds, require backoff when `max_attempts > 1`, prohibit unnecessary backoff when
`max_attempts == 1` if that is the existing strict-schema convention, and calculate the
exact one-hour admission total from the persisted delay sequence.

Extend `WorkflowTask` with an optional `execution` field that is omitted from dumps when not
authored. Reject it on `wait` tasks. Preserve whether the key was authored so `execution: {}`
is policy-aware even though field defaults are ordinary values.

Add `WorkflowRetryableError` and `WorkflowTerminalError` in a dependency-safe public module
and export them from `src/azure_functions_agents/__init__.py`. Codes must match
`^[a-z][a-z0-9_]{0,63}$`, reject the `workflow_` prefix, and messages must follow the FRD's
control-character, whitespace, empty-message, and 256-code-point rules.

Extend `@workflow_tool` overloads and `WorkflowToolMetadata` with optional `timeout` and
`retry`. Metadata declarations are authoritative per field. Both `def` and `async def`, and
both `@tool`/`@workflow_tool` orders, must be discoverable. Remove the registry's async
rejection and freeze the metadata on `WorkflowToolEntry`.

Implement one start-time resolver used by plan submission. For tools, choose decorator
timeout or task timeout or the ten-minute policy-aware fallback, and independently choose
decorator retry or task retry or one attempt. For Sub Agents, choose task retry or one
attempt and enforce the specialist timeout bound. Return a JSON-safe internal effective
policy with normalized timeout and precomputed delays. Do not serialize anything for
policy-free tasks.

This milestone is complete when focused schema, discovery, registry, integration, export,
and plan-submission tests pass and existing policy-free serialization assertions remain
unchanged.

### Milestone 2: Activity invocation, context, and outcomes

Add frozen `WorkflowTaskContext` and `current_workflow_task_context()` in
`src/azure_functions_agents/workflows/context.py`, plus private token set/reset helpers for
the Activity wrapper. Its deadline is timezone-aware UTC. Propagate the context across
normal async awaits and into the executor context used for sync handlers, and always reset
it in `finally`.

Define strict internal `TypedDict` inputs and outcomes in
`src/azure_functions_agents/workflows/engine.py`. A successful capability-bearing Activity
returns an outcome with the task identity and JSON-safe result. Expected execution failures
return a sanitized failure containing `error_code`, public `error`, `kind`, `retryable`, and
`continuable`. Authorization, malformed persisted input, missing catalog entries, and
non-JSON handler results remain terminal and non-continuable.

Implement one invocation helper that calls the handler exactly once. If the direct return is
awaitable, await it. Run a synchronous callable in the event loop executor under the same
deadline. An async timeout cooperatively cancels the await; a sync timeout stops waiting but
cannot stop the thread. Log raw unexpected exceptions through the shared logger and return
only the generic sanitized public failure. Let Python cancellation used by the host propagate
after telemetry cleanup rather than disguising it as a handler result.

Wrap `run_leaf_agent_task()` with the effective Activity deadline and the same task context.
Retain the fixed `{agent, text}` success result. Every delivered attempt performs current
policy/catalog authorization before invoking either target.

This milestone is complete when direct Activity tests prove sync, async, sync-returning-
awaitable, timeout, context cleanup, error sanitization, JSON-contract rejection, Sub Agent
deadline, and authorization behavior.

### Milestone 3: Deterministic retry and continued failure

Route any plan containing `when`, `for_each`, or a policy-aware task into
`_run_dynamic_workflow()`. Keep `_run_static_workflow()` untouched for policy-free static
plans.

Extend the structured scheduler's persisted task, instance, and mutable state contracts with
optional execution fields. Initialize an executable unit's next attempt as one. Dispatch
only when no backoff timer is pending and capacity is available. Put attempt, max attempts,
deadline, idempotency key, and frozen effective policy into each Activity input.

When an Activity returns a retryable outcome and attempts remain, store the last failure,
store the current attempt, set state to `retry_wait`, derive an absolute deadline from
`context.current_utc_datetime` plus the corresponding persisted delay, and schedule a
Durable timer. Retry-delay timers consume no Activity slot. When the timer completes, clear
the deadline, increment the attempt for dispatch, and invoke the same frozen task input.
Infrastructure Activity exceptions follow the same retry path without pretending that a
handler outcome was returned.

After a terminal outcome or retry exhaustion, fail fast when `continue_on_error` is false.
Apply all outcomes from the already-dispatched wave in stable node/instance order before
selecting the lowest-ordered failure. When continuation is allowed, commit the exact
sanitized `failed_continued` result, satisfy dependencies, and allow descendants to test its
fields with `when`. Authorization, contract, template, scheduler, cancellation, and
termination failures never continue.

For `for_each`, keep attempt and backoff state per materialized instance. Continued failures
do not stop sibling instances. Aggregate source-ordered entries with `completed`, `skipped`,
or `failed_continued`; set the logical node to `aggregated_with_errors` when any committed
entry continued a failure.

Cancellation cancels pending wait/backoff timers, schedules no new attempt, preserves
committed success and continued-failure results, and ignores late Activity completion. It
does not claim to signal any already-dispatched Activity.

This milestone is complete when deterministic replay tests produce identical Activity calls,
timers, status transitions, attempt numbers, and results, and targeted retry, continuation,
authorization-revocation, `when`, `for_each`, and cancellation tests pass.

### Milestone 4: Status version 3 and observability

Extend `_dynamic_status()` so a plan with any policy-aware task emits schema version 3.
Plans using only `when` or `for_each` remain version 2. Policy-free static plans retain the
version-1 string.

On a policy-aware non-iterated node or materialized instance, emit `max_attempts`; emit
one-based `attempt` only after dispatch. Emit RFC 3339 UTC `next_retry_time` only in
`retry_wait`. Emit `last_failure_kind` and `last_error_code` only after failure and retain
them after later success. Do not put synthetic execution fields on a `for_each` logical
node, and omit execution fields on policy-free units that happen to share a v3 plan.
Version-3 counts classify executable units into pending, running, retry_wait, completed,
skipped, and failed_continued, with buckets summing to materialized_total.

Update `src/azure_functions_agents/public/index.html` to accept version 3 and render retry,
attempt, continued-failure, and aggregate-error states without exposing arguments, results,
idempotency keys, or session identity. Retain v1/v2 rendering tests.

Add bounded Activity-delivery logs/spans/counters through
`src/azure_functions_agents/_observability.py` using workflow ID, task ID, instance ID,
attempt, target type/name, policy source, outcome, retry decision, timeout, and delay.
Activity telemetry represents actual delivery and may appear more than once under Durable
redelivery. Orchestrator replay must not emit duplicate metrics.

This milestone is complete when status tools, HTTP/list envelopes, UI formatter tests, and
observability tests cover v1, v2, v3, redelivery, and sensitive-field exclusion.

### Milestone 5: Independent testing review and demonstrable scenario

Run a separate testing review against the finalized FRD and the implementation diff. Add any
missing boundary, replay, security, compatibility, or state-machine tests before declaring
the implementation complete.

Add a deterministic fake tool in the most focused existing workflow sample or test app. It
must fail twice with `WorkflowRetryableError`, then succeed while recording one stable
idempotency key. Also demonstrate an exhausted async timeout and a continued `for_each`
failure followed by an explicitly conditioned recovery node. Do not depend on live provider
faults or wall-clock sleeps in unit tests.

Use existing E2E infrastructure for Azure Storage and DTS when available. Unit and simulated
orchestrator tests remain mandatory even when local Functions Core Tools, Azurite, or DTS are
unavailable; record any unavailable external prerequisite in this plan rather than silently
skipping evidence.

### Milestone 6: Documentation and complete gate

Update `docs/architecture.md` with effective-policy translation, immutable metadata,
Activity outcome/context ownership, deterministic retry timers, cancellation limitations,
status v3, and telemetry. Update `docs/workflows.md` with task and decorator syntax,
authoritative precedence, idempotency, error types, continuation, Sub Agent limits, and full
status examples. Replace text that says per-task retry/timeout is future work. Update the
relevant workflow section in `README.md`, add a cross-reference in
`docs/front-matter-spec.md`, and document public exports.

This feature changes workflow schema but not `config/schema.py`, so the generated
`docs/front-matter-reference.md` workflow applies only if implementation unexpectedly
requires a config-schema change. If that happens, run
`eng/scripts/generate_config_reference.py` and invoke the `update-schema-docs` skill before
continuing.

Run formatting only through existing tooling, targeted tests during each milestone, then
the canonical lint, strict type check, and full coverage test command. Update the FRD test
checkboxes and this plan's living sections with actual evidence. Commit coherent milestones
frequently.

## Concrete Steps

All commands run from the repository root:

    Q:\ghcpapp\copilot-worktrees\azure-functions-agents-runtime\tsuyoshiushio-crispy-robot

Start with the focused baseline:

    python -m pytest tests/test_workflow_schema.py tests/test_discovery_tools.py \
      tests/test_workflow_registry.py tests/test_workflow_engine.py tests/test_chat_ui.py -q

PowerShell uses a backtick or one physical line instead of the backslash shown above. Expect
all selected non-E2E tests to pass before product edits. Record the exact count in
`Surprises & Discoveries`.

After Milestone 1:

    python -m pytest tests/test_workflow_schema.py tests/test_discovery_tools.py \
      tests/test_workflow_registry.py tests/test_workflow_integration_validation.py \
      tests/test_package_imports.py -q

After Milestones 2 and 3:

    python -m pytest tests/test_workflow_engine.py tests/test_workflow_schema.py \
      tests/test_workflow_registry.py tests/test_per_agent_workflows.py -q

After Milestone 4:

    python -m pytest tests/test_workflow_engine.py tests/test_chat_ui.py \
      tests/test_workflow_registry.py -q

At the Phase 3 implementation gate:

    python -m ruff check src tests
    python -m mypy src

At the Phase 4 and final gate:

    python -m pytest --cache-clear --cov=./src/azure_functions_agents \
      --cov-report=xml --cov-branch tests

After documentation:

    python -m mkdocs build --strict

Run sample E2E commands only through existing sample scripts or existing pytest `e2e` tests,
and record the exact command and backend in this plan.

## Validation and Acceptance

Acceptance requires all of the following observable behavior.

A policy-free static plan serializes exactly as before, selects the static scheduler, and
publishes its legacy string status. A `when`/`for_each` plan without execution policy
publishes schema version 2. A static plan with `execution: {}` publishes schema version 3 and
has a ten-minute effective tool timeout.

A tool without decorator retry metadata accepts a DAG retry policy. A tool with decorator
`max_attempts=1` executes once even if the DAG requests more attempts. A decorator timeout
overrides only task timeout while task retry remains usable when decorator retry is omitted.

Invalid attempts, durations, multiplier, backoff order, elapsed total, error codes, and wait
task execution fields fail before Durable starts and expose stable non-sensitive validation
metadata. Decimal backoff precomputation yields the documented integer sequence and replay
uses that sequence without floating-point recomputation.

An async transient tool that fails twice and succeeds once is invoked three delivered
attempts with attempt numbers 1, 2, and 3, one stable idempotency key, and two deterministic
Durable timers. Unknown exceptions do not retry. Authorization revocation during backoff
prevents the next handler invocation.

An exhausted continuable failure becomes a `failed_continued` result, satisfies dependencies,
and can be inspected by a downstream `when`. A non-continuable failure cannot be converted
to success. Mixed `for_each` aggregation preserves source order and becomes
`aggregated_with_errors`.

Status v3 follows the complete FRD shape, counts executable units rather than attempts, and
omits sensitive fields. The built-in UI renders it while retaining existing v1 and v2
behavior. Cancellation during backoff creates no later retry and makes no false promise
about recalling an already-running Activity.

Ruff, strict mypy, the full non-E2E coverage suite, and strict documentation build all exit
zero.

## Idempotence and Recovery

Schema and runtime edits are additive until each compatibility test is green. Re-running
tests, lint, type checking, documentation generation, and builds is safe. Do not delete or
rewrite Durable storage as part of unit validation.

If a milestone fails, keep the last passing commit and update `Progress` with completed and
remaining work. Fix forward; do not reset or revert unrelated user changes. If an internal
persisted contract changes after tests have recorded it, update its compatibility decoder at
the single payload boundary and add a legacy-payload test rather than scattering defaults.

Timed-out sync handlers may remain alive during a test. Use bounded test events rather than
infinite loops so worker threads eventually exit. Do not terminate processes by name.

If E2E prerequisites are unavailable, preserve all unit/simulated replay evidence and record
the exact missing command or service. Resume using the existing scripts after prerequisites
are restored.

## Artifacts and Notes

The design record is `docs/frds/0004-dynamic-workflows.md`, especially Decisions 67 through
81. The current architecture source is `docs/architecture.md`. The implementation and test
mapping was independently researched before this plan was written; the main risk is not
finding files but preserving deterministic replay and compatibility while extending the
typed scheduler.

Expected retry evidence should resemble:

    Activity calls: fetch_inventory attempt=1, attempt=2, attempt=3
    Timer delays: 1000ms, 2000ms
    Idempotency keys: one unique value
    Final result: {"service": "inventory", "status": "healthy"}

Expected continued aggregate evidence should resemble:

    [
      {"index": 0, "status": "completed", "result": {...}},
      {"index": 1, "status": "failed_continued", "result": {
        "failed": true,
        "error_code": "inventory_unavailable",
        "error": "Inventory is temporarily unavailable.",
        "kind": "handler_transient",
        "attempts": 3
      }}
    ]

## Interfaces and Dependencies

Use only existing runtime dependencies and the Python standard library. Do not add a
duration or retry package. Pydantic v2 validates authored models. `decimal.Decimal` provides
deterministic delay arithmetic. `asyncio`, `inspect`, `contextvars`, `hashlib`, `datetime`,
and `re` provide Activity invocation, context, idempotency, deadlines, and public error
validation. Azure Durable Functions remains the scheduler and timer implementation.

The public API must export these names from `azure_functions_agents`:

    WorkflowRetryBackoff
    WorkflowRetryPolicy
    WorkflowTaskExecution
    WorkflowRetryableError
    WorkflowTerminalError
    WorkflowTaskContext
    current_workflow_task_context

The authored models have these semantic interfaces:

    WorkflowRetryBackoff(
        initial: str,
        multiplier: float,
        max: str,
    )

    WorkflowRetryPolicy(
        max_attempts: int = 1,
        backoff: WorkflowRetryBackoff | None = None,
    )

    WorkflowTaskExecution(
        timeout: str | None = None,
        retry: WorkflowRetryPolicy | None = None,
        continue_on_error: bool = False,
    )

The decorator retains both bare and called forms and adds keyword-only fields:

    workflow_tool(
        func=None,
        *,
        name: str | None = None,
        description: str | None = None,
        public: bool = True,
        timeout: str | None = None,
        retry: WorkflowRetryPolicy | None = None,
    )

The public handler context is immutable:

    WorkflowTaskContext(
        workflow_id: str,
        task_id: str,
        node_instance_id: str,
        attempt: int,
        max_attempts: int,
        idempotency_key: str,
        deadline: datetime,
    )

    current_workflow_task_context() -> WorkflowTaskContext | None

`WorkflowRetryableError(error_code, message)` signals a retryable handler failure.
`WorkflowTerminalError(error_code, message)` signals a non-retryable but continuable handler
failure. Neither grants continuation; `continue_on_error` remains task-local.

Internal effective policy and Activity input/outcome types must be strict `TypedDict` or
frozen dataclass contracts with JSON-safe fields. Name them according to existing module
conventions after inspecting adjacent types; do not expose persisted implementation fields
unless the FRD explicitly makes them public.

Revision note (2026-08-24): Initial ExecPlan created after human sign-off, two implementation
mapping passes, and the final architecture review. It incorporates Decision 81 so a novice
implementer does not need to invent replay, status, cancellation, or validation behavior.

Revision note (2026-08-24): Recorded the 226-test focused baseline before product changes so
later milestones can distinguish regressions from newly introduced failures.

Revision note (2026-08-24): Recorded Milestone 1 completion, its 245-test/ruff/mypy evidence,
and the compatibility decision to floor existing Sub Agent timeout seconds only for stored
resolution metadata.

Revision note (2026-08-24): Recorded Milestone 2 completion and independent review fixes for
handler-originated `TimeoutError` and boolean persisted `max_attempts`.

Revision note (2026-08-24): Recorded Milestone 3 completion and review-driven corrections for
failed-wave draining, independent backoff timers, persisted authorization, strict outcome
validation, and timer cleanup.

Revision note (2026-08-24): Recorded Milestone 4 completion and review-driven corrections for
blocked-unit accounting, cancellation completion telemetry, and isolated metric creation.

Revision note (2026-08-24): Recorded the passing independent testing checkpoint and its
deterministic incident-triage execution-policy demonstration.
