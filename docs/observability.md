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
2. The worker OpenTelemetry exporter package `azure-monitor-opentelemetry`. The runtime configures
   it **automatically**, so no code is needed. (If it isn't installed, the runtime falls back to the
   Functions host exporter — see the optional step below.)

That's it — the runtime's `agent.run` / `dynamic_session.execute` spans and metrics flow to
Application Insights.

**Optional — unified host + worker correlation:**

Add `"telemetryMode": "OpenTelemetry"` to `host.json` if you *also* want the Functions **host** to
export in OpenTelemetry format, so host request telemetry and the runtime's worker spans share one
end-to-end operation. This is **additive** — it is **not required** for the agent/tool spans
themselves. Avoid turning on both a worker exporter and host worker-streaming
(`PYTHON_APPLICATIONINSIGHTS_ENABLE_TELEMETRY`) for the same data, which can double telemetry volume.

**Optional — tune behavior in `agents.config.yaml`:**

```yaml
observability:
  enabled: true                  # default: on when the App Insights connection string is set
  capture_sensitive_data: false  # default: off — see "Sensitive data" below
```

The runtime bootstraps everything from `create_function_app()`
(`_observability.configure_observability()`): it enables Microsoft Agent Framework (MAF) `gen_ai`
instrumentation and, when the `azure-monitor-opentelemetry` package is installed, the Azure Monitor
exporter. When disabled or unconfigured it is a no-op.

## Attribute naming: what `af.` means

Every attribute the runtime adds is prefixed **`af.`** — short for **Azure Functions agents**.
The prefix:

- keeps our attributes from colliding with MAF's `gen_ai.*` or OpenTelemetry semantic conventions,
- makes them trivial to query — *everything we add starts with `af.`*.

Two sub-namespaces group the detail, plus two cross-cutting attributes:

| Namespace | Used for |
| --- | --- |
| `af.agent.*` | attributes on the per-run `agent.run {name}` span |
| `af.dynamic_session.*` | attributes on the `dynamic_session.execute` (code sandbox) span |
| `af.fault_domain`, `af.lifecycle_stage` | cross-cutting; may appear on any runtime span |

Where a standard OpenTelemetry attribute already exists we reuse it instead of inventing an `af.`
name — for example `server.address` for the session-pool host. MAF keeps emitting its own
`gen_ai.*` spans (agent invocation, chat, tool calls, token usage); we don't touch those.

## Spans and attributes we emit today

### Cross-cutting `af.*` (any runtime span)

| Attribute | Meaning |
| --- | --- |
| `af.fault_domain` | Whose fault a failure is: `app`, `runtime`, `platform`, `model`, `connector`, `sandbox`, `unknown`. Set **only on failing spans**. |
| `af.lifecycle_stage` | Which run stage the span represents, e.g. `agent_run`, `tool_execution`. |

### Span `agent.run {name}`

One per agent invocation (timer, connector, HTTP, …). It is the parent that ties the MAF `gen_ai`
spans and the sandbox span together.

| Attribute | Meaning |
| --- | --- |
| `af.agent.name` | Agent name. |
| `af.agent.trigger_type` | `timer`, `connectorTrigger`, `http`, … |
| `af.agent.model` | Model/deployment used. |
| `af.agent.session_id` | Conversation/session id. |
| `af.agent.outcome` | `success` or `error`. |
| `af.agent.tool_call_count` | Number of tool calls in the run. |
| `af.agent.tool_error_count` | Tool calls that failed — includes a "successful" call whose result carried an error or non-empty stderr. |
| `af.agent.input_bytes` | Size of the trigger payload / HTTP body. |
| `af.agent.response_bytes` | Size of the model's final response. |
| `af.agent.input` | The trigger payload / HTTP body. **Content — only when `capture_sensitive_data` is on.** |
| `af.agent.response` | The final response text. **Content — only when `capture_sensitive_data` is on.** |

Plus `af.lifecycle_stage=agent_run`, and `af.fault_domain` if the run fails.

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
| `af.dynamic_session.code` / `.stdout` / `.stderr` | **Content — only when `capture_sensitive_data` is on.** |

Plus `af.lifecycle_stage=tool_execution`. When stderr is present or the call throws, the span is
marked ERROR with `af.fault_domain=sandbox`. This is the key fix: a broken execution no longer
looks like a successful tool call.

### Metrics

Namespace `azure_functions_agents.*`:

| Metric | Meaning |
| --- | --- |
| `azure_functions_agents.dynamic_session.executions` | Count of `execute_python` calls. |
| `azure_functions_agents.dynamic_session.errors` | Count that failed or produced stderr. |

## Sensitive data

`capture_sensitive_data` is a single flag, **default off**, resolved from
`agents.config.yaml` (`observability.capture_sensitive_data`) or the environment
(`AZURE_FUNCTIONS_AGENTS_CAPTURE_SENSITIVE_DATA`, or MAF's `ENABLE_SENSITIVE_DATA`).

- **Off (default):** only metadata is recorded — sizes, counts, outcome, fault domain. The
  `*_bytes` attributes above are emitted; the content attributes are not.
- **On:** content attributes are attached (bounded in length): `af.agent.input`, `af.agent.response`,
  `af.dynamic_session.code` / `.stdout` / `.stderr`, plus MAF prompt/response/tool-arg content
  (via `enable_instrumentation(enable_sensitive_data=True)`).
- **Never captured, regardless of the flag:** secrets — MCP `Authorization` headers/tokens,
  connection strings, and the ACA system key. Endpoints are reduced to host only.

## Noise & cost control

Most Application Insights volume from a function app is **not** the runtime's spans — on a real app,
runtime + MAF spans were only a few KB/run, while over 90% of ingestion was low-signal traces:
Azure SDK HTTP request/response dumps, the Azure Monitor exporter's own "Transmission succeeded…"
logs, credential chatter, and Functions host startup dumps.

**Worker-side noise — handled for you.** When observability is enabled, the runtime raises the log
level of known-noisy third-party loggers (Azure SDK HTTP logging, the Azure Monitor exporter,
`azure.identity`, `httpx`, and OpenTelemetry internals) — but only when no level is set directly on
that logger, so a level you set directly on it is never overridden (a level set on a parent/root
logger is not consulted). See `_NOISY_LOGGERS` in `_observability.py`.

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

**Other levers:** keep `capture_sensitive_data` off (default); sample with
`OTEL_TRACES_SAMPLER=parentbased_traceidratio` + `OTEL_TRACES_SAMPLER_ARG=0.1`; set a daily cap /
retention on the workspace; and **avoid double export** — don't run both the worker exporter and
host worker-streaming (`PYTHON_APPLICATIONINSIGHTS_ENABLE_TELEMETRY` + `telemetryMode`) for the same
telemetry.

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

**Implemented (must-have):** runtime-owned OTel bootstrap + `observability` config; fault-domain /
lifecycle-stage conventions; sandbox truth-telling + ACA correlation; the `agent.run` summary span;
sensitive-data gating (default off).

**Follow-ups (good-to-have):**

- Broader fleet metrics (per-agent run counts/duration, tokens, tool-call metrics).
- Full stage attribution for registration, model build, tool/MCP **dropped-tool**, storage, and
  response-validation failures (today's remaining silent gaps).
- Automatic per-tool span enrichment via the tool wrapper.
- The Tier-1/2 dashboards and alerts above, shipped as reusable infra.

## Implementation map

| Area | Files |
| --- | --- |
| Bootstrap, conventions, helpers, metrics | `src/azure_functions_agents/_observability.py` |
| Bootstrap call site | `src/azure_functions_agents/app.py` |
| Config (`observability` block) | `src/azure_functions_agents/config/schema.py`, `config/__init__.py` |
| Sandbox span + stderr surfacing + ACA correlation | `src/azure_functions_agents/system_tools/sandbox.py` |
| `agent.run` span + sensitive-log gating | `src/azure_functions_agents/registration/_handlers.py` |
| Tests | `tests/test_observability.py`, `tests/test_system_tools_sandbox.py` |
