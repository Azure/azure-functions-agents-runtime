# ACA qualification CI guide

Operational guide for the deployed ACA qualification stage in
[`eng/ci/e2e-tests.yml`](../e2e-tests.yml).

## Execution conditions

`AcaQualification` runs automatically only for `IndividualCI` and `BatchedCI`
builds of `refs/heads/main`. Trusted operators may also queue it manually from
any branch. Pull request and scheduled builds are excluded. The matrix job uses
`continueOnError: true` while qualification remains non-required; promotion to
a blocking gate requires a separate decision.

## Current pipeline and job graph

`AcaQualification` depends only on `Build`. Its matrix expands two independent
jobs that deploy and qualify Python 3.13 and Python 3.14 in parallel.

| Stage | Depends on | Job | Current work |
| --- | --- | --- | --- |
| `AcaQualification` | `Build` | `AcaQualify_python313` | Deploy; cold start; public turn; lifecycle; backing loss; N=5 |
| `AcaQualification` | `Build` | `AcaQualify_python314` | Deploy; cold start; public turn; lifecycle; backing loss; N=5 |

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
API, and group-wide sweeping and post-run cleanup are outside this layer.

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
| Suite raises `ACA-SMOKE-ENV` | Correct basic pipeline variables, target configuration, identity, or capacity. |
| Suite assertion fails | Treat the environment as ready and investigate runtime behavior. |
