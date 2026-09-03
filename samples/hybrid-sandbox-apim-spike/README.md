# Hybrid ACA Sandbox tool execution spike

This sample is the deployable qualification fixture for
[FRD 0009](../../docs/frds/0009-hybrid-sandbox-tool-execution-spike.md). It is
not a supported runtime mode. The model loop and remote MCP client stay in
Azure Functions; every local executable tool uses one fresh ACA Sandbox for
the top-level MAF invocation.

## Required private settings

| Setting | Purpose |
| --- | --- |
| `AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_HYBRID_TOOL_SANDBOX_GROUP_RESOURCE_ID` | Enables the private spike and identifies the customer Sandbox Group. |
| `AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_REGION` | Authored ACA Sandbox Group region (`westus2` for this spike). |
| `AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_HYBRID_ALLOWED_HOSTS` | Comma-separated sandbox egress allowlist. Model and MCP hosts do not belong here. |
| `AZURE_FUNCTIONS_AGENTS_APIM_MODEL_BASE_URL` | Azure OpenAI-compatible APIM base ending in `/openai/v1`. |
| `AZURE_FUNCTIONS_AGENTS_APIM_SUBSCRIPTION_KEY` | Spike-only APIM caller key sent as the API's custom `api-key` header. |
| `AZURE_FUNCTIONS_AGENTS_APIM_MODEL` | APIM-routed deployment name. |
| `AZURE_FUNCTIONS_AGENTS_APIM_MCP_URL` | Separate read-only remote MCP APIM endpoint. |

`tools/customer_probe.py` intentionally raises if imported outside ACA
Sandbox. A successful Function host startup therefore proves worker discovery
did not import customer code.

Do not set `PYTHON_ENABLE_OPENTELEMETRY` for this fixture. The runtime's
`[monitor]` dependency configures export from
`APPLICATIONINSIGHTS_CONNECTION_STRING`; enabling the Functions worker's
separate OTel hook as well can create an incompatible duplicate bootstrap.

## Qualification prompts

1. `Call customer_probe with message alpha and repeat 3. Return its JSON.`
2. `Call customer_probe twice sequentially. Confirm the process_id is the same.`
3. `Run two independent local calls in parallel and report both results.`
4. `Write workspace/evidence.txt, read it, then search for evidence.`
5. `Run a shell command that prints stdout, stderr, and exits 7.`
6. `Use shell Python urllib to fetch the allowed host, then try a blocked host.`
7. `Use the sandbox managed identity to read the granted AIServices ARM resource, then an ungranted resource.`
8. `Use Microsoft Learn MCP through APIM to find Azure Functions documentation.`

Also close an active SSE request during a long tool call and run a request with
a deliberately short timeout. After every scenario, verify no invocation
sandbox remains after bounded cleanup; create one labeled orphan for the timer
reaper scenario.

## Telemetry queries

```kusto
dependencies
| where timestamp > ago(2h)
| where cloud_RoleName == "func-hybrid-sbx-0902"
| project timestamp, operation_Id, name, duration, success, target
| order by timestamp asc
```

```kusto
customMetrics
| where timestamp > ago(2h)
| where name startswith "azure_functions_agents.hybrid."
| summarize count(), avg(value), percentiles(value, 50, 95, 99) by name
| order by name asc
```

```kusto
AzureDiagnostics
| where TimeGenerated > ago(2h)
| where ResourceProvider == "MICROSOFT.APIMANAGEMENT"
| where ApiId_s in ("hybrid-sandbox-spike-model", "hybrid-sandbox-spike-mcp")
| summarize Requests=count(), Errors=countif(ResponseCode_d >= 400),
    TotalP50=percentile(TotalTime_d, 50), TotalP95=percentile(TotalTime_d, 95),
    TotalP99=percentile(TotalTime_d, 99),
    BackendP50=percentile(BackendTime_d, 50),
    BackendP95=percentile(BackendTime_d, 95),
    BackendP99=percentile(BackendTime_d, 99)
    by ApiId_s
```

APIM diagnostics use W3C correlation and 100% sampling for this bounded spike,
with frontend/backend body capture set to zero bytes.

## Cleanup

Resources remain for result review. When teardown is requested, use the exact
commands in
[`docs/decisions/0009-hybrid-sandbox-tool-execution-spike.md`](../../docs/decisions/0009-hybrid-sandbox-tool-execution-spike.md).
