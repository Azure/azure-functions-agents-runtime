---
frd: 0003
title: Runtime-owned observability (OpenTelemetry)
status: Finalized            # Draft → In review → Finalized  (→ Implemented after merge)
author: larohra
created: 2026-07-01
updated: 2026-07-02
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
Everything is enabled by installing the optional `[monitor]` extra and setting the standard `APPLICATIONINSIGHTS_CONNECTION_STRING`; the
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
- Keep the app's `requirements.txt` to a single line — `azurefunctions-agents-runtime[monitor]` pulls the runtime plus the exporter.

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
| bootstrap (app factory) | `app.py`, **new** `_observability.py` | `create_function_app()` calls `configure_observability()`: quiet noisy loggers (unconditionally); and when a connection string is present, the `[monitor]` exporter is importable, and no provider is active, `configure_azure_monitor()` + `enable_instrumentation(enable_sensitive_data=…)`. Idempotent; runtime spans no-op unless a real OpenTelemetry provider is active. |
| register | `registration/_handlers.py` | Both handlers wrap the run in an `agent.run {name}` span with `af.agent.*` attributes and outcome; response-validation failures tagged `af.fault_domain=app`; existing free-text response/trigger logs re-gated behind `capture_sensitive_data`. |
| execute | `system_tools/sandbox.py` | `dynamic_session.execute` span with `af.dynamic_session.*` attributes; non-empty stderr/exception ⇒ span ERROR + `dynamic_session.errors` metric (tool still returns its string); `operation-id` header sent to the ACA `/executions` API. |

### Authoring / API surface

- **Enablement** — no `agents.config.yaml` observability block; telemetry turns on when the `[monitor]`
  extra is installed and `APPLICATIONINSIGHTS_CONNECTION_STRING` is set (see "Exporter packaging" below).
- **Environment variables** — `ENABLE_SENSITIVE_DATA` (reused from MAF; default off) gates content
  capture across both MAF `gen_ai` content and the runtime's `af.*` content attributes.
- **Telemetry surface** (attribute prefix `af.` = "Azure Functions agents"):
  - Cross-cutting (any runtime span): `af.fault_domain`, `af.lifecycle_stage`.
  - Span `agent.run {name}`: `af.agent.{name,trigger_type,model,session_id,outcome,
    tool_call_count,tool_error_count,input_bytes,response_bytes}`; gated content `af.agent.input`,
    `af.agent.response`; runtime lifecycle span events
    `af.{input.validation_failed,response.invalid_json,response.schema_validation_failed,agent.invoke.completed}`.
  - Span `dynamic_session.execute`: `server.address` (OTel semconv), `af.operation_id` (the trace
    id, also sent to ACA in the `operation-id` header for correlation) + `af.dynamic_session.{session_id,
    code_bytes,stdout_bytes,stderr_bytes,stderr_present,session_reused,setup_ran}`; gated content
    `af.dynamic_session.{code,stdout,stderr}`.
  - Metrics: `azure_functions_agents.dynamic_session.executions` / `.errors`.
- **Exporter packaging (`[monitor]` extra)**: the Azure Monitor exporter ships as an **optional
  extra**, not a hard dependency — `pip install azurefunctions-agents-runtime[monitor]` (pinned
  `azure-monitor-opentelemetry==1.8.8`; see Decisions log #15 for the exporter gen_ai crash the pin
  avoids). The same pin is mirrored in the `dev` extra so CI, tests, and `mypy` still resolve the
  exporter. *Why an extra:* the OpenTelemetry **SDK** and ~20 transitive packages (the
  `opentelemetry-instrumentation-{django,fastapi,flask,logging,psycopg2,requests,urllib,urllib3}`
  auto-instrumentations, the resource detector, the exporter, `msrest`, `psutil`, …) reach the
  install **only** via the Azure Monitor Distro, so making it an extra removes all of them from the
  default install — apps that don't export (or adopt another backend later) don't pay for it, and the
  runtime core stops floating on one exporter's release cadence. *Enablement:* the extra installed
  **plus** `APPLICATIONINSIGHTS_CONNECTION_STRING`. When a connection string is present the runtime
  auto-configures Azure Monitor if the exporter is importable and no OpenTelemetry provider is already
  active (Decision #19); runtime spans/metrics are emitted only when a real provider is active — the
  one we configured, or one the Functions worker/host installed — otherwise every helper no-ops.
  *Silent-gap mitigation:* the one real cost of an extra is a target user forgetting it — connection
  string present but exporter missing. Because the SDK ships only with the extra, in that state the
  runtime's own spans are not even generated, let alone exported. This is made **loud, not silent**:
  runtime spans are suppressed when no provider is active (no orphan work), and the runtime logs a
  one-time WARNING telling the user to install `azurefunctions-agents-runtime[monitor]`; every install
  surface (samples, README, docs) uses `[monitor]`. *Host path (unchanged):* there is still **no
  host-only export path** for the runtime's worker spans — `host.json`
  `telemetryMode: OpenTelemetry` exports only **host** telemetry and propagates trace context, but
  does **not** export worker-emitted spans/metrics; the `[monitor]` extra (or the worker's
  `PYTHON_APPLICATIONINSIGHTS_ENABLE_TELEMETRY` provider) is the export route. The extra is also the
  seam for other first-class exporters (e.g. OTLP) later; Option 3 (the bare
  `azure-monitor-opentelemetry-exporter`) stays rejected — still a **Beta / pre-release** package.
  This reverses the original bundling decision — see Decisions log **#20** (superseding **#18**).
  Earlier follow-up (a) (trim the Distro's forced footprint) is largely **moot** — the extra removes
  the Distro from the default install; follow-up (b) (the inaccurate "falls back to the Functions host
  exporter" wording) is **done**.

### Compatibility

- Backward compatible. Observability is a no-op unless a real OpenTelemetry provider is active — which
  the runtime sets up when the `[monitor]` extra is installed and an App Insights connection string is
  present. No behavior change otherwise.
- All helpers degrade to no-ops if OpenTelemetry is unavailable.
- Noise control only *raises* a logger's level, and only when no level is set directly on that
  logger (its level is `NOTSET`); a level set directly on that logger is preserved. A level set on a
  parent/root logger is not consulted.
- Adds **no** hard dependency: the Azure Monitor exporter is an optional `[monitor]` extra (verified
  to resolve alongside `agent-framework-core==1.3.*`). Apps that want export install
  `azurefunctions-agents-runtime[monitor]`; the default install is lighter (the OpenTelemetry SDK and
  the Azure Monitor Distro are no longer pulled in).

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
| 15 | Mitigating the exporter gen_ai `TypeError` crash | pin distro to last-good `==1.8.8` / monkeypatch the exporter's gen_ai processor / wait for the fixed release | Narrow #8 to `azure-monitor-opentelemetry==1.8.8` (opentelemetry-sdk 1.40, mutable span attrs), so the exporter's gen_ai main-agent processor stays crash-free and deploys stop floating to 1.8.9 (sdk 1.43 freezes attrs on end → the processor's `on_end` write raises `TypeError`, failing the invocation). Known upstream bug fixed in exporter b55 (Azure/azure-sdk-for-python#47796, not yet on PyPI); revert to a range once it ships | Agent (Human approved) | 2026-07-01 |
| 16 | Span events for runtime lifecycle milestones | attributes only / add span events / duplicate MAF tool spans | Add `af.*` span events at runtime input/output-contract boundaries on `agent.run`; do NOT duplicate MAF `gen_ai.*` tool/model spans | Agent (Human approved) | 2026-07-02 |
| 17 | Surfacing the runtime's own logs in user App Insights | rename logger off `azure.functions.*` / keep + document / worker-provided logger | Keep the logger under `azure.functions.*` — the Functions Python worker classifies it as a **system log** (`customer_app_insight=false`), so it does not appear in the app's `traces`; **document** that spans (not logs) are the debugging surface and that failures are captured as span exceptions. Rename deferred (would change the App Insights `Category` and the samples' `host.json` keys) | Human | 2026-07-02 |
| 18 | Exporter packaging: bundle vs. optional extra | bundle `azure-monitor-opentelemetry` as a hard dep (Opt 1) / optional `[monitor]` extra (Opt 2) / bundle the bare `azure-monitor-opentelemetry-exporter` (Opt 3) | Keep bundled (Opt 1): there is no host-only export path for the runtime's worker spans, and an extra would add a silent-failure gate for our target users; trim the Distro's forced footprint as a follow-up. The `[monitor]` extra is the migration path once another exporter (e.g. OTLP) is first-class; Opt 3 rejected (exporter still Beta / pre-release). Rationale + follow-ups in §4 | Human (larohra) — PR #79 review | 2026-07-02 **(Superseded by #20.)** |
| 19 | Preventing double export when the worker also configures Azure Monitor | always configure / detect existing provider and skip / rely on distro idempotency | Detect an already-installed OTel SDK TracerProvider (e.g. the worker's `PYTHON_APPLICATIONINSIGHTS_ENABLE_TELEMETRY` path) and skip the runtime's `configure_azure_monitor()`; MAF instrumentation + runtime spans still ride the existing provider | Human (larohra) — PR #79 review | 2026-07-02 |
| 20 | Revisit #18 — exporter packaging + the `observability` config block (new feedback) | keep bundled (#18) / ship an optional `[monitor]` extra **and** remove the `observability` config block | **Reverse #18:** ship `azure-monitor-opentelemetry` as an optional **`[monitor]` extra** (the default install then drops the OpenTelemetry SDK + ~20 transitive packages, which reach the install *only* via the Distro) and **remove the `observability` config block** — the extra is the single opt-in, so the config `enabled` flag / double gate is redundant. Enablement = `[monitor]` installed + `APPLICATIONINSIGHTS_CONNECTION_STRING`; runtime spans emit only when a real OTel provider is active (else suppressed); a connection-string-set-but-exporter-missing state logs an actionable warning (no silent gap). `capture_sensitive_data` becomes **env-only, reusing MAF's `ENABLE_SENSITIVE_DATA`** (drop `AZURE_FUNCTIONS_AGENTS_CAPTURE_SENSITIVE_DATA`; verified MAF auto-reads it, so keeping our own name would clobber it). Noise control now runs **unconditionally**. Force-disable env knob deferred as a follow-up. The `dev` extra pulls the exporter so CI/tests/mypy still resolve it. **Follow-ups (not in this change):** rewrite §4 packaging rationale, runtime version bump. | Human (larohra) — new feedback | 2026-07-02 |

## 6. Test plan

- [x] Unit: `tests/test_observability.py` — enabled/sensitive resolution (config + env precedence),
  `configure_observability` sets the flag and is idempotent, `start_span` / `RuntimeSpan` no-op
  safety (including `RuntimeSpan.add_event`), `bounded_content` truncation,
  `record_sandbox_execution` safety, `_quiet_noisy_loggers` raises unset levels and respects
  explicit levels.
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
(P0 scope): `ruff` and `mypy --strict` clean, `pytest` green (311 tests). The change ships the Azure
Monitor exporter as an optional `[monitor]` extra (superseding the original bundling — see Decisions
log #20); the runtime version bump is a follow-up.

- **Architecture review (phase 2):** completed via an independent `rubber-duck` pass (gpt-5.4). It
  raised one blocker (reconcile the cross-cutting bootstrap with the architecture contract) and three
  refinements (over-broad noise-control guarantee, OTLP wording, `af.operation_id` scope). **All
  resolved:** boundary note added to §4 + `docs/architecture.md` §3/§6; noise-control wording narrowed
  in code + docs; `docs/front-matter-spec.md` OTLP wording corrected; `af.operation_id` scoped to the
  sandbox span. See Decisions log #11–#13.
- **Human sign-off:** @larohra — 2026-07-01. **Finalized.** Set `status: Implemented` after PR #79 merges.
