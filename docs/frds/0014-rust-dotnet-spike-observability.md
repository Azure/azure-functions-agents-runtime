---
frd: 0014
title: End-to-end observability for the Rust/.NET spike
status: Finalized
author: larohra
created: 2026-10-28
updated: 2026-10-28
issues: []
pull_requests: []
branch: larohra/foundry-telemetry
---

# FRD 0014 - End-to-end observability for the Rust/.NET spike

## 1. Summary

The Rust/.NET spike exports correlated OpenTelemetry spans to Application
Insights for a Functions request, Rust runtime execution, Foundry model calls,
and C# tool callbacks. Telemetry is enabled only when an Application Insights
connection string is configured. Prompts, model responses, tool arguments, and
tool results are not captured.

## 2. Motivation / problem

The support response lists `tool_calls_made`, which confirms tool handler
dispatch, but it does not show timings, failures, or the relationship between
the Functions request, Rust execution, Foundry calls, and C# tool callbacks.
Operators need a correlated trace to inspect a live run in Application
Insights.

## 3. Goals / Non-goals

**Goals**
- Correlate worker and runtime spans with the Functions invocation trace.
- Show an agent-run span for the Rust runtime request.
- Show individual Foundry model-call spans from Copilot SDK lifecycle events.
- Show C# tool callback spans, including tool identity, outcome, and duration.
- Export traces to Application Insights when configured; preserve no-export
  behavior when it is not configured.
- Keep prompts, completions, arguments, results, access tokens, and endpoint
  values out of telemetry.

**Non-goals**
- Change the production Python runtime or its existing observability feature.
- Add metrics, dashboards, alerts, or a new telemetry backend.
- Capture model content, tool inputs/outputs, or credentials.
- Instrument non-.NET language workers or triggers not present in this spike.
- Change inference mode, tool dispatch, response content, or session behavior.

## 4. Proposed design

Observability is cross-cutting and applies only to the experimental spike; it
does not add a stage to the production runtime pipeline.

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| bootstrap | `dotnet/HostedSkills.Spike/Program.cs`, `host.json`, project file | Configure Functions and worker OpenTelemetry with the Azure Monitor exporter when an Application Insights connection string is available. Register the spike's `ActivitySource`; leave export inactive otherwise. |
| execute | `OrderFunctions.cs`, `HostedSkillRuntimeClient.cs` | Create the Rust agent-run span beneath the Functions invocation, carry its W3C context and a random run ID over JSON-RPC, and instrument each C# tool callback as a child of the run span. |
| execute | `src/engine.rs`, `src/protocol.rs` | Consume the pinned Copilot SDK's `model.call_start` and `model.call_finished` events. Bridge only lifecycle metadata to .NET, where each model-call span is parented to the agent-run span. |
| docs | `spikes/rust-runtime/README.md` | Document opt-in configuration, safe span attributes, local/live setup, and an Application Insights query for a correlated run. |

The .NET worker owns telemetry export. It creates a Rust agent-run span around
the `agent.run` RPC and passes its trace context and a per-run correlation ID in
the RPC parameters. Rust associates that ID with the SDK session and its
reverse-tool handlers. Rust sends model-call lifecycle notifications carrying
the same ID and a per-run call sequence. The worker starts and ends model-call
Activities using the stored agent-run context. For a tool call, Rust includes
the run ID in the existing `tool.call` parameters; .NET starts a child Activity
around dispatch and handler execution. Tool callback duration measures the
worker-visible callback from receipt through response, including stdio and
handler time. It does not record model arguments or tool payloads.

The pinned Copilot SDK event schema exposes the model-call lifecycle events
used here. Start and finished events share a `turn_id`, which Rust maps to its
own per-run call ID. The SDK's `model.call_failure` event is attempt-level and
does not carry that correlation field; final success/error/cancellation is
reported by `model.call_finished.outcome`, so that event closes the span. The
runtime does not inject trace context into SDK requests or depend on
SDK/network propagation. If the run context is missing or malformed, the
worker omits child spans and continues the agent request; telemetry failures
never fail inference or tool execution.

### Model-call Activity lifecycle

For each `model.call_start`, Rust allocates one monotonically increasing
`call_id` within the run, maps the SDK's `turn_id` to it, and sends a `start`
notification. .NET creates exactly one `gen_ai.request` Activity, parented to
the stored agent-run context, and stores it by `(run_id, call_id)`. The
matching `model.call_finished` notification ends that same Activity and removes
the mapping. Its outcome sets the span status. A duplicate start or unmatched
terminal event is ignored for telemetry; it does not affect the model request.
If the run fails before the SDK emits its matching terminal event, the worker
ends remaining model Activities with error status when the run span closes.

Each reverse `tool.call` creates one `fhs.tool.execute` Activity as a sibling
under the agent-run Activity. It uses the parent context saved for its `run_id`,
not the current model-call Activity, so callback timing and event ordering
cannot nest a tool span beneath a model-call span.

### JSON-RPC telemetry contract

When telemetry is active, .NET adds an optional `trace_context` and opaque
`run_id` to `agent.run` parameters:

```json
{
  "trace_context": {
    "trace_id": "32 lowercase hex characters",
    "span_id": "16 lowercase hex characters",
    "trace_flags": "2 lowercase hex characters"
  },
  "run_id": "random per-invocation identifier"
}
```

Rust echoes the `run_id` in `fhs.telemetry.model_call` notifications. Each
notification contains `run_id`, a per-run monotonic `call_id`, `phase`
(`start` or `end`), the configured model name, and a bounded outcome on
terminal events. The SDK's `turn_id` is used only inside Rust to correlate
events and is not transmitted. Rust adds `run_id` to `tool.call` parameters.
Notifications are best-effort and do not change the existing JSON-RPC result
contract.

### Authoring / API surface

- No agent frontmatter or project configuration changes.
- Use the standard `APPLICATIONINSIGHTS_CONNECTION_STRING` setting to enable
  Azure Monitor export.
- Use `OTEL_SERVICE_NAME` or the spike's default service name for identifying
  the worker in traces. Set Functions `telemetryMode` to `OpenTelemetry` to
  export host invocation telemetry and correlate worker spans.
- Custom spans use a spike-owned `ActivitySource` and stable names for
  `fhs.rust.agent.run`, `gen_ai.request`, and `fhs.tool.execute`.
- Attributes are limited to agent name, provider, model name, tool name, and
  bounded outcomes. The random run/call IDs are used
  only for in-process correlation and are not exported as attributes.
- No custom identifiers, session IDs, prompts, responses, tool arguments,
  tool results, endpoint values, tokens, or exception messages are exported.

### Compatibility

With no connection string, application behavior and current response fields
remain unchanged and no telemetry is exported. When configured, telemetry is
additive and does not affect inference, tool results, or sessions. The feature
is specific to the spike and does not imply production support for the Rust
runtime.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Trace coverage | Function only / worker tools / end-to-end across worker, Rust, and Foundry | Correlated invocation, Rust agent-run, model-call, and C# tool spans | Human (larohra) | 2026-10-28 |
| 2 | Enablement | Always on / explicit flag / standard connection setting | Export when `APPLICATIONINSIGHTS_CONNECTION_STRING` is configured | Human (larohra) | 2026-10-28 |
| 3 | Content capture | Capture prompts and payloads / metadata only | Metadata only; no prompts, responses, tool arguments, results, secrets, or endpoint values | Human (larohra) | 2026-10-28 |
| 4 | Cross-process telemetry | Separate Rust exporter / bridge SDK lifecycle metadata to .NET | Bridge SDK model-call lifecycle metadata over stdio; .NET owns span creation and export | Human (larohra) | 2026-10-28 |
| 5 | Model-call event source | Infer from full agent-run duration / consume SDK lifecycle events | Pair `model.call_start` and `model.call_finished` by SDK `turn_id`; use finished outcome for span status. `model.call_failure` is attempt-level and uncorrelated in the pinned schema, so it does not end a span. | Agent (validated against pinned crate schema) | 2026-10-28 |
| 6 | Trace propagation | SDK/network injection / explicit stdio context | Pass the .NET agent-run Activity context and a random run ID in the existing `agent.run` parameters; do not inject into SDK requests | Human (larohra) | 2026-10-28 |

## 6. Test plan

- [x] Unit: span names, safe attributes, statuses, and W3C trace-context
  parsing/parentage; missing or invalid context omits child spans without
  failing the agent run.
- [x] Unit: Copilot SDK model-call start/finished events produce matching
  `fhs.telemetry.model_call` notifications, paired by `run_id` + `call_id`,
  with SDK `turn_id` used only in Rust; assert finished outcomes set the
  Activity status, each lifecycle produces exactly one Activity, and
  duplicate/unmatched events produce none.
- [x] Unit: when the agent run fails before a matching finished event,
  outstanding model Activities end with error status.
- [x] Unit: C# callback spans cover success and validation/error results and
  are children of the matching `fhs.rust.agent.run` Activity.
- [x] Unit: tool spans remain children of the agent-run Activity when a tool
  callback occurs after a model-call Activity has ended.
- [x] Integration: no connection string leaves exporting disabled and preserves
  existing behavior.
- [x] Integration: an in-memory ActivityListener observes agent-run,
  model-call, and tool spans with the same trace ID and expected parent IDs;
  assert excluded content and identifiers are absent.
- [ ] Live validation (manual, when Application Insights and Foundry
  configuration are available): run a Foundry turn that invokes both tools,
  query the trace, and verify invocation → agent-run → model-call/tool spans
  and absence of prompt, response, argument, result, endpoint, and credential
  data. This validation is not part of offline CI.

## 7. Docs impact

- [x] `spikes/rust-runtime/README.md` — configuration, span coverage, safe
  attributes, and Application Insights query.
- [x] `docs/architecture.md` — clarify this spike-only instrumentation if
  needed; do not alter the production pipeline.
- [x] `docs/frds/README.md` — index this FRD.

## 8. Status & sign-off

- **Architecture review (phase 2):** independent review confirmed feasibility
  against the pinned SDK event schema. The protocol, trace-context handling,
  one-Activity-per-model-call lifecycle, span parenting, privacy boundaries,
  and tests were clarified in response; no blockers remain.
- **Human sign-off:** larohra approved the telemetry scope and contract on
  2026-10-28. **Finalized.**
