# Durable Agent Loop — Segment Optimizer Spike Guide

This guide covers end-to-end execution of the **Segment Optimizer** spike (per-step **Microsoft-Decision-1** model routing, prefix-cache cost ledger, and forced **Dynamic Workflow DAG** execution) both locally against the **Durable Task Scheduler (DTS) Emulator** and in **Azure Functions**.

---

## 1. What the Segment Optimizer Does

When `AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_DECISION_ROUTING_ENABLED=true`:

1. **Durable routing activity (`durable_agent_route_model_v1`):**
   Before each routed model step, `durable_agent_turn_orchestrator_v4` schedules a separate routing activity. The activity projects deterministic bounded state using application-level character budgets: agent instructions up to 16,384 characters, the latest user goal up to 8,192, up to 12 recent user/assistant text messages (24,576 total and 4,096 per message), up to 64 tools with descriptions (16,384 description characters total and 1,024 per tool), bounded completed-tool names/status, cost/cache facts, and the configured `fast` and `frontier` model names. Text that fits is retained in full. Overflow retains both the beginning and tail around an explicit `...[truncated]...` marker, while state fields report truncation and omission counts. These character budgets bound the spike payload but are not claimed to be exact token counts or Microsoft-Decision-1 model limits. Common credential assignments and bearer tokens are redacted on a best-effort basis; this is not a security boundary. Tool-result bodies and reasoning content are excluded. The state is passed as data; the routing rubric remains in the Decision-1 questions rather than being mixed into conversation context. It then queries Microsoft-Decision-1 (`/providers/microsoft/v1/systemone`, or the built-in heuristic fallback if no endpoint is configured) with three questions:
   - `dag_viable` (`noul`): True only when 2 or more executable calls can be fully specified now as one fixed DAG, including static arguments or declared `${node_id.result.field}` dependencies. It is false for no executable task, one call, missing arguments, clarification, branching, recovery, or any plan that must inspect an intermediate result before choosing the next call.
   - `step_tier` (`choice`: `fast` | `frontier`): Chooses the minimum sufficient tier. Greetings, small talk, acknowledgements, simple clarification, direct answers, straightforward summaries, and one obvious tool call belong to `fast`; `frontier` is reserved for substantive next-action reasoning, consequential ambiguity, multi-constraint synthesis, or recovery.
   - `dag_tier` (`choice`: `fast` | `frontier`): Also chooses the minimum sufficient tier. A static fan-out or obvious fixed dependency chain may use `fast`; template dependencies alone do not imply `frontier`.
   The resulting content-free route is stored as a first-writer-wins receipt and passed unchanged to `durable_agent_model_step_v1`. The model activity applies that frozen route to the raw provider, so foreground streaming and model-activity retry do not route a second time.
2. **Prefix-Cache Cost Ledger (`PrefixCacheLedger`):**
   Tracks per-model token watermarks (1024-token minimum, 128-token buckets, 5-minute TTL). If the Frontier model already holds a warm prefix cache and fewer than 2 tools remain, the optimizer overrides a `fast` choice to `frontier` when a cold-cache switch to `fast` would cost more than staying on warm `frontier`.
   The bounded per-run watermark, last routed model, and last-DAG guard are
   committed in the immutable run document. Each routing or model activity
   restores that snapshot before using the ledger, so separate Functions
   workers observe the same optimizer state.
3. **Guaranteed DAG Execution (`compile_segment_dag`):**
   When `dag_viable >= 0.70` and at least 2 tools are eligible:
   - The model-visible tool catalog is locked to **only** `compile_segment_dag`.
   - The underlying tool schemas are injected into the system instructions addendum so the model knows exact parameter shapes.
   - `tool_choice` is forced to `{"type": "function", "name": "compile_segment_dag"}`.
4. **Orchestrator-Level DAG Wave Execution (`durable_agent_dag_wave_v1`):**
   The Durable Orchestrator (`_run_durable_loop`) detects `compile_segment_dag` and runs `_schedule_dag_waves`:
   - `durable_agent_dag_wave_v1` validates the `WorkflowPlan`, resolves `${node_id.result.field}` templates from prior waves, and emits `ToolDispatchRefV1` items for the current topological wave.
   - `_schedule_tool_refs` schedules the wave's tool activities in the orchestrator using `context.task_all` (parallelizing read-only tools and checkpointing every node individually in DTS).
   - After the final wave, non-leaf outputs are stored as `dag-node-result` blobs and a collapsed `ToolResultV1` is returned to the conversation.

When `AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_DECISION_ROUTING_ENABLED=false` (default), the runtime bypasses the optimizer completely and executes the standard single-model Durable Agent Loop.

Route sources are explicit:

- `live`: Decision-1 returned a valid HTTP 200 response.
- `heuristic`: the local evaluator was selected directly or used after a bounded fallback reason such as `timeout`, `http_non_200`, `invalid_response`, or `request_failed`.
- `short_circuit`: the cache/cost or fail-safe rules selected a route without calling Decision-1.

### Decision-1 documented limits

The public Microsoft documentation reviewed on 2026-10-20 does **not** publish
a numerical Decision-1 context-window, per-request token, request-body,
maximum-question, or maximum-option limit. The how-to states that the response
includes token usage but does not document how usage is divided between
`state` and `questions`. Its example sends three questions in one request, so
this spike preserves its existing single request with three routing questions.

The published Decision-1 quota is requests per minute, not a context limit:
Global Standard and Data Zone Standard list 60, 150, 225, 360, 500, and 900
RPM for subscription tiers 1 through 6. The general Foundry limit of 10 custom
headers is also unrelated to model context. Therefore, the payload caps above
are deliberately labeled application budgets rather than model limits; no
generic or third-party context-window figure is assumed.

Official sources:

- [Deploy and use Microsoft-Decision-1 in Microsoft Foundry](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/how-to/use-foundry-models-microsoft-decision)
- [Microsoft Foundry Models quotas and limits](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/quotas-limits)
- [Microsoft-Decision-1 catalog entry](https://ai.azure.com/catalog/models/microsoft-decision-1) (client-rendered; the public static response did not expose additional numerical limits during this review)

---

## 2. Configuration Reference

| Setting | Default | Description |
| --- | --- | --- |
| `AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_ENABLED` | `false` | Master gate for the experimental Durable Agent Loop. Must be `true`. |
| `AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_DECISION_ROUTING_ENABLED` | `false` | Enables the Segment Optimizer (`true` = hybrid routing + DAG; `false` = single-model baseline). |
| `AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_FAST_MODEL` | `gpt-4.1-mini` | Deployment name used when the optimizer selects the `fast` tier. |
| `AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_DECISION_MODEL` | `msft-decision-1` | Model deployment name sent to `/providers/microsoft/v1/systemone`. |
| `AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_DECISION_ENDPOINT` | *(unset)* | Optional full URL to `/providers/microsoft/v1/systemone`. When unset, the deterministic `HeuristicDecisionOneClient` is used. |
| `FOUNDRY_MODEL` / `AZURE_FUNCTIONS_AGENTS_APIM_MODEL` | `gpt-6-astra` | Frontier model deployment name used as baseline and when the `frontier` tier is selected. |

### Frozen pricing snapshot

The optimizer contains a static USD list-rate snapshot; it does not call a
pricing endpoint during inference. The verified entries match exact selected
model identities:

| Selected model | Runtime role | Input / 1M | Cached input / 1M | Output / 1M | Effective date |
| --- | --- | ---: | ---: | ---: | --- |
| `gpt-6-luna` | Recommended `fast` | $0.10 | $0.01 | $0.50 | 2026-09-01 |
| `gpt-6.1-sol` | Recommended `frontier` | $2.00 | $0.10 | $10.00 | 2026-10-01 |

Source: Azure Retail Prices API, USD, Foundry Models, product **Azure OpenAI
GPT6**, **Global Standard short-context** meters `ShortCo Inp Std Gl 1M
Tokens`, `ShortCo Cd Inp Std Gl 1M Tokens`, and `ShortCo Opt Std Gl 1M
Tokens`:

- [`gpt-6-luna` Retail API query](https://prices.azure.com/api/retail/prices?$filter=serviceName%20eq%20%27Foundry%20Models%27%20and%20contains(meterName,%276-luna%27)&currencyCode=%27USD%27)
- [`gpt-6.1-sol` Retail API query](https://prices.azure.com/api/retail/prices?$filter=serviceName%20eq%20%27Foundry%20Models%27%20and%20contains(meterName,%276.1-sol%27)&currencyCode=%27USD%27)

These are public list rates, not negotiated billing rates. `fast` and
`frontier` are runtime routing roles, not Azure billing qualifiers. The
verified prices apply only when `selected_model` is exactly `gpt-6-luna` or
`gpt-6.1-sol`; another deployment such as `gpt-6-astra` keeps the compatible
legacy tier assumption and is logged as `assumed_tier_default`. Decision-1
continues to use its separate spike assumption because no verified Decision-1
price was supplied.

Pre-step comparisons estimate prompt size from the bounded messages and tool
schemas and assume 250 output tokens. Recorded model-step cost uses the
provider-reported input/output totals and the existing per-model cache
watermark when provider cached-token usage is unavailable. These estimates are
not invoices.

To select the verified pair, set:

```text
AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_FAST_MODEL=gpt-6-luna
AZURE_FUNCTIONS_AGENTS_MODEL=gpt-6.1-sol
```

The frozen run-plan model is the frontier target and takes precedence over
provider fallback settings when the model client is constructed. Agent
frontmatter `model`, then global agent config `model`, then
`AZURE_FUNCTIONS_AGENTS_MODEL` determine that requested plan model before
provider-specific fallbacks such as `FOUNDRY_MODEL` or
`AZURE_FUNCTIONS_AGENTS_APIM_MODEL`. Therefore an existing
`gpt-6-astra` plan/config remains Astra-priced with the legacy assumption; it
does not receive Sol pricing merely because the router labels it `frontier`.

---

## 3. Local E2E Testing with the DTS Emulator

### Step 3.1 — Start Azurite and the DTS Emulator

The local sample uses Azurite for `AzureWebJobsStorage` and the official Durable Task Scheduler (DTS) emulator for orchestration state (`localhost:8080` gRPC endpoint, `localhost:8082` dashboard).

```bash
# 1. Start Azurite (if not already running)
docker run -d --name azurite \
  -p 10000:10000 -p 10001:10001 -p 10002:10002 \
  mcr.microsoft.com/azure-storage/azurite

# 2. Start the DTS Emulator
docker run -d --name dts-emulator \
  -p 8080:8080 -p 8082:8082 \
  mcr.microsoft.com/dts/dts-emulator:latest
```

Verify the DTS dashboard is reachable at `http://localhost:8082`.

### Step 3.2 — Configure `local.settings.json`

Copy `local.settings.template.json` to `local.settings.json` inside `samples/durable-agent-loop-spike/src/`:

```bash
cp samples/durable-agent-loop-spike/src/local.settings.template.json \
   samples/durable-agent-loop-spike/src/local.settings.json
```

Update `samples/durable-agent-loop-spike/src/local.settings.json` with these values enabled for local testing:

```json
{
  "IsEncrypted": false,
  "Values": {
    "FUNCTIONS_WORKER_RUNTIME": "python",
    "AzureWebJobsStorage": "UseDevelopmentStorage=true",
    "DURABLE_TASK_SCHEDULER_CONNECTION_STRING": "Endpoint=http://localhost:8080;TaskHub=default;Authentication=None",
    "TASKHUB_NAME": "default",
    "AZURE_FUNCTIONS_AGENTS_PROVIDER": "foundry",
    "FOUNDRY_PROJECT_ENDPOINT": "https://aidurableloop0904e2.services.ai.azure.com/api/projects/durable-agent-loop",
    "FOUNDRY_MODEL": "gpt-6.1-sol",
    "AZURE_FUNCTIONS_AGENTS_MODEL": "gpt-6.1-sol",
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_ENABLED": "true",
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_DECISION_ROUTING_ENABLED": "true",
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_FAST_MODEL": "gpt-6-luna",
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_DECISION_MODEL": "msft-decision-1"
  }
}
```

> **Note:** The currently documented sample infrastructure elsewhere in this
> repository still identifies `gpt-6-astra`; no Azure resource or local setting
> is changed by this guide update. Before using this verified pair, provision
> deployments named `gpt-6.1-sol` and `gpt-6-luna` in the selected Foundry/APIM
> target. To call a live Decision-1 endpoint instead of the local heuristic
> client, also set
> `AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_DECISION_ENDPOINT`
> (and `AZURE_FUNCTIONS_AGENTS_APIM_SUBSCRIPTION_KEY` if routing through APIM).
> For a Foundry project API imported into APIM, set the model base URL through
> its `/openai` segment; the spike adds the required
> `api-version=2025-03-01-preview` query to Responses requests. A subscription
> key is authorized by its APIM product scope, not merely by belonging to the
> same APIM service. If model and Decision requests return `401` with an
> `AzureApiManagementKey` challenge, associate both APIs with the key's product
> or use a subscription already scoped to those APIs. Do not send that APIM key
> directly to the Foundry backend.

### Step 3.3 — Install the Runtime and Start the Functions Host

From the repository root:

```bash
uv pip install -e ".[dev]"
cd samples/durable-agent-loop-spike/src
func start
```

### Step 3.4 — Trigger a Run Locally and Inspect DTS Waves

Once `func start` is listening on `http://localhost:7071`:

1. **Via the Hosted Durable Chat UI:**
   Open `http://localhost:7071/api/experimental/durable-chat/` in your browser and send a multi-tool prompt (for example, asking the agent to run a multi-step probe or workspace flow).
2. **Via PowerShell Client:**
   ```powershell
   $client = '.\samples\durable-agent-loop-spike\scripts\Invoke-DurableAgentLoop.ps1'
   $session = & $client -NewLogicalSession

   $run = & $client -StartRun `
     -BaseUrl 'http://localhost:7071' `
     -AllowInsecureLocalHttp `
     -SessionId $session.session_id `
     -Prompt 'Run the chain probe and summarize the observations.'

   $completed = & $client -WaitRun `
     -BaseUrl 'http://localhost:7071' `
     -AllowInsecureLocalHttp `
     -RunId $run.run_id `
     -PollIntervalSeconds 2
   $completed.result.response
   ```
3. **Verify in the DTS Emulator Dashboard (`http://localhost:8082`):**
   Open the orchestration instance for `run_id` in the `default` task hub. When a DAG segment is triggered, you will see:
   - `durable_agent_route_model_v1` before each routed model step
   - `durable_agent_model_step_v1` (forced `compile_segment_dag` tool call)
   - `durable_agent_dag_wave_v1` (`wave_index = 0`, validating the DAG and resolving templates)
   - Parallel/wave tool activities (`durable_agent_sandbox_tool_v1` / `durable_agent_mcp_tool_v1` / `durable_agent_tool_step_v1`) scheduled via `context.task_all`
   - Subsequent `durable_agent_dag_wave_v1` waves until `status = "completed"`
   - Final synthesis `durable_agent_model_step_v1` routed to `FAST_MODEL` (`gpt-6-luna` with the recommended verified configuration).

DTS proves the durable ordering and retry boundaries. The
`durable_agent_model_step_v1` result includes `requested_model` and
`selected_model`, plus `provider_reported_model` when the provider supplies
one. Detailed route cost and fallback fields remain in the routing worker log
or exported span.

### Step 3.5 — Make routing decisions visible

Keep general host logging quiet while enabling the dedicated routing category:

```json
{
  "logging": {
    "logLevel": {
      "default": "Warning",
      "AgentRuntime.Routing": "Information"
    }
  }
}
```

After changing `host.json` or app settings, restart the Functions host and start
a **new** run. Existing orchestration histories keep their recorded version and
do not gain the new routing activity.

- The local console proves which route the worker selected. Successful routes
  are `Information`; a live Decision-1 failure that falls back to the heuristic
  is `Warning`.
- With the `[monitor]` extra and
  `APPLICATIONINSIGHTS_CONNECTION_STRING`, Application Insights proves export
  of the `routing` activity span and its `af.durable_loop.routing.*`
  attributes. `APPLICATIONINSIGHTS_RESOURCE_ID` only enables UI linking; it
  does not configure telemetry export.
- Logs and spans use a hashed run correlation, never the raw run ID. They do not
  include prompts, state projections, arguments, outputs, headers, secrets, or
  raw exception text.

---

## 4. Azure Function App Deployment & Testing

### Step 4.1 — Assemble and Deploy the Package

The assembly script builds the local wheel from your worktree (including `durable_loop_optimizer.py` and the orchestrator wave changes) and packages `samples/durable-agent-loop-spike/src`:

```powershell
# 1. Select subscription
az account set --subscription <subscription-id-or-name>

# 2. Build wheel + deterministic deployment ZIP
uv run --with pip python eng\scripts\durable_loop_spike.py assemble

# 3. Deploy to the Flex Consumption Function App
uv run --with pip python eng\scripts\durable_loop_spike.py deploy `
  --acknowledge-existing-app func-durable-loop-0904
```

### Step 4.2 — Configure App Settings on Azure Function App

Enable the durable loop and the Segment Optimizer on `func-durable-loop-0904` (in resource group `larohra-durable-agent-loop`):

```powershell
az functionapp config appsettings set `
  --resource-group larohra-durable-agent-loop `
  --name func-durable-loop-0904 `
  --settings `
    AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_ENABLED=true `
    AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_DECISION_ROUTING_ENABLED=true `
    AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_FAST_MODEL=gpt-6-luna `
    AZURE_FUNCTIONS_AGENTS_MODEL=gpt-6.1-sol `
    AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_DECISION_MODEL=msft-decision-1
```

*(Optional)* If routing through APIM (`larohra-ai-gateway`), ensure your APIM
backend / Foundry project has the `gpt-6-luna` deployment provisioned alongside
`gpt-6.1-sol`. If you also have a `msft-decision-1` deployment exposed, add:

```powershell
az functionapp config appsettings set `
  --resource-group larohra-durable-agent-loop `
  --name func-durable-loop-0904 `
  --settings `
    AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_DECISION_ENDPOINT="https://aidurableloop0904e2.services.ai.azure.com/providers/microsoft/v1/systemone"
```

### Step 4.3 — Run A/B Comparison in Azure (Baseline vs. Segment Optimizer)

You can toggle between the single-model baseline and the Segment Optimizer without redeploying code:

1. **Run Scenario A — Frontier Baseline (`DECISION_ROUTING_ENABLED=false`):**
   ```powershell
   az functionapp config appsettings set `
     --resource-group larohra-durable-agent-loop `
     --name func-durable-loop-0904 `
     --settings AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_DECISION_ROUTING_ENABLED=false

   $client = '.\samples\durable-agent-loop-spike\scripts\Invoke-DurableAgentLoop.ps1'
   $baselineStart = & $client -StartRun `
     -Prompt 'Prepare the demo workspace and then read it back.' `
     -SandboxProfile retained_session `
     -FunctionAppName func-durable-loop-0904 `
     -ResourceGroup larohra-durable-agent-loop `
     -RetrieveFunctionKey

   $baselineDone = & $client -WaitRun `
     -RunId $baselineStart.run_id `
     -FunctionAppName func-durable-loop-0904 `
     -ResourceGroup larohra-durable-agent-loop `
     -RetrieveFunctionKey
   $baselineDone.status_snapshot.counters
   ```

2. **Run Scenario B — Segment Optimizer (`DECISION_ROUTING_ENABLED=true`):**
   ```powershell
   az functionapp config appsettings set `
     --resource-group larohra-durable-agent-loop `
     --name func-durable-loop-0904 `
     --settings AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_DECISION_ROUTING_ENABLED=true

   $optStart = & $client -StartRun `
     -Prompt 'Prepare the demo workspace and then read it back.' `
     -SandboxProfile retained_session `
     -FunctionAppName func-durable-loop-0904 `
     -ResourceGroup larohra-durable-agent-loop `
     -RetrieveFunctionKey

   $optDone = & $client -WaitRun `
     -RunId $optStart.run_id `
     -FunctionAppName func-durable-loop-0904 `
     -ResourceGroup larohra-durable-agent-loop `
     -RetrieveFunctionKey
   $optDone.status_snapshot.counters
   ```

3. **Compare Counters & DTS Execution History:**
   - Compare `model_steps`, `tool_calls`, `input_tokens`, `output_tokens`, and `cost_microunits` in `$baselineDone.status_snapshot.counters` vs `$optDone.status_snapshot.counters` (or via `eng/scripts/durable_loop_spike_qualification.py status`).
   - Open the Azure Portal **Durable Task Scheduler (`dts-durable-loop-0904` / task hub `durable-loop-demo`)** dashboard to inspect the `durable_agent_dag_wave_v1` wave activities and per-node tool executions.

---

## 5. Offline / Unit Verification

To verify the Segment Optimizer, forced `compile_segment_dag` tool choice, orchestrator wave progression, and the 5-tool Cloud Incident Triage benchmark without Azure resources:

```bash
.venv\Scripts\python.exe -m pytest tests\test_durable_loop_optimizer.py tests\test_durable_loop_durable_execution.py -q
```

Offline tests prove route freezing, receipt reuse, raw-provider consumption,
retry reuse, cancellation between routing and model scheduling, and the bounded
Decision-1 request shape. They do not prove DTS dashboard rendering, a live
Decision-1 classification improvement, Functions-host log classification, or
Application Insights export.

## 6. Spike limitations

- `PrefixCacheLedger` uses worker memory only as a transient view. Its bounded
  per-run routing state is restored from the immutable run document, but cache
  warmth and both model cost figures remain conservative estimates rather than
  provider-confirmed cache state or billing records.
- The route receipt freezes the selected decision for activity redelivery, but
  it does not turn the conservative watermark into a provider cache guarantee.
- Routing context truncation favors the newest user/assistant text and the
  catalog's deterministic tool order. It can omit older context or tools beyond
  the documented budgets, and credential redaction is best effort rather than
  a general secret scanner. Raw tool-result bodies and model reasoning are
  never included in the Decision-1 state.
- Luna and Sol model prices are the frozen verified Global Standard
  short-context public list-rate snapshot documented above. Unknown models and
  Decision-1 retain explicitly logged spike assumptions. Validate actual usage,
  negotiated rates, and charges from provider telemetry before drawing
  production conclusions.
- Model-step spans record `af.durable_loop.requested_model`,
  `af.durable_loop.selected_model`, and `gen_ai.response.model` when reported by
  the provider. The `AgentRuntime.Routing` information log emits the requested
  and selected names without prompts, tool arguments, headers, or credentials.
