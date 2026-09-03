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
| 2026-09-02 | Generic shell | Use non-login `/bin/sh -c` so `run_shell` inherits the ACA disk environment. Login `/bin/sh -lc` reset `PATH` and hid the selected disk's `/opt/python/3/bin`; the absolute interpreter path proved the disk itself was healthy. |
| 2026-09-02 | Live authorization | Require positive and negative live controls for both identity and egress. The sandbox UAMI could read only its scoped AIServices resource, and sandbox HTTP could reach only the allowed host. |

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
| Functions UAMI | `id-hybrid-func-0902` | West US 2 | Client `f6f0d9b7-ea3c-4490-9619-f5e305dfb236` | Provisioned; Sandbox Group and storage access proven by deployed runs |
| Sandbox Group | `sbg-hybrid-tools-0902` | West US 2 | `id-hybrid-sandbox-0902` | Provisioned; 1 CPU/2 GiB/20 GiB, max 25, timeout 1800 |
| Sandbox workload UAMI | `id-hybrid-sandbox-0902` | West US 2 | Client `ede205e9-fd0d-4ead-b1f6-5b9ba0856493` | Reader only on `aishybspk0902w2`; all other spike resources ungranted |
| APIM | `larohra-ai-gateway` in `larohra-operations-agent-3p-rg` | West US | System MI `90504d46-790c-47dd-bdda-66b4a64f5386` | Reused; BasicV2 capacity 1; empty global policy/diagnostics |
| APIM model API/product | API `hybrid-sandbox-spike-model`, product `hybrid-sandbox-spike`, subscription `hybrid-sandbox-spike-model` | West US | APIM system MI | Isolated API live; backend `larohra-openai-project-resource` |
| APIM MCP API/product | API `hybrid-sandbox-spike-mcp`, product `hybrid-sandbox-spike` | West US | APIM system MI | Read-only Microsoft Learn MCP route live |
| Attempted AIServices | `aishybspk0902w2` | West US 2 | APIM MI granted model user | Account exists; deployments entitlement-blocked |
| Azure OpenAI | `larohra-openai-project-resource` in `larohra-operations-agent-3p-rg` | West US | APIM `Cognitive Services OpenAI User` | Existing account selected after entitlement failure |
| Model deployment | `gpt-4.1-mini` version `2025-04-14`, capacity 250 | West US | N/A | Existing deployment selected |
| Positive MI test resource | `aishybspk0902w2` AIServices account | West US 2 | Sandbox UAMI Reader on this resource only | ARM GET returned 200 from sandbox |
| Negative MI test resource | `sthybspk0902w2` storage account | West US 2 | No Sandbox UAMI grant | ARM GET returned 403 from sandbox |
| Read-only MCP backend | Microsoft Learn MCP (`https://learn.microsoft.com/api/mcp`) | Global | APIM-facing only | Discovery returned three documentation tools |

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
| 2026-09-02 22:31 | Probed the selected public `python-3.13` sandbox disk. | Running in 2989.5 ms; Python 3.13.15 at `/opt/python/3/bin/python`, Linux x86_64/glibc 2.36; verified deletion in 5895.9 ms. | Use this disk for the executor and compare full package/readiness latency. |
| 2026-09-02 22:34 | Reproduced MAF MCP discovery using the branch dependency shape. | MAF core 1.3.0 plus MCP 1.29.1 connected through APIM in 3260.7 ms and returned three Learn tools when both authenticated `http_client` and `header_provider` were supplied. Header-provider-only initialization returned 401. | Preserve the repository discovery wiring; do not downgrade MCP based on the earlier unconstrained environment. |
| 2026-09-02 22:36 | Generated correlated APIM model and MCP traffic. | Operation `4b0136a316734401b40ecb411296231d` completed both lanes. Temporary service diagnostic exports only GatewayLogs and AllMetrics; LLM/MCP content logs remain disabled. | Use this operation only to validate body-blind gateway correlation and the later TotalTime/BackendTime join. |
| 2026-09-02 22:41 | Assembled the exact Flex remote-build input from commit `fa101f3`. | Existing qualification assembly produced the local runtime wheel plus pinned Python 3.13 dependency closure; wheel inspection confirmed the hybrid controller and executor modules are present. | Deploy the staged fixture, then run bounded qualification. |
| 2026-09-02 22:47 | Deployed the first Function package and invoked cold/warm chat. | Deployment and six-function indexing succeeded, but both requests failed before user code: the Functions Python worker's OTel hook received a null propagator while `PYTHON_ENABLE_OPENTELEMETRY` was set. No sandbox was created or leaked. | Remove that non-contract setting; retain `APPLICATIONINSIGHTS_CONNECTION_STRING` so the runtime's monitor bootstrap owns worker telemetry. |
| 2026-09-02 22:52 | Retried after removing duplicate worker OTel bootstrap. | The Function reached the hybrid lease and cleaned up without leaks, but executor readiness timed out. An exact live launch succeeded with the fixture; the remote-built dependency archive exceeded the executor's inconsistent 4096-member cap while controller packaging permits 65,535 standard ZIP entries. | Align the standalone executor with the controller's non-ZIP64 member bound, add a >4096-entry regression, and emit content-blind process/log diagnostics on readiness timeout. |
| 2026-09-02 23:02 | Retried with aligned archive bounds and startup diagnostics. | Executor exited with a bounded `RuntimeError` diagnostic: the runtime supplied canonical `AZURE_FUNCTIONS_AGENTS_SANDBOX=1`, but the sample guard checked a stale marker name. Failed-acquire deletion also transiently failed once, leaving one labeled sandbox until manual deletion. | Correct the sample guard, regression-check it against the runtime constant, and give failed-acquire cleanup a bounded provider-level delete fallback after handle deletion fails. |
| 2026-09-02 23:12 | Deployed `e2dd995` and ran the primary custom-tool scenario. | HTTP 200 in 20,947.4 ms. MAF emitted exact call ID `call_IzBI4sQV4aTpirDh7EQuaO9k`; sandbox-discovered `customer_probe` returned `hello` repeated three times with sandbox marker true and process ID 16. The strict stdout/stderr/exit-code envelope survived the round trip. | Primary Functions MAF -> APIM model -> inert stub -> ACA journal/executor -> APIM model path is proven. Inventory returned zero after success. |
| 2026-09-02 23:18 | Ran remote Microsoft Learn MCP through the deployed Function and isolated APIM API. | HTTP 200 in 28,020.5 ms with exactly one `microsoft_docs_search` call and the expected official Functions Python guide. MCP called next in the worker while the eager invocation sandbox remained local-tool-only. | Pass. Inventory was transiently visible immediately after HTTP completion and absent 10 seconds later, consistent with list/delete convergence rather than a persistent leak. |
| 2026-09-02 23:21 | Ran sequential customer and generic local tools in one deployed invocation. | HTTP 200 in 18,399.4 ms. Two customer calls returned the same executor process ID 15; write/read/search preserved `hybrid-evidence-0902`; shell preserved stdout `shell-out`, stderr `shell-err`, and exit 7. | One shared sandbox/executor plus generic file/search/shell protocol passed. Inventory converged to zero within 25 seconds after the response. |
| 2026-09-02 23:27 | Exercised the SandboxGroup UAMI through the deployed agent path. | HTTP 200 in 22,459.9 ms with one `run_shell` call. Token acquisition remained internal; scoped AIServices ARM GET returned 200, ungranted storage ARM GET returned 403, and stderr was empty. | Identity boundary passed. The login shell hid the disk Python alias, so this run used `/opt/python/3/bin/python3`; commit `77ef399` switches `run_shell` to a non-login shell. |
| 2026-09-02 23:31 | Exercised allowed and disallowed sandbox egress through the deployed agent path. | HTTP 200 in 30,841.3 ms. `www.example.com` returned 200 and disallowed `example.org` returned 403. The model emitted two shell calls despite an exactly-once instruction. | Egress policy passed, but this request is not single-call protocol evidence. Direct ARM collection probes failed with stale-version and 404 responses; the typed ACA data-plane provider confirmed inventory zero. |
| 2026-09-02 23:35 | Exercised normal streaming and client disconnect during local execution. | Normal stream returned 200, first event in 10,072.2 ms, and completed in 20,104.0 ms. A second client observed `tool_start`, disconnected at 21,337.8 ms while a 30-second shell command was active, and inventory was zero after 45 seconds. | Streaming response and disconnect cleanup passed. |
| 2026-09-02 23:42 | Deployed and verified the non-login shell correction from `77ef399`. | HTTP 200 in 30,499.8 ms with exactly one shell call. Bare `python3` resolved to `/opt/python/3/bin/python3`, reported Python 3.13.15, and exited 0 with empty stderr. | Generic shell now inherits the selected disk environment. |
| 2026-09-02 23:48 | Ran a bounded c1 diagnostic benchmark on `77ef399`. | Ten sequential requests all returned 200. Total p50 was 19,155.2 ms, p95/p99 22,523.9 ms, workload 187.462 seconds, and throughput 0.05334 requests/second. | Retained as diagnostic only: temporary debug logging and the 180-second readiness override were still active. |
| 2026-09-02 23:49 | Completed an unintentionally overlapping second bounded c1 batch. | A coordinator message asking this child not to duplicate load arrived after the batch had started. All ten returned 200; p50 was 19,233.3 ms, p95/p99 28,450.5 ms, workload 201.737 seconds, and throughput 0.04957 requests/second. | Retain as transparent supplemental evidence. This child stopped load; the coordinator owns the sole c10 batch. |
| 2026-09-02 23:55 | Ran a bounded c10 diagnostic benchmark after c1. | Ten concurrent requests all returned 200. Total p50 was 31,084.25 ms, p95/p99 32,904.44 ms, workload 32.9133 seconds, and throughput 0.30383 requests/second. | Retained as diagnostic only because temporary debug/readiness settings were active. |
| 2026-09-02 23:58 | Exercised concurrent calls sharing one invocation sandbox. | One model turn emitted two `run_shell` calls; both two-second commands completed with exit 0 in a 31,103.0 ms HTTP request. The clean metric window recorded tool queue p50/p95/p99 of 42/120/2121 ms. | Same-sandbox calls serialize safely and expose queue wait. |
| 2026-09-02 23:59 | Exercised a bounded sandbox tool timeout. | A five-second command with a one-second limit failed closed as `Function failed`; HTTP orchestration completed in 30,326.7 ms and inventory converged to zero. | Tool timeout and cleanup passed without exposing sandbox diagnostics to the model. |
| 2026-09-03 00:01 | Exercised live orphan recovery with no concurrent Function load. | Starting from inventory zero, created exactly one spike-labeled orphan in 6306.0 ms. The reaper observed and deleted one in 8475.0 ms; typed final inventory was zero. | Worker-crash fallback passed. |
| 2026-09-03 00:03 | Joined the diagnostic runtime and APIM measurement window. | Runtime emitted 32 requests, 32 creates, 32 deletes, 64 model calls, 33 tool calls, complete stage histograms, and no failure count. APIM model n=64 had total/backend/gateway p50 1804/1803/2 ms and p95 3884/3882/2 ms. | Retained as diagnostic only because temporary debug/readiness settings were active. |
| 2026-09-03 00:10 | Restored final runtime settings and reran cold, c1, and c10. | Removed temporary Python/JobHost debug settings, restored readiness to 45 seconds, and restarted. Cold returned 200 in 27,705.7 ms. Final c1 and c10 were both 10/10 with p50 19,720.69 and 27,559.73 ms. | Canonical benchmark pair: c10 delivered 6.41x throughput with 39.7% higher p50 latency. |
| 2026-09-03 00:16 | Joined final debug-off runtime and APIM telemetry. | Function requests were 21/21 successful; runtime emitted 21 creates, deletes, and tool calls plus 42 model calls. APIM model n=42 had total/backend/gateway p50 1746/1745/2 ms and zero errors. Inventory was zero after 60 seconds. | Final telemetry passed. OTel percentile estimates are histogram-bucket approximations; client and Function percentiles are authoritative. |
| 2026-09-03 00:18 | Re-ran the canonical local gate on final source. | Ruff passed, mypy checked 106 files, and pytest completed with 2513 passed and 70 skipped. | Local Definition of Done gate passed. |
| 2026-09-03 00:20 | Verified the final deployed state. | Temporary debug and worker OTel settings were absent; six expected Functions were indexed; isolated APIM model and MCP APIs remained active and subscription-protected; typed Sandbox Group inventory was zero. | Spike qualification complete; retain resources until teardown is explicitly requested. |
| 2026-09-03 00:22 | Removed the temporary service-wide APIM diagnostic after final capture. | Historical GatewayLogs remain in `log-hybrid-sbx-0902`. API-scoped model/MCP Application Insights diagnostics remain active with request and response body capture disabled. | Shared APIM no longer exports unrelated service-wide traffic for this spike. |

## Measurement record

The complete machine-readable report is
[`0009-hybrid-sandbox-tool-execution-results.json`](0009-hybrid-sandbox-tool-execution-results.json).
It contains no prompt, completion, tool payload, credential, key, or customer
content.

| Scenario | N | p50 | p95 | p99 | Throughput | Errors | Cleanup |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Functions cold + sandbox cold, final settings | 1 | 27,705.7 ms total | N/A | N/A | N/A | 0 | Complete; final inventory 0 |
| Concurrency 1, canonical | 10 | 19,720.69 ms | 32,037.24 ms | 32,037.24 ms | 0.047903 req/s | 0 | Complete; final inventory 0 |
| Concurrency 10, canonical | 10 | 27,559.73 ms | 32,555.02 ms | 32,555.02 ms | 0.307089 req/s | 0 | Complete; final inventory 0 |
| Concurrency 1, diagnostic duplicate | 10 | 19,233.3 ms | 28,450.5 ms | 28,450.5 ms | 0.04957 req/s | 0 | Complete |
| Concurrency 25 | 25 | Not run | Not run | Not run | Not run | N/A | Capped at 10 after sufficient stable evidence |
| Direct Sandbox Group baseline | 1 | create 3524.7 ms | N/A | N/A | N/A | 0 | Complete; delete 5085.3 ms |
| Sandbox MI positive/negative | 1 | create 2318.9 ms | N/A | N/A | N/A | N/A | Complete; delete 6851.4 ms, no leak |
| Sandbox identity-env probe | 1 | create 2881.6 ms | N/A | N/A | N/A | N/A | Complete; delete 5772.5 ms |
| Python 3.13 disk probe | 1 | create 2989.5 ms | N/A | N/A | N/A | 0 | Complete; delete 5895.9 ms |
| Function-path sandbox MI positive/negative | 1 | 22,459.9 ms total | N/A | N/A | N/A | 0 | Complete; granted 200, ungranted 403 |
| Function-path egress allowed/blocked | 1 | 30,841.3 ms total | N/A | N/A | N/A | 0 | Complete; allowed 200, blocked 403, inventory 0 |
| Normal streaming | 1 | first event 10,072.2 ms | N/A | N/A | total 20,104.0 ms | 0 | Complete |
| Disconnect during sandbox call | 1 | disconnected 21,337.8 ms after `tool_start` | N/A | N/A | N/A | 0 | Complete; inventory 0 after 45 seconds |
| Parallel same-sandbox local calls | 2 | tool queue 42 ms | 120 ms | 2121 ms | N/A | 0 | Complete; both calls exit 0 |
| Bounded shell timeout | 1 | 30,326.7 ms total | N/A | N/A | N/A | 0 | Complete; failed closed and inventory 0 |
| Live orphan reaper | 1 | create 6306.0 ms | N/A | N/A | reaper 8475.0 ms | 0 | Complete; deleted 1, inventory 0 |
| APIM nonstream baseline | 1 | 1966.7 ms | N/A | N/A | N/A | 0 | N/A |
| APIM stream baseline | 1 | first event 794.1 ms | N/A | N/A | total 1730.8 ms | 0 | N/A |
| APIM MCP initialize baseline | 1 | 820.4 ms | N/A | N/A | N/A | 0 | N/A |
| MAF 1.3.0 + MCP 1.29.1 through APIM | 1 | connect 3260.7 ms | N/A | N/A | N/A | 0 | Session close returned nonfatal 404 |
| Function MAF -> MCP through APIM | 1 | 28,020.5 ms total | N/A | N/A | N/A | 0 | Complete; inventory 0 after 10-second convergence |
| Sequential local + shell/file/search | 1 | 18,399.4 ms total | N/A | N/A | N/A | 0 | Complete; same PID and inventory 0 after convergence |
| Direct-model control, preliminary paired n=5 | 1 | 1203.9 ms | 1298.5 ms | Not meaningful | N/A | 0 | N/A |
| APIM-model control, preliminary paired n=5 | 1 | 1174.9 ms | 1241.3 ms | Not meaningful | N/A | 0 | N/A |

The final clean window contained 48,391 model tokens. APIM model requests
(n=42, zero errors) had total/backend/gateway-overhead p50 of 1746/1745/2 ms,
p95 of 2288/2286/3 ms, and p99 of 5848/5846/3 ms. Function request p50/p95/p99
was 22,982.3/28,607.9/28,972.6 ms. Complete runtime stage histograms, MCP
diagnostics, token totals, and scenario records are in the JSON report.

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
- The initial separate MCP environment suggested MCP 1.24.0 was necessary.
  Reproduction against this branch showed the actual requirement was to pass
  both the authenticated HTTP client and header provider; MCP 1.29.1 then
  discovered all three Learn tools. Learn MCP still returns a nonfatal 404 when
  MAF attempts session termination after successful discovery.
- The first deployed requests failed before entering the Function because the
  worker-level `PYTHON_ENABLE_OPENTELEMETRY` setting produced a null trace
  propagator with the deployed app's OTel closure. The sample never required
  that setting; removing it avoids duplicate worker/runtime bootstrap while
  retaining runtime export through `APPLICATIONINSIGHTS_CONNECTION_STRING`.
- Login-shell execution reset the ACA disk `PATH`, so bare `python3` was not
  initially visible even though `/opt/python/3/bin/python3` worked. Commit
  `77ef399` uses a non-login shell and the deployed regression passed.
- Direct ARM Sandbox Group inventory probes returned stale-version and 404
  errors. All final cleanup evidence uses the typed ACA data-plane provider.

## Cleanup

The operator sequence first disables the Function App, runs the bounded
spike-label reaper and verifies no spike-labeled sandboxes remain, then removes
only the isolated shared-APIM surfaces before deleting the spike resource group:

The temporary service-wide APIM diagnostic
`hybrid-sandbox-spike-temporary` was already removed after final measurement.
The API-scoped body-free diagnostics remain until the isolated APIs are deleted.

```powershell
az functionapp stop --resource-group larohra-test-adc-tools-hosted-skill --name func-hybrid-sbx-0902
uv run python eng\scripts\reap_hybrid_spike_sandboxes.py --sandbox-group-resource-id "/subscriptions/2ac40cf6-193e-4a44-a55b-d7a17bdd5aee/resourceGroups/larohra-test-adc-tools-hosted-skill/providers/Microsoft.App/sandboxGroups/sbg-hybrid-tools-0902" --region westus2 --minimum-age-seconds 1 --confirm delete-hybrid-spike-sandboxes
az apim api delete --resource-group larohra-operations-agent-3p-rg --service-name larohra-ai-gateway --api-id hybrid-sandbox-spike-model --yes
az apim api delete --resource-group larohra-operations-agent-3p-rg --service-name larohra-ai-gateway --api-id hybrid-sandbox-spike-mcp --yes
az apim product delete --resource-group larohra-operations-agent-3p-rg --service-name larohra-ai-gateway --product-id hybrid-sandbox-spike --delete-subscriptions true --yes
az group delete --name larohra-test-adc-tools-hosted-skill --subscription 2ac40cf6-193e-4a44-a55b-d7a17bdd5aee --yes --no-wait
```

Before group deletion, verify the benchmark artifact and required aggregate
telemetry have been exported. After deletion, verify the resource group and
all spike-labeled sandboxes are absent. Do not execute cleanup until the user
requests teardown.
