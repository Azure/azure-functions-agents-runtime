---
frd: 0003
title: Runtime-owned observability (OpenTelemetry)
status: Finalized            # Draft → In review → Finalized  (→ Implemented after merge)
author: larohra
created: 2026-07-01
updated: 2026-07-01
issues: []
pull_requests: [https://github.com/Azure/azure-functions-agents-runtime/pull/79]
branch: larohra/add-observability
---

# FRD 0003 — Runtime-owned observability (OpenTelemetry)

## 1. Summary

The runtime now emits OpenTelemetry traces and metrics for every agent run and adds a low/no-code
observability layer so an app author gets useful, correctly-attributed telemetry in Application
Insights without writing any telemetry plumbing. `create_function_app()` bootstraps Microsoft Agent
Framework (MAF) `gen_ai` instrumentation and the Azure Monitor exporter, emits a per-run
`agent.run {name}` span plus a `dynamic_session.execute` span for each sandbox call, tags failures
with a fault domain so problems are quickly triaged as **app vs runtime vs platform**, gates
sensitive content behind an opt-in flag (default off), and quiets known-noisy third-party loggers.
Everything is enabled by configuration and the standard `APPLICATIONINSIGHTS_CONNECTION_STRING`; the
app's `function_app.py` stays two lines.

## 2. Motivation / problem

Debugging a real agent run required manual KQL spelunking and a direct ACA probe to discover that
`execute_python` reported success while the sandbox had actually failed (a missing OCR binary). The
root causes were structural:

- The runtime emitted **no OpenTelemetry itself** — only `logging`. `gen_ai` spans appeared only if
  the *app* wired up instrumentation, which breaks the low/no-code promise.
- The sandbox tool **swallowed failures**: a non-empty `stderr` or an exception was returned as a
  *successful* tool result, so telemetry showed `Success=true` for a broken run.
- There was **no fault attribution** and **no correlation** between the Function App operation and
  the ACA-side execution, so every investigation started from zero.

App authors and operators feel this: they can't tell whether a bad run is their agent's fault, the
runtime's, or a downstream platform, and they can't see it at all without hand-rolling OTel.

## 3. Goals / Non-goals

**Goals**
- Enable OpenTelemetry from the runtime with **zero app code** — config + connection string only.
- Emit a per-run parent span and a sandbox span with structured, consistently-named attributes.
- Make failures self-classifying via a fault-domain attribute (app / runtime / platform / model /
  connector / sandbox).
- Surface sandbox failures (stderr/exception) as span errors + a failure metric, even though the
  tool still returns a string to the model.
- Correlate Function App telemetry with ACA dynamic-session executions.
- Keep sensitive content **off by default**; never capture secrets.
- Reduce low-signal telemetry noise by default.
- Keep the app's `requirements.txt` minimal (exporter comes transitively from the runtime).

**Non-goals**
- Full fleet metrics (per-agent run counts/duration/tokens, tool-call metrics) — deferred (P1).
- Full stage attribution for registration / model-build / dropped-tool / storage / response
  validation — deferred (P1).
- Automatic per-tool span enrichment in the tool wrapper — deferred (P1).
- Shipping dashboards/alerts as reusable infra — sample-app/optional only (P1).
- Changing agent authoring semantics or any non-observability behavior.

## 4. Proposed design

A new cross-cutting module `azure_functions_agents/_observability.py` owns the OTel bootstrap, the
span/attribute conventions, the resolved sensitive-data flag, minimal metrics, and noise control.
`create_function_app()` calls `configure_observability(global_config)` once, before agents run.

**Boundary note.** Observability is an intentional *cross-cutting* concern, not a fifth pipeline
stage. Its bootstrap runs at the app-factory level (before discovery), and it deliberately holds the
only Azure-Monitor/ACA-aware calls outside the registration stage — because exporting telemetry and
correlating an execution are *observing* the pipeline, not wiring agents into it. This is an
explicit, documented extension to the discover → translate → register → execute contract;
`docs/architecture.md` §3 (module map + startup trace) and §6 (boundaries) are updated to match.

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| translate | `config/schema.py`, `config/__init__.py` | New `ObservabilityConfig` (`enabled: bool \| None`, `capture_sensitive_data: bool`); added to `GlobalConfig.observability`; exported. |
| bootstrap (app factory) | `app.py`, **new** `_observability.py` | `create_function_app()` calls `configure_observability()`: resolve settings, `enable_instrumentation(enable_sensitive_data=…)`, optional `configure_azure_monitor()`, quiet noisy loggers. Idempotent, no-op when disabled. |
| register | `registration/_handlers.py` | Both handlers wrap the run in an `agent.run {name}` span with `af.agent.*` attributes and outcome; response-validation failures tagged `af.fault_domain=app`; existing free-text response/trigger logs re-gated behind `capture_sensitive_data`. |
| execute | `system_tools/sandbox.py` | `dynamic_session.execute` span with `af.dynamic_session.*` attributes; non-empty stderr/exception ⇒ span ERROR + `dynamic_session.errors` metric (tool still returns its string); `operation-id` header sent to the ACA `/executions` API. |

### Authoring / API surface

- **`agents.config.yaml`** — new optional `observability` block:
  ```yaml
  observability:
    enabled: true                  # default: on when App Insights connection string is present
    capture_sensitive_data: false  # default: off
  ```
- **Environment variables** — `AZURE_FUNCTIONS_AGENTS_OBSERVABILITY_ENABLED`,
  `AZURE_FUNCTIONS_AGENTS_CAPTURE_SENSITIVE_DATA` (and MAF's `ENABLE_SENSITIVE_DATA`).
- **Telemetry surface** (attribute prefix `af.` = "Azure Functions agents"):
  - Cross-cutting (any runtime span): `af.fault_domain`, `af.lifecycle_stage`.
  - Span `agent.run {name}`: `af.agent.{name,trigger_type,model,session_id,outcome,
    tool_call_count,tool_error_count,input_bytes,response_bytes}`; gated content `af.agent.input`,
    `af.agent.response`.
  - Span `dynamic_session.execute`: `server.address` (OTel semconv), `af.operation_id` (the trace
    id, also sent to ACA in the `operation-id` header for correlation) + `af.dynamic_session.{session_id,
    code_bytes,stdout_bytes,stderr_bytes,stderr_present,session_reused,setup_ran}`; gated content
    `af.dynamic_session.{code,stdout,stderr}`.
  - Metrics: `azure_functions_agents.dynamic_session.executions` / `.errors`.
- **New dependency**: `azure-monitor-opentelemetry==1.8.*` (so the exporter is transitive and the
  app needs only `azurefunctions-agents-runtime`).

### Compatibility

- Backward compatible. Observability is a no-op unless enabled (default: on only when an App
  Insights connection string is present); no behavior change when disabled.
- All helpers degrade to no-ops if OpenTelemetry is unavailable.
- Noise control only *raises* a logger's level, and only when no level is set directly on that
  logger (its level is `NOTSET`); a level set directly on that logger is preserved. A level set on a
  parent/root logger is not consulted.
- Adds one hard dependency (`azure-monitor-opentelemetry`); verified to resolve alongside
  `agent-framework-core==1.3.*`. Apps that previously added it explicitly can drop that line.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Where the OTel bootstrap lives | App `function_app.py` / runtime `create_function_app()` | Runtime-owned | Agent (Human approved) | 2026-06-30 |
| 2 | Enablement default | Always on / off / auto | Auto: on when `APPLICATIONINSIGHTS_CONNECTION_STRING` present | Agent | 2026-06-30 |
| 3 | Attribute naming | flat `af.*` / dotted namespaces | `af.` prefix; grouped `af.agent.*`, `af.dynamic_session.*`; reuse semconv (`server.address`) | Human raised, Agent standardized | 2026-07-01 |
| 4 | Sensitive content default | on / off | Off (parity with MAF `ENABLE_SENSITIVE_DATA`) | Agent | 2026-06-30 |
| 5 | Sandbox failure signalling | keep returning success string / mark span error | Return string to model **and** mark span ERROR + failure metric on stderr/exception | Agent (fixes debugged bug) | 2026-06-30 |
| 6 | ACA correlation | none / pass operation-id | Send `operation-id` header on `/executions` | Agent | 2026-06-30 |
| 7 | Noise control | none / raise noisy loggers | Raise known-noisy loggers, only when the logger's own level is `NOTSET` | Agent (Human asked to clean up) | 2026-07-01 |
| 8 | Exporter dependency | app adds it / runtime bundles it | Runtime depends on `azure-monitor-opentelemetry==1.8.*` | Human asked, Agent decided | 2026-07-01 |
| 9 | `host.json` `telemetryMode` | required / optional | Optional (worker exporter path); additive for host correlation | Human asked, Agent clarified | 2026-07-01 |
| 10 | Scope/phasing | ship all / phase | P0 (this FRD) vs P1 follow-ups (see Non-goals) | Human | 2026-06-30 |
| 11 | Observability vs pipeline stages | new stage / cross-cutting | Cross-cutting app-factory concern outside the four stages; documented in `architecture.md` | Agent (after review) | 2026-07-01 |
| 12 | `af.operation_id` scope | all spans / sandbox only | Scoped to the `dynamic_session.execute` span (the value sent to ACA); not on `agent.run` | Agent (after review) | 2026-07-01 |
| 13 | Noise-control guarantee wording | broad "explicit level" / precise `NOTSET` | Narrowed docs to "no level set directly on that logger"; parent/root not consulted | Agent (after review) | 2026-07-01 |
| 14 | Gating runtime telemetry on `enabled` | rely on tracer/meter presence / gate on resolved state | Gate `start_span` + `record_sandbox_execution` on the resolved `_enabled` flag, so `observability.enabled: false` suppresses runtime spans/metrics even under a host OTel provider | Agent (PR #79 review) | 2026-07-01 |

## 6. Test plan

- [x] Unit: `tests/test_observability.py` — enabled/sensitive resolution (config + env precedence),
  `configure_observability` sets the flag and is idempotent, `start_span` / `RuntimeSpan` no-op
  safety, `bounded_content` truncation, `record_sandbox_execution` safety, `_quiet_noisy_loggers`
  raises unset levels and respects explicit levels.
- [x] Unit: `tests/test_system_tools_sandbox.py` — a clean run records success; a non-empty stderr
  is surfaced as an error while the tool still returns the payload string.
- [x] Regression: `tests/test_config_loader.py` — empty `GlobalConfig` dump includes the new
  `observability` key.
- [ ] Fixture scenario: not required — the config change is covered by existing config-loader/schema
  tests; add `tests/fixtures/config_scenarios/<nn_observability>/` only if authoring grows.

## 7. Docs impact

- [x] `docs/observability.md` — new: enablement, `af.` naming, span/attribute reference,
  sensitive-data, noise & cost control, and volume KQL.
- [x] `docs/front-matter-spec.md` — documents the `observability` global-config block.
- [x] `docs/architecture.md` — added the `_observability` cross-cutting module to the §3 module map
  and startup trace, plus a §6 boundary note; linked `observability.md` in §7.
- [x] `README.md` — added an "Observability" section under Deployment Notes (on by default with a
  connection string; links `docs/observability.md`).

## 8. Status & sign-off

This FRD documents work that is **already implemented and validated** on `larohra/add-observability`
(P0 scope): `ruff` and `mypy --strict` clean, `pytest` green (311 tests). The change bundles the
Azure Monitor exporter and bumps the runtime version to `0.1.0b5`.

- **Architecture review (phase 2):** completed via an independent `rubber-duck` pass (gpt-5.4). It
  raised one blocker (reconcile the cross-cutting bootstrap with the architecture contract) and three
  refinements (over-broad noise-control guarantee, OTLP wording, `af.operation_id` scope). **All
  resolved:** boundary note added to §4 + `docs/architecture.md` §3/§6; noise-control wording narrowed
  in code + docs; `docs/front-matter-spec.md` OTLP wording corrected; `af.operation_id` scoped to the
  sandbox span. See Decisions log #11–#13.
- **Human sign-off:** @larohra — 2026-07-01. **Finalized.** Set `status: Implemented` after PR #79 merges.
