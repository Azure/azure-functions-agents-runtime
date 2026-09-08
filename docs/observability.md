# Observability

The runtime emits OpenTelemetry traces and metrics so you can see what an agent and its tools
actually did — and tell whether a failure is the **app's** fault or the **platform's** — without
writing any telemetry code. This page explains how to turn it on, what we emit, and how the
attribute names are structured.

> App authors don't need to read this to get value: with telemetry enabled, the standard
> Application Insights views light up automatically. The optional dashboards/workbooks that make
> monitoring even easier are in [Appendix A](#appendix-a-optional-app-level-enhancements).

## Turn it on (no code)

The app's `function_app.py` stays two lines — you enable telemetry through configuration only.

**Minimum (path of least resistance):**

1. `APPLICATIONINSIGHTS_CONNECTION_STRING` — already present on most function apps.
2. Install the worker exporter extra: `pip install azurefunctions-agents-runtime[monitor]`. The
   runtime configures Azure Monitor **automatically** when that extra is installed and no
   OpenTelemetry provider is already active.

That's it — when a real OpenTelemetry provider is active, the runtime's `agent.run` /
`dynamic_session.execute` spans and metrics flow to Application Insights. Without the `[monitor]`
extra, the runtime's worker spans are not exported unless another OpenTelemetry provider is already
active, and if a connection string is set in that state the runtime logs a warning telling you to
install `azurefunctions-agents-runtime[monitor]`.

**Optional — unified host + worker correlation:**

Add `"telemetryMode": "OpenTelemetry"` to `host.json` if you *also* want the Functions **host** to
export in OpenTelemetry format, so host request telemetry and the runtime's worker spans share one
end-to-end operation. This is **additive** — it is **not required** for the agent/tool spans
themselves. Avoid turning on both a worker exporter and host worker-streaming
(`PYTHON_APPLICATIONINSIGHTS_ENABLE_TELEMETRY`) for the same data, which can double telemetry volume.
The runtime now auto-detects an already-configured OpenTelemetry provider and skips its own Azure
Monitor setup, so the worker path will not double-export — though it is still unnecessary when the
runtime is already configuring the exporter.

The runtime bootstraps everything from `create_function_app()`
(`_observability.configure_observability()`): it enables Microsoft Agent Framework (MAF) `gen_ai`
instrumentation and, when the `[monitor]` extra is installed and no OpenTelemetry provider is
already active, the Azure Monitor exporter. When no OpenTelemetry provider is active it is a no-op.

## Where the runtime's output shows up (traces, not logs)

The runtime is instrumented **span-first**, so its telemetry surfaces as **spans** — look in
**Transaction Search** / the end-to-end transaction view, **not** the "Logs" (`AppTraces`) list.

The runtime's own log lines are **intentionally not in your Application Insights `traces`**: its
internal logger sits under the `azure.functions.*` namespace, which the Functions Python worker
classifies as **system logs** (emitted with `customer_app_insight = false`), so they never reach your
app's `traces`. `host.json` `logLevel` does **not** surface them, and worker-side `DEBUG` would
require the `PYTHON_ENABLE_DEBUG_LOGGING` app setting. This is by design.

**So to debug a run, use the spans — not the log list:**

- `agent.run {name}` and `dynamic_session.execute`, with all the `af.*` attributes below.
- Runtime **span events** on `agent.run` (input/response-contract milestones — see that span's
  section) and MAF's `gen_ai.*` child spans (per model/tool call).
- **Failures are captured on the span**, not just in logs: a failing run is marked error with
  `record_exception` + `af.fault_domain`, so it is fully visible in the transaction view and the
  `AppExceptions` table.

The host lifecycle lines you *do* see in the logs list (`Executing…`, `Executed… (Succeeded)`,
`Function duration…`) come from the Functions **host** — a separate telemetry plane from the
runtime's worker spans. Use the [Quick KQL](#quick-kql) below to pull a whole run together.

## Attribute naming: what `af.` means

Every attribute the runtime adds is prefixed **`af.`** — short for **Azure Functions agents**.
The prefix:

- keeps our attributes from colliding with MAF's `gen_ai.*` or OpenTelemetry semantic conventions,
- makes them trivial to query — *everything we add starts with `af.`*.

Four sub-namespaces group the detail, plus two cross-cutting attributes:

| Namespace | Used for |
| --- | --- |
| `af.agent.*` | attributes on the per-run `agent.run {name}` span |
| `af.dynamic_session.*` | attributes on the `dynamic_session.execute` (code sandbox) span |
| `af.web_request.*` | attributes on the `web_request` (outbound HTTP tool) span |
| `af.delegate.*` | attributes on the `execute_tool delegate_<slug>` span (chat-time sub-agent delegation) |
| `af.fault_domain`, `af.lifecycle_stage` | cross-cutting; may appear on any runtime span |

Where a standard OpenTelemetry attribute already exists we reuse it instead of inventing an `af.`
name — for example `server.address` for the session-pool host. MAF keeps emitting its own
`gen_ai.*` spans (agent invocation, chat, tool calls, token usage); we don't touch those.

## Spans and attributes we emit today

### Cross-cutting `af.*` (any runtime span)

| Attribute | Meaning |
| --- | --- |
| `af.fault_domain` | Whose fault a failure is: `app`, `runtime`, `platform`, `model`, `connector`, `sandbox`, `web_request`, `delegate`, `unknown`. Set **only on failing spans**. |
| `af.lifecycle_stage` | Which run stage the span represents, e.g. `agent_run`, `tool_execution`. |

### Span `agent.run {name}`

One per agent invocation (timer, connector, HTTP, …). It is the parent that ties the MAF `gen_ai`
spans and the sandbox/`web_request` tool spans together.

| Attribute | Meaning |
| --- | --- |
| `af.agent.name` | Agent name. |
| `af.agent.trigger_type` | `timer`, `connectorTrigger`, `http`, … |
| `af.agent.model` | Model/deployment used. |
| `af.agent.session_id` | Conversation/session id. |
| `af.agent.outcome` | `success` or `error`. |
| `af.agent.tool_call_count` | Number of tool calls in the run. |
| `af.agent.tool_error_count` | Tool calls that failed — includes a "successful" call whose result carried an error or non-empty stderr, **plus** any `delegate_<slug>` call the coordinator recovered from (a specialist failure or timeout — see [Span `execute_tool delegate_<slug>`](#span-execute_tool-delegate_slug-chat-time-sub-agent-delegation)). |
| `af.agent.input_bytes` | Size of the trigger payload / HTTP body. |
| `af.agent.response_bytes` | Size of the model's final response. |
| `af.agent.input` | The trigger payload / HTTP body. **Content — only when `ENABLE_SENSITIVE_DATA=true`.** |
| `af.agent.response` | The final response text. **Content — only when `ENABLE_SENSITIVE_DATA=true`.** |

Plus `af.lifecycle_stage=agent_run`, and `af.fault_domain` if the run fails.

#### Span events (runtime lifecycle milestones)

These `agent.run {name}` span events mark runtime-owned input/output-contract boundaries. They carry
only non-sensitive metadata (names/status/counts — never request/response/model content). MAF's
`gen_ai.*` child spans already cover per-model/per-tool detail, so these events intentionally track
runtime milestones rather than duplicating tool/model spans.

- `af.input.validation_failed` — HTTP input-schema validation failed before agent execution; includes
  `af.fault_domain=app` and `af.http.status_code`.
- `af.response.invalid_json` — the agent completed, but its HTTP response could not be parsed as the
  required JSON contract; includes `af.fault_domain=app`.
- `af.response.schema_validation_failed` — the agent completed, but the parsed HTTP JSON response
  failed schema validation; includes `af.fault_domain=app`.
- `af.agent.invoke.completed` — `_run_agent(...)` returned successfully and the runtime is handling
  the final response contract.

### Span `dynamic_session.execute`

One per `execute_python` call, as a child of `agent.run`.

| Attribute | Meaning |
| --- | --- |
| `server.address` | Session-pool host (OTel semconv). |
| `af.operation_id` | Correlation id also sent to ACA in the `operation-id` header. |
| `af.dynamic_session.session_id` | ACA dynamic-session id. |
| `af.dynamic_session.code_bytes` | Size of the submitted code. |
| `af.dynamic_session.stdout_bytes` | Size of stdout. |
| `af.dynamic_session.stderr_bytes` | Size of stderr. |
| `af.dynamic_session.stderr_present` | `true` ⇒ the code failed (this is what used to be invisible). |
| `af.dynamic_session.session_reused` | Whether the ACA session already existed. |
| `af.dynamic_session.setup_ran` | Whether the one-time session setup ran this call. |
| `af.dynamic_session.code` / `.stdout` / `.stderr` | **Content — only when `ENABLE_SENSITIVE_DATA=true`.** |

Plus `af.lifecycle_stage=tool_execution`. When stderr is present or the call throws, the span is
marked ERROR with `af.fault_domain=sandbox`. This is the key fix: a broken execution no longer
looks like a successful tool call.

### Span `web_request`

One per `web_request` tool call, as a child of `agent.run`. Attributes are deliberately
**host-only** — the full URL (with query string, and any userinfo) is never attached to the span,
and secrets are never logged, regardless of `ENABLE_SENSITIVE_DATA`.

| Attribute | Meaning |
| --- | --- |
| `http.request.method` | HTTP verb used (`GET`, `POST`, …). |
| `server.address` | Target host (OTel semconv) — set once the SSRF validator has approved a host. |
| `url.scheme` | `http` or `https`. |
| `http.response.status_code` | Response status code, when a response was received. |
| `af.web_request.blocked_reason` | Present only when the SSRF validator rejects the request (e.g. `private_ip`, `imds`, `allowlist_denied`). |
| `af.web_request.response_bytes` | Size of the response body actually read (before truncation applies). |
| `af.web_request.body_truncated` | `true` when the response exceeded `max_response_bytes` and was truncated. |

Plus `af.lifecycle_stage=tool_execution`. SSRF rejections, timeouts, and transport errors all mark
the span ERROR with `af.fault_domain=web_request`.

### Span `execute_tool delegate_<slug>` (chat-time sub-agent delegation)

Chat-time delegation ([FRD 0007](./frds/0007-multi-agent-delegation.md)) needs **no new span** —
the `delegate_<slug>` tool's handler calls the specialist's plain, non-streaming `Agent.run(task)`
directly, and MAF already traces every `Agent.run()` and
every `FunctionTool.invoke()`. A coordinator that declares `subagents:` gets this nested span tree
for free the moment a `delegate_<slug>` tool is called:

```
agent.run {coordinator}              runtime span (af.*)
└─ invoke_agent {coordinator}        MAF
   ├─ chat {model}                   the routing decision
   └─ execute_tool delegate_<slug>   the delegation (an ordinary tool span)
      └─ invoke_agent {specialist}   auto-nested
         └─ chat {model}             the specialist's own model call
```

All of these spans share one trace, so Application Insights ties the whole fan-out together under a
single `OperationId` — including **concurrent** specialists (`asyncio.gather`), because OpenTelemetry
context propagates through `contextvars` into each gathered task.

The runtime does not open a *new* span for delegation — it annotates the existing
`execute_tool delegate_<slug>` span (already opened by MAF's `FunctionTool.invoke()`) with
`af.delegate.*` attributes, the same way `agent.run` is annotated, for parity with the
sandbox/`web_request` tools:

| Attribute | Meaning |
| --- | --- |
| `af.delegate.specialist` | The specialist's slug (the same identity used for its `delegate_<slug>` tool name). |
| `af.delegate.outcome` | `success`, `error`, `timeout`, or `cancelled`. |
| `af.delegate.task_bytes` | Size of the `task` argument passed to the specialist. |
| `af.delegate.response_bytes` | Size of the specialist's response text (only set on success). |
| `af.delegate.task` / `.result` | **Content — only when `ENABLE_SENSITIVE_DATA=true`.** |

Plus `af.fault_domain=delegate` on a failing span: the specialist raised (including a failure
*constructing* the specialist itself), or the *effective*
delegation timeout — `min(specialist timeout, coordinator's remaining time)` — was exceeded. A
**parent/request cancellation** (`asyncio.CancelledError`) is different: the handler tags the span
`outcome=cancelled` and still counts it in the delegate *call* metric (it was genuinely dispatched),
but never converts it into a recoverable error — it re-raises immediately and aborts the whole run,
rather than being recorded as a delegate error (FRD 0007 Decision #12).

**Error accounting.** `_looks_like_tool_error` (the sandbox/`web_request` JSON `{"error": …}` /
non-empty-`stderr` heuristic) does not understand a specialist's sanitized free-text failure message,
so relying on it alone would silently under-count. The delegated adapter tracks its own recoverable
failures explicitly and folds them into `af.agent.tool_error_count` on top of the heuristic's count.

**Accepted limitations (v1):**
- **SSE is a black box at the boundary.** The coordinator's stream emits `tool_start`/`tool_end` for
  `delegate_<slug>` (task in, final text out) exactly like the sandbox/`web_request` tools — a
  specialist's own internal deltas and nested tool calls do not surface on the wire unless a MAF
  `stream_callback` is wired into `run_agent_stream` (out of scope for v1).
- **Token usage does not roll up across the boundary.** MAF records usage per-run on each
  `invoke_agent`/`chat` span; the `execute_tool` span carries no usage, and a specialist's tokens are
  not merged into the coordinator's totals. Sum the child spans by trace (`OperationId`) in the
  backend for a combined per-request total.

### Metrics

Namespace `azure_functions_agents.*`:

| Metric | Meaning |
| --- | --- |
| `azure_functions_agents.dynamic_session.executions` | Count of `execute_python` calls. |
| `azure_functions_agents.dynamic_session.errors` | Count that failed or produced stderr. |
| `azure_functions_agents.web_request.requests` | Count of `web_request` tool calls. |
| `azure_functions_agents.web_request.errors` | Count that were blocked by the SSRF validator, timed out, or otherwise failed. |
| `azure_functions_agents.delegate.calls` | Count of `delegate_<slug>` tool invocations (chat-time sub-agent delegation). |
| `azure_functions_agents.delegate.errors` | Count that failed, raised, or timed out (specialist-side; sanitized before reaching the model). |

## Sensitive data

Sensitive-data capture is controlled by the single `ENABLE_SENSITIVE_DATA` environment variable from
Microsoft Agent Framework, **default off**.

- **Off (default):** only metadata is recorded — sizes, counts, outcome, fault domain. The
  `*_bytes` attributes above are emitted; the content attributes are not.
- **On:** content attributes are attached (bounded in length): `af.agent.input`, `af.agent.response`,
  `af.dynamic_session.code` / `.stdout` / `.stderr`, `af.delegate.task` / `.result`, plus MAF
  prompt/response/tool-arg content (via `enable_instrumentation(enable_sensitive_data=True)`).
- **Never captured, regardless of the flag:** secrets — MCP `Authorization` headers/tokens,
  connection strings, and the ACA system key. Endpoints are reduced to host only. The `web_request`
  span never carries the full request URL (query string or userinfo stripped), request/response
  bodies, or header values — it is host/status/size metadata only, unaffected by
  `ENABLE_SENSITIVE_DATA`.

## Noise & cost control

Most Application Insights volume from a function app is **not** the runtime's spans — on a real app,
runtime + MAF spans were only a few KB/run, while over 90% of ingestion was low-signal traces:
Azure SDK HTTP request/response dumps, the Azure Monitor exporter's own "Transmission succeeded…"
logs, credential chatter, and Functions host startup dumps.

**Worker-side noise — handled for you.** Regardless of whether telemetry export is active, the
runtime raises the log level of known-noisy third-party loggers (Azure SDK HTTP logging, the Azure
Monitor exporter, `azure.identity`, `httpx`, and OpenTelemetry internals) — but only when no level
is set directly on that logger, so a level you set directly on it is never overridden (a level set
on a parent/root logger is not consulted). See `_NOISY_LOGGERS` in `_observability.py`.

**Host-side noise — set it in `host.json`.** Host startup/options logging is emitted by the
Functions host, so quiet it with log-level overrides (or the equivalent
`AzureFunctionsJobHost__logging__logLevel__<category>` app settings):

```json
{
  "logging": {
    "logLevel": {
      "default": "Warning",
      "Host.Startup": "Warning",
      "Host.Function.Console": "Warning",
      "Microsoft.Azure.WebJobs.Hosting.OptionsLoggingService": "Warning"
    }
  }
}
```

**Other levers:** keep `ENABLE_SENSITIVE_DATA` off (default); sample with
`OTEL_TRACES_SAMPLER=parentbased_traceidratio` + `OTEL_TRACES_SAMPLER_ARG=0.1`; set a daily cap /
retention on the workspace; and **avoid double export** — don't run both the worker exporter and
host worker-streaming (`PYTHON_APPLICATIONINSIGHTS_ENABLE_TELEMETRY` + `telemetryMode`) for the same
telemetry. The runtime now detects an existing OpenTelemetry provider and skips its own Azure
Monitor setup, so enabling the worker exporter path will not double-export — it is simply
unnecessary when the runtime is already handling exporter configuration.

**Delegation-heavy apps:** a single coordinator turn with several `subagents:` can fan out into many
child spans (coordinator + N specialists + MAF's own `chat`/`invoke_agent` children — see
[Span `execute_tool delegate_<slug>`](#span-execute_tool-delegate_slug-chat-time-sub-agent-delegation)).
Azure Monitor's default rate-limited sampler counts spans, so a large fan-out can exhaust its budget
and drop whole traces under load — prefer the explicit `OTEL_TRACES_SAMPLER=parentbased_traceidratio`
+ `OTEL_TRACES_SAMPLER_ARG` setting above instead of relying on the default. Because that sampler is
trace-id-deterministic, a sampling decision applies consistently to the whole nested trace (no
half-traces), but logs on a dropped trace are dropped with it.

## Quick KQL

```kql
// Everything that happened in one run, in order
union AppRequests, AppDependencies, AppTraces, AppExceptions
| where OperationId == "<operation-id>"
| project TimeGenerated, itemType, Name, Message, Success, DurationMs
| order by TimeGenerated asc
```

```kql
// Sandbox executions that actually failed (even if the tool "succeeded")
AppDependencies
| where Name == "dynamic_session.execute"
| extend stderr_present = tostring(Properties["af.dynamic_session.stderr_present"])
| where Success == false or stderr_present == "true"
| project TimeGenerated, OperationId, DurationMs, Properties
```

```kql
// Delegate calls that failed or timed out (recovered — the coordinator kept running)
AppDependencies
| where Name startswith "execute_tool delegate_"
| extend outcome = tostring(Properties["af.delegate.outcome"]), specialist = tostring(Properties["af.delegate.specialist"])
| where outcome in ("error", "timeout")
| project TimeGenerated, OperationId, specialist, outcome, DurationMs, Properties
```

### Measuring telemetry volume (billed bytes per run)

Use these to size ingestion/cost before and after enabling observability. `_BilledSize` is the
billed bytes per item.

```kql
// Average billed volume per agent run, broken down by table.
// A "run" = an operation that contains an invoke_agent span for this app role.
let runs = AppDependencies
| where TimeGenerated > ago(30d)
| where AppRoleName == "func-agents-6q2arnxahkobm"        // <-- your function app role name
| where Name startswith "invoke_agent "
| distinct OperationId;
union withsource=TableName AppRequests, AppDependencies, AppTraces, AppExceptions, AppMetrics
| where TimeGenerated > ago(30d)
| where OperationId in (runs)
| summarize items = count(), billedBytes = sum(_BilledSize) by OperationId, TableName
| summarize avgItemsPerRun = avg(items), avgBytesPerRun = avg(billedBytes) by TableName
| order by avgBytesPerRun desc
```

```kql
// Isolate THIS runtime's spans (agent.run + dynamic_session.execute) vs MAF gen_ai spans.
// Rerun after deploying an observability change to confirm the real before/after.
let runs = AppDependencies
| where TimeGenerated > ago(7d)
| where AppRoleName == "func-agents-6q2arnxahkobm"
| where Name startswith "invoke_agent "
| distinct OperationId;
AppDependencies
| where TimeGenerated > ago(7d)
| where OperationId in (runs)
| summarize
    runtimeSpanItems = countif(Name == "dynamic_session.execute" or Name startswith "agent.run "),
    runtimeSpanBytes = sumif(_BilledSize, Name == "dynamic_session.execute" or Name startswith "agent.run "),
    mafItems = countif(Name startswith "invoke_agent " or Name startswith "chat " or Name startswith "execute_tool "),
    mafBytes = sumif(_BilledSize, Name startswith "invoke_agent " or Name startswith "chat " or Name startswith "execute_tool ")
  by OperationId
| summarize avgRuntimeSpanItems = avg(runtimeSpanItems), avgRuntimeSpanBytes = avg(runtimeSpanBytes),
            avgMafItems = avg(mafItems), avgMafBytes = avg(mafBytes)
```

---

## Appendix A — Optional app-level enhancements

These make monitoring easier for app authors but are **not required** — share them if someone
wants richer dashboards. They build on the spans/metrics above.

- **Tier 0 — Native portal (automatic).** With telemetry on, Transaction Search, Application Map,
  Failures, and the GenAI/Agents views work with no queries, because the runtime emits the
  `agent.run` parent span plus MAF `gen_ai` children.
- **Tier 1 — One `azd`-provisioned Workbook.** An Azure Monitor Workbook
  (`Microsoft.Insights/workbooks`) wired to the app's App Insights: runs over time, failure rate,
  tool failure rate, dynamic-session errors, token usage, and a failed-runs drilldown by
  `operation_Id`. A reference module ships with the sample app at
  `infra/app/observability-workbook.bicep`.
- **Tier 2 — Optional alerts (off by default).** Bicep metric/scheduled-query alert rules for run
  failure-rate and dynamic-session error-rate.
- **Tier 3 — Metrics charts (no KQL).** Build portal charts from the `azure_functions_agents.*`
  metrics by picking a metric and dimension.
- **Tier 4 — KQL pack.** Copy-paste queries (see "Quick KQL" above) for deep dives.

## Appendix B — Roadmap

**Implemented (must-have):** runtime-owned OTel bootstrap + Azure Monitor auto-configuration via
the optional `[monitor]` extra; fault-domain / lifecycle-stage conventions; sandbox truth-telling +
ACA correlation; the `agent.run` summary span; sensitive-data gating (default off); chat-time
sub-agent delegation span enrichment + error accounting (FRD 0007).

**Follow-ups (good-to-have):**

- Broader fleet metrics (per-agent run counts/duration, tokens, tool-call metrics).
- Full stage attribution for registration, model build, tool/MCP **dropped-tool**, storage, and
  response-validation failures (today's remaining silent gaps).
- Automatic per-tool span enrichment via the tool wrapper.
- The Tier-1/2 dashboards and alerts above, shipped as reusable infra.
- Delegation SSE passthrough (surface a specialist's internal stream deltas through the
  coordinator's stream) and cross-boundary token roll-up (FRD 0007 §4.12 accepted limitations).

## Implementation map

| Area | Files |
| --- | --- |
| Bootstrap, conventions, helpers, metrics | `src/azure_functions_agents/_observability.py` |
| Bootstrap call site | `src/azure_functions_agents/app.py` |
| Sensitive-data env handling (`ENABLE_SENSITIVE_DATA`) | `src/azure_functions_agents/_observability.py` |
| Sandbox span + stderr surfacing + ACA correlation | `src/azure_functions_agents/system_tools/sandbox.py` |
| `web_request` span, SSRF blocking, and truncation reporting | `src/azure_functions_agents/system_tools/web_request.py` |
| `agent.run` span + sensitive-log gating | `src/azure_functions_agents/registration/_handlers.py` |
| `delegate_<slug>` tool build, failure/cancellation adapter, span annotation (FRD 0007) | `src/azure_functions_agents/runner.py` |
| Tests | `tests/test_observability.py`, `tests/test_system_tools_sandbox.py`, `tests/test_web_request.py`, `tests/test_runner_delegation.py` |
