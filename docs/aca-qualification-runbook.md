# ACA qualification runbook

Operating guide for the post-main ACA deployment and qualification stages in
`eng/ci/official-build.yml`. Design and decisions live in
[`frds/0008-aca-sandbox-session-runtime.md`](frds/0008-aca-sandbox-session-runtime.md) §14.

## What runs, and when

On every merge to `main` or `release/*` (the pipeline is `pr: none`):

```
AcaSweep ──┐
           ├─► AcaDeployColdPy313 ─┐
Build ─────┤                       ├─► AcaQualifyPy313
           └─► AcaDeployColdPy314 ─┘   AcaQualifyPy314
```

**Two phases.** Both runtimes finish deploy + attest + cold start before either
enters the qualification suite. A cold-start regression therefore fails fast for
both runtimes instead of being discovered after one has already spent its N=5
budget.

| Stage | Does |
| --- | --- |
| `AcaSweep` | Reports and clears sandboxes left by *earlier* runs. Never fatal. |
| `AcaDeployCold*` | Preflight → assemble → deploy (remote build) → verify build → cold start |
| `AcaQualify*` | Re-verify build → public turn → lifecycle → backing loss → N=5 |

Every stage is `continueOnError` during stabilization.

### ⚠ Network isolation on the official pipeline blocks the ACA data plane

**Open issue, found on the first live run.** The ACA data-plane host is not
reachable from pipeline **1733**:

```
ServiceRequestError: Cannot connect to host management.westus2.azuredevcompute.io:443
ssl:default [Operation not permitted]
```

`Operation not permitted` on an outbound connect is 1ES **network isolation**,
not authentication and not RBAC.

The e2e pipeline (**1777**) reaches the same host successfully, including on
PR merge refs. The material difference is that 1733 carries
`PipelineClassification_Audited: production`, and production-classified
pipelines get stricter egress policy.

This affects `AcaSweep` and any stage using the ACA SDK. Deployment itself uses
the `az` CLI against ARM, which is permitted.

Two ways out, and this is a policy decision rather than a code change:

1. **Request an egress exemption** for `*.azuredevcompute.io` on pipeline 1733.
   Keeps the stages in the official build, as currently designed.
2. **Move the ACA stages to a separate, non-production-classified pipeline**,
   mirroring how 1777 already runs ACA work. This reverses the decision to
   extend the official build, and would also re-narrow the deployment
   credential's blast radius.

Until one is chosen, the ACA stages are `continueOnError`, so they surface the
problem without blocking anything.

## Prerequisites

| Item | Notes |
| --- | --- |
| Two Flex Consumption Linux apps | one Python 3.13, one 3.14, Easy Auth enabled |
| Shared ACA Sandbox Group | both apps point at it |
| ADO service connection | currently `larohra-sandboxgroup-test`, reused for deployment |
| **Service-connection authorization for this pipeline** | see below — easy to miss |
| Pipeline variables | see below |

### Authorize the service connection for the official build pipeline

A service connection is authorized **per pipeline**. `larohra-sandboxgroup-test`
was originally authorized only for the e2e pipeline (1777), so the official build
(1733) could not use it.

The symptom is unhelpful: affected stages sit at `pending` indefinitely with no
error, no log, and no failed task. In the timeline they are held at a
`Checkpoint.Authorization`. It looks like a queue backlog rather than a
permission problem, and it blocks the pre-existing `RunE2ETests` stage too, not
just the ACA ones.

Check and grant:

```bash
# Inspect
az rest --method get --resource 499b84ac-1321-427f-aa17-267ca6975798 \
  --uri "https://dev.azure.com/azfunc/internal/_apis/pipelines/pipelinePermissions/endpoint/<endpointId>?api-version=7.1-preview.1"

# Grant
az rest --method patch --resource 499b84ac-1321-427f-aa17-267ca6975798 \
  --headers "Content-Type=application/json" \
  --body '{"pipelines":[{"id":1733,"authorized":true}]}' \
  --uri "https://dev.azure.com/azfunc/internal/_apis/pipelines/pipelinePermissions/endpoint/<endpointId>?api-version=7.1-preview.1"
```

Or in the UI: **Project settings → Service connections → the connection →
Security → grant access to the pipeline.**

## Testing without running against `main`

Two mechanisms, in increasing order of cost:

**1. Preview compile — validates the YAML, executes nothing.**

```bash
az rest --method post --resource 499b84ac-1321-427f-aa17-267ca6975798 \
  --headers "Content-Type=application/json" \
  --body '{"previewRun": true, "resources": {"repositories": {"self": {"refName": "refs/heads/<branch>"}}}}' \
  --uri "https://dev.azure.com/azfunc/internal/_apis/pipelines/1733/runs?api-version=7.1-preview.1"
```

Returns the fully compiled YAML, so template and parameter errors surface with
no agent time and no Azure cost.

**2. Manual queue on a branch — the real thing, off `main`.**

```bash
az pipelines run --id 1733 --branch <branch> --org https://dev.azure.com/azfunc --project internal
```

ADO compiles from the queued branch, so this exercises the real stages without
running on `main` and without publishing anything — releases are pipeline
**1735**, a separate definition. The ACA stages execute on a topic branch
because there are no ref guards, which is deliberate.

**The branch must be pushed to the ADO remote**, not just GitHub. Pipeline 1733
builds from `dev.azure.com/azfunc/internal`, so a GitHub-only push fails
validation with *"Unable to resolve the reference … to a specific version."*

### Pipeline variables

Set these as ordinary pipeline variables on the official build pipeline
(`agent-runtime.official-build`, definition **1733**), matching how the e2e
pipeline (1777) already carries its `ACA_DEPLOYED_*` set. **Already created.**

| Variable | Notes |
| --- | --- |
| `ACA_DEPLOYED_APP_SUBSCRIPTION_ID` | |
| `ACA_DEPLOYED_RESOURCE_GROUP` | |
| `ACA_DEPLOYED_APP_SITE_NAME_PY313` / `_PY314` | one per runtime leg |
| `ACA_DEPLOYED_FUNCTION_BASE_URL_PY313` / `_PY314` | include the `/api` route prefix |
| `ACA_DEPLOYED_AGENT_SLUG` | must equal the fixture agent's `name` |
| `ACA_DEPLOYED_EASY_AUTH_TOKEN_SCOPE` | must end in `/.default` |
| `ACA_DEPLOYED_EASY_AUTH_AUDIENCE` | resource URI **or** its client ID |
| `ACA_DEPLOYED_TABLE_SERVICE_URI` | |
| `ACA_DEPLOYED_TABLE_NAME` | `AzureFunctionsAgentsSessions` |
| `ACA_SANDBOX_GROUP_RESOURCE_ID` | marked secret, matching pipeline 1777 |

Names match the e2e pipeline's existing convention so both pipelines share one
vocabulary and values can be copied between them. The per-runtime suffixes are
the only addition, because this pipeline drives two apps rather than one.

**Only `ACA_SANDBOX_GROUP_RESOURCE_ID` is secret.** The rest are configuration
that must not sit in a public repository — site names, URLs, a resource group,
an app-registration client ID — but are not credentials.

A variable group is deliberately **not** used. A `- group:` reference that does
not resolve fails pipeline *compilation*, so a missing or renamed group would
take `Build`, `RunTests`, and `RunE2ETests` down with it. With plain variables an
unset value fails inside the ACA stages instead, and those are `continueOnError`.

Move to a variable group only if these values need sharing across pipelines or
Key Vault backing — and if you do, create the group **before** merging the
change that references it.

### The base URL must match the fixture's route prefix

`ACA_DEPLOYED_FUNCTION_BASE_URL_*` ends in `/api` because the fixture's
`host.json` leaves the default route prefix in place. Override that prefix to
`""` and every agent route — plus `/__buildinfo` — moves, so the build check and
the qualification suites would fail against a perfectly healthy app. Change both
together or neither.

Likewise `ACA_DEPLOYED_AGENT_SLUG` must equal the `name` in the fixture's
`*.agent.md`. Both are currently `deployed_turn`.

**No site name, base URL, resource group, subscription, or endpoint is committed
to this repository.** Everything environment-specific arrives here or from app
settings.

Two values must satisfy a contract the deployed helpers enforce, or the suite
fails as an environment error rather than a test failure:

- `ACA_DEPLOYED_EASY_AUTH_TOKEN_SCOPE` must end in `/.default`.
- `ACA_DEPLOYED_EASY_AUTH_AUDIENCE` must equal the resource URI **or** its client ID.

Do **not** set `AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_BEARER_TOKEN`. The helper
rejects it outright; the job authenticates app-only through `AzureCLI@2`.

## How a deployment is proven

The build stamps `BUILD_INFO.json` into the package. The fixture serves it at
`/__buildinfo`, and the pipeline compares `build_id`, `commit_sha`, and the
app's **live** `sys.version_info` against the build under test.

This works only because **the marker is a file inside the deployed package**. A
file can be served only if the package containing it is genuinely on disk, so a
stale app cannot claim a build it is not running. An app setting or a resource
tag could be changed without deploying anything — which is exactly where a
service reporting its own version stops being evidence. Tags *are* set after a
successful deploy, but only as portal-readable metadata, never as the gate: a
tag write is a separate ARM call and so is not atomic with the deployment.

The check runs twice — once after deploying, once at the start of the
qualification suite. The second is not redundant: phase 2 can begin much later,
and a redeploy in between would otherwise silently move the target.

## Triage

| Symptom | Meaning | Action |
| --- | --- | --- |
| `preflight-deploy` fails naming `Website Contributor` | The service connection lacks deploy rights on that app | Grant it; the connection was originally created for ACA data-plane access only |
| `check-build` reports `marker_absent` | The app is running a package with no marker — almost always a failed or partial deploy | Re-run the deploy stage |
| `check-build` reports `build_id` | The app is running a *different* build | Check whether another run redeployed concurrently |
| `check-build` reports `python_version` | The wrong runtime's package reached this app | Check the stage's `constraintsFile` and `pythonVersion` wiring |
| Deployment fails during remote build | Oryx could not resolve `requirements.txt` | Read the deployment logs; this is the customer build path, so it is a genuine finding |
| Sweep warns that stale sandboxes were found | ACA idle-delete or the controller's hourly reconciliation is not firing | Investigate the reconciliation timer; the sweep already deleted the leftovers |
| Suite errors with `ACA-SMOKE-ENV:` | Environment/config problem | Call ops |
| Suite *fails* an assertion | The environment was healthy and behavior was wrong | Call the runtime owners |

That last distinction is load-bearing: environment problems surface as **errors**
and correctness problems as **failures**, so an incomplete environment can never
masquerade as the defect the test exists to detect.

## Recovery

**There is no rollback machinery, by design.** The apps are disposable and every
merge redeploys them, so a bad deployment is corrected by the next merge. If you
need the previous build immediately, re-run the pipeline on the previous commit.

## Sandbox cleanup

Sandboxes are reclaimed by ACA idle-delete and by the controller's hourly
reconciliation. The pipeline adds **no** end-of-run reaper.

That is deliberate. A post-run reaper would quietly tidy up after every run and
so would hide the failure of both automatic mechanisms — we would keep paying
for leaks and never notice. Running the sweep *first* inverts that: a clean
sweep is evidence the mechanisms work, and a dirty one is a warning that they
have stopped.

The sweep scopes by **age** (6 hours), not by build ID, because it is hunting
*other* runs' leftovers and so cannot use this run's identifier. A sandbox whose
creation time cannot be determined is **never** deleted — unprovable age is not
grounds for deleting something that may be live — but it is reported, since a
group full of un-ageable sandboxes is itself a finding.

## Load, and what this pipeline does not prove

Automated load is **N=5**. FRD 0008 Decision #29 requires a **100**-concurrent
gate before GA, and this pipeline does not discharge it: N=100 stays human-only,
requires a manually queued build, and targets a single runtime. A green
post-main run is therefore **not** FRD 0008 GA sign-off.

## Promotion to blocking

Stages stay `continueOnError` until the criteria already set by FRD 0008
Decision #156 are met: five scheduled low-level smoke runs with zero reaper
leaks, plus five manual N=5 diagnostics. N=100 is never an automated promotion
prerequisite. Promotion is an explicit human decision and also requires an ADO
build-validation change outside this repository.
