# ACA qualification runbook

Operating guide for the post-main ACA deployment and qualification stages in
`eng/ci/e2e-tests.yml`. Design and decisions live in
[`frds/0008-aca-sandbox-session-runtime.md`](frds/0008-aca-sandbox-session-runtime.md) §14.

## What runs, and when

On merge to `main`, and on manual runs from any branch. Pull request and
scheduled builds are excluded by a per-stage condition, because this pipeline
does carry PR triggers:

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

### Network isolation: why these stages are not on the official build

1ES **DefaultDeny** network isolation is an allow-list, and the ACA data-plane
host is not on it:

```
ServiceRequestError: Cannot connect to host management.westus2.azuredevcompute.io:443
ssl:default [Operation not permitted]
```

`Operation not permitted` on an outbound connect is network isolation, not
authentication and not RBAC. It affects `AcaSweep` and every suite that reads
sandbox state through the ACA SDK — lifecycle, loss, and load all call the data
plane directly to verify what the deployed app reports. Deployment itself uses
the `az` CLI against ARM, which is permitted.

The policy is keyed by **pipeline definition ID**, not by pool: the official
build (**1733**) and this pipeline (**1777**) share a pool and image, but only
1733 is enrolled. Enrollment is a progressive migration, so 1777 is unenrolled
rather than exempt.

That makes the current arrangement a reprieve, not a fix. The durable fix is an
allow-list entry for `management.*.azuredevcompute.io`; until then, treat a
future enrollment of 1777 as a scheduled outage for these stages.

### Resolved: remote build now produces a loadable app

Early live testing hit a state where `az functionapp deployment source config-zip`
reported *"Deployment was successful"* while the app registered **zero functions**
(`az functionapp function list` returned `[]`), so `/api/__buildinfo` and every
agent route returned 404. The host stayed healthy and Easy Auth still answered
401, which is why a simple reachability check did not reveal it.

The cause was not the remote build. The fixture's build-info route was annotated
with `azure.functions.HttpRequest`, but the runtime registers routes with the
FastAPI `Request`/`Response` types from
`azurefunctions.extensions.http.fastapi`. The worker rejects that binding, and
because indexing is all-or-nothing, one bad route takes down the whole app —
reported only as the generic *"No job functions found"*.

Deployments now index 16 functions. `tests/test_aca_qualification_pipeline.py`
carries a guard asserting the route's parameter and return annotations, so the
same mistake fails the unit gate rather than a 40-minute live run.

Two Flex Consumption facts worth keeping:

- `SCM_DO_BUILD_DURING_DEPLOYMENT` is rejected outright (*"not supported with
  this SKU"*) and must not be set. Remote build is implicit.
- SCM basic auth is disabled on these apps, so build logs need an Entra token
  rather than publishing credentials.

## Prerequisites

| Item | Notes |
| --- | --- |
| Two Flex Consumption Linux apps | one Python 3.13, one 3.14, Easy Auth enabled |
| Shared ACA Sandbox Group | both apps point at it |
| ADO service connection | currently `larohra-sandboxgroup-test`, reused for deployment |
| **Service-connection authorization for this pipeline** | see below — easy to miss |
| Pipeline variables | see below |

### Authorize the service connection for the pipeline

A service connection is authorized **per pipeline**. `larohra-sandboxgroup-test`
is authorized for the e2e pipeline (1777), which is where these stages run. A
pipeline that has not been granted access cannot use it.

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
  --body '{"pipelines":[{"id":1777,"authorized":true}]}' \
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
  --uri "https://dev.azure.com/azfunc/internal/_apis/pipelines/1777/runs?api-version=7.1-preview.1"
```

Returns the fully compiled YAML, so template and parameter errors surface with
no agent time and no Azure cost.

**2. Manual queue on a branch — the real thing, off `main`.**

```bash
az pipelines run --id 1777 --branch <branch> --org https://dev.azure.com/azfunc --project internal
```

ADO compiles from the queued branch, so this exercises the real stages without
running on `main` and without publishing anything — releases are pipeline
**1735**, a separate definition. The stage conditions admit `Manual` on any
branch precisely so this works; automatic CI runs are still restricted to
`main`.

**The branch must be pushed to the ADO remote**, not just GitHub. Pipeline 1777
builds from `dev.azure.com/azfunc/internal`, so a GitHub-only push fails
validation with *"Unable to resolve the reference … to a specific version."*

### Pipeline variables

Set these as ordinary pipeline variables on the e2e pipeline
(`agent-runtime.e2e-tests`, definition **1777**), alongside the `ACA_DEPLOYED_*`
set it already carries. **Already created.**

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

## How runtime telemetry is proven

The fixture locks include the runtime's `[monitor]` extra. With the protected
`APPLICATIONINSIGHTS_CONNECTION_STRING` app setting, this enables export of the
runtime's OpenTelemetry spans; host-level Function telemetry alone does not.

After deployment, prove installation by finding
`azure_monitor_opentelemetry-1.8.8.dist-info` in the built
`released-package.zip`. Prove initialization and export with a normal runtime
span:

```kusto
AppDependencies
| where TimeGenerated > ago(30m)
| where AppRoleName == "<fixture-app-role-name>"
| where Name == "dynamic_session.execute" or Name startswith "agent.run "
| project TimeGenerated, AppRoleName, Name, Success, DurationMs, Properties
| order by TimeGenerated desc
```

The `af.setup.*` attributes are emitted only when a setup timeout occurs. Their
absence in a green run is therefore not evidence that instrumentation failed;
ordinary runtime spans are the initialization proof.

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
