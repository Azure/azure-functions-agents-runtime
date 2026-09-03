# Hybrid ACA Sandbox tool execution spike record

This is the durable operational record for
[FRD 0009](../frds/0009-hybrid-sandbox-tool-execution-spike.md). It records
judgment calls, deviations, Azure resources, authentication choices, failed
experiments, measurements, and cleanup. It may contain resource names and
opaque IDs but never credentials, tokens, keys, prompts, completions, customer
tool inputs, or tool outputs.

## Scope constraints

- Subscription: `Private Test Sub LAROHRA`
  (`2ac40cf6-193e-4a44-a55b-d7a17bdd5aee`)
- Tenant: `72f988bf-86f1-41af-91ab-2d7cd011db47`
- Resource group: `larohra-test-adc-tools-hosted-skill`
- Base branch: `feature/aca-sandboxes`
- Spike branch: `larohra/hybrid-sandbox-apim-spike`
- Initial concurrency cap: 10; 25 requires stable 10-way evidence and quota.
- Resources remain after results are reported unless teardown is requested.

## Durable decisions

| Date | Area | Decision and rationale |
| --- | --- | --- |
| 2026-09-02 | Boundary | Keep MAF/model orchestration and remote MCP in Functions. Place customer tools and generic shell/file/search in one fresh customer-owned ACA Sandbox per top-level run. |
| 2026-09-02 | Reuse | Reuse ACA SDK transport, ports/models, deterministic packaging, egress types, fakes, lifecycle patterns, and benchmark quantiles. Do not reuse the full-run conversation-scoped execution backend. |
| 2026-09-02 | Discovery | Hybrid startup skips import-based `tools/*.py` discovery. The sandbox imports tools once, returns an exact manifest, and the worker creates executable fail-closed stubs. |
| 2026-09-02 | Protocol | Use no-ingress file journals plus one persistent executor started through the existing process port. Calls are strict, idempotent, deadline-bound, size-capped, and initially serialized. |
| 2026-09-02 | MAF | Use `FunctionMiddleware` to set `context.result` for trusted local stub objects; remote MCP calls `call_next`; unknown executable local tools fail closed. |
| 2026-09-02 | API | Gate the spike with `AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_HYBRID_TOOL_SANDBOX_GROUP_RESOURCE_ID`; do not add a public config model. |
| 2026-09-02 | Model | Use APIM's Azure OpenAI-compatible Responses path with unbuffered streaming. Keep only one minimal direct-model benchmark control. |
| 2026-09-02 | Auth | Prefer Functions MI to a custom APIM audience and APIM MI to Azure OpenAI. Use an APIM subscription key only if tenant policy blocks app registration; never use a model key in Functions or sandbox. |
| 2026-09-02 | Isolation | Keep Functions, APIM, and Sandbox Group identities separate. Give the Sandbox Group identity one narrow positive resource grant and prove denial on an ungranted resource. |
| 2026-09-02 | Cleanup | Request-owned bounded drain/delete is primary. ACA auto-delete and a bounded label reaper cover cancellation races and worker death. |
| 2026-09-02 | Telemetry | Put identifiers only on spans/logs; metrics use bounded dimensions. APIM and sandbox protocol propagate trace context without body logging. |
| 2026-09-02 | MCP provenance | Reject all worker-local executable tools during hybrid assembly. Middleware routes only exact sandbox stubs and calls next for lazily materialized framework/MCP functions. |
| 2026-09-02 | Base stack | Integrate the active ACA qualification chain through `larohra/aca-qualification-sweep` before product code so region, reliability, qualification, and leak-sweep fixes are reused. |
| 2026-09-02 | APIM reuse | Reuse shared BasicV2 `larohra-ai-gateway` only through new API/backend/product IDs prefixed `hybrid-sandbox-spike-`; never change its global policy or existing surfaces. |
| 2026-09-02 | Model fallback | Use existing `gpt-4.1-mini` via isolated APIM routing. New-account deployments failed with `SpecialFeatureOrQuotaIdRequired` despite visible quota. |

## Architecture review

An independent review completed on 2026-09-02. Its blocking finding was that
MAF materializes remote MCP `FunctionTool`s lazily, so their object identities
cannot be registered before `Agent` connection. The resolution is to prevent
all worker-local executable tools at hybrid discovery/assembly, route only
exact sandbox stub objects in middleware, and call next for all remaining
framework functions. This preserves fail-closed local execution without
blocking MCP.

The review also required explicit detached executor launch and file-plane
readiness, a minimal no-model/no-telemetry sandbox environment, a hybrid-only
APIM client factory, reaper age beyond the maximum live run bound, a distinct
schema-bearing tool manifest, and an outermost streaming lease. FRD 0009
incorporates every finding and is finalized.

## Resource inventory

The coordinator is selecting a quota-compatible region and deterministic names.
No resources have been provisioned by this child session yet.

| Component | Name / resource ID | Region | Identity | State |
| --- | --- | --- | --- | --- |
| Resource group | `larohra-test-adc-tools-hosted-skill` | West US 2 | N/A | Provisioned with spike tags |
| Storage | `sthybspk0902w2` | West US 2 | Functions access | Provisioned |
| Log Analytics | `log-hybrid-sbx-0902` | West US 2 | N/A | Provisioned |
| Application Insights | `appi-hybrid-sbx-0902` | West US 2 | N/A | Provisioned |
| Function App / plan | `func-hybrid-sbx-0902`, Flex Consumption Python 3.13, max 10 | West US 2 | `id-hybrid-func-0902` | Provisioned |
| Functions UAMI | `id-hybrid-func-0902` | West US 2 | Client `f6f0d9b7-ea3c-4490-9619-f5e305dfb236` | Provisioned; sandbox/storage roles pending confirmation |
| Sandbox Group | `sbg-hybrid-tools-0902` | West US 2 | `id-hybrid-sandbox-0902` | Provisioned; 1 CPU/2 GiB/20 GiB, max 25, timeout 1800 |
| Sandbox workload UAMI | `id-hybrid-sandbox-0902` | West US 2 | Client `ede205e9-fd0d-4ead-b1f6-5b9ba0856493` | Reader only on `aishybspk0902w2`; all other spike resources ungranted |
| APIM | `larohra-ai-gateway` in `larohra-operations-agent-3p-rg` | West US | System MI `90504d46-790c-47dd-bdda-66b4a64f5386` | Reused; BasicV2 capacity 1; empty global policy/diagnostics |
| APIM model API/product | API `hybrid-sandbox-spike-model`, product `hybrid-sandbox-spike`, subscription `hybrid-sandbox-spike-model` | West US | APIM system MI | Isolated API live; backend `larohra-openai-project-resource` |
| APIM MCP API/product | API `hybrid-sandbox-spike-mcp`, product `hybrid-sandbox-spike` | West US | APIM system MI | Read-only Microsoft Learn MCP route live |
| Attempted AIServices | `aishybspk0902w2` | West US 2 | APIM MI granted model user | Account exists; deployments entitlement-blocked |
| Azure OpenAI | `larohra-openai-project-resource` in `larohra-operations-agent-3p-rg` | West US | APIM `Cognitive Services OpenAI User` | Existing account selected after entitlement failure |
| Model deployment | `gpt-4.1-mini` version `2025-04-14`, capacity 250 | West US | N/A | Existing deployment selected |
| Positive MI test resource | Pending | Pending | Sandbox UAMI narrow role | Not provisioned |
| Negative MI test resource | Pending | Pending | No Sandbox UAMI role | Not provisioned |
| Read-only MCP backend | Pending | Pending | APIM-facing only | Not provisioned |

## Authentication and policy record

- Functions to APIM model API: preferred custom-audience Entra token; APIM
  subscription key is the only permitted fallback.
- APIM to Azure OpenAI: APIM managed identity.
- Functions to Sandbox Group control/data plane: Functions identity with only
  required Sandbox Group roles.
- Sandbox code to positive test resource: Sandbox Group workload UAMI.
- Sandbox code to negative test resource: same credential flow, no RBAC grant,
  expected authorization denial.
- APIM model policy: caller validation where applicable, high token limit,
  token metric, managed-identity backend authentication, unbuffered forward,
  no cache/body logging/retry.
- APIM MCP policy: separate API/product/endpoint, read-only backend operation,
  unbuffered forward, no body logging.

## Experiment log

| Timestamp (Pacific) | Experiment | Outcome | Follow-up |
| --- | --- | --- | --- |
| 2026-09-02 21:41 | Verified active Azure context. | Subscription, tenant, and user matched the authorized values. Required providers are registered; East US 2 and West US 2 advertise the relevant resource types. | Wait for coordinator's model-quota region and resource names before provisioning. |
| 2026-09-02 21:45 | Inspected stacked branch and local toolchain. | Branch tip is `6ebe067` from `origin/feature/aca-sandboxes`. `az` 2.89.1 and `uv` are available; bare `python`, `azd`, Core Tools, Node/npm, Azurite, Docker, and ACA CLI are absent. | Use `uv run` for local validation and Azure CLI/ARM for deployment; do not add missing tooling unless required. |
| 2026-09-02 21:48 | Inspected installed MAF versions and middleware contract. | The actual branch installs `agent-framework-core==1.3.0` and `agent-framework-openai==1.3.0`, not 1.13.0. `Agent` accepts middleware; `FunctionMiddleware.process` can set `context.result`; declaration-only `FunctionTool.invoke` fails before execution. | Implement against branch reality. Use executable fail-closed stubs and propagate middleware to `Agent`. |
| 2026-09-02 21:49 | Inspected ACA transport and packaging substrate. | Existing provider-neutral handle supplies six file operations, process exec, delete, no-port enforcement, and fail-closed default-Deny/Full-inspection egress. Deterministic packaging is Linux-only and process-cached. | Reuse these boundaries; live Functions Linux performs secure capture while Windows unit tests use fakes/prebuilt package values. |
| 2026-09-02 22:00 | Reviewed FRD independently against current MAF and ACA substrate. | Lazy MCP functions made the original unknown-function middleware rejection incorrect. Detached launch, minimal environment, dedicated tool manifest/client, reaper bound, and streaming lease also needed precision. | FRD corrected and finalized; worker-local rejection moved to assembly and exact sandbox stubs remain the only routed functions. |
| 2026-09-02 22:02 | Selected region, hosting, APIM, and initial model target. | West US 2 supports Flex Consumption Python 3.13 and Sandbox Groups. Shared BasicV2 APIM has empty global policy and no diagnostics, permitting isolated API-scoped reuse. | Use West US 2 workload resources and West US APIM; never modify global/existing APIM surfaces. |
| 2026-09-02 22:05 | Attempted new gpt-5.4-mini and gpt-4.1 deployments. | Both new AIServices and dedicated OpenAI provisioning paths returned `SpecialFeatureOrQuotaIdRequired` despite catalog and quota visibility. | Keep the failed AIServices account as evidence and route isolated APIM API to existing `gpt-4.1-mini` in `larohra-openai-project-resource`. |
| 2026-09-02 22:08 | Provisioned base workload resources and Sandbox Group. | Storage, LAW, App Insights, separate Function/sandbox UAMIs, AIServices account, and `sbg-hybrid-tools-0902` are provisioned. Initial Sandbox Group call returned `UnsupportedMediaType`; retry with explicit `Content-Type` succeeded. | Confirm narrow RBAC, create Function App/APIM APIs, then deploy. |
| 2026-09-02 22:12 | Proved APIM Responses traffic against the existing deployment. | POST to `https://larohra-ai-gateway.azure-api.net/larohra-openai-project-resource/openai/v1/responses` completed with model `gpt-4.1-mini`. The existing API uses subscription header `api-key`. | Functions reads `AZURE_FUNCTIONS_AGENTS_APIM_SUBSCRIPTION_KEY` and sends it only as `api-key`; APIM MI remains backend auth. |
| 2026-09-02 22:13 | Provisioned Python 3.13 Flex Function App. | `func-hybrid-sbx-0902` is bound to the dedicated Function UAMI and capped at 10 instances. | Deploy only after local implementation/gates and ACA stack integration. |
| 2026-09-02 22:15 | Ran a direct Sandbox Group data-plane baseline. | Public Ubuntu disk with Deny+Full egress reached Running in 3524.7 ms, process exec succeeded, verified deletion took 5085.3 ms, and no sandbox leaked. | Preserve as pre-code create/delete evidence and compare with packaged executor readiness. |
| 2026-09-02 22:18 | Completed the isolated APIM model API and diagnostics. | Responses nonstream completed in 1966.7 ms. Streaming first event was 794.1 ms, total 1730.8 ms, 19 events. API policy uses token limit 100000, token metric namespace `HybridSandboxSpike`, managed-identity backend, timeout 180, and unbuffered response. | Use base `https://larohra-ai-gateway.azure-api.net/hybrid-sandbox-spike-model/openai/v1`; keep body capture at zero and W3C 100% sampling for bounded spike runs. |
| 2026-09-02 22:20 | Narrowed Sandbox Group workload identity. | Removed RG-wide Reader and granted Reader only on `aishybspk0902w2`; every other spike resource is an ungranted negative control. Function UAMI has SandboxGroup Data Owner only at the new group and required storage data roles only at the spike storage account. | Run positive AIServices ARM GET and negative ARM GET against another resource from inside the sandbox. |
| 2026-09-02 22:22 | Completed the isolated APIM MCP lane. | MCP initialize protocol `2025-06-18` returned HTTP 200 `text/event-stream`, a session ID, and tools/resources capability in 820.4 ms. Policy rate-limits 120/min, forwards unbuffered, and logs W3C telemetry with zero body bytes. | Configure `AZURE_FUNCTIONS_AGENTS_APIM_MCP_URL=https://larohra-ai-gateway.azure-api.net/hybrid-sandbox-spike-mcp` and the same isolated subscription key. |
| 2026-09-02 22:25 | Proved Sandbox Group workload managed identity end to end. | The default Ubuntu disk exposed the identity endpoint/header. Raw endpoint token acquisition worked under Deny+Full with only `management.azure.com` allowed; no runtime package install was needed. Granted AIServices ARM GET returned 200 and ungranted storage ARM GET returned 403. | Vendor dependencies with the app package; use raw identity endpoint for this focused test and never install packages at runtime. |
| 2026-09-02 22:27 | Ran preliminary paired direct/APIM model control. | After one warmup, five direct calls had p50 1203.9 ms/p95 1298.5; isolated APIM calls had p50 1174.9 ms/p95 1241.3. The -28.9 ms p50 delta is noise-dominated, not evidence of negative overhead. | Mark preliminary; final report needs more paired samples plus APIM `TotalTime`/`BackendTime`. |

## Measurement record

Pending deployment. The final machine-readable report will be stored at the
sample's documented benchmark artifact path and summarized here without
request/tool content.

| Scenario | N | p50 | p95 | p99 | Throughput | Errors | Cleanup |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Functions cold + sandbox cold | Pending | Pending | Pending | Pending | Pending | Pending | Pending |
| Functions warm + fresh sandbox | Pending | Pending | Pending | Pending | Pending | Pending | Pending |
| Concurrency 1 | 1 | Pending | Pending | Pending | Pending | Pending | Pending |
| Concurrency 10 | 10 | Pending | Pending | Pending | Pending | Pending | Pending |
| Concurrency 25 | 25 | Conditional | Conditional | Conditional | Conditional | Conditional | Conditional |
| Direct Sandbox Group baseline | 1 | create 3524.7 ms | N/A | N/A | N/A | 0 | Complete; delete 5085.3 ms |
| Sandbox MI positive/negative | 1 | create 2318.9 ms | N/A | N/A | N/A | N/A | Complete; delete 6851.4 ms, no leak |
| Sandbox identity-env probe | 1 | create 2881.6 ms | N/A | N/A | N/A | N/A | Complete; delete 5772.5 ms |
| APIM nonstream baseline | 1 | 1966.7 ms | N/A | N/A | N/A | 0 | N/A |
| APIM stream baseline | 1 | first event 794.1 ms | N/A | N/A | total 1730.8 ms | 0 | N/A |
| APIM MCP initialize baseline | 1 | 820.4 ms | N/A | N/A | N/A | 0 | N/A |
| Direct-model control, preliminary paired n=5 | 1 | 1203.9 ms | 1298.5 ms | Not meaningful | N/A | 0 | N/A |
| APIM-model control, preliminary paired n=5 | 1 | 1174.9 ms | 1241.3 ms | Not meaningful | N/A | 0 | N/A |

Cold-start decomposition, token counts, APIM `TotalTime`, APIM `BackendTime`,
derived APIM overhead, MCP timing, tool queue/execution/transfer, sandbox
create/readiness/discovery/delete, and reaper evidence will be appended with
the final run.

## Failed experiments and deviations

- Bare `python` is unavailable on the local machine. The project environment
  was successfully created with `uv run` and Python 3.13.15.
- The requested MAF version `1.13.0` does not match the stacked branch or
  installed environment (`1.3.0`). No dependency upgrade is included in this
  spike because it would change the base feature independently of the hybrid
  experiment.
- The deterministic production package capture is intentionally Linux-only.
  Windows-local tests must exercise protocol/lifecycle behavior through typed
  fakes rather than weakening the secure capture implementation.
- The initial Sandbox Group ARM request failed with `UnsupportedMediaType`.
  Retrying the same request with explicit `Content-Type` succeeded.
- New gpt-5.4-mini and gpt-4.1 deployments were rejected with
  `SpecialFeatureOrQuotaIdRequired` even though catalog and quota APIs showed
  capacity. The spike uses an existing gpt-4.1-mini deployment through its own
  APIM API instead.

## Cleanup

Exact generated names will be substituted after provisioning. The final
operator sequence will first disable/stop the Function App, run the sandbox
reaper and verify no spike-labeled sandboxes remain, then delete the isolated
resource group:

```powershell
az functionapp stop --resource-group larohra-test-adc-tools-hosted-skill --name <function-app>
uv run python eng\scripts\reap_hybrid_spike_sandboxes.py --resource-group larohra-test-adc-tools-hosted-skill --sandbox-group <sandbox-group> --confirm
az apim api delete --resource-group larohra-operations-agent-3p-rg --service-name larohra-ai-gateway --api-id <hybrid-model-api-id> --yes
az apim api delete --resource-group larohra-operations-agent-3p-rg --service-name larohra-ai-gateway --api-id <hybrid-mcp-api-id> --yes
az apim product delete --resource-group larohra-operations-agent-3p-rg --service-name larohra-ai-gateway --product-id <hybrid-product-id> --delete-subscriptions true --yes
az group delete --name larohra-test-adc-tools-hosted-skill --subscription 2ac40cf6-193e-4a44-a55b-d7a17bdd5aee --yes --no-wait
```

Before group deletion, verify the benchmark artifact and required aggregate
telemetry have been exported. After deletion, verify the resource group and
all spike-labeled sandboxes are absent. Do not execute cleanup until the user
requests teardown.
