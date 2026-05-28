# Samples E2E pipeline

## What this validates

This manual-dispatch Azure DevOps 1ES pipeline runs each supported app under `samples/` with `func start`, invokes its registered HTTP, timer, and MCP surfaces, and verifies the app responds against the real shared resources the samples depend on: a shared Foundry project, the ACA session pool, and the Office 365 MCP service. It is intended for operator-driven validation before or after sample changes, runtime changes, or release work.

## Scope (v1)

- Sample `basic-chat` — full HTTP + SSE + MCP webhook coverage (4 endpoints)
- Sample `daily-azure-report` — full HTTP + SSE + MCP webhook + timer + custom HTTP route (6 endpoints)
- Sample `daily-tech-news-email` — timer endpoint (1 endpoint)
- Sample `outlook-reply-agent` — **registration only**; the connector trigger binding is validated but not invoked

## Pipeline files

| File | Purpose |
|---|---|
| `eng/ci/samples-e2e.yml` | Entry pipeline. Disables PR/scheduled triggers, links the `samples-e2e-secrets` variable group, and instantiates one job-template invocation per supported sample. |
| `eng/templates/jobs/sample-e2e.yml` | Per-sample job template. Installs Python, Functions Core Tools, and Azurite; runs the optional experimental-bundle preflight; executes the Python harness; and publishes `sample-e2e-<sample>`. |
| `scripts/e2e/run_sample_e2e.py` | Harness CLI entrypoint. Validates env vars, writes `settings.redacted.json`, starts Azurite and `func`, runs the sample invocation matrix, writes transcripts, and uploads `summary.md`. |
| `scripts/e2e/expectations.py` | Static expectations table for each sample: registered function names, invocation plan, skip-only functions, and `required_env_vars`. |
| `scripts/e2e/harness.py` | Low-level helpers for process management, host readiness polling, HTTP/SSE/admin/MCP invocation, and timer completion detection from `func.log`. |
| `scripts/e2e/settings.py` | Environment precheck and redacted settings snapshot helpers used before the host starts. |
| `scripts/e2e/redaction.py` | Secret redaction pass for logs, JSON transcripts, summaries, and other published text artifacts. |
| `tests/e2e/test_expectations.py` | Drift guard that runs in normal pytest CI and asserts the expectations table still matches `create_function_app(...).get_functions()`. |

## Operator one-time setup

### 1. ADO variable group

Create an Azure DevOps variable group named `samples-e2e-secrets`, populate the variables below, and authorize the pipeline to use it.

| Variable | Mark secret? | What it is | Where to get it |
|---|---|---|---|
| `AZURE_TENANT_ID` | Yes | Tenant ID for the service principal the pipeline uses for `DefaultAzureCredential` | Microsoft Entra ID tenant overview |
| `AZURE_CLIENT_ID` | Yes | Client ID for the pipeline service principal | App registration or enterprise application overview |
| `AZURE_CLIENT_SECRET` | Yes | Client secret for that service principal | App registration client secret you create for the pipeline |
| `FOUNDRY_PROJECT_ENDPOINT` | Yes | Foundry project endpoint URL used by all samples | Foundry project overview / endpoint blade |
| `FOUNDRY_MODEL` | No | Foundry model deployment name, for example `gpt-5.4` | Foundry model deployment name |
| `ACA_SESSION_POOL_ENDPOINT` | Yes | ACA session-pool endpoint used by sandbox-enabled samples | ACA session pool resource endpoint |
| `O365_MCP_SERVER_URL` | Yes | Base URL for the shared Office 365 MCP service | Deployed O365 MCP service endpoint |
| `O365_MCP_CLIENT_ID` | Yes | Optional client ID forwarded to the O365 MCP credential path | Leave empty in CI, or set it to `AZURE_CLIENT_ID` if your setup expects it |
| `SUBSCRIPTION_ID` | No | Azure subscription queried by `daily-azure-report` | Azure portal subscription overview |
| `TO_EMAIL` | No | Recipient mailbox used by the email-report samples | Test mailbox or distribution list chosen for E2E runs |
| `WATCHED_SENDER_EMAIL` | No | Sender mailbox watched by `outlook-reply-agent` | Test mailbox used for connector validation |
| `MAF_REASONING_EFFORT` | No | Reasoning-effort override for the report/email samples | Operator-chosen runtime setting, for example `high` |
| `MAF_REASONING_SUMMARY` | No | Reasoning-summary override for the report/email samples | Operator-chosen runtime setting, for example `concise` |

Notes:

- Even for values that are not inherently secrets, the current pipeline treats several endpoints as secrets so Azure DevOps masks them in logs.
- A missing or empty variable fails the harness before `func start`; see [Troubleshooting](#troubleshooting).

### 2. Service principal RBAC

The `AZURE_*` service principal must have:

- A Foundry project/model role that permits inference against the configured project and model deployment
- Session-pool executor access on the ACA session pool used by the sandbox-enabled samples
- `Reader` on the Azure subscription identified by `SUBSCRIPTION_ID`
- Whatever Microsoft 365 permissions and consent your shared O365 MCP deployment requires

`O365_MCP_CLIENT_ID` has one caveat: the runtime forwards it to `DefaultAzureCredential(managed_identity_client_id=...)`. That matters for user-assigned managed identity, not for a normal client-secret service principal. In CI, either leave `O365_MCP_CLIENT_ID` empty or set it to `AZURE_CLIENT_ID`, then grant the service principal the required O365 MCP access directly.

### 3. Link the pipeline

1. In Azure DevOps, create a pipeline from existing YAML and point it at `eng/ci/samples-e2e.yml`.
2. Authorize the pipeline to use the `samples-e2e-secrets` variable group referenced in the YAML.
3. Confirm the users or groups who should run it have queue permission on that pipeline.
4. Confirm the `1es-pool-azfunc` pool is available and can allocate the `1es-ubuntu-22.04` image.

## Running the pipeline

Queue it manually from the Azure DevOps UI with **Run pipeline**. This YAML has `trigger: none`, `pr: none`, and no `schedules:` block, so it only runs when a maintainer explicitly starts it.

## What artifacts get published

Each job publishes a `sample-e2e-<name>` artifact. The harness writes its files
directly at the artifact root (no extra `<sample-name>/` nesting).

| File | What it contains |
|---|---|
| `func.log` | Raw Functions host stdout/stderr, used for host-startup failures and timer completion triage |
| `func.json` | Core Tools JSON log output from `func start` |
| `harness.log` | High-level harness actions and failure context |
| `azurite.log` | Azurite startup and runtime output |
| `admin-functions.json` | `/admin/functions` metadata captured after the host reaches `Running` |
| `settings.redacted.json` | Presence/length/preview snapshot of required env vars with secret values redacted |
| `summary.md` | Per-sample markdown summary uploaded to the Azure DevOps Summary tab |
| `transcripts/*.json` | One JSON transcript per invocation, including request body, status, excerpt, and any timer completion lines |

## Reading the build summary

Each sample uploads its own `summary.md` to the Azure DevOps **Summary** tab. Start there:

1. Open the failed job's summary and confirm the expected-function count matches the actual-function count.
2. Check the invocation table to see which endpoint or admin function failed.
3. If the summary is not enough, download `sample-e2e-<name>` and inspect `func.log`, `harness.log`, and the relevant `transcripts/*.json`.

Common failure modes:

- **Env precheck failure** — the summary shows missing env vars before host startup; fix the variable group or the pipeline authorization.
- **Host indexing failure** — the host never reaches `Running`; inspect `func.log` and `azurite.log`.
- **Function-name drift** — the sample registers a different set of functions than `scripts/e2e/expectations.py` declares.
- **Invocation 5xx / non-2xx** — the endpoint was reached but the sample failed mid-request; inspect the transcript plus `func.log`.
- **Timer completion timeout** — the admin invoke returned `202`, but the expected completion line never appeared in `func.log`.

## Local dry run (developer)

Use the harness locally before queuing Azure DevOps if you want to validate a sample against the same class of shared resources.

```powershell
Set-Location Q:\src\Playground\azure-functions-agent-runtime

$env:AZURE_TENANT_ID = "<tenant-guid>"
$env:AZURE_CLIENT_ID = "<app-guid>"
$env:AZURE_CLIENT_SECRET = "<client-secret>"
$env:FOUNDRY_PROJECT_ENDPOINT = "https://<project>.services.ai.azure.com/api/projects/<project>"
$env:FOUNDRY_MODEL = "<deployment-name>"
$env:ACA_SESSION_POOL_ENDPOINT = "https://<session-pool-endpoint>"

.\.venv\Scripts\python.exe .\scripts\e2e\run_sample_e2e.py `
  --sample-name basic-chat `
  --sample-path samples/basic-chat/src `
  --artifacts-dir .artifacts\samples-e2e\basic-chat
```

The harness writes directly into the directory passed to `--artifacts-dir` (no
extra `<sample-name>/` nesting), so the command above produces files such as
`.artifacts\samples-e2e\basic-chat\harness.log`.

If you already have storage configured elsewhere and do not want the harness to launch Azurite, add `--no-azurite`:

```powershell
.\.venv\Scripts\python.exe .\scripts\e2e\run_sample_e2e.py `
  --sample-name basic-chat `
  --sample-path samples/basic-chat/src `
  --artifacts-dir .artifacts\samples-e2e\basic-chat `
  --no-azurite
```

For other samples, export the additional variables listed in `scripts/e2e/expectations.py` for that sample. Prefer a non-production or sandbox Foundry deployment for local dry runs so you do not consume the same quota the shared CI pipeline uses.

## Drift guard in normal CI

`tests/e2e/test_expectations.py` runs in normal pytest CI and compares the static expectations table in `scripts/e2e/expectations.py` with the live result of `create_function_app(...).get_functions()` for every supported sample. This catches sample renames, route drift, and other registration mismatches before anyone manually queues the E2E pipeline. The test is hermetic: it seeds stub env vars in-process and does not depend on external Azure resources.

## Maintainer follow-ups

- Migrate the variable-group secrets to Azure Key Vault
- Add real `OnNewEmail` invocation coverage instead of registration-only validation
- Add a scheduled cadence once cost and flakiness are understood
- Consider per-PR gating once the manual pipeline is stable
- Add an `azd up` / `azd down` deployment mode for true cloud-side E2E coverage
- Publish JUnit results so failures also light up the Azure DevOps **Tests** tab

## Troubleshooting

- `STATUS: blocked` / env precheck failure — One or more required variables are missing or empty. Link `samples-e2e-secrets`, re-authorize it if needed, and verify the specific variable has a non-empty value.
- `function name drift` — The sample's registered functions no longer match `scripts/e2e/expectations.py`. Update the expectations entry and let `tests/e2e/test_expectations.py` pass before re-running the manual pipeline.
- `host did not reach Running state` — The Functions host failed during startup, commonly because Azurite is unavailable or the sample's local settings are malformed. Check `azurite.log`, then inspect `func.log` for the first startup exception.
- `completion log line not seen` — The timer admin invoke returned `202`, but the underlying agent failed before the success marker was logged. Inspect `func.log` around the invocation time for the real model or downstream-service error.
- `connector_extension` / experimental bundle download failure — The 1ES agent could not reach `https://cdn.functions.azure.com/` for the experimental connector bundle. Fix network reachability or rerun from a pool that can reach the CDN; the preflight step in `eng/templates/jobs/sample-e2e.yml` is meant to fail early here.
- Redaction overzealous — A secret value is short or common enough to match normal text. Rotate it to a longer or more specific value, then rerun so the published artifact is more readable.
