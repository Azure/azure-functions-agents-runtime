# ACA qualification CI guide

Operational guide for the deployed ACA qualification stage in
[`eng/ci/e2e-tests.yml`](../e2e-tests.yml).

## Execution conditions

`AcaSweep` and `AcaQualification` run automatically only for `IndividualCI` and
`BatchedCI` builds of `refs/heads/main`. Trusted operators may also queue them
manually from any branch. Pull request and scheduled builds are excluded. Both
jobs use `continueOnError: true` while qualification remains non-required;
promotion to a blocking gate requires a separate decision.

## Current pipeline and job graph

`AcaSweep` starts alongside `Build`. `AcaQualification` waits for both stages,
but its condition requires success only from `Build`, so a sweep job or agent
infrastructure failure cannot suppress qualification. Its matrix expands two
independent jobs that deploy and qualify Python 3.13 and Python 3.14 in parallel.

| Stage | Depends on | Job | Current work |
| --- | --- | --- | --- |
| `AcaSweep` | none | `AcaSweep` | Inspect the dedicated group and report/delete resources older than six hours |
| `AcaQualification` | `Build`, `AcaSweep` | `AcaQualify_python313` | Deploy; cold start; public turn; lifecycle; backing loss; N=5 |
| `AcaQualification` | `Build`, `AcaSweep` | `AcaQualify_python314` | Deploy; cold start; public turn; lifecycle; backing loss; N=5 |

The cold-start module is first. Only after its acceptance, first-event, and
terminal timing completes does it compare the embedded marker with the expected
build ID, commit SHA, and Python runtime. A mismatch fails and suppresses the
cold-start metrics. The deployed-suite runner uses pytest fail-fast behavior and
does not start the turn, lifecycle, loss, or load modules after a cold/provenance
failure. This remains lightweight in-package provenance: it does not attest the
exact wheel digest, installed package version, deploy-input manifest, or
deployment-storage version.

Each job passes provisioning concurrency 1; `maxParallel: 2` makes aggregate
provisioning concurrency 2. Do not serialize the matrix. The dedicated Sandbox
Group must have quota and operational headroom for both jobs plus sessions
retained by prior runs. Qualification does not rely on an unverifiable quota
API.

## Pre-run cleanup signal

The sweep lists the configured Sandbox Group without a label filter and deletes
only resources whose creation time proves they are strictly older than six
hours. Recent resources and resources with missing or unparseable age are never
deleted. Every unknown-age resource, inspection failure, and delete failure
emits a durable Azure DevOps warning with a hashed resource reference or
redacted error detail. A stale sandbox already removed by ACA idle-delete is
counted as `already_absent`, not as a failure. The summary includes
already-absent, incomplete, and delete-failure counts; an inspection that did
not complete reports counts as unavailable rather than presenting a clean group.

Unfiltered deletion is safe only because this infrastructure is externally
provisioned as a CI-dedicated Sandbox Group. The data-plane inventory cannot
prove group ownership or exclusivity. The required
`--dedicated-group-scope exclusive-ci-qualification` argument is an explicit
acknowledgment of that prerequisite, not an ownership check. Do not point the
pipeline at a shared group. The six-hour floor must remain above the maximum
qualification duration.

This cleanup runs before qualification by design. The deployed suites already
assert cleanup of resources created by the current run. A destructive post-run
reaper would hide failures in ACA idle-delete or controller reconciliation, and
a report-only final audit could flag intentionally retained sessions. The next
pre-run sweep is the durable signal for accumulated leftovers, so there is no
post-run destructive cleanup or final group-wide audit.

## Required basic pipeline variables

Configure these ordinary/basic variables directly on Azure DevOps pipeline
1777 without committing their values:

- `ACA_DEPLOYED_APP_SUBSCRIPTION_ID`
- `ACA_DEPLOYED_RESOURCE_GROUP`
- `ACA_DEPLOYED_APP_SITE_NAME_PY313`
- `ACA_DEPLOYED_APP_SITE_NAME_PY314`
- `ACA_DEPLOYED_FUNCTION_BASE_URL_PY313`
- `ACA_DEPLOYED_FUNCTION_BASE_URL_PY314`
- `ACA_DEPLOYED_AGENT_SLUG`
- `ACA_DEPLOYED_EASY_AUTH_TOKEN_SCOPE`
- `ACA_DEPLOYED_EASY_AUTH_AUDIENCE`
- `ACA_DEPLOYED_TABLE_SERVICE_URI`
- `ACA_DEPLOYED_TABLE_NAME`
- `ACA_SANDBOX_GROUP_RESOURCE_ID`
- `ACA_SANDBOX_REGION`

Do not place these values in or add a dependency on an Azure DevOps variable
group. The existing `- template:` entries under `variables:` import shared YAML
variable templates for build infrastructure; they are unrelated to variable
groups and do not provide the ACA settings.

The service connection selected by the `acaServiceConnection` parameter must be
authorized for pipeline 1777.

## Manual-run trust boundary

A manual run executes YAML and scripts from the queued branch under the
deployment service connection. This is intentional so trusted operators can
validate a feature branch before merge. Restrict pipeline queue permission to
those operators. Do not substitute protected-branch or protected-environment
checks: either would defeat the approved feature-branch validation workflow.

## Fixture-app prerequisites

Each standing qualification Function App must have these app settings before
deployment:

- ACA runtime: `AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID`. The
  deployment job writes `AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_REGION`.
- Azure OpenAI: `AZURE_FUNCTIONS_AGENTS_PROVIDER=azure_openai`,
  `AZURE_OPENAI_ENDPOINT`, and `AZURE_OPENAI_DEPLOYMENT`.
  `AZURE_OPENAI_API_VERSION` is optional; when omitted, the Agent Framework
  default is used.
- Auth allowlists:
  `AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_ENTRA_TENANT_ID`,
  `AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EASY_AUTH_AUDIENCE`, and
  `AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_TEST_INVOKER_CLIENT_ID`.
- Session storage: either an `AzureWebJobsStorage` connection string or
  identity-based `AzureWebJobsStorage__tableServiceUri`. For a user-assigned
  storage identity, also set `AzureWebJobsStorage__clientId`; otherwise the
  runtime follows its documented `AZURE_CLIENT_ID` or default-credential
  resolution.

The runtime table name is fixed as `AzureFunctionsAgentsSessions`; it is not an
additional app setting. Pipeline variable `ACA_DEPLOYED_TABLE_SERVICE_URI` must
identify the same Table service as the app's `AzureWebJobsStorage`, and
`ACA_DEPLOYED_TABLE_NAME` must be `AzureFunctionsAgentsSessions`.

Platform Easy Auth must be enabled for the standing app. Its configured allowed
token audience must match `ACA_DEPLOYED_EASY_AUTH_AUDIENCE`, whose value is
passed into the fixture as
`AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EASY_AUTH_AUDIENCE`. App Service injects
`WEBSITE_AUTH_ENABLED` when Easy Auth is enforced; only environments where that
platform signal is unavailable need the explicit
`AZURE_FUNCTIONS_AGENTS_ENTRA_EASY_AUTH=true` assertion.

Each `ACA_DEPLOYED_FUNCTION_BASE_URL_*` value must include the app's HTTP route
prefix. With the fixture's default `host.json`, the URL ends in `/api`.

## Preview and manual execution

Preview-compile a pushed branch without running jobs:

```bash
az rest --method post \
  --resource 499b84ac-1321-427f-aa17-267ca6975798 \
  --headers "Content-Type=application/json" \
  --body '{"previewRun":true,"resources":{"repositories":{"self":{"refName":"refs/heads/<branch>"}}}}' \
  --uri "https://dev.azure.com/<organization>/<project>/_apis/pipelines/1777/runs?api-version=7.1-preview.1"
```

Run pipeline 1777 manually from a pushed branch:

```bash
az pipelines run \
  --id 1777 \
  --branch <branch> \
  --org https://dev.azure.com/<organization> \
  --project <project>
```

## Failure triage

| Symptom | Check |
| --- | --- |
| Connection denied, DNS failure, or timeout to the regional ACA endpoint | Confirm the agent can resolve and reach the authored regional endpoint on port 443. |
| ACA data-plane `401` or `403` | Check the service-connection identity and Sandbox Group data-plane role. |
| Stage waits at an authorization checkpoint | Authorize the selected service connection for pipeline 1777. |
| Cold start reports unavailable or mismatched provenance | Check for a concurrent deployment or runtime-target wiring error. |
| Deployment fails during remote build | Inspect deployment logs and the generated `requirements.txt`. |
| Sweep warning or nonzero `incomplete`/`delete_failures` summary | Inspect the CI-dedicated group and automatic idle-delete/controller reconciliation; qualification continues. |
| Suite raises `ACA-SMOKE-ENV` | Correct basic pipeline variables, target configuration, identity, or capacity. |
| Suite assertion fails | Treat the environment as ready and investigate runtime behavior. |
