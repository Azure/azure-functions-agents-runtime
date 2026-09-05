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
| `AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_HYBRID_TOOL_BUNDLE_ROOT` | Optional app-root-relative bundle directory. The sample uses `sandbox_bundle`; invalid paths fail closed. |
| `AZURE_FUNCTIONS_AGENTS_APIM_MODEL_BASE_URL` | Azure OpenAI-compatible APIM base ending in `/openai/v1`. |
| `AZURE_FUNCTIONS_AGENTS_APIM_SUBSCRIPTION_KEY` | Spike-only APIM caller key sent as the API's custom `api-key` header. |
| `AZURE_FUNCTIONS_AGENTS_APIM_MODEL` | APIM-routed deployment name. |
| `AZURE_FUNCTIONS_AGENTS_APIM_MCP_URL` | Separate read-only remote MCP APIM endpoint. |

`sandbox_bundle/tools/customer_probe.py` intentionally raises if imported outside ACA
Sandbox. A successful Function host startup therefore proves worker discovery
did not import customer code.

When the bundle setting is absent, the compatibility path captures the complete
Function app. When present, the bundle is the archive root and must contain
`tools/`, every helper/module/package and package-data file those tools need,
and any vendored `.python_packages/lib/site-packages` dependencies. The runtime
does not infer include globs. It uploads the deterministic archive once and the
sandbox verifies its SHA-256 before extraction or manifest publication.

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
a deliberately short timeout. Start every optimized scenario from typed
inventory zero, account for every request against the group's hard
`maxSandboxCount`, and wait for eventual zero before continuing. Normal close
arms the 300-second Disk suspend/600-second delete lifecycle backstop and asks
the service to delete immediately, but it does not await deletion completion.
Create one labeled orphan for the timer reaper scenario.

## Optimized live qualification

Exact commit `cb53d51` was deployed with `sandbox_bundle`, no Functions
always-ready configuration, and no debug or worker-level OTel setting. The
single completed cold/c1/c10 sequence on 2026-09-03 started each stage at typed
inventory zero:

| Scenario | N | p50 | p95 | Throughput | Errors |
| --- | ---: | ---: | ---: | ---: | ---: |
| Cold | 1 | 17,742.909 ms | N/A | N/A | 0 |
| c1 | 10 | 9,108.227 ms | 18,339.288 ms | 0.090227 req/s | 0 |
| c10 | 10 | 18,468.408 ms | 25,834.005 ms | 0.386948 req/s | 0 |

All 21 requests emitted create, package-upload, SHA-256 verification,
lifecycle-handoff, delete-request-accepted, and tool-call metrics with no
hybrid failure counter. The first post-run inventory checks were already zero.
Compared with the `77ef399` baseline, runtime-average request latency fell by
9,843.642 ms (48.03%), exceeding the expected 6.3-6.8-second saving.

This remains the latest **complete** qualification. An authorized streaming
rerun on 2026-09-04 was stopped after its cold request failed the inventory-zero
gate. A shell chaining defect admitted three of the planned c1 requests before
termination; c10 never started. All four HTTP requests completed successfully,
but all four server-side delete initiations failed. Typed inventory peaked at
four and returned to zero through the existing scoped reaper windows, without
operator cleanup. Because only four of 21 planned requests ran and the earlier
qualification used nonstream responses, the aborted attempt publishes no new
latency comparison. Its exact evidence is retained under
`latest_rerun_attempt` in the results JSON.

A corrected nonstream rerun then used a fail-closed orchestrator with explicit
native exit-code, report-shape, and typed-inventory checks. Its sole cold canary
returned HTTP 200 in 22,700.556 ms, but the invocation-handle delete request
again timed out before response headers. The orchestrator stopped before c1, so
c10 also never started. The sandbox remained `Running` until the scoped reaper
issued a group-level DELETE at 00:40 UTC; that request returned 200 in 244 ms
and typed inventory reached zero without operator cleanup. The service-side
delete-response stall is therefore a concrete external blocker, and the
2026-09-03 sequence remains the latest complete qualification.

The lifecycle backstop is not prompt cleanup. In an isolated probe with no
explicit delete, the sandbox was first observed `Stopped` 340.441 seconds after
policy application and absent at 1,071.150 seconds. The 600-second delete timer
does not run from policy application; evidence is consistent with a post-stop
timer plus platform reconciliation delay. Until absence, customer code and Disk
state remain retained and the object must be treated as consuming group
capacity. Nonblocking delete initiation remains the normal primary path; the
300/600 policy and app-scoped reaper are backstops.

The demo lifecycle is exposed as content-free `hybrid.progress` events on the
`hybrid.invocation` trace. Phase and status are fixed enums with optional
duration only; no prompt, tool arguments/results, manifest, credential, or
operation/session/sandbox identifier is attached. Render the events as an
Application Insights Workbook waterfall. The public chat SSE vocabulary and
non-hybrid behavior are unchanged.

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
| extend bucket_average = valueSum / valueCount
| summarize samples=sum(valueCount), average=sum(valueSum) / sum(valueCount),
    percentilesw(bucket_average, valueCount, 50, 95, 99),
    minimum=min(valueMin), maximum=max(valueMax) by name
| order by name asc
```

`sandbox_delete_requests_accepted` means the service accepted a delete request;
it does not assert LRO completion. `sandbox_deletes` is reserved for explicit
deletion whose completion was observed. Lifecycle handoff counts, duration, and
failures are reported separately.

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
