# Durable Agent Loop — Segment Optimizer Spike Guide

This guide covers end-to-end execution of the **Segment Optimizer** spike (per-step **Microsoft-Decision-1** model routing, prefix-cache cost ledger, and forced **Dynamic Workflow DAG** execution) both locally against the **Durable Task Scheduler (DTS) Emulator** and in **Azure Functions**.

---

## 1. What the Segment Optimizer Does

When `AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_DECISION_ROUTING_ENABLED=true`:

1. **Compact State Projection & Decision-1 Call (`durable_loop_optimizer.py`):**
   Before each `durable_agent_model_step_v1` activity, the optimizer projects a bounded state summary (`step_index`, `user_goal`, `completed_tools`, `last_tool_status`, `latest_tool_output_preview`, `available_tools`) and queries Microsoft-Decision-1 (`/providers/microsoft/v1/systemone`, or the built-in heuristic fallback if no endpoint is configured) with three questions:
   - `dag_viable` (`noul`): Can the next bounded segment execute as a multi-tool DAG with `${node_id.result.field}` templates?
   - `step_tier` (`choice`: `fast` | `frontier`): Which model tier should execute a single step?
   - `dag_tier` (`choice`: `fast` | `frontier`): Which model tier should author the `compile_segment_dag` plan?
2. **Prefix-Cache Cost Ledger (`PrefixCacheLedger`):**
   Tracks per-model token watermarks (1024-token minimum, 128-token buckets, 5-minute TTL). If the Frontier model already holds a warm prefix cache and fewer than 2 tools remain, the optimizer overrides a `fast` choice to `frontier` when a cold-cache switch to `fast` would cost more than staying on warm `frontier`.
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
    "FOUNDRY_MODEL": "gpt-6-astra",
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_ENABLED": "true",
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_DECISION_ROUTING_ENABLED": "true",
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_FAST_MODEL": "gpt-4.1-mini",
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_DECISION_MODEL": "msft-decision-1"
  }
}
```

> **Note:** Ensure your Foundry project (`aidurableloop0904e2`) has both your Frontier deployment (`gpt-6-astra`) and your Fast deployment (`gpt-4.1-mini`) available, and that you are signed in via `az login`. To call a live Decision-1 endpoint instead of the local heuristic client, also set `AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_DECISION_ENDPOINT` (and `AZURE_FUNCTIONS_AGENTS_APIM_SUBSCRIPTION_KEY` if routing through APIM).

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
   - `durable_agent_model_step_v1` (forced `compile_segment_dag` tool call)
   - `durable_agent_dag_wave_v1` (`wave_index = 0`, validating the DAG and resolving templates)
   - Parallel/wave tool activities (`durable_agent_sandbox_tool_v1` / `durable_agent_mcp_tool_v1` / `durable_agent_tool_step_v1`) scheduled via `context.task_all`
   - Subsequent `durable_agent_dag_wave_v1` waves until `status = "completed"`
   - Final synthesis `durable_agent_model_step_v1` routed to `FAST_MODEL` (`gpt-4.1-mini`).

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
    AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_FAST_MODEL=gpt-4.1-mini `
    AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_DECISION_MODEL=msft-decision-1
```

*(Optional)* If routing through APIM (`larohra-ai-gateway`), ensure your APIM backend / Foundry project `aidurableloop0904e2` has the `gpt-4.1-mini` deployment provisioned alongside `gpt-6-astra`. If you also have a `msft-decision-1` deployment exposed, add:

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
.venv/bin/pytest tests/test_durable_loop_optimizer.py -q
```
