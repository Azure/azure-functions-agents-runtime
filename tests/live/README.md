# Live ACA harness smoke coverage

The following opt-in, paid-service tests are intentionally different layers of
evidence. None provisions infrastructure from CI.

- **#152 adapter/harness coverage**:
  `tests/live/test_aca_harness_entrypoint_smoke.py` proves the production
  harness entrypoint resolves and runs. It exercises a real
  `AcaSandboxAdapter.create()` call, a real `SandboxSessionHandle`, a direct
  file upload, and synchronous process execution.
  `tests/live/test_aca_run_journal_acceptance.py` proves the full
  controller-to-harness round trip: a real `SandboxRunControl.submit()` writes
  an inbox envelope, launches the harness, and observes the journal status the
  harness wrote back.
- **Lower-level real model/harness smoke**:
  `tests/live/test_aca_real_agent_turn.py` manually qualifies a captured,
  minimal no-tools agent catalog through the production content package,
  bootstrap, harness, journal, and run-control path. The harness performs a
  real Azure OpenAI MAF turn using the Sandbox Group's user-assigned managed
  identity (UAMI), then publishes ordered journal events and a successful
  terminal result.
- **GA full-stack deployed Function qualification**:
  `tests/live/test_aca_deployed_agent_turn.py` is the persistent manual proof
  through only a deployed, Easy-Auth-protected Azure Function's public routes.
  It submits a run, reads journal SSE, status, and result; it never calls ACA,
  the harness, or Table storage directly.
- **GA deployed persistent-session lifecycle qualification**:
  `tests/live/test_aca_deployed_lifecycle.py` creates and resumes turns only
  through that same protected public route. It uses an authorized, read-only
  Table observation and ACA inventory to establish lifecycle evidence. The
  deployed controller timer is the sole state writer and reclaimer.

Shared provisioning, dependency-closure delivery, and cleanup live in
`tests/live/aca_smoke_support.py`.

## Run locally

The dependency closure is built for the host platform with the host interpreter,
so these tests must run on **Linux (or WSL) x86_64 with the CPython minor
version that matches the target sandbox disk** — CPython 3.13 for a
`python-3.13` disk, 3.14 for `python-3.14`. On Windows or macOS, or on a
mismatched minor version, the fixture fails fast with an `ACA-SMOKE-ENV` error
rather than shipping incompatible native wheels into the sandbox.

Install the ACA transport extra, authenticate with the Azure CLI, and set the
following values for a CI-only Sandbox Group:

```bash
python -m pip install -U -e ".[dev,aca_sandbox]"
az login
export AZURE_FUNCTIONS_AGENTS_RUN_ACA_SMOKE=1
export AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID="/subscriptions/<subscription>/resourceGroups/<resource-group>/providers/Microsoft.App/sandboxGroups/<group>"
export AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_DISK="<sandbox-disk-name>"
python -m pytest -m live_aca \
  tests/live/test_aca_harness_entrypoint_smoke.py \
  tests/live/test_aca_run_journal_acceptance.py \
  -v -o log_cli=true -o log_cli_level=INFO
```

Run these two files by name rather than the whole `tests/live` directory: the
pre-existing `test_aca_sdk_smoke.py` reports configuration problems as test
failures instead of environment errors, which defeats the triage rule below.

The test skips before collecting a live fixture unless
`AZURE_FUNCTIONS_AGENTS_RUN_ACA_SMOKE` is exactly `1`. Do not set that variable
in normal local development or ordinary unit-test jobs.

## Run the lower-level real-agent-turn qualification manually

This lower-level model/harness test is deliberately **manual only**. It is not wired to a
scheduled CI job until its live reliability has been demonstrated. In addition
to the common ACA variables above, provide the Azure OpenAI deployment and the
client ID of the UAMI attached to the dedicated Sandbox Group:

```bash
export AZURE_FUNCTIONS_AGENTS_RUN_ACA_SMOKE=1
export AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID="/subscriptions/<subscription>/resourceGroups/<resource-group>/providers/Microsoft.App/sandboxGroups/<group>"
export AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_DISK="python-3.13"
export AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_REGION="<sandbox-group-region>"
export AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_MODEL_PROVIDER="azure_openai"
export AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_AZURE_OPENAI_ENDPOINT="https://<account>.openai.azure.com"
export AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_AZURE_OPENAI_DEPLOYMENT="u3-gpt-5-6-luna-20260709"
export AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_MODEL_UAMI_CLIENT_ID="<sandbox-group-uami-client-id>"
export AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_REASONING_EFFORT="none"
python -m pytest -m live_aca tests/live/test_aca_real_agent_turn.py \
  -v -o log_cli=true -o log_cli_level=INFO
```

The test forwards only the provider, endpoint, deployment, optional reasoning
effort, and the Sandbox Group UAMI client ID into the guest. It never forwards
API keys, bearer tokens, controller/CI credentials, storage credentials, or
state-store permissions. The UAMI must have access to the selected deployment.
The fixture first classifies guest managed-identity, role, quota, and model
reachability failures as `ACA-SMOKE-ENV` errors; after that preflight, journal
or result assertions are product test failures. Prompt and model-result content
are not logged or asserted verbatim.

This proves one isolated, model-backed sandbox harness turn using the checked-in
minimal project. It is **not** the GA full-stack proof: it does not prove a
deployed Function App request or controller identity access, and it does not
replace the existing adapter/harness acceptance tests. The fixture has no tools; if a future
qualification adds one, it must set
`AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_REASONING_EFFORT=none`.

## Run the deployed Function GA qualification manually

`tests/fixtures/live_aca_deployed_agent_turn/` is the minimal deployable
package for this test. It contains the regular no-tools HTTP agent and a
load-only hold-tool agent, a persistent ACA session runtime, Easy Auth
allow-lists, and no web or MCP tools. It does not contain dependencies or
secrets. Build the package from this checkout, not from a published runtime
release:

```bash
cd tests/fixtures/live_aca_deployed_agent_turn
python -m pip install --target .python_packages/lib/site-packages \
  "<path-to-azure-functions-agents-runtime>[aca_sandbox]"
func azure functionapp publish <function-app-name> --python --no-build
```

The fixture's `.gitignore` prevents `.python_packages` from being committed, while
`.funcignore` deliberately includes that locally built Linux dependency tree in the
published package. Before publish,
configure the Function App's protected application settings (for example via
`az functionapp config appsettings set`): `AZURE_FUNCTIONS_AGENTS_PROVIDER=azure_openai`,
`AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`,
`AZURE_FUNCTIONS_AGENTS_SANDBOX_DISK=python-3.13` or `python-3.14` (the public
Python disk matching that deployed app),
`AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID`, the Entra tenant,
Easy Auth audience, the U3.TestInvoker client ID, and
`AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT=none` for the load-only agent.
`AzureWebJobsStorage`
must be configured for the runtime's persistent session state. Keep the Azure
OpenAI endpoint and Function App on managed identity; do not put model keys,
storage keys, or bearer tokens in the fixture or test environment.

After the app package, Easy Auth policy, U3.TestInvoker role assignment, ACA
Sandbox Group access, and Azure OpenAI access are ready, run this manually:

```bash
export AZURE_FUNCTIONS_AGENTS_RUN_DEPLOYED_ACA_SMOKE=1
export AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_FUNCTION_BASE_URL="https://<app>.azurewebsites.net/api"
export AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_AGENT_SLUG="deployed_turn"
export AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EASY_AUTH_TOKEN_SCOPE="api://<app-client-id>/.default"
export AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EASY_AUTH_AUDIENCE="<app-client-id>"
export AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_TIMEOUT_SECONDS=180
python -m pytest -m live_aca tests/live/test_aca_deployed_agent_turn.py -v
```

The test acquires its token only through
`azure.identity.aio.DefaultAzureCredential`; it rejects a bearer-token
environment variable. The protected `larohra-sandboxgroup-test` ADO service
connection holds **U3.TestInvoker**. The `ACADeployedAgentTurn` runs this qualification only for manually queued or
scheduled builds, never pull requests. It remains nonblocking.
Missing URL/configuration, token acquisition, or unavailable-app failures are
reported as `ACA-SMOKE-ENV` pytest errors; public response and protocol
assertions remain pytest failures. Never log prompt or model-result content.

### Run the deployed cold-start qualification

`tests/live/test_aca_deployed_cold_start.py` is a separate Manual/Scheduled
customer-path performance qualification. It always targets the regular
no-tools `deployed_turn` agent; it does not use `deployed_load`, its hold tool,
or the N-load concurrency path. The default is exactly **three sequential fresh
sessions**. This avoids bursts and file-plane contention and is intentionally
not an N=100 test.

Run it with the lifecycle settings so the test can make read-only Table
observations and use exact-label ACA cleanup. The sample count may be explicitly
set to **1..5** by `--aca-cold-start-samples` or
`AZURE_FUNCTIONS_AGENTS_ACA_COLD_START_SAMPLES`; the CLI value wins. Do not
increase this bound.

```bash
export AZURE_FUNCTIONS_AGENTS_RUN_DEPLOYED_ACA_SMOKE=1
export AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_FUNCTION_BASE_URL="https://<app>.azurewebsites.net/api"
export AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_AGENT_SLUG="deployed_turn"
export AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EASY_AUTH_TOKEN_SCOPE="api://<app-client-id>/.default"
export AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EASY_AUTH_AUDIENCE="<app-client-id>"
export AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_TIMEOUT_SECONDS=180
export AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_TABLE_SERVICE_URI="https://<storage-account>.table.core.windows.net"
export AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_TABLE_NAME="AzureFunctionsAgentsSessions"
export AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID="/subscriptions/<subscription>/resourceGroups/<resource-group>/providers/Microsoft.App/sandboxGroups/<group>"
export AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_APP_SUBSCRIPTION_ID="<function-app-subscription-id>"
export AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_APP_SITE_NAME="<function-app-site-name>"
python -m pytest -m live_aca tests/live/test_aca_deployed_cold_start.py -v \
  -o log_cli=true -o log_cli_level=INFO
```

The test acquires app-only Easy Auth evidence, records monotonic POST start,
first response, first SSE event, and terminal `done`, then verifies public
terminal status and result availability for every fresh session. It retries
only typed `504 setup_deadline_exceeded` responses, honors `Retry-After`, and
reuses the same idempotency key. A first attempt passes only when it returns
`202` in at most **35 seconds** (the 30-second setup contract plus a 5-second
network allowance); a typed first-attempt `504` is a cold-start SLO failure
even if a later retry succeeds. Model terminal latency has no new threshold:
the existing authored timeout remains the bound and terminal latency is
reported only.

Operator output contains only aggregate sample count, retries, p50/p95/max
first-attempt acceptance, total acceptance, first event, terminal latency, and
cleanup status. It never prints IDs, prompts, model output, tokens, headers,
or resource IDs. Ambiguous submissions are recovered by owner idempotency
reservation only. The test never writes Table state: cleanup deletes only
exact-label owned backing and requires the deployed controller to tombstone it,
then verifies zero owned backing and snapshots. Failure candidates are retained
for that cleanup boundary.

The separate nonblocking `ACADeployedColdStart` job runs this test independently
of `ACADeployedAgentTurn` and its optional load concurrency. The load job keeps
its 360-minute cap and remains the sole human N=100 path.
`ACA_DEPLOYED_COLD_START_SAMPLES` is an optional, non-secret Manual/Scheduled pipeline
variable mapped only when provided; the default is three. The pipeline accepts
only integer values **1..4**. Its enforced four-sample maximum is
**4 x 465 + 60 final recovery + 4 x 240 cleanup = 2,880 seconds (48 minutes)**,
leaving exactly **12 minutes** of the 60-minute cap for job overhead. All job
setup must fit within that allowance. The test still allows **1..5** for direct
local/manual pytest:
five samples are not pipeline-supported and require a caller-provided watchdog
longer than 65 minutes.

Each sample allows 180 seconds of admission (at most three 45-second attempts
and retry waits), 240 seconds of SSE terminal observation, and 45 seconds of
public terminal reads. The shared final recovery window polls once per second;
every attempted key must resolve for cleanup or the report marks cleanup
incomplete. Unit doubles validate orchestration and report contracts; they do
not prove real Azure cold-start latency. No live result is implied until an
authorized manual run passes.

### Run the deployed lifecycle qualification manually

The deployed fixture intentionally uses the shortest supported ACA
`auto_suspend_idle` value, **60 seconds**, and a **120-second**
`reclaim_idle`. The configuration contract accepts `reclaim_idle` values that
are positive and strictly greater than the selected auto-suspend value; 120
leaves a full minute to observe suspension and submit the resumed public turn.
The runtime's reclamation eligibility includes its 300-second safety grace, so
the reclaim phase begins about seven minutes after the resumed terminal turn.
The deployed Function App must set
`AZURE_FUNCTIONS_AGENTS_RECONCILER_CADENCE_SECONDS=60`; after eligibility the
test observes up to four controller cadence windows for the timer to reclaim.

Republish the fixture after changing retention, then run the lifecycle file
with the same public endpoint settings plus the non-secret state and app
identity settings:

```bash
export AZURE_FUNCTIONS_AGENTS_RUN_DEPLOYED_ACA_SMOKE=1
export AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_FUNCTION_BASE_URL="https://<app>.azurewebsites.net/api"
export AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_AGENT_SLUG="deployed_turn"
export AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EASY_AUTH_TOKEN_SCOPE="api://<app-client-id>/.default"
export AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EASY_AUTH_AUDIENCE="<app-client-id>"
export AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_TIMEOUT_SECONDS=180
export AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_TABLE_SERVICE_URI="https://<storage-account>.table.core.windows.net"
export AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_TABLE_NAME="AzureFunctionsAgentsSessions"
export AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID="/subscriptions/<subscription>/resourceGroups/<resource-group>/providers/Microsoft.App/sandboxGroups/<group>"
export AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_APP_SUBSCRIPTION_ID="<function-app-subscription-id>"
export AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_APP_SITE_NAME="<function-app-site-name>"
python -m pytest -m live_aca tests/live/test_aca_deployed_lifecycle.py -v \
  -o log_cli=true -o log_cli_level=INFO
```

Expected duration is the two real public turns plus roughly 60 seconds for
provider auto-suspend, 120 seconds of idle retention, 300 seconds of safety
grace, and up to four 60-second controller windows. The Manual/Scheduled
`ACADeployedAgentTurn` ADO job runs
this file alongside the existing deployed-turn test and additionally requires
the protected non-secret variables `ACA_DEPLOYED_TABLE_SERVICE_URI`,
`ACA_DEPLOYED_TABLE_NAME`, `ACA_DEPLOYED_APP_SUBSCRIPTION_ID`, and
`ACA_DEPLOYED_APP_SITE_NAME`.

ACA resume can outlast one 30-second request setup budget while the provider
continues bringing the sandbox online. The lifecycle client retries only the
typed `setup_deadline_exceeded` response, with the same session and idempotency
key, for a fixed bounded window. This is a public client retry; it does not add
an outer retry or timeout around the runtime's attach/resume handshake.

The qualification proves only normal lifecycle behavior: ACA reports the
owned sandbox `Stopped` or `Suspended`; a second public turn resumes the same
sandbox ID and generation; and the deployed controller timer deletes the owned
backing sandbox and any owned snapshot resources before writing the documented
`reclaimed_idle_session` tombstone. It confirms terminal status remains
readable and result retrieval returns `410`. CI holds **Storage Table Data
Reader scoped only to the `AzureFunctionsAgentsSessions` table** and never
writes Table state; it has no Storage Table Data Contributor or Owner role. On
a failure, CI may delete only the complete immutable exact-label owned sandbox
through its Sandbox Group Data Owner role, then must observe the deployed
controller timer's tombstone; inability to confirm it is an `ACA-SMOKE-ENV`
error that names the exact selector. It does **not** certify external sandbox
loss, cursor replay, cancel, chaos, or load behavior.

### Run the deployed persistent-session load qualification manually

The deployable fixture keeps the no-tools `deployed_turn` agent for turn and
lifecycle qualification and adds the separate load-only `deployed_load` agent.
Only the load agent exposes the fixture-only `qualification_hold` tool. A real
model is asked to call it once, holding each admitted run for five minutes
before it returns its minimal acknowledgement. Tool selection is model-mediated,
not deterministic: the test verifies exactly one public `tool_start` and
`tool_end` for `qualification_hold` per run and fails if the model does not
cooperate. The five-minute hold is credential-free; no test or sandbox writes Table state,
and the controller remains the sole durable-state writer. The runner provisions
sessions through the Easy-Auth-protected public endpoint in bounded batches,
then submits the held runs concurrently only after every prepared session is
public-terminal and durably idle.

Use the same deployed settings as the lifecycle test, then supply the load
concurrency explicitly. Omission intentionally skips this test even when the
deployed-smoke opt-in is set.

**`N=5` is the sole agent/CI diagnostic validation size.** It verifies the
public orchestration, bounded common-active interval, replay/`409` behavior,
and controller cleanup path; it is not a capacity or formal acceptance claim.
Agents and CI must run only this diagnostic size, including the persistent
pipeline default. Both Python runtime legs run in parallel for Manual/Scheduled
diagnostics:

```bash
export AZURE_FUNCTIONS_AGENTS_RUN_DEPLOYED_ACA_SMOKE=1
export AZURE_FUNCTIONS_AGENTS_ACA_LOAD_CONCURRENCY=5
python -m pytest -m live_aca tests/live/test_aca_deployed_load.py -v \
  -o log_cli=true -o log_cli_level=INFO

# Equivalent explicit CLI input; this wins over the environment fallback.
python -m pytest -m live_aca tests/live/test_aca_deployed_load.py \
  --aca-load-concurrency 5 -v -o log_cli=true -o log_cli_level=INFO
```

The CLI and environment range remains **1..100** so the formal value is still
possible. `N=100` is formal Decision #29 acceptance and is **human-only**: it
must not be queued or executed by an agent or CI. A human operator performs the
formal run only when ready, using this exact explicit override (never as a
default):

```bash
az pipelines run --id 1777 --branch larohra-u3-ga-gate \
  --organization https://dev.azure.com/azfunc --project internal \
  --parameters acaRuntimeTarget=python313 acaLoadConcurrency=100 acaProvisionConcurrency=4
```

The `ACADeployedAgentTurn` ADO job remains Manual/Scheduled plus
`continueOnError`. The `acaLoadConcurrency` queue-time parameter defaults to
`5` and accepts only `5` or `100`; an existing
`ACA_DEPLOYED_LOAD_CONCURRENCY` pipeline variable does not control this job.
The N=100 parameter value is human-only and requires
`acaRuntimeTarget=python313` or `acaRuntimeTarget=python314`. The preflight
rejects N=100 with the default `both` target before pytest starts, so no
dual-runtime N=100 run is launched.

This run can consume at least 500 sandbox-minutes at `N=100`, plus model and
storage costs, and needs ACA and model quota for all sessions. The fixed
four-session provisioning batches deliberately avoid treating simultaneous
content/package delivery saturation as evidence about concurrent runs: each
batch reaches public-terminal, durable ready/suspended idle before the next
batch posts. Every public setup POST has an enforced 45-second attempt bound,
and each complete Phase A batch has an outermost 660-second (11m)
deadline covering submission, retry waits, public terminal evidence, and
durable idle validation. Setup may retry up to 12 times, but individual retry
waits are not additive:
the outer 11m batch deadline is enforced. The same 11m bound applies to the
Phase B admission and backing-loss held admission. Twenty-five enforced 11m
Phase A batches cap `N=100` provisioning at 275m. Do not add individual 540-second
`ClientSession` values to that bound: the batch deadline is outermost. Phase B
setup is concurrent before its 300-second formal hold and 11-minute event
bound. The load-only agent has a
900-second authored timeout; plan for batch provisioning plus the hold, and use
a CI-dedicated Sandbox Group. The Manual/Scheduled ADO job has a 360-minute safety cap;
the remaining 85m cover Phase B, the other deployed qualifications,
bounded 900-second failure settlement, controller cleanup, and job overhead.
Do not run N=100 from PR, default, or scheduled jobs. `N=100` refers only to the
concurrent Phase B held runs, not the four-way Phase A provisioning rate. The
redacted report separates prepared count, provisioning duration/attempts/retries,
prepared suspension evidence, and formal held-run latency, overlap, race, and
cleanup evidence; it never prints prompts, model output, headers, tokens, or
resource IDs.

The deployed Function's protected settings must set
`AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT=none` for this load agent. The ADO
identity deliberately lacks Function ARM Reader, so the pipeline does not
attempt an ARM preflight. An operator must verify that deployment setting on
the dedicated Function App before the live run; the exact public
`tool_start`/`tool_end` evidence is the end-to-end functional proof.

### Use the deployed Python runtime matrix

`ACADeployedColdStart` is phase one of the deployed matrix and
`ACADeployedAgentTurn` depends on it, so all selected cold-start legs complete
before either deployed/load leg starts. Within each phase, `Python313` and
`Python314` run in parallel (`maxParallel: 2`). `acaRuntimeTarget` accepts
`both` (the default), `python313`, or `python314`; compile-time matrix inclusion
creates two legs for `both` and one for either selected runtime. Both jobs run
only for **Manual** and **Schedule** reasons, stay nonblocking with
`continueOnError`, and are excluded from pull requests.

The checked-in non-secret target variables are available to both scheduled
pipelines through `eng/ci/variables/aca-deployed-runtime-targets.yml`:

| Runtime target | Function URL input | Site-name input |
| --- | --- | --- |
| `python313` | `ACA_DEPLOYED_PY313_FUNCTION_BASE_URL` = `https://func-afar-u3q-6k9m2p7.azurewebsites.net/api` | `ACA_DEPLOYED_PY313_APP_SITE_NAME` = `func-afar-u3q-6k9m2p7` |
| `python314` | `ACA_DEPLOYED_PY314_FUNCTION_BASE_URL` = `https://func-afar-u3q314-6k9m2p7.azurewebsites.net/api` | `ACA_DEPLOYED_PY314_APP_SITE_NAME` = `func-afar-u3q314-6k9m2p7` |

Keep any branch-specific replacements in protected, non-secret pipeline
configuration; do not place tokens, storage keys, or model credentials in these
variables. The legs intentionally share the existing Easy Auth scope/audience,
Table service/name, Sandbox Group, and `acaServiceConnection`. Existing
`ACA_DEPLOYED_FUNCTION_BASE_URL` and `ACA_DEPLOYED_APP_SITE_NAME` pipeline
variables remain harmless for callers that still define them, but the matrix
uses the runtime-specific inputs above.

`acaProvisionConcurrency` is a queue-time string parameter with values `1`,
`2`, or `4`; it defaults to `1`. The matrix's default `both` target therefore
provisions at most one session per runtime leg (two total) against the shared
Sandbox Group. The preflight rejects a `both` run above `1`; it preserves
parallel Python 3.13/3.14 validation rather than serializing the matrix. A
human-selected single runtime may use `4`, including the N=100 formal path.
Direct/local pytest accepts
`--aca-provision-concurrency` or
`AZURE_FUNCTIONS_AGENTS_ACA_PROVISION_CONCURRENCY` in `1..4` and defaults to
`4`.

Phase A uses the no-tools readiness prompt and requires public SSE/status/result
success plus a read-only durable idle projection; it fails if a
`qualification_hold` tool event appears. Prepared sessions may suspend before
Phase B, which is expected. For the formal `N=100` run, the runner reads only
each prepared session's exact-label backing at most once per second and requires
at least one aggregate provider observation in `Stopped` or `Suspended` before
Phase B. Every existing-session Phase B response must preserve its requested
prepared session ID, and the final held-session set must exactly equal the
prepared-session set; the subsequent running common-active proof therefore
demonstrates activation of that same set. Lower-N diagnostics do not require
the suspension observation. The formal assertion uses conservative overlapping observations rather than
claiming an atomic Table snapshot: each owned row is read twice no faster than
one second apart, and the common interval is bounded by the latest first-read
completion and earliest second-read start. Every distinct durable run must be
`accepted` or `running`, every session is `running` and identifies that run as active, and any
active operation must target that run and generation. During that proven common
interval, a bounded sample repeats each original idempotency key (same run) and
submits a different key to the same session (`409 active_run_exists`).
Before that proof, the runner requires the observed admission spread to leave
more than the 120-second proof timeout plus a 15-second margin inside the
five-minute hold. This rejects an over-spread batch instead of assuming it can
meet the common-active requirement. Ambiguous, malformed, and final
setup-deadline admissions wait for the bounded public `Retry-After` lease
hint (falling back to 60 seconds), then reuse the same key and resolve
read-only through their owner-scoped idempotency reservations for cleanup;
unresolved reservations are reported and
keep cleanup incomplete. After the fixed hold releases, every admitted run must expose ordered public
SSE ending in `done`, a successful status/result, a terminally consistent
read-only Table projection, terminal latency of at least 299 seconds for the
300-second hold, and no owned sandbox or snapshot leak after controller-observed
cleanup.
If another qualification assertion fails before the hold completes, the runner
first waits (for up to the load agent's 900-second authored budget) for
provisioning to reach `running` or terminal, publicly cancels nonterminal
running work, and requires the read-only durable projection to become terminal
and idle before controller cleanup. If that settlement fails, exact-label
provider cleanup is only a last resort and durable cleanup is reported failed.

### Run the deployed backing-loss qualification manually

`tests/live/test_aca_deployed_loss.py` uses exactly one `deployed_load`
`qualification_hold` run, waits for its active exact-label ACA backing, then
deletes only that backing through the test's ACA control-plane adapter. It
never writes Table state: the deployed reconciler is the only durable-state
writer. The test reads the owner partition derived from the Easy Auth token,
then requires the controller to write an `abandoned` run and `tombstoned`
session with reason `sandbox_backing_lost`, clear the active run and operation,
complete the durable operation, and leave no exact-label sandbox or snapshot.
It also proves the public status remains an `abandoned` HTTP 200 projection
while the public result is HTTP 410 (`result_unavailable` or `session_gone`).
Status error metadata is optional; if present it must be the typed
`session_tombstoned` or `run_abandoned` error.

Run it with the same deployed lifecycle settings; it does **not**
require `AZURE_FUNCTIONS_AGENTS_ACA_LOAD_CONCURRENCY`, so omitting that value
skips only the N-load test. The Manual/Scheduled `ACADeployedAgentTurn` job runs this
single-loss proof independently of load size. It polls at most once per second
for the operation lease plus bounded controller-cadence window. Unit doubles
cover the selector and public-response contracts only; they cannot certify
real ACA provider loss, so do not treat them as a substitute for a live pass.

If the exact backing delete succeeds but the controller does not tombstone,
the test leaves no provider backing and reports an `ACA-SMOKE-ENV` cleanup
failure with only exact-label selector-key diagnostics. It does not cancel the
held run or mutate Table rows.

### Host ABI prerequisite

Run the live tests only from Linux x86_64, including WSL, on the CPython minor
version that matches the sandbox disk you are targeting. The fixture builds the
dependency closure with the host interpreter, so compiled wheels such as
`pydantic_core` and `aiohttp` carry that interpreter's ABI tag: a `cp314` wheel
cannot import on the CPython 3.13 that a `python-3.13` disk boots. Being on
"some Linux CPython" is not sufficient.

The guard therefore requires host minor == disk minor, and its error names both
versions and the two ways out: run on the matching interpreter, or target the
`python-<host-version>` disk. Windows and macOS hosts are rejected outright. All
of these checks run before the closure is built and before any sandbox is
created, so a mismatch costs nothing.

A `uv`-created virtual environment may ship without `pip`. The closure build
shells out to `pip`, so run `python -m ensurepip` once in such an environment;
the failure surfaces pip's own message, so this is self-explanatory the first
time it happens.

## Enable the scheduled CI job

The job already exists — it is `ACAHarnessEntrypointSmoke` in
`eng/templates/official/jobs/e2e-tests.yml`, which the existing
`eng/ci/e2e-tests.yml` pipeline expands. **No new pipeline needs to be
created or registered.** It runs only on `Schedule` or `Manual` builds, never on
pull requests, and carries `continueOnError: true`.

Azure sign-in is handled by the `AzureCLI@2` task, exactly as the Foundry E2E
job does: the task performs `az login` as the service connection's identity, and
`AZURE_TOKEN_CREDENTIALS: 'dev'` makes `DefaultAzureCredential` inside the test
reuse that login. Nothing needs to be authenticated by hand at run time.

Four things must be supplied before the job can pass:

| # | Item | Where |
| --- | --- | --- |
| 1 | A Sandbox Group dedicated to CI | Azure |
| 2 | A service connection to that subscription, ideally workload-identity federated | ADO project settings |
| 3 | `Container Apps SandboxGroup Data Owner` on that group for the connection's identity — needs Owner or User Access Administrator | Azure IAM |
| 4 | Pipeline variables `ACA_SANDBOX_GROUP_RESOURCE_ID`, `ACA_SANDBOX_DISK` | ADO pipeline |

The connection name is the `acaServiceConnection` **runtime parameter**, exposed
in the Run pipeline dialog as "ACA smoke: Azure service connection". Set it at
queue time to point at a connection whose identity holds the role in item 3 —
no YAML edit is needed to change it. The `saf-foundry-connection` default only
exists so the template compiles and is unlikely to hold that role.

The preflight step `Verify ACA sandbox group is reachable` runs before any test
and prints the signed-in identity, then rejects a missing variable, an
un-substituted `$(NAME)` placeholder, or a resource ID that is not a Sandbox
Group. Each of those fails at a step whose name is the diagnosis, so setup
mistakes never surface as test failures.


| Result | Meaning | Triage |
| --- | --- | --- |
| Skip | Live ACA was not explicitly enabled. | No action. |
| Error with `ACA-SMOKE-ENV:` | The opted-in subscription, group, credentials, quota, sandbox creation, closure delivery, or closure verification is unusable. | Call ops. |
| Failure | The sandbox was healthy and the synchronous harness probe returned a nonzero exit code. | Call the runtime owners. |

**Triage rule: errors = call ops; failures = call the runtime owners.**

The fixture builds and verifies the complete dependency closure before the
entrypoint probe. In particular, it runs `python -c "import agent_framework"`
inside the sandbox and treats a nonzero result as an environment error. This
prevents an incomplete closure from being misreported as an entrypoint defect.

## Dependency closure and budget

The live fixture uses local `pip install --target` to build the runtime's full
dependency closure, emits the archive size and ZIP entry count, creates a
deterministic `ZIP_STORED` archive, and sends that archive through one
`write_file` call. The sandbox extracts it into the `PYTHONPATH` directory
before the verification and probe commands run. No network package install is
attempted inside a sandbox.

For the lower-level real-agent-turn qualification, that same already-built
closure is also deterministically embedded in the captured application under
`.python_packages/lib/site-packages/`. Production bootstrap intentionally runs
with `-E -S`, so this captured copy is required for bootstrap and cannot rely
on the sandbox-level `PYTHONPATH`.

The archive budget is 80 MiB. The measured closure contains 5,968 ZIP entries
and is 75.9 MiB. The budget is deliberately not expanded with compression:
`ZIP_STORED` keeps the payload digest-stable and the 80 MiB threshold is the
largest verified incompressible single write.

Measured direct-write timing was approximately:

| Payload | Time |
| --- | ---: |
| 1 MiB | 2.1 s |
| 16 MiB | 2.0 s |
| 64 MiB | 7.6 s |
| 80 MiB | 13.8 s |

There is roughly two seconds of fixed overhead per write, with throughput
leveling near 6–8 MiB/s. The archive is well below the service's 256 MiB file
cap and 65,535 ZIP-entry cap.

## Lifecycle and cleanup

Every smoke sandbox has dedicated CI owner and app hash labels, receives an
explicit per-sandbox lifecycle policy immediately after creation, and is
deleted in fixture teardown. The teardown first uses the live handle, then
falls back to direct ID deletion and finally an exact-label reconciliation.
The pipeline also runs a label-scoped reaper with `condition: always()` so a
failed job cannot leave an unbounded paid resource behind. The dedicated labels
must never be reused for non-CI sandboxes.

## CI prerequisites

The ACA smoke job is in `eng/templates/official/jobs/e2e-tests.yml`. It runs
only for scheduled and manually started builds and is currently
`continueOnError`, avoiding a permanently red pull-request check while the
entrypoint change lands.

Ops must provide:

1. A CI-dedicated ACA Sandbox Group in the intended region.
2. A federated Azure service connection, selected at queue time through the
   `acaServiceConnection` runtime parameter.
3. `Container Apps SandboxGroup Data Owner` (role id
   `c24cf47c-5077-412d-a19c-45202126392c`) for that identity, scoped to the CI
   Sandbox Group.
4. Non-secret pipeline variables:
   `ACA_SANDBOX_GROUP_RESOURCE_ID` and `ACA_SANDBOX_DISK`.

The job first runs `Verify ACA sandbox group is reachable` through `AzureCLI@2`
with `AZURE_TOKEN_CREDENTIALS=dev`. That gives authentication, RBAC, and group
configuration failures an explicit operational diagnosis before pytest runs.
