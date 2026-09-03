> **Deployed qualification fixture — not an E2E app.**
>
> This app is deployed to a protected Flex Consumption test app by the deployed
> ACA qualification tooling in `eng/scripts/` (FRD 0008 §14, issue #166). It is
> deliberately **not** under `tests/endtoend/apps/`, because
> `tests/endtoend/test_apps_start.py` globs `*/host.json` there and
> auto-parameterizes a `func start` test over every match. This app selects the
> ACA Sandbox backend, so it cannot start without real Azure and would fail that
> suite.

# ACA qualification fixture app

A minimal agent app whose only job is to be **deployed** and then exercised by
the deployed qualification suites (`tests/live/test_aca_deployed_*.py`).

## Contents

| File | Purpose |
| --- | --- |
| `agents.config.yaml` | Selects the ACA Sandbox backend, Entra HTTP auth, model, timeout, and 120-second reclaim retention; successful results preserve expiry for 300 seconds from terminal run update |
| `deployed_turn.agent.md` | No-tools built-in-endpoint agent, slug `deployed_turn` |
| `deployed_load.agent.md` | Load/loss built-in-endpoint agent, slug `deployed_load` |
| `tools/qualification_hold.py` | Fixture-only tool that holds an active run for load and backing-loss suites |
| `function_app.py` | `create_function_app()` plus the fixture-only `/__buildinfo` route |
| `host.json` | Default `/api` route prefix, so agent routes are `/api/agents/<slug>/...` |
| `requirements.txt` | Generated, fully pinned; installs the runtime wheel by filename |

## No configuration is committed

Every environment-specific value is read from app settings at load time using
`$VAR` substitution — the sandbox group resource ID, the model endpoint and
deployment, and storage. Nothing identifying a subscription, resource group,
site, or endpoint appears in this directory.

The operator supplies those settings as app settings on the target Function
App before deployment. The deploy command sets only the required Sandbox Group
region; the group resource ID, model deployment, storage, and Entra values must
already be configured. This layer adds no pipeline wiring — the deploy is
driven by `eng/scripts/aca_qualification_pipeline.py` by hand.

The fixed 120-second reclaim policy supports the lifecycle suite and N=5 load
diagnostics only. N=100 is rejected before authentication or provider work; it
remains future human-only formal acceptance requiring a purpose-built workflow.

## `/__buildinfo`

Returns the `BUILD_INFO.json` that `aca_qualification_pipeline.py assemble`
stamps into this directory before deployment, plus values read live from the
running host.

It is **corroborating evidence, not self-attestation**: it is trustworthy only
because the marker is a *file inside the deployed package*. A file can be served
only if the package containing it is genuinely on disk, so the app cannot report
a build it is not running. An app setting or resource tag could be changed
without deploying anything, which is exactly where "the service reports its own
version" stops being evidence.

This is intentionally not a detached content-addressed chain: it does not prove
the wheel digest, installed package version, deploy-input manifest, or deployment
storage version. FRD 0008 Decision 193 records that narrowed scope.

The route is defined here, in the fixture, and touches no product module — in
particular not `registration/endpoints.py` (FRD 0008 Decision 172).
