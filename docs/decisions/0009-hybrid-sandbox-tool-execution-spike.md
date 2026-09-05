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
| 2026-09-02 | Completed-run cleanup | Treat exhausted post-run deletion as observable best effort: retry handle/provider seams, accept `NotFound`, count/log failure, and preserve already-completed nonstream or streaming output. |
| 2026-09-02 | Reaper ownership | Label and select with the canonical platform `AppIdentity` hash plus `owner_kind`; never select all hybrid sandboxes in a shared group. |
| 2026-09-02 | Admission failure | A broken customer tool module rejects the whole invocation. This differs from legacy partial discovery because the sandbox manifest is the executable admission boundary; bounded readiness diagnostics report the terminal exception class without log content. |
| 2026-09-02 | Package verification | Retain full archive readback for this measured spike. Its weighted package-upload cost was 5.51 s; replace the duplicate transfer with in-sandbox SHA-256 before productionizing. |
| 2026-09-03 | Deletion budget slicing | Give each bounded deletion attempt only a slice of the remaining budget instead of the whole window, so a hung `handle.delete` cannot starve the independent provider seam. Failed-acquire rollback keeps the 90 s total; completed-run cleanup is bounded separately so a successful response is never held for rollback-length cleanup. Auto-delete plus the bounded reaper remain the backstop. |
| 2026-09-03 | Deletion budget sizing | Size the completed-run budget from measured deletion latency instead of the drain window. The drain window covers in-flight tool calls, and deriving cleanup from it capped the total at 5 s and the first attempt at 1.67 s, below the 3 s transport LRO polling interval and below every recorded delete, so routine deletions were being cancelled. Use a 24 s total across three seam attempts (8 s each, minimum slice floor 4 s) which clears one poll cycle per seam and covers the recorded range (clean-window average 3.793 s, maximum 6.929 s; diagnostic p95 14.381 s, maximum 15.280 s; live reaper delete 8.475 s). Slices are an even division of the time actually remaining, so no attempt extends the deadline. |
| 2026-09-03 | Reaper label re-check | Re-verify exact `owner_kind` and `app_hash` on every returned `SandboxSummary` locally and skip mismatches, rather than trusting the server-side list filter as the only ownership gate in a shared Sandbox Group. |
| 2026-09-03 | Tool call accounting | Count `TOOL_CALLS` before the deadline rejection so every `TOOL_FAILURES` increment has a matching attempted call and the failure ratio stays interpretable. |
| 2026-09-03 | Terminal lifecycle handoff | Remove normal synchronous deletion from request latency. Protect active runs from auto-suspend; after admissions stop, calls drain, and executor shutdown is marked, apply Disk suspend at 300 idle seconds and delete at 600 stopped seconds. Fall back to bounded explicit deletion if handoff fails. Normal inventory and customer code/disk may remain for roughly 15 minutes. |
| 2026-09-03 | Pre-handoff crash coverage | Disabling active auto-suspend prevents lifecycle deletion until a sandbox is stopped. Before terminal handoff, the app-scoped reaper is therefore the primary worker-crash backstop; the active-policy auto-delete delay remains relevant only after an external stop. |
| 2026-09-03 | Package verification optimization | Remove the full `app.zip` readback and verify the existing package SHA-256 inside the executor before extraction/readiness. Keep full app-root capture by default; only an explicit private bundle contract may narrow content without guessing about imports, dependencies, or package data. |
| 2026-09-03 | Demo progress | Keep public SSE and non-hybrid behavior unchanged. Emit content-free `hybrid.progress` span events with bounded phase/status/duration attributes for an App Insights Workbook waterfall. |
| 2026-09-03 | Hosting scope | Exclude Functions always-ready configuration so later measurements isolate hybrid runtime optimizations. |
| 2026-09-03 | Private thin bundle | Add optional `AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_HYBRID_TOOL_BUNDLE_ROOT` as a validated app-root-relative directory. Absence keeps full capture; invalid values fail closed. The bundle must own tools, helpers, package data, and vendored dependencies and reuses deterministic capture safety. |
| 2026-09-03 | Capacity-safe terminal cleanup | Live read-only evidence confirmed `maxSandboxCount=25`, so lifecycle-only retention would cap sustained rate near 0.0278 req/s. After terminal 300/600 policy, request server-side delete but do not await LRO completion; initiation failure leaves lifecycle/reaper armed. Confirmed deletion remains mandatory when acquisition or policy setup fails. |
| 2026-09-03 | Active policy ordering | Create with a 3600-second suspend interval, then await `auto_suspend=None` plus the 600-second delete backstop before package delivery. A failed policy write fails acquisition and confirms deletion, proving no package/model/tool work begins under an unsafe suspend policy. |
| 2026-09-03 | Optimized live capacity gate | Make no sustained-load capacity claim. Treat all retained objects as consuming the hard group count; start every optimized batch at typed inventory zero, account for every sandbox within 25, and wait for eventual zero before more work. Verify stopped-object/timer semantics before deployment and record uncertainty. |
| 2026-09-03 | Sandbox Group capacity headroom | With user approval, raise `sbg-hybrid-tools-0902` `maxSandboxCount` from 25 to 100. Keep nonblocking deletion, 300/600 lifecycle, reaper, inventory-zero preflight, and eventual-zero verification; capacity headroom does not replace prompt cleanup. |
| 2026-09-05 | Delete-initiation fallback | Repeated live five-second invocation-handle stalls occurred before any DELETE status or poller, while the independent exact-ID provider seam later confirmed deletion in 244 ms. Keep handle initiation as the normal path; on pre-acceptance failure, immediately attempt one separately bounded provider delete. Count fallback use separately from overall cleanup failure, preserve completed output, and retain lifecycle/reaper backstops. |

Historical reports retain `sandbox_delete_request_failures` from pre-fallback
builds. Starting with this remediation, `sandbox_delete_fallbacks` records
pre-acceptance handle failures and `sandbox_delete_failures` records failure of
the overall confirmed deletion/reaper path.

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
| Sandbox Group | `sbg-hybrid-tools-0902` | West US 2 | `id-hybrid-sandbox-0902` | Provisioned; 1 CPU/2 GiB/20 GiB, max 100, timeout 1800; capacity update `Succeeded` |
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
| 2026-09-03 00:42 | Applied final independent-review remediations without Azure load. | Hardened completed-run cleanup, app-scoped reaping, production model fallback, tool failure counts, and streaming benchmark terminal validation. Accepted strict broken-module admission and measured archive readback as spike tradeoffs. | Focused tests cover every remediation; deployed benchmark numbers remain unchanged and resources were not modified. |
| 2026-09-03 04:55 | Applied three post-qualification re-review fixes without Azure load. | Sliced the bounded deletion budget per attempt so a hung handle seam still leaves budget for the provider seam, bounded completed-run cleanup by the drain window (capped at 5 s) instead of the 90 s rollback window, re-checked exact `owner_kind`/`app_hash` labels locally before every reaper delete, and counted `TOOL_CALLS` before deadline rejection. | Targeted pytest, ruff, and mypy passed locally. No benchmark, deployment, or Azure call was rerun, so the canonical live metrics and results JSON are unchanged. |
| 2026-09-03 05:20 | Resized the completed-run deletion budget from recorded latency without Azure load. | The drain-derived 5 s cap sliced the first attempt to 1.67 s, under the 3 s transport LRO polling interval and under every recorded delete, so routine deletions were cancelled. Completed-run cleanup now uses a 24 s total across three seam attempts (8 s first slice, 4 s minimum slice floor); failed-acquire rollback still uses 90 s, and slices divide only the time actually remaining. | Budget covers the recorded range (average 3.793 s, maximum 6.929 s; diagnostic p95 14.381 s, maximum 15.280 s; reaper delete 8.475 s). Targeted pytest, ruff, and mypy passed locally; no benchmark, deployment, or Azure call was rerun, so canonical metrics and the results JSON latency data are unchanged. |
| 2026-09-03 18:00 | Implemented the optimization amendment without Azure calls. | Normal cleanup now arms terminal lifecycle and initiates delete without polling completion; active suspend is disabled before packaging. Package upload no longer reads back `app.zip`; the executor verifies SHA-256 and publishes bounded failure metadata. Added the explicit bundle-root contract and content-free progress span events. | Focused lifecycle, package, executor, config, observability, and ACA adapter tests passed with ruff and mypy. Live behavior and savings remain unmeasured. |
| 2026-09-03 18:10 | Raised Sandbox Group capacity with user approval. | Updated `sbg-hybrid-tools-0902` through `Microsoft.App/sandboxGroups@2026-02-01-preview`; `maxSandboxCount` is 100, provisioning state is `Succeeded`, and `defaultTimeoutSeconds` remains 1800. | The higher cap is bounded benchmark headroom only. Optimized runs still require typed inventory zero before start and eventual zero afterward. |
| 2026-09-03 18:20 | Completed the optimization source gate. | Focused optimization coverage passed (199 tests, 2 skipped), independent code review found no significant issues, ruff passed, mypy checked 106 source files, and the sequential CI-equivalent suite completed with 2568 passed, 71 skipped, and 85 deselected. | Source is ready for review/deployment; no optimized Azure load has been run. |
| 2026-09-03 11:39 | Deployed and cold-qualified optimized commit `cb53d51`. | Rebuilt and Flex ZIP-deployed the exact commit with bundle root `sandbox_bundle`, no debug/worker OTel, and no always-ready configuration. Six functions indexed. Cold `customer_probe` returned HTTP 200 in 17,742.909 ms with exact call-result correlation, `alpha\|alpha\|alpha`, and `sandbox_marker=true`; digest and delete-request traces completed. | Pass. Typed inventory was zero before the request and at the first check 15.175 seconds after completion. |
| 2026-09-03 11:40 | Ran the sole optimized c1 batch. | Ten deterministic nonstream requests completed 10/10: p50 9,108.227 ms, p95/p99 18,339.288 ms, throughput 0.090227 req/s. | Pass. No duplicate batch was run; typed inventory was zero before c1 and at the first post-batch check. |
| 2026-09-03 11:43 | Ran the sole optimized c10 batch. | Ten deterministic concurrent requests completed 10/10: p50 18,468.408 ms, p95/p99 25,834.005 ms, throughput 0.386948 req/s. | Pass. Typed inventory was zero before c10 and at the first post-batch check. |
| 2026-09-03 11:44 | Joined the optimized clean telemetry window. | Exactly 21 requests/creates/uploads/verifications/handoffs/delete acceptances/tool calls and 42 model calls were emitted with no hybrid failure counters. Runtime request average was 10,649.358 ms; upload 135.085 ms, verify 0.157 ms, ready 333.192 ms, lifecycle handoff 49.914 ms. APIM model n=42 had zero errors. | The 9,843.642 ms average saving exceeded the expected 6.3-6.8 seconds. OTel percentiles are weighted bucket-average approximations; client and Function request percentiles are authoritative. |
| 2026-09-03 11:44-12:02 | Ran one isolated lifecycle-only 300/600 probe. | No explicit delete was requested. `Running` lasted through 309.543 seconds, `Stopped` was first observed at 340.441 seconds, and the object was absent at 1,071.150 seconds. | Auto-delete is not measured from policy application; it follows stop with additional reconciliation delay. No reaper was needed and final typed inventory was zero. |
| 2026-09-03 12:03 | Verified final optimized retained state. | Exact deployed tag is `cb53d513964cb4ab225144f12c8018c49b25cd84`; six functions remain indexed; model and MCP APIM APIs remain active and subscription-required; group capacity is 100/`Succeeded`; debug/worker OTel remain absent; typed inventory is zero. | Qualification complete. Retain resources and API-scoped body-free diagnostics until explicit teardown. |
| 2026-09-04 23:24-2026-09-05 00:01 | Attempted the authorized streaming cold/c1/c10 rerun and stopped at the inventory safety gate. | No redeploy was needed: PR head `a6f344d` changes only documentation/demo surfaces after deployed `cb53d51`. The cold request returned HTTP 200 with SSE `done` in 24,017.687 ms, but delete initiation failed and its sandbox remained `Running`. A shell chaining defect then admitted three c1 requests before termination; c10 was never started. All four requests returned 200 and all four lifecycle handoffs succeeded, but all four delete requests failed. | This is an aborted attempt, not a new qualification and not comparable with either complete nonstream run. Typed inventory peaked at four, dropped to two after the 23:50 reaper, and reached zero at 00:00:26 after the next reaper window. No manual cleanup or resource/configuration change occurred. The prior `cb53d51`/`26a78b9` qualification remains the latest complete measurement. |
| 2026-09-05 00:13-00:40 | Ran the required nonstream cold canary through an artifact-local fail-closed orchestrator. | Preflight verified exact deployed `cb53d51`, six functions, group 100/`Succeeded`, and typed inventory zero. The deterministic cold request returned HTTP 200 in 22,700.556 ms, but `SandboxClient.begin_delete` again timed out at the five-second bound while `aiohttp` awaited response headers. The orchestrator validated the report, observed inventory one, and stopped before c1; c10 was never started. | External blocker. Across both attempts, all five invocation-handle deletes timed out before an HTTP status or poller existed; the same code's 2026-09-03 accepted subset completed in 944-1,297 ms. Inventory stayed `Running` until the 00:40 scoped reaper issued a group-level DELETE that returned 200 in 244 ms; typed inventory was zero at 00:40:05. No manual cleanup, deployment, or resource/configuration change occurred. |
| 2026-09-05 | Implemented the approved exceptional cleanup fallback. | Regression coverage cancels a hung handle initiation, immediately exercises the exact-ID provider seam, preserves completed tool output, treats `NotFound` as success, bounds a hung provider, leaves terminal lifecycle/reaper armed on dual failure, and proves the normal accepted path never calls the provider. | Source validation completed before deployment. Live qualification remains pending and the 2026-09-03 complete run remains authoritative until then. |
| 2026-09-05 | Rejected a completed 21-request attempt after proving overlapping deployment. | Its c1 stage crossed an `fd6589e` to `bcdd366` deployment boundary and c10 ran after it; telemetry contained the bcdd-only fallback metric. | Preserve the artifact as diagnostic history only; publish none of its latency values. |
| 2026-09-05 01:22-01:27 | Redeployed exact `fd6589e`, restarted once, verified the released source and fd metric vocabulary, and enforced a 300.775-second quiet boundary. | Zero agent chat requests and zero fd/bcdd fallback metric rows appeared during the boundary. | Exact-source attribution established before load. |
| 2026-09-05 01:27-01:31 | Completed exactly one nonstream cold N=1, c1 N=10, c10 N=10 sequence. | 21/21 HTTP 200; zero client/runtime failures; typed inventory zero before and after every stage. | Authoritative latest measurement. |
| 2026-09-05 | Qualified the exceptional cleanup path. | Four handle delete initiations were accepted normally; 17 stalled at five seconds and exact-ID provider fallback confirmed all 17 in 592.645 ms average. | No overall cleanup failure, output replacement, operator deletion, or residual inventory. |

## Measurement record

The complete machine-readable report is
[`0009-hybrid-sandbox-tool-execution-results.json`](0009-hybrid-sandbox-tool-execution-results.json).
It contains no prompt, completion, tool payload, credential, key, or customer
content.

The original canonical baseline rows below describe deployed commit `77ef399`.
The historical optimized rows describe exact commit
`cb53d513964cb4ab225144f12c8018c49b25cd84` with the private
`sandbox_bundle` boundary, in-sandbox digest verification, terminal lifecycle
handoff, and nonblocking delete initiation. Both runs used the same deterministic
prompt. The recorded baseline `sandbox_delete` latencies remain the sizing basis
for confirmed deletion when acquisition or terminal-policy setup is not
trustworthy.

The authoritative latest rows describe exact commit
`fd6589eb0df88be39c8777caa807946fc513d8d2` after its bounded
exact-ID provider fallback. The released package was source-compared with that
commit after newline normalization, and a five-minute quiet boundary excluded
overlapping traffic or deployments before the 21-request run.

The timestamped `latest_rerun_attempt` object records the incomplete
2026-09-04 streaming attempt. It intentionally publishes no c1/c10 percentiles
or baseline delta: only four of the requested 21 calls completed before the
inventory safety stop, and every delete initiation failed. The complete
attempt is retained solely as operational failure and cleanup evidence.

The `latest_nonstream_rerun_attempt` object records the corrected apples-to-apples
canary. Its fail-closed gates prevented any c1 or c10 request after the ACA
delete endpoint again failed to return response headers within five seconds.
Because the cold sandbox retained capacity until the scoped reaper ran, a valid
21-request replacement qualification could not be completed without violating
the no-duplicate-stage or inventory-zero constraints. It motivated the
provider fallback that was subsequently qualified.

The `mixed_deployment_rerun_attempt` object records a later completed sequence
whose c1 stage crossed an overlapping deployment. Its c10 stage ran after that
boundary, and bcdd-only telemetry proved the source mix. It is retained for
diagnosis and contributes no current performance claim.

| Scenario | N | p50 | p95 | p99 | Throughput | Errors | Cleanup |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Functions cold + sandbox cold, final settings | 1 | 27,705.7 ms total | N/A | N/A | N/A | 0 | Complete; final inventory 0 |
| Concurrency 1, canonical | 10 | 19,720.69 ms | 32,037.24 ms | 32,037.24 ms | 0.047903 req/s | 0 | Complete; final inventory 0 |
| Concurrency 10, canonical | 10 | 27,559.73 ms | 32,555.02 ms | 32,555.02 ms | 0.307089 req/s | 0 | Complete; final inventory 0 |
| Functions cold + sandbox cold, optimized | 1 | 17,742.909 ms total | N/A | N/A | N/A | 0 | Complete; delete accepted, final inventory 0 |
| Concurrency 1, optimized | 10 | 9,108.227 ms | 18,339.288 ms | 18,339.288 ms | 0.090227 req/s | 0 | Complete; final inventory 0 |
| Concurrency 10, optimized | 10 | 18,468.408 ms | 25,834.005 ms | 25,834.005 ms | 0.386948 req/s | 0 | Complete; final inventory 0 |
| Functions cold + sandbox cold, latest fallback-qualified | 1 | 22,021.121 ms total | N/A | N/A | 0.045410 req/s | 0 | Complete; final inventory 0 |
| Concurrency 1, latest fallback-qualified | 10 | 12,862.314 ms | 17,524.462 ms | 17,524.462 ms | 0.077131 req/s | 0 | Complete; final inventory 0 |
| Concurrency 10, latest fallback-qualified | 10 | 22,490.808 ms | 25,900.302 ms | 25,900.302 ms | 0.385972 req/s | 0 | Complete; final inventory 0 |
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

The optimized clean window contained 48,262 model tokens. APIM model requests
(n=42, zero errors) had total/backend/gateway-overhead p50 of
1734.495/1733.237/1.324 ms, p95 of 6778.700/6777.334/1.443 ms, and p99 of
6990.510/6989.245/1.528 ms. Function request p50/p95/p99 was
14,190.905/15,436.523/22,230.984 ms. Compared with the baseline, cold improved
9,962.791 ms (35.96%), c1 p50 improved 10,612.463 ms (53.81%), and c10 p50
improved 9,091.322 ms (32.99%). The runtime-average request saving was
9,843.642 ms, so the expected 6.3-6.8-second saving materialized and was
exceeded by 3.044-3.544 seconds.

The 2026-09-05 latest clean window contained exactly 21 Function requests and
zero errors; request p50/p95/p99 was
14,052.128/20,320.389/20,893.798 ms. Runtime telemetry contains 21 requests,
21 sandbox creates, 21 lifecycle handoffs, 21 tool calls, 42 model calls, four
normal delete acceptances, 17 exact-ID provider fallbacks, 17 confirmed
provider deletions, and no hybrid failure metric. The provider fallbacks
averaged 592.645 ms (464.601-1,098.475 ms), after each affected request paid
the existing five-second handle bound.

Client-observed APIM dependencies contain 42 successful model posts, 44
successful MCP posts, and 22 expected MCP close probes returning 405. Model
dependency p50/p95/p99 was 1,461.384/2,006.946/2,633.163 ms; successful MCP
POST p50/p95/p99 was 75.704/171.044/187.080 ms. APIM
`AzureDiagnostics` contained no row for the clean window or preceding day, so
backend and gateway-only percentile decomposition is unavailable and is not
inferred from client spans. Retained APIM policies and diagnostics were not
changed.

Token telemetry reports 46,662 prompt, 39,936 prompt-cached, 1,505 completion,
and 48,167 total tokens across 42 model calls. Progress traces expose 15
sampled core lifecycle series and two sampled tool start/completion pairs;
aggregate metrics, not sampled progress traces, are authoritative.

Latest runtime-average latency was 13,218.470 ms, 7,274.530 ms (35.50%) below
the canonical baseline. Cold, c1 p50, and c10 p50 improved by
20.52%, 34.78%, and 18.39%, respectively. The expected 6.3-6.8-second saving
materialized and was exceeded by 0.475-0.975 seconds. The latest result was
2,569.112 ms (24.12%) slower than the historical optimized runtime average,
consistent with 17 of 21 requests paying the handle initiation bound before
successful fallback. Remaining differences are treated as natural
model/platform variance across measurement days.

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
- A customer module import error rejects the complete hybrid invocation rather
  than preserving tools from other modules. This is intentional: discovery
  occurs inside the isolation boundary and only one exact manifest is admitted.
- The baseline archive delivery read the full upload back and compared only its
  length, at a 5.51-second clean-window weighted average. Optimized commit
  `cb53d51` preserves that measured baseline but replaces the readback with
  in-sandbox SHA-256 verification before extraction/readiness. The optimized
  upload/verification averages were 135.085/0.157 ms.

## Cleanup

The operator sequence first disables the Function App, runs the bounded
spike-label reaper and verifies no spike-labeled sandboxes remain, then removes
only the isolated shared-APIM surfaces before deleting the spike resource group:

The temporary service-wide APIM diagnostic
`hybrid-sandbox-spike-temporary` was already removed after final measurement.
The API-scoped body-free diagnostics remain until the isolated APIs are deleted.

```powershell
az functionapp stop --resource-group larohra-test-adc-tools-hosted-skill --name func-hybrid-sbx-0902
uv run python eng\scripts\reap_hybrid_spike_sandboxes.py --sandbox-group-resource-id "/subscriptions/2ac40cf6-193e-4a44-a55b-d7a17bdd5aee/resourceGroups/larohra-test-adc-tools-hosted-skill/providers/Microsoft.App/sandboxGroups/sbg-hybrid-tools-0902" --region westus2 --app-hash a1-g3rs6sm34ab2go7wspx4n2x3s3gid7bn7waubpa7wsh65dqhwq7a --minimum-age-seconds 1 --confirm delete-hybrid-spike-sandboxes
az apim api delete --resource-group larohra-operations-agent-3p-rg --service-name larohra-ai-gateway --api-id hybrid-sandbox-spike-model --yes
az apim api delete --resource-group larohra-operations-agent-3p-rg --service-name larohra-ai-gateway --api-id hybrid-sandbox-spike-mcp --yes
az apim product delete --resource-group larohra-operations-agent-3p-rg --service-name larohra-ai-gateway --product-id hybrid-sandbox-spike --delete-subscriptions true --yes
az group delete --name larohra-test-adc-tools-hosted-skill --subscription 2ac40cf6-193e-4a44-a55b-d7a17bdd5aee --yes --no-wait
```

Before group deletion, verify the benchmark artifact and required aggregate
telemetry have been exported. After deletion, verify the resource group and
all spike-labeled sandboxes are absent. Do not execute cleanup until the user
requests teardown.
