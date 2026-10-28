---
frd: 0013
title: Configurable Foundry inference in the Rust runtime spike
status: Finalized
author: larohra
created: 2026-10-28
updated: 2026-10-28
issues: []
pull_requests: []
branch: larohra/legendary-journey
---

# FRD 0013 - Configurable Foundry inference in the Rust runtime spike

## 1. Summary

The Rust/.NET runtime spike uses deterministic mock inference when Foundry is
not configured. When valid Foundry project settings are supplied, it starts the
live Foundry inference path instead. This lets the same HTTP and tool-call
sample run locally without cloud configuration or connect to a configured
Foundry project without changing code.

## 2. Motivation / problem

The spike currently requires mock mode even when a developer has configured a
Foundry project. Its Rust runtime contains provider and token-provider code, but
the .NET host always starts it in mock mode and reports every result as mock.
This prevents developers from exercising the live provider through the real
Functions host and makes available configuration ineffective.

The default mock path remains important: local host and Azurite smoke tests
must be deterministic, require no Azure credentials, and never contact a
placeholder endpoint.

## 3. Goals / Non-goals

**Goals**
- Start deterministic mock inference when no Foundry project configuration is
  supplied.
- Attempt live Foundry inference when a complete, valid Foundry project
  endpoint and model are configured.
- Fail clearly at startup for partial or invalid explicit Foundry
  configuration; do not silently fall back to mock.
- Preserve an explicit way for automated tests and local smoke runs to force
  mock inference, even if they provide provider-shaped placeholder values.
- Report the selected inference mode accurately in initialization logs and the
  support HTTP response.
- Exercise live authentication and model invocation through the existing Rust
  provider and SDK path without exposing tokens or secrets.
- Keep current local and Azurite mock smoke tests independent of Foundry
  credentials and network access.

**Non-goals**
- Changes to the production Python runtime or its harness selection.
- General multi-provider live inference enablement through the .NET host.
- New trigger types, Functions bindings, or non-HTTP tool adaptations.
- Bundling Azure CLI or SDK credentials, or provisioning Foundry resources.
- Enabling public access to the sample's anonymous local test endpoints.

## 4. Proposed design

This feature changes only the experimental Rust/.NET spike. It does not add a
stage or module to the production Python runtime pipeline.

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| discover | `src/config.rs`, `src/env.rs` | Retain discovery and environment substitution for the sample agent; resolve its configured model and the available provider settings. |
| translate | `src/providers.rs`, `src/protocol.rs` | Resolve inference mode and validate Foundry configuration. Keep provider/model settings and mock selection explicit in initialization. |
| register | `dotnet/HostedSkills.Spike/RuntimeService.cs`, `HostedSkillRuntimeClient.cs` | Select mock or live mode once during worker startup and pass that choice to the Rust runtime. Invalid partial live configuration fails startup. |
| execute | `src/engine.rs`, `src/providers.rs`, `dotnet/HostedSkills.Spike/OrderFunctions.cs` | Run either the deterministic mock turn or the existing Foundry SDK branch, and report the actual mode in host diagnostics and the support response. |

### Inference mode and configuration

The Rust runtime selects inference mode during initialization, after it has
discovered the agent and resolved its effective model:

- `FHS_MOCK_LLM=true` explicitly forces deterministic mock inference. This
  override takes precedence over provider settings and is used by automated
  tests and smoke runs, including those with placeholder endpoints.
- Without that override, any nonblank Foundry-intent setting
  (`AZURE_FUNCTIONS_AGENTS_PROVIDER=foundry`, `FOUNDRY_PROJECT_ENDPOINT`, or
  `FOUNDRY_MODEL`) selects an attempt at live Foundry inference. The setting
  expresses intent; malformed, partial, or unauthorized live configuration
  fails clearly and never silently falls back to mock.
- When none of those Foundry-intent settings is present, use deterministic
  mock inference. `DEFAULT_AGENT_MODEL` and `AZURE_FUNCTIONS_AGENTS_MODEL`
  alone do not select a live provider.

Live mode requires the Foundry project endpoint
(`FOUNDRY_PROJECT_ENDPOINT`) and a resolvable model deployment name. Resolve
the model in this order: the selected agent's resolved `model` (including
`DEFAULT_AGENT_MODEL` substitution), `AZURE_FUNCTIONS_AGENTS_MODEL`, then
`FOUNDRY_MODEL`. If the endpoint or model is absent, or the endpoint is
malformed, initialization fails with a configuration error. The endpoint must
be an HTTPS Foundry project endpoint in the supported
`/api/projects/<project>` form; a resource root or deployment URL is not
sufficient. Deployment names are project-specific opaque names: do not
special-case strings such as `mock-model`; smoke tests select mock explicitly.

The initialization result reports the selected mode. The C# host uses that
value in startup logs and the support response. Missing Foundry settings default
to mock, but a requested live path that fails authentication or inference
returns an error.

The Rust provider uses `https://ai.azure.com/.default` for Entra authentication.
It may use the supported Functions Managed Identity endpoint in a hosted
environment or Azure CLI token acquisition in a local developer environment.
Authentication and model invocation errors are surfaced as live inference
errors. The runtime must not retry by switching to mock mode.

The implementation must not log access tokens, API keys, the raw project
endpoint, `AZURE_CLIENT_ID`, or other credentials/identity values. Diagnostic
output may identify only the selected mode, provider, and effective model.

### Observability and compatibility

The initialization result, startup logs, and support HTTP response identify
whether the selected path is `MOCK` or `FOUNDRY`; live responses must never be
labeled `MOCK`. The existing mock response behavior, session persistence,
support route, and direct tool-call behavior remain unchanged. The mode is
selected at worker startup and does not change between turns.

The checked-in local and Azurite smoke tests explicitly select mock mode and
continue asserting deterministic tool results. Live validation is a separate,
opt-in smoke path that requires explicit live configuration; it must not use
placeholder Foundry settings or run against a service unless the operator has
provided the required project endpoint and model.

No Python authoring format or production API changes. The feature remains an
experimental .NET/Rust spike and does not establish production deployment
support.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Default and configured inference behavior | Always mock / always live / mock without Foundry settings and live when configured | Mock when no Foundry settings are provided; attempt Foundry when settings are provided | Human (larohra) | 2026-10-28 |
| 2 | Handling incomplete live configuration | Silently use mock / fail startup | Fail startup rather than silently fall back | Human (larohra) | 2026-10-28 |
| 3 | Preserving deterministic mock tests | Infer live from Foundry intent / explicit mock override | `FHS_MOCK_LLM=true` explicitly forces mock mode; mock is default when Foundry intent is absent | Human (larohra) | 2026-10-28 |
| 4 | Live model precedence and placeholder values | Agent model / provider setting / fixed name validation | Agent-resolved model, then `AZURE_FUNCTIONS_AGENTS_MODEL`, then `FOUNDRY_MODEL`; deployment names are opaque and are not specially rejected | Human (larohra) | 2026-10-28 |

## 6. Test plan

- [x] Rust provider/config tests: no Foundry settings select mock mode.
- [x] Rust provider/config tests: complete Foundry project endpoint and resolved
  model select live mode.
- [x] Rust provider/config tests: partial or malformed explicit Foundry
  configuration fails rather than selecting mock mode.
- [x] Rust/provider tests: explicit `FHS_MOCK_LLM=true` keeps mock mode even
  when provider-shaped placeholder values are present.
- [x] Rust provider/config tests: Foundry model precedence follows the resolved
  agent model, `AZURE_FUNCTIONS_AGENTS_MODEL`, then `FOUNDRY_MODEL`; arbitrary
  deployment names pass configuration validation.
- [x] C# checks: host forwards the selected mode and reports it accurately.
- [x] Existing local and Azurite real-host smoke tests remain deterministic,
  require no credentials, and make no live model request.
- [ ] Opt-in live smoke: with operator-provided Foundry settings and authorized
  credentials, the actual Foundry provider request succeeds and the HTTP
  response reports `FOUNDRY`.
- [ ] Failure checks: live authentication or model errors are surfaced without
  mock fallback; logs and HTTP output contain no token or secret.

The live service test is not a substitute for provider unit tests and is only
run when a real Foundry project, model deployment, network access, and valid
credentials are available. If those are unavailable, record live invocation
and authentication as unvalidated.

## 7. Docs impact

- [x] `spikes/rust-runtime/README.md` — document mode selection, Foundry
  configuration, authentication, and separate live validation; preserve mock
  smoke instructions.
- [x] `spikes/rust-runtime/LEADERSHIP-README.md` — distinguish verified mock
  results from configured live inference behavior and validation status.
- [x] `docs/architecture.md` — update only the experimental spike note; keep
  the production pipeline description unchanged.
- [x] `docs/frds/README.md` — index this FRD as Finalized.

## 8. Status & sign-off

- **Architecture review (phase 2):** Review requested exact Foundry-intent
  detection, model precedence, returned inference mode, validation ownership,
  and safe logging. The FRD was updated to record these contracts and received
  dedicated review approval on 2026-10-28.
- **Human sign-off:** larohra approved mock-by-default/live-when-configured,
  fail-fast handling of partial live settings, an explicit mock override, and
  model precedence on 2026-10-28.
