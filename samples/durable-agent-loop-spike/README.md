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
| Storage / deployment container | `stdurableloop0904e2` / `app-package-func-durable-loop-0904` |
| Log Analytics / App Insights | `log-durable-loop-0904` / `appi-durable-loop-0904` |
| Sandbox Group | `sbg-durable-loop-0904` |
| Flex app | `func-durable-loop-0904` |
| Shared APIM | `larohra-ai-gateway` in `larohra-operations-agent-3p-rg` |
| Model API | `https://larohra-ai-gateway.azure-api.net/durable-agent-loop-model/openai/v1` |
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

Local operators must supply inbound Function authentication through an
environment variable. Do not place Function keys, APIM keys, prompts, answers,
or response bodies in command lines, committed files, or logs.

## Final application assembly

The final stacked layer adds `function_app.py` and all application files
directly under `src/`. Until then, assembly fails with
`application_source_incomplete:function_app.py`.

After that layer is present:

```powershell
python eng\scripts\durable_loop_spike.py assemble
```

The command:

- builds exactly one local `azurefunctions-agents-runtime` wheel with
  `pip wheel --no-deps`;
- copies the sample source while excluding local settings and caches;
- writes `requirements.txt` with the local wheel first;
- writes a content-only `DEPLOYMENT_MANIFEST.json`;
- creates a sorted ZIP with fixed timestamps and permissions; and
- prints only the archive path, wheel filename, and SHA-256.

If the final source needs additional packages, place them in an
operator-reviewed file and pass `--requirements-extra <path>`. The helper
copies its text after the local wheel requirement.

Application deployment is a separate, explicit command and is not run by
infrastructure provisioning:

```powershell
python eng\scripts\durable_loop_spike.py deploy `
  --acknowledge-existing-app func-durable-loop-0904
```

The acknowledgement must exactly match `--app-name`. Deployment performs a
read preflight and `config-zip` only. It does not retrieve or update app
settings and therefore never persists the APIM subscription key.

## Bounded qualification CLI

The final layer owns route and payload names. Every invocation therefore
requires an explicit route template, and template values are supplied
separately. Bodies come from standard input or an operator-controlled file;
the CLI validates bounded JSON but never prints it. Output contains HTTP
status, latency, response byte count, and only explicitly selected control
fields.

Store an inbound Function key only in the current process environment:

```powershell
$env:DURABLE_LOOP_FUNCTION_KEY = '<securely-obtained-function-key>'
```

Examples below intentionally use route placeholders. Replace each route and
JSON field path with the finalized application contract.

```powershell
# Start: request.json remains outside the repository and is not printed.
python eng\scripts\durable_loop_spike_qualification.py start `
  --route-template '/api/<start-route>' `
  --body-file $env:DURABLE_LOOP_START_REQUEST `
  --header-name x-functions-key `
  --header-secret-env DURABLE_LOOP_FUNCTION_KEY `
  --extract run_id=<response.run-id-json-path> `
  --extract session_id=<response.session-id-json-path>

# Status.
python eng\scripts\durable_loop_spike_qualification.py status `
  --route-template '/api/<status-route>/{run_id}' `
  --value "run_id=$env:DURABLE_LOOP_RUN_ID" `
  --header-name x-functions-key `
  --header-secret-env DURABLE_LOOP_FUNCTION_KEY `
  --extract status=<response.status-json-path> `
  --extract phase=<response.phase-json-path>

# Result. The result body is counted and discarded unless a safe control field is selected.
python eng\scripts\durable_loop_spike_qualification.py result `
  --route-template '/api/<result-route>/{run_id}' `
  --value "run_id=$env:DURABLE_LOOP_RUN_ID" `
  --header-name x-functions-key `
  --header-secret-env DURABLE_LOOP_FUNCTION_KEY

# Cancel.
python eng\scripts\durable_loop_spike_qualification.py cancel `
  --route-template '/api/<cancel-route>/{run_id}' `
  --value "run_id=$env:DURABLE_LOOP_RUN_ID" `
  --header-name x-functions-key `
  --header-secret-env DURABLE_LOOP_FUNCTION_KEY

# Human answer: answer.json remains outside the repository and is not printed.
python eng\scripts\durable_loop_spike_qualification.py human-answer `
  --route-template '/api/<human-answer-route>/{run_id}/{request_id}' `
  --value "run_id=$env:DURABLE_LOOP_RUN_ID" `
  --value "request_id=$env:DURABLE_LOOP_REQUEST_ID" `
  --body-file $env:DURABLE_LOOP_ANSWER_REQUEST `
  --header-name x-functions-key `
  --header-secret-env DURABLE_LOOP_FUNCTION_KEY
```

Each call is capped at 600 seconds and 1 MiB of response data. Start and human
answer bodies are capped at 256 KiB. Safe selectable output labels are limited
to run/session/request IDs, status/phase, and control URLs. The default emits no
response fields.

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
