# ACA qualification CI guide

Operational guide for the post-main ACA deployment and qualification stages in
[`eng/ci/e2e-tests.yml`](../e2e-tests.yml).

## Execution conditions

The ACA stages run for:

- automatic `IndividualCI` and `BatchedCI` builds of `refs/heads/main`;
- manually queued builds from any branch.

They do not run for pull request or scheduled builds. All ACA jobs currently use
`continueOnError: true` while the qualification remains nonblocking.

## Current pipeline and job graph

`AcaSweep` starts alongside `Build`. One `AcaQualification` stage then starts
after both and runs independent Python 3.13 and 3.14 jobs in parallel. Each job
deploys its fixture app and runs one ordered pytest suite.

| Stage | Depends on | Job | Current work |
| --- | --- | --- | --- |
| `AcaSweep` | none | `AcaSweep` | Report and clear stale sandboxes |
| `AcaQualification` | `Build`, `AcaSweep` | `AcaQualify_python313` | Deploy; cold start; public turn; lifecycle; backing loss; N=5 |
| `AcaQualification` | `Build`, `AcaSweep` | `AcaQualify_python314` | Deploy; cold start; public turn; lifecycle; backing loss; N=5 |

The cold-start module is explicitly first. Its first acceptance is therefore
the first request to the newly deployed Function App. Only after acceptance,
first-event, and terminal timing has completed does that test call
`/__buildinfo` and compare its embedded marker with the expected build ID,
commit SHA, and Python runtime. A mismatch fails the test and suppresses cold
latency metrics, so evidence from the wrong deployment is never presented as
trustworthy.

This is intentionally lightweight in-package provenance. It does not attest the
wheel digest, installed package version, deploy-input manifest, or deployment
storage version; FRD 0008 Decision 196 explicitly narrows those original issue
#166 requirements.

## Required basic pipeline variables

Configure these ordinary/basic variables directly on the E2E pipeline without
committing their values:

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

Do not place these values in, or add a dependency on, an Azure DevOps variable
group. The `- template:` entries under `variables:` in `e2e-tests.yml` import
shared YAML variable templates for build infrastructure; they are distinct from
variable groups and do not supply the ACA settings.

The service connection selected by the `acaServiceConnection` pipeline
parameter must be authorized for the pipeline. The pipeline intentionally marks
`ACA_SANDBOX_GROUP_RESOURCE_ID` as secret to keep its value non-public; an Azure
resource ID is not itself a credential or intrinsically secret. Treat the other
environment-specific settings as non-public configuration.

## Fixture-app prerequisites

Each standing qualification Function App must define
`AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID`. The deployment job writes
`AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_REGION` before uploading the package.

Each `ACA_DEPLOYED_FUNCTION_BASE_URL_*` value must include the app's HTTP route
prefix. With the fixture's default `host.json`, that means the URL ends in
`/api`. If the route prefix changes, update the configured base URL with it.

## Preview and manual execution

Preview-compiling a branch validates expanded YAML without running jobs:

```bash
az rest --method post \
  --resource 499b84ac-1321-427f-aa17-267ca6975798 \
  --headers "Content-Type=application/json" \
  --body '{"previewRun":true,"resources":{"repositories":{"self":{"refName":"refs/heads/<branch>"}}}}' \
  --uri "https://dev.azure.com/<organization>/<project>/_apis/pipelines/<pipeline-id>/runs?api-version=7.1-preview.1"
```

Run the real pipeline manually from a branch:

```bash
az pipelines run \
  --id <pipeline-id> \
  --branch <branch> \
  --org https://dev.azure.com/<organization> \
  --project <project>
```

The branch must exist in the Azure DevOps repository used by the pipeline.

## Connectivity and failure triage

| Symptom | Check |
| --- | --- |
| Connection denied, DNS failure, or timeout to the regional ACA data-plane host | Confirm the agent can resolve and reach the authored regional endpoint on port 443; investigate network policy before authentication. |
| ACA data-plane `401` or `403` | Check the service-connection identity and Sandbox Group data-plane role assignment. |
| Stage remains pending at an authorization checkpoint | Authorize the selected service connection for this pipeline. |
| Cold start reports unavailable or mismatched provenance | Re-run qualification and check for concurrent deployment or runtime-target wiring. |
| Deployment fails during remote build | Inspect deployment logs and the generated `requirements.txt`. |
| Suite raises `ACA-SMOKE-ENV` | Correct pipeline variables, target configuration, identity, or capacity. |
| Suite assertion fails | Treat the environment as ready and investigate runtime behavior. |

## Promotion criteria

Keep the stages nonblocking until five scheduled low-level smoke runs complete
with zero reaper leaks and five manual N=5 diagnostics pass. Promotion is an
explicit human decision and requires the corresponding Azure DevOps
build-validation change. N=100 remains a human-only formal acceptance exercise
for one selected runtime and is not an automated promotion prerequisite.
