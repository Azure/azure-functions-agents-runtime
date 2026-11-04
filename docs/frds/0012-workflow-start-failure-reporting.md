---
frd: 0012
title: Workflow start failure reporting
status: Finalized
author: TsuyoshiUshio
created: 2026-11-03
updated: 2026-11-03
issues: []
pull_requests: []
branch: tsuyoshiushio-workflow-failure-reporting
---

# FRD 0012 - Workflow start failure reporting

## 1. Summary

Give each Dynamic Workflow start failure a safe, stable result and an error
span for the failed operation. Separate a failed admission check from a
schedule request whose acceptance is unknown. Keep tool failure, agent run
completion, Function invocation completion, and background workflow completion
as separate facts. Do not infer business success from a normal agent response.

The human approved this design on 2026-11-03.

## 2. Motivation / problem

`fhs_start_workflow` checks the active workflow count before it submits a
schedule request. Both client calls can fail. The tool logs each exception,
but both failures return the same string:

```json
{"error": "failed to start workflow"}
```

The model cannot tell which operation failed. Logs alone are not a reliable
Application Insights surface. The shared logger is under `azure.functions.*`;
the worker can treat its records as system logs. FRD 0003 therefore makes spans
the main diagnostic surface.

A non-HTTP handler returns normally when the agent returns normally. Its run
span can report `success` even when a tool returned an error. This fact does
not prove that a workflow started. Conversely, an earlier tool error does not
prove that the agent failed: the model can correct a plan or use another valid
method. The runtime has no general contract that proves task recovery.

A schedule exception does not prove that the service rejected the request.
The service can accept a request before the client loses its response.
Each new start call uses a new workflow instance ID. A Queue redelivery also
gets a new agent session. An automatic retry can thus start duplicate work.

## 3. Goals / Non-goals

**Goals**

- Return fixed error codes for the two client failure sites in workflow start.
- State whether submission did not occur or its acceptance is unknown.
- Link the safe tool result, logs, and operation spans with one diagnostic ID.
- Record failed operations even when the agent run returns normally.
- Keep successful recovery from a tool error as a possible successful run.
- Use the same workflow tool implementation for MAF and Copilot.
- Preserve the rule that raw internal exception text does not reach the model.

**Non-goals**

- Decide whether arbitrary model output proves business success or recovery.
- Change the `AgentResult`, HTTP, SSE, MCP, or HostedSkill result contracts.
- Fail a Function invocation solely because a tool returned an error.
- Add automatic start retries, automatic reconciliation, or duplicate prevention.
- Add a new workflow start endpoint, authoring field, exporter, or dependency.
- Change workflow Activity retries, orchestration history, or terminal status.
- Change the error results of status, list, cancel, or terminate tools.
- Fix the Durable SDK or Queue sample message encoding.

## 4. Proposed design

### Pipeline and module boundaries

Observability stays a cross-cutting concern, as defined in FRD 0003. No new
pipeline stage is needed.

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| discover | `discovery/*` | None. Discovery stays read-only. |
| translate | `config/*`, `workflows/schema.py` | None. Existing plan validation and policy rules stay in force. |
| register | `registration/_handlers.py`, `registration/endpoints.py` | No product behavior change. Preserve transport and invocation completion rules. |
| execute | `workflows/tools.py` | Add safe start-failure fields and operation spans around the admission check and schedule call. Keep both harnesses on this shared path. |
| cross-cutting | `_observability.py` | Own the new telemetry attribute names. Reuse `start_span()`, `RuntimeSpan.set_error()`, `LifecycleStage`, and `FaultDomain`. |
| cross-cutting | `_agent_run_diagnostics.py` | No product behavior change. The existing error-result detector must still recognize the non-empty `error` field. |
| execute guidance | `workflows/integration.py`, `workflows/tools.py` tool description | Explain confirmed start versus unknown acceptance. Do not tell the model to repeat an unknown schedule request. |

Do not add a generic operation framework or a new run-state tracker. A small
private helper in `workflows/tools.py` can build the fixed failure envelope.
Keep workflow-specific vocabulary out of the selected harness adapters.

### Authoring / API surface

There are no new authoring fields, tool parameters, or endpoints.
The two dependency failure results gain fields. Success stays unchanged:

```json
{"workflow_id": "<confirmed-instance-id>"}
```

The admission check failure result is:

```json
{
  "error": "failed to start workflow",
  "error_code": "workflow_admission_check_failed",
  "operation": "check_active_workflows",
  "start_status": "not_started",
  "diagnostic_id": "<opaque-diagnostic-id>"
}
```

The schedule failure result is:

```json
{
  "error": "failed to start workflow",
  "error_code": "workflow_schedule_outcome_unknown",
  "operation": "schedule_workflow",
  "start_status": "unknown",
  "diagnostic_id": "<opaque-diagnostic-id>",
  "attempted_workflow_id": "<submitted-instance-id>"
}
```

These are fixed runtime classifications, not exception messages or claims
about the root cause. Define the following meanings:

| Field | Contract |
| --- | --- |
| `error` | Preserve the existing generic message for both client exceptions. |
| `error_code` | Fixed code from the table below. Do not derive it from exception text or an SDK class name. |
| `operation` | Logical runtime operation, independent of SDK method names. |
| `start_status` | `not_started`: this start call did not call schedule. `unknown`: this start call entered the schedule call but did not get a normal return. Neither value states the status of other calls. |
| `diagnostic_id` | Fresh UUID hex value for this tool call. Use the same value in its operation spans and logs. It is not a trace ID, authorization token, or idempotency key. Generate it without an OpenTelemetry provider. |
| `attempted_workflow_id` | The exact ID passed to schedule. Include it only for unknown acceptance. It is not evidence that an instance exists. Existing agent/session authorization still applies to status reads. |

| Failure site | `error_code` | `operation` | `start_status` |
| --- | --- | --- | --- |
| `count_active_session_workflows()` raises | `workflow_admission_check_failed` | `check_active_workflows` | `not_started` |
| `schedule_new_orchestration()` raises | `workflow_schedule_outcome_unknown` | `schedule_workflow` | `unknown` |

Use the conservative `unknown` result for every caught schedule exception.
Do not guess whether the request reached the service from its exception type.
Do not return `workflow_id` on this path. This reserves that field for the
existing confirmed-start result.

Generate the diagnostic ID once, after plan validation and before the admission
check. Both operation spans use it, including on successful calls. It does not
add a field to the successful tool result. Include it in the existing
confirmed-start log.

Missing request context, missing app policy, plan-validation errors, and the
active-count limit retain their existing results. They are not dependency
failures in this contract. Preserve existing plan `error_code`, `node_id`,
and `path` metadata. Do not submit a plan after an admission-check error.

Catch ordinary client exceptions only at the two existing boundaries.
Cancellation must propagate, not become a dependency error. Do not broaden
the catch to plan serialization or unrelated runtime work.

### Operation spans and diagnostic correlation

Emit one span for each operation that this start call enters:

| Span name | Scope |
| --- | --- |
| `workflow.start.check_active` | The active-count client call and its existing result processing. |
| `workflow.start.schedule` | The schedule client call. Do not create this span if admission failed or the active limit was reached. |

These spans measure client operations, not full tool execution. Existing MAF
and Copilot tool spans remain valid parents; they are not replaced or counted
again as tool calls. Use the active OpenTelemetry context. If there is no
active parent, a direct helper call can produce a root span. Do not create
another `agent.run` span to supply a parent.

Use these content-free attributes:

| Attribute | Meaning |
| --- | --- |
| `af.lifecycle_stage` | Existing `tool_execution` value. |
| `af.workflow.operation` | Same fixed value as result `operation`. |
| `af.workflow.diagnostic_id` | Same diagnostic ID as the tool result and log. |
| `af.workflow.agent_slug` | Agent slug from the captured workflow session. |
| `af.workflow.start_status` | Check success: `not_started`; schedule success: `scheduled`; failure: the value in its result. |
| `af.workflow.error_code` | Fixed failure code, on failed operations only. |
| `af.workflow.attempted_workflow_id` | Submitted instance ID on the schedule span only, on success or failure. |
| `error.type` | Same fixed error code, on failed operations only. |
| `af.fault_domain` | Existing `unknown` value on failed operations. The client failure alone does not establish an app, runtime, network, or service fault. |

`start_status` states what is known when that operation ends. A successful
check has not yet submitted work, so it also uses `not_started`. It is not a
failure flag. If the check finds the active limit, its span still completes
without ERROR status or error attributes: the client operation succeeded.

On a caught exception, call `span.set_error()` with a fixed message and code.
The operation span must have OpenTelemetry ERROR status even when the tool
returns a string. Create it with `record_exception=False` to prevent automatic
capture of raw exception details. Do not attach prompts, plans, arguments,
tags, endpoint addresses, or exception messages to these spans.

Handle cancellation separately inside each operation span. Set the existing
`af.tool.outcome=cancelled` attribute before cancellation escapes. The shared
span helper then suppresses exception-based ERROR status and propagates the
cancellation. Do not set a dependency error code or return a failure envelope.
If cancellation occurs during schedule, set the span's start status to
`unknown`; cancellation cannot prove that the service did not accept work.
Do not retry or reconcile that request automatically.

Keep the existing `logger.exception()` calls and their internal stack traces.
Add the same diagnostic ID, fixed operation, fixed code, and start status.
Add the attempted workflow ID to schedule-failure logs. These existing
internal logs are not model-visible. Do not copy their text into span status,
span events, tool results, or transport responses.

Use the trace parent for Function-to-run-to-operation correlation. Use
`diagnostic_id` for result-to-log-to-span lookup. Do not reuse `af.operation_id`:
that existing attribute identifies ACA sandbox correlation. Keep IDs as
trace/log attributes, not metric dimensions. No new metric is required.

If telemetry is disabled, both client behavior and the safe result remain
the same. Existing span helpers become no-ops; logs and the diagnostic ID
remain available. Do not add a second exporter or provider.

### Four separate completion facts

| Layer | What success proves | What a workflow tool error changes |
| --- | --- | --- |
| Workflow client operation | That operation returned normally. Schedule success confirms submission, not workflow completion. | Mark the failed operation ERROR and return its safe classification. |
| Agent run | The runner returned normally and any applicable response checks passed. | Preserve existing completion rules. Keep aggregate tool errors; do not convert them to a failed run. |
| Functions invocation | The trigger handler completed according to the host and transport rules. | Do not raise a new exception or change a transport result solely for a caught tool error. |
| Background workflow | Its later Durable status and output describe its result. | A submission error does not create a known terminal workflow result. Unknown acceptance needs separate investigation. |

`af.agent.outcome=success` is not a business-success assertion. The same applies
to a successful Function request in the host. A failed operation span must
remain visible beneath a successful run. Monitoring must query operation
errors as well as failed requests.

Do not change `_total_tool_error_count()` to a run-failure predicate.
Do not parse model prose to detect recovery. Do not clear an earlier operation
error if a later call succeeds. A failed call followed by a confirmed start
must preserve both operation records and can end in a successful agent run.
This proposal adds no `recovered` or `unrecovered` label because there is no
general proof for either label.

An application that requires "this message must start a workflow" needs an
explicit completion contract before the runtime can fail that task reliably.
That is different from the current general agent-run contract.

### Retry safety and unknown acceptance

Do not add an automatic retry at the tool, harness, or handler boundary.
Do not turn either caught client failure into a Queue handler exception.
The existing host retry rules for other uncaught exceptions are unchanged;
this feature does not provide exactly-once workflow start.

An attempted ID allows an authorized user or operator to check the original
request. A `not_found` status is not proof of rejection: visibility can lag.
Do not start a replacement automatically after that result. Do not promise
that repeating the same instance ID is safe across all supported providers.
Task-level Activity idempotency keys do not deduplicate workflow submission.

Update the start tool description and shared workflow prompt guidance:

- Confirm a start only after a normal result with `workflow_id`.
- On `not_started`, state that this call did not submit a workflow.
- On `unknown`, state that acceptance is unknown. Preserve the attempted ID
  for investigation. Do not claim that no work started.
- Do not repeat an unknown schedule request, automatically poll status, or
  replace it with a new start without an explicit user or application action.

These instructions guide the model; they are not an enforcement mechanism
or a duplicate-prevention guarantee. The approved design accepts this
limitation. There is no new retryability flag in the tool result.

### Harness and transport compatibility

| Surface | Required behavior |
| --- | --- |
| MAF direct and streaming | Shared workflow descriptors return the same safe JSON text. Existing tool evidence sees non-empty `error`. Preserve normal run and stream completion rules. |
| Copilot direct and streaming | Reuse the same descriptors through the bound per-agent workflow policy. Preserve custom tool wrapper spans and their explicit success evidence. Do not change SDK success semantics merely because an operation child failed. |
| Declared HTTP trigger and built-in chat | Keep response body, status, session headers, and response-schema checks unchanged. Nested tool evidence can carry the added fields where already exposed. Do not add a new top-level error response. |
| SSE | Keep event names, order, and completion behavior. Existing tool-result events carry the safe JSON text. A caught workflow error does not become an SSE transport error. |
| MCP agent endpoint | Keep its response envelope and existing session rules. A failed nested workflow operation does not by itself make the MCP agent call a protocol error. |
| Queue, Timer, Blob, and other supported non-HTTP triggers | Keep handler return and exception rules. A normal return remains a completed invocation; monitor operation errors separately. Preserve the current supported-trigger allowlist. |
| HostedSkill binding | No new Dynamic Workflow support or facade fields. Existing capability checks stay in force. |
| Standalone workflow helper use | Use the same result and operation-span rules, without requiring an enclosing run span. |

Compatibility is additive for JSON consumers that ignore unknown keys.
Consumers that compare the entire old two-field-free error object as text
must change. Keep the generic `error` string and successful response stable.
No change to the generated configuration reference is needed.

## 5. Decisions log

The log preserves the original proposals and the later human approval.

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Model-visible exception details | Raw exception text / fixed runtime classification | Keep raw internal exceptions out of model-visible results. | Human requirement | 2026-11-03 |
| 2 | Automatic retry and duplicate prevention | Add retries or a new duplicate-prevention store / require separate approval | Neither mechanism is approved. Do not assume approval. | Human requirement | 2026-11-03 |
| 3 | Client failure vocabulary | One generic result / SDK exception names / fixed logical codes and start status | Proposed: the two fixed codes, operations, and status values in section 4. | Agent proposal; human approval pending | 2026-11-03 |
| 4 | Unknown schedule acceptance | Report definite failure / report unknown with attempted ID | Proposed: `unknown` for every caught schedule exception; return `attempted_workflow_id`, not `workflow_id`. | Agent proposal; human approval pending | 2026-11-03 |
| 5 | Run and invocation outcomes | Fail on any tool error / infer recovery / preserve completion rules | Proposed: record operation failures without failing a normally completed run or invocation. This does not certify task success. | Agent proposal; human approval pending | 2026-11-03 |
| 6 | Diagnostic correlation | Trace ID only / new global tracker / per-tool-call diagnostic ID plus existing trace context | Proposed: one UUID diagnostic ID shared by safe result, logs, and operation spans. | Agent proposal; human approval pending | 2026-11-03 |
| 7 | Observation boundary | New telemetry framework / reuse existing span helpers | Proposed: two operation spans with fixed error metadata and no raw exception content. | Agent proposal; human approval pending | 2026-11-03 |
| 8 | Approval of the minimum design | Operation failure records with unchanged invocation rules / a new task-completion and redelivery contract | Approve the proposed fields, attempted ID, diagnostic correlation, operation spans, cancellation rules, and existing harness/transport contracts. No automatic retry. | Human: TsuyoshiUshio, through the coordinating session | 2026-11-03 |

### Approved human decisions

The human selected the following option:
「操作単位の失敗を記録する設計を承認する（推奨）」.
This approves the following choices:

1. Use the exact public error fields and code values.
2. Use unknown acceptance with an attempted ID and manual investigation,
   without automatic retry, reconciliation, or enforced duplicate prevention.
3. Keep run and Functions invocation completion rules unchanged, with
   operation-error monitoring as the failure signal. If the required outcome
   is instead a failed Queue invocation when no workflow starts, this design
   is not sufficient. That alternative needs a task-completion contract,
   a recovery rule, and a safe redelivery/unknown-acceptance policy.
4. Use the two start-operation spans and diagnostic ID. Other management
   operations keep their current error contracts.

## 6. Test plan

Add failing regression tests before product fixes. Extend existing test files
where practical; do not add configuration fixtures because no authoring
behavior changes.

- [x] `tests/test_workflow_tools.py`: admission exception returns the exact
  fixed fields, omits both workflow ID fields, and never calls schedule.
- [x] `tests/test_workflow_tools.py`: schedule exception returns the exact
  fixed fields and submitted attempted ID, but no confirmed `workflow_id`.
- [x] Simulate a client that accepts a schedule and then raises. Assert one
  schedule call, `unknown` acceptance, and no retry, status read, or replacement.
- [x] Preserve success, validation metadata, missing context/policy, ownership,
  active-cap rejection, and the existing returned-ID mismatch behavior.
- [x] Verify `CancelledError` propagates. No safe dependency error is returned
  for cancellation; it does not gain a dependency-failure ERROR record.
- [x] Shared span-helper tests and workflow tests: assert operation names,
  parent trace, ERROR status, fixed `error.type`, fault domain, and matching
  diagnostic IDs. No schedule span exists when schedule was not called.
- [x] With telemetry disabled, assert the same safe results and call counts.
- [x] Put secret-like text in both client exceptions. Assert it is absent from
  tool results, stream events, transport responses, operation span attributes,
  status descriptions, and exception events. Existing internal exception logs
  keep their stack traces and gain safe correlation fields.
- [x] Run concurrent calls and different sessions. Each tool call has a distinct
  diagnostic ID, with no mutable process-wide failure state or ID leakage.
- [x] MAF and Copilot adapter tests: invoke the real shared workflow descriptor
  with stub clients. Verify safe text, existing evidence/count behavior, and
  operation spans beneath the existing tool/run context.
- [x] Workflow tool and handler tests: a returned result with a workflow
  error can complete normally. An actual runner exception still escapes a
  non-HTTP handler. Verify no new Queue redelivery is caused by a caught error.
- [x] Test a tool failure followed by a confirmed start in one run. Preserve the
  failed operation span, a nonzero existing tool-error count, and a successful
  completed run. Also test normal completion without a later start; do not
  invent a business-success or recovery label for it.
- [x] Workflow tool, endpoint, streaming, and harness tests:
  exercise HTTP, SSE, and MCP safe error evidence. Preserve current status,
  envelopes, terminal events, response validation, and session behavior.
- [x] Confirm HostedSkill capability checks and both harness selections remain
  unchanged. Do not add a mixed-harness execution path.
- [x] Verify that shared and tool-specific prompt guidance distinguishes a
  confirmed start from unknown acceptance, and does not request automatic retry.
- [x] After implementation, run the canonical `ruff`, `mypy`, and full `pytest`
  gate from `AGENTS.md`.

## 7. Docs impact

- [x] `docs/architecture.md`: add operation observation to the workflow module
  map. Explain that this does not change run-result or trigger contracts.
- [x] `docs/observability.md`: document operation spans and attributes, diagnostic
  ID lookup, and a query for failed operations under successful Function
  requests. Separate operation failure from run and background completion.
- [x] `docs/workflows.md`: document the safe error examples, unknown
  acceptance, attempted ID, and the absence of automatic retry.
- [x] `docs/triggers.md`: explain unchanged non-HTTP completion rules and Queue
  duplicate risk if the application adds retries.
- [x] `docs/frds/README.md`: add this design to the index.
- `docs/front-matter-spec.md`, generated `docs/front-matter-reference.md`,
  `README.md`, and onboarding pages: no new authoring or quickstart behavior.
  Update only if implementation changes that assessment.

## 8. Status & sign-off

- **Architecture review (phase 2):** An independent `rubber-duck` review on
  2026-11-03 found one blocker: the workflow documentation path did not exist.
  Corrected it to `docs/workflows.md`. Also clarified diagnostic ID creation,
  the successful active-limit check, and the point-in-time start status.
  The review found no other architecture blockers. A further helper check
  showed that cancellation needs an explicit `af.tool.outcome=cancelled`
  attribute to avoid automatic ERROR status. Section 4 now requires it.
- **Human sign-off:** TsuyoshiUshio, 2026-11-03. The coordinating session
  supplied the user's exact approval above. The approved scope is the minimum
  operation-level failure design, with existing Function completion rules and
  no automatic retry.
- **Testing review (phase 4):** An independent `rubber-duck` review on
  2026-11-03 found no blockers. Added the suggested checks for cancellation
  with telemetry disabled, root spans without a parent, and the successful
  schedule operation value. Tests use local clients and SDK adapters. No live
  Azure or Functions host evidence was collected for this change.
- **Status:** Finalized.
