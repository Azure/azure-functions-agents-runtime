> **Deployed qualification fixture — not an E2E app.**
>
> This app is deployed to the protected Flex Consumption test apps by the
> post-main ACA qualification stages (FRD 0008 §14, issue #166). It is
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
| `agents.config.yaml` | Selects the ACA Sandbox backend via `session_runtime.aca_sandbox` |
| `qualification.agent.md` | One built-in-endpoint agent, slug `qualification` |
| `function_app.py` | `create_function_app()` plus the fixture-only `/__buildinfo` route |
| `host.json` | Empty route prefix, so agent routes are `/agents/<slug>/...` |
| `requirements.txt` | Generated, fully pinned; installs the runtime wheel by filename |

## No configuration is committed

Every environment-specific value is read from app settings at load time using
`$VAR` substitution — the sandbox group resource ID, the model endpoint and
deployment, and storage. Nothing identifying a subscription, resource group,
site, or endpoint appears in this directory.

## `/__buildinfo`

Returns the `BUILD_INFO.json` that the pipeline stamps into this directory
before deployment, plus values read live from the running host.

It is **corroborating evidence, not self-attestation**: it is trustworthy only
because the marker is a *file inside the deployed package*. A file can be served
only if the package containing it is genuinely on disk, so the app cannot report
a build it is not running. An app setting or resource tag could be changed
without deploying anything, which is exactly where "the service reports its own
version" stops being evidence.

The route is defined here, in the fixture, and touches no product module — in
particular not `registration/endpoints.py` (FRD 0008 Decision 172).
