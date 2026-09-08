# Durable agent loop private infrastructure spike

This folder codifies the independently deployable infrastructure and
qualification scaffolding for finalized
[FRD 0010](../../docs/frds/0010-durable-agent-loop-spike.md). It reproduces the
authorized private environment in `eastus2` with exact stable names. It does
not contain the unfinished durable-loop application or define its routes,
payloads, or runtime API.

Do not run `azd up` for this slice. Provisioning and application deployment are
deliberately separate:

1. `infra/` owns only resources, isolated APIM child resources, RBAC, and app
   settings.
2. `src/` is an assembly slot with host configuration. The final stacked layer
   must add `src/function_app.py` and its agent/runtime source.
3. `eng/scripts/durable_loop_spike.py` builds the local runtime wheel, stages
   the completed source, creates a deterministic ZIP, and deploys only when the
   operator invokes the explicit `deploy` command.
4. `eng/scripts/durable_loop_spike_qualification.py` calls operator-supplied
   route templates without printing request or response bodies.

## Authorized contract

| Resource | Exact name or endpoint |
| --- | --- |
| Resource group | `larohra-durable-agent-loop` |
| Location | `eastus2` |
| AIServices account / project | `aidurableloop0904e2` / `durable-agent-loop` |
| Model deployment | `gpt-6-astra` (`gpt-6-astra`, version `2026-09-03`, GlobalStandard 200) |
| Function / sandbox identities | `id-durable-loop-func-0904` / `id-durable-loop-sandbox-0904` |
| Storage / containers | `stdurableloop0904e2` / `app-package-func-durable-loop-0904` / `durable-loop-content` |
| Log Analytics / App Insights | `log-durable-loop-0904` / `appi-durable-loop-0904` |
| Sandbox Group | `sbg-durable-loop-0904` |
| Durable Task Scheduler / task hub | `dts-durable-loop-0904` / `durable-loop-demo` |
| Flex app | `func-durable-loop-0904` |
| Shared APIM | `larohra-ai-gateway` in `larohra-operations-agent-3p-rg` |
| Model API | `https://larohra-ai-gateway.azure-api.net/durable-agent-loop-model/openai/v1` |
| Model control API | `https://larohra-ai-gateway.azure-api.net/durable-agent-loop-model-control` |
| MCP API | `https://larohra-ai-gateway.azure-api.net/durable-agent-loop-mcp` |

The resource group receives only these tags:

```text
purpose=durable-agent-loop-spike
owner=larohra
source=azure-functions-agents-runtime
environment=private-spike
```

## Safe infrastructure validation and provisioning

Select the subscription externally before using the template:

```powershell
az account set --subscription <subscription-id-or-name>
az account show --query "{name:name,id:id}" --output table
```

Compile and preview from the repository root:

```powershell
az bicep build `
  --file samples\durable-agent-loop-spike\infra\main.bicep `
  --stdout > $null

az deployment sub what-if `
  --name durable-agent-loop-spike-preview `
  --location eastus2 `
  --template-file samples\durable-agent-loop-spike\infra\main.bicep `
  --parameters '@samples\durable-agent-loop-spike\infra\main.parameters.json' `
  --result-format ResourceIdOnly
```

The existing manually proven role assignments have non-deterministic resource
names. `main.bicep` carries those exact assignment IDs so deployment adopts
them instead of attempting duplicate assignments. If principals or resources
are overridden for another environment, override the corresponding
`roleAssignmentNames` entries at the same time.

Provision only after reviewing the what-if:

```powershell
az deployment sub create `
  --name durable-agent-loop-spike `
  --location eastus2 `
  --template-file samples\durable-agent-loop-spike\infra\main.bicep `
  --parameters '@samples\durable-agent-loop-spike\infra\main.parameters.json'
```

`azd provision` can also consume `azure.yaml`, but `azd up` and `azd deploy`
must wait for the final application layer.

## Secret handling

The APIM subscription resource omits `primaryKey` and `secondaryKey`, so ARM
generates and retains both secrets. The Bicep root calls `listSecrets()` only
inside the secure nested Function App deployment parameter that writes
`AZURE_FUNCTIONS_AGENTS_APIM_SUBSCRIPTION_KEY`. No template output exposes the
key and no script reads it.

The API-scoped Application Insights logger uses the secret APIM named value
`durable-agent-loop-appinsights-key`. Its value is resolved directly from the
Application Insights resource within the deployment; it is never an output or
committed literal.

The model API exposes only response creation and chat completions. Background
response polling and cancellation use the separate model-control API. That API
accepts the provider response ID only in `x-af-response-id`, validates the
bounded `resp_` shape, stores it in a policy variable, deletes the header, and
then rewrites to the fixed model backend. The control API has no Application
Insights diagnostic because backend dependency names include the rewritten
provider path and would disclose the response ID. Because the shared APIM
service has an inherited all-API Azure Monitor diagnostic, the control API
defines a child `azuremonitor` override with sampling `0`, no client
IP/body/header capture, and all query parameters masked. Model-start/chat and
MCP Application Insights diagnostics remain API-scoped with zero
request/response body bytes and all query parameters masked. All three inbound
policies delete both APIM subscription-key carriers (`api-key` header and
`subscription-key` query parameter) before forwarding so neither can be
confused with or exposed as a backend model key.

Local operators must supply inbound Function authentication through an
environment variable. Do not place Function keys, APIM keys, prompts, answers,
or response bodies in command lines, committed files, or logs.

Durable prompt, tool, and result content uses the dedicated private
`durable-loop-content` Blob container rather than `AzureWebJobsStorage`. The
Function app receives the account URI, container name, and Function UAMI client
ID through the three
`AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_CONTENT_*` settings.
The existing account-scoped Storage Blob Data Owner assignment grants the
Function identity access without a connection string or storage key.

Durable orchestration state uses the dedicated Consumption SKU Durable Task
Scheduler `dts-durable-loop-0904` and task hub `durable-loop-demo`. The Function
UAMI receives `Durable Task Data Contributor` only on that task hub. The app
connects through managed identity using
`DURABLE_TASK_SCHEDULER_CONNECTION_STRING`; `TASKHUB_NAME` selects the hub, and
`src/host.json` selects the `azureManaged` Durable storage provider. The
identity-based `AzureWebJobsStorage__*` settings remain unchanged for Functions
host storage, session history, and a rollback to Azure Storage Durable.

For local development, `local.settings.template.json` targets the official DTS
emulator at `http://localhost:8080` and task hub `default`; its dashboard is at
`http://localhost:8082`.

Rollback is deliberately package-based rather than secret-based: redeploy the
known storage-backed source package at commit
`d205c4206b6ac539979aef518c4fb249818fd285`, remove only
`DURABLE_TASK_SCHEDULER_CONNECTION_STRING` and `TASKHUB_NAME`, and restart the
app. No storage key, connection string, or publishing profile is required.

The application settings also pin
`AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_HYBRID_TOOL_BUNDLE_ROOT=sandbox_bundle`
and
`AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_HYBRID_ALLOWED_HOSTS=management.azure.com,www.example.com`.
The final application layer must place its bounded customer-tool package under
that bundle root. The compatibility fallback therefore cannot archive the
whole Function app into each sandbox, and sandbox egress remains limited to the
two approved qualification hosts. No credential is copied into the bundle or
sandbox. The durable-loop feature gate remains `false` in IaC until the
operator explicitly activates the deployed application.

The deployment pins the private qualification controls explicitly rather than
depending on process defaults:

| Setting | Provisioned value |
| --- | --- |
| `AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_ENABLED` | `false` |
| `AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_BACKGROUND_MODEL_ENABLED` | `false` |
| `AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_RETAINED_SANDBOX_ENABLED` | `false` |
| `AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_FAULT_INJECTION_ENABLED` | `false` |
| `AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_MAX_APP_OWNED_SANDBOXES` | `10` |
| `AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_RETAINED_SANDBOX_AUTO_DELETE_SECONDS` | `600` |
| `AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_SANDBOX_REAPER_AGE_SECONDS` | `600` |
| `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS` | `aiohttp-client,httpx,requests,urllib,urllib3` |

For a retained-session demo, a successful turn leaves its stopped sandbox
available for the next turn. Each tool handoff applies the bounded ACA
auto-delete policy, and the durable reaper remains a second deletion backstop.
Failure and cancellation still schedule explicit cleanup before releasing the
session fence.

After the package is deployed and indexed with the main gate off, live
qualification enables the main, background-model, retained-sandbox, and
fault-injection gates together through a secure app-setting update.
The HTTP client auto-instrumentors are disabled for this privacy-focused spike;
the runtime's bounded durable-loop spans and metrics remain enabled.

## Final application assembly

The final stacked layer adds `function_app.py` and all application files
directly under `src/`. Until then, assembly fails with
`application_source_incomplete:function_app.py`.

After that layer is present:

```powershell
uv run --with pip python eng\scripts\durable_loop_spike.py assemble
```

The command:

- builds exactly one local `azurefunctions-agents-runtime` wheel with
  `pip wheel --no-deps`;
- copies the sample source while excluding local settings and caches;
- writes `requirements.txt` with
  `./<wheel>[aca_sandbox,monitor]` first, so the sample resolves the runtime's
  own ACA SDK and Azure Monitor optional extras without duplicating their pins;
- adds the three exact, hash-pinned official MAF release-wheel URLs from the
  repository lock so Flex remote build does not depend on package-index
  propagation;
- writes a content-only `DEPLOYMENT_MANIFEST.json`;
- creates a sorted ZIP with fixed timestamps and permissions; and
- prints only the archive path, wheel filename, and SHA-256.

If the final source needs additional packages, place them in an
operator-reviewed file and pass `--requirements-extra <path>`. The helper
copies its text after the local wheel requirement.

`src/host.json` sets `functionTimeout` to 30 minutes. Individual durable
activities remain bounded to eight minutes or less; the extra host margin
covers provider polling and cleanup. Multi-hour run lifetime still comes from
orchestration checkpoints and durable timers, never one long-running Function
invocation.

Application deployment is a separate, explicit command and is not run by
infrastructure provisioning:

```powershell
uv run --with pip python eng\scripts\durable_loop_spike.py deploy `
  --acknowledge-existing-app func-durable-loop-0904
```

The acknowledgement must exactly match `--app-name`. Deployment performs a
read preflight and Flex `config-zip --build-remote true`. Remote build is an
explicit deployment operation; the Function app template does not set the
unsupported `SCM_DO_BUILD_DURING_DEPLOYMENT` setting. The helper does not
retrieve or update app settings and therefore never persists the APIM
subscription key.

## Bounded qualification CLI

Each command defaults to the finalized application route. `--route-template`
remains available only for controlled tests and accepts relative templates with
fixed placeholder values supplied through `--value`. Bodies come from standard
input or an operator-controlled file outside the repository. The CLI validates
bounded JSON, rejects duplicate keys at every nesting level, and never prints a
request or response body.

Store an inbound Function key only in the current process environment:

```powershell
$env:DURABLE_LOOP_FUNCTION_KEY = '<securely-obtained-function-key>'
$env:DURABLE_LOOP_START_REQUEST = 'C:\secure\durable-loop\start.json'
$env:DURABLE_LOOP_START_IDEMPOTENCY_KEY = '<operator-generated-idempotency-key>'
$env:DURABLE_LOOP_ANSWER_REQUEST = 'C:\secure\durable-loop\answer.json'
$env:DURABLE_LOOP_ANSWER_IDEMPOTENCY_KEY = '<operator-generated-idempotency-key>'
$env:DURABLE_LOOP_METRICS = 'C:\secure\durable-loop\qualification.jsonl'
```

The start document must contain `prompt` and may contain `session_id`. It must
also contain `request_id` or the command must supply `Idempotency-Key` from an
environment variable. Qualification requests may also select
`sandbox_profile` as `per_call` or `retained_session`, and `fault_profile` as
one of the fixed runtime fault values; the corresponding private gate must be
enabled. The human-answer document contains only `answer` and always supplies
`Idempotency-Key` from an environment variable.

```powershell
# Start.
uv run --with pip python eng\scripts\durable_loop_spike_qualification.py start `
  --body-file $env:DURABLE_LOOP_START_REQUEST `
  --header-name x-functions-key `
  --header-secret-env DURABLE_LOOP_FUNCTION_KEY `
  --idempotency-key-env DURABLE_LOOP_START_IDEMPOTENCY_KEY `
  --extract run_id `
  --extract session_id `
  --extract status

# One status snapshot with content-free counters.
uv run --with pip python eng\scripts\durable_loop_spike_qualification.py status `
  --value "run_id=$env:DURABLE_LOOP_RUN_ID" `
  --header-name x-functions-key `
  --header-secret-env DURABLE_LOOP_FUNCTION_KEY `
  --extract status `
  --extract phase `
  --extract model_steps `
  --extract tool_calls `
  --extract human_waits `
  --extract step_index `
  --extract input_tokens `
  --extract output_tokens `
  --extract reasoning_tokens `
  --extract cost_microunits `
  --extract external_content_bytes `
  --extract parked_seconds

# Poll until Completed, Failed, Cancelled, or Waiting. JSONL is appended.
uv run --with pip python eng\scripts\durable_loop_spike_qualification.py poll `
  --value "run_id=$env:DURABLE_LOOP_RUN_ID" `
  --header-name x-functions-key `
  --header-secret-env DURABLE_LOOP_FUNCTION_KEY `
  --poll-interval-seconds 2 `
  --poll-deadline-seconds 1800 `
  --metrics-output $env:DURABLE_LOOP_METRICS `
  --metrics-format jsonl

# When status is Waiting, inspect only safe human-input control metadata.
uv run --with pip python eng\scripts\durable_loop_spike_qualification.py status `
  --value "run_id=$env:DURABLE_LOOP_RUN_ID" `
  --header-name x-functions-key `
  --header-secret-env DURABLE_LOOP_FUNCTION_KEY `
  --extract status `
  --extract human_request_id `
  --extract human_respond_url `
  --extract human_detail_url `
  --extract human_expires_at `
  --extract human_allow_free_text `
  --extract human_choice_count `
  --extract human_schema_present

# Authenticated HITL detail availability probe. Content is counted and discarded.
uv run --with pip python eng\scripts\durable_loop_spike_qualification.py human-detail `
  --value "run_id=$env:DURABLE_LOOP_RUN_ID" `
  --value "request_id=$env:DURABLE_LOOP_REQUEST_ID" `
  --header-name x-functions-key `
  --header-secret-env DURABLE_LOOP_FUNCTION_KEY

# Result. The final response is counted and discarded.
uv run --with pip python eng\scripts\durable_loop_spike_qualification.py result `
  --value "run_id=$env:DURABLE_LOOP_RUN_ID" `
  --header-name x-functions-key `
  --header-secret-env DURABLE_LOOP_FUNCTION_KEY

# Cancel.
uv run --with pip python eng\scripts\durable_loop_spike_qualification.py cancel `
  --value "run_id=$env:DURABLE_LOOP_RUN_ID" `
  --header-name x-functions-key `
  --header-secret-env DURABLE_LOOP_FUNCTION_KEY `
  --extract status `
  --extract disposition `
  --extract possibly_committed `
  --extract error_code

# Human answer.
uv run --with pip python eng\scripts\durable_loop_spike_qualification.py human-answer `
  --value "run_id=$env:DURABLE_LOOP_RUN_ID" `
  --value "request_id=$env:DURABLE_LOOP_REQUEST_ID" `
  --body-file $env:DURABLE_LOOP_ANSWER_REQUEST `
  --header-name x-functions-key `
  --header-secret-env DURABLE_LOOP_FUNCTION_KEY `
  --idempotency-key-env DURABLE_LOOP_ANSWER_IDEMPOTENCY_KEY `
  --extract status `
  --extract delivery `
  --extract disposition `
  --extract possibly_committed `
  --extract error_code
```

Each call is capped at 600 seconds and 1 MiB of response data. Start and human
answer bodies are capped at 256 KiB. Polling intervals are bounded from 0.5 to
60 seconds and deadlines from 1 second to 6 hours. Poll output contains
attempts, total elapsed time, total response bytes, p50 request latency, and p95
latency when at least two requests were made. `Waiting` terminates polling just
like a terminal run state.

`--extract` accepts only this fixed content-free selector allowlist:
`run_id`, `session_id`, `status`, `phase`, `model_steps`, `tool_calls`,
`human_waits`, `step_index`, `input_tokens`, `output_tokens`,
`reasoning_tokens`, `cost_microunits`, `external_content_bytes`,
`parked_seconds`, `delivery`, `disposition`, `possibly_committed`,
`error_code`, `human_request_id`, `human_respond_url`, `human_expires_at`,
`human_allow_free_text`, `human_choice_count`, `human_detail_url`, and
`human_schema_present`. The `human_*` selectors map to the exact nested
`human_input` control fields `request_id`, `expires_at`, `respond_url`,
`detail_url`, `allow_free_text`, `choice_count`, and `schema_present`.
Both control URLs must equal the fixed owner-authorized input route for the
validated run and human-request IDs. Phase, delivery, disposition, and error
codes are fixed enums rather than arbitrary response strings. Legacy selectors such as
`completed_model_count`, `completed_tool_count`, `duration_ms`, control URL
aliases, arbitrary dotted paths, and label-to-path redirects are rejected.

Question text, choice values, response schemas, prompts, answers, final
responses, tool data, provider IDs, credentials, and raw bodies can never be
selected. The `human-detail` GET command verifies authorization, availability,
latency, and response size but discards its `question`, `choices`, and
`response_schema`; those values are never written to metrics. HTTP and local
errors use content-free codes. Metrics output requires
an absolute `.jsonl` or `.json` path outside the repository, refuses symlinks
and unsafe targets, and contains only the same validated fields plus
latency/byte/count aggregates. JSONL appends one record per command; JSON
replaces one single-command snapshot.

## Sandbox lifecycle evidence

A bounded live probe without explicit cleanup first observed the sandbox
`Stopped` at **83.527 seconds** and absent from the group inventory at
**327.959 seconds**. Timed stop/delete is a failure backstop, not normal prompt
cleanup. Explicit server-side delete remains the primary completion path; the
policy and reconciler cover interrupted or ambiguous cleanup.

## Resource inventory

Inventory the dedicated group without reading app settings or secrets:

```powershell
az resource list `
  --resource-group larohra-durable-agent-loop `
  --query "sort_by([].{name:name,type:type,location:location}, &type)" `
  --output table
```

Inventory only this spike's APIM children:

```powershell
$apimId = az apim show `
  --resource-group larohra-operations-agent-3p-rg `
  --name larohra-ai-gateway `
  --query id --output tsv

az resource show --ids "$apimId/apis/durable-agent-loop-model"
az resource show --ids "$apimId/apis/durable-agent-loop-model-control"
az resource show --ids "$apimId/apis/durable-agent-loop-mcp"
az resource show --ids "$apimId/backends/durable-agent-loop-model"
az resource show --ids "$apimId/products/durable-agent-loop-spike"
az resource show --ids "$apimId/subscriptions/durable-agent-loop-spike"
az resource show --ids "$apimId/loggers/durable-agent-loop-ai"
az resource show --ids "$apimId/namedValues/durable-agent-loop-appinsights-key"
```

## Exact cleanup

Cleanup is intentionally resource-ID scoped. Never delete or redeploy the
shared APIM service, its global policy, or any API not listed here.

```powershell
$subscriptionId = az account show --query id --output tsv
$apimId = "/subscriptions/$subscriptionId/resourceGroups/larohra-operations-agent-3p-rg/providers/Microsoft.ApiManagement/service/larohra-ai-gateway"
$workload = "/subscriptions/$subscriptionId/resourceGroups/larohra-durable-agent-loop/providers"

# Isolated APIM children only.
az resource delete --ids "$apimId/subscriptions/durable-agent-loop-spike"
az resource delete --ids "$apimId/products/durable-agent-loop-spike"
az resource delete --ids "$apimId/apis/durable-agent-loop-model"
az resource delete --ids "$apimId/apis/durable-agent-loop-model-control"
az resource delete --ids "$apimId/apis/durable-agent-loop-mcp"
az resource delete --ids "$apimId/backends/durable-agent-loop-model"
az resource delete --ids "$apimId/loggers/durable-agent-loop-ai"
az resource delete --ids "$apimId/namedValues/durable-agent-loop-appinsights-key"

# Exact workload resources only.
az resource delete --ids "$workload/Microsoft.Web/sites/func-durable-loop-0904"
az resource delete --ids "$workload/Microsoft.Web/serverfarms/ASP-larohradurableagentloop-7c8a"
az resource delete --ids "$workload/Microsoft.App/sandboxGroups/sbg-durable-loop-0904"
az resource delete --ids "$workload/Microsoft.CognitiveServices/accounts/aidurableloop0904e2/deployments/gpt-6-astra"
az resource delete --ids "$workload/Microsoft.CognitiveServices/accounts/aidurableloop0904e2/projects/durable-agent-loop"
az resource delete --ids "$workload/Microsoft.CognitiveServices/accounts/aidurableloop0904e2"
az resource delete --ids "$workload/Microsoft.Storage/storageAccounts/stdurableloop0904e2"
az resource delete --ids "$workload/Microsoft.Insights/components/appi-durable-loop-0904"
az resource delete --ids "$workload/Microsoft.OperationalInsights/workspaces/log-durable-loop-0904"
az resource delete --ids "$workload/Microsoft.ManagedIdentity/userAssignedIdentities/id-durable-loop-func-0904"
az resource delete --ids "$workload/Microsoft.ManagedIdentity/userAssignedIdentities/id-durable-loop-sandbox-0904"
```

The commands deliberately leave the resource group in place. Delete it only
after a separate inventory proves it is empty; this sample never performs a
broad resource-group deletion.
