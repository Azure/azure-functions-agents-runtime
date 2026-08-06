---
frd: 0008
title: Internal per-agent token usage logging
status: Finalized        # Draft -> In review -> Finalized  (-> Implemented after merge)
author: victoriahall
created: 2026-08-05
updated: 2026-08-06
issues: []
pull_requests: []
branch: hallvictoria/token-usage
---

# FRD 0008 - Internal per-agent token usage logging

## 1. Summary

The runtime will write one structured internal token-usage record through its existing
`azure.functions.AgentRuntime` logger for every Microsoft Agent Framework (MAF) `Agent.run()`
invocation attempt, including top-level agents, chat-time delegated specialists, and Dynamic
Workflow Sub Agents. The runtime will consume MAF's public `AgentResponse.usage_details` contract,
preserve calls whose provider does not report usage by marking them unavailable, and keep each
agent's usage local rather than silently rolling specialist tokens into a coordinator total. Each
record will also identify the transport provider and effective model/deployment used to construct
the client.

## 2. Motivation / problem

MAF already captures detailed model token usage and exports it on `gen_ai.*` spans, so customers can
inspect the data in Application Insights. This feature does not replace or extend that customer
observability surface. The internal runtime currently lacks one stable, machine-parseable usage log
per agent invocation attempt that can feed internal token-volume and revenue analysis. Internal
consumers need to identify:

- reported input, output, total, cache, and reasoning token counts;
- which coordinator or specialist incurred the usage; and
- which configured transport handled the call;
- which effective model/deployment generated the usage; and
- whether an attempted call completed without provider usage metadata.

The record belongs on the existing shared logger. This preserves the runtime's current category,
routing, filtering, and visibility behavior. Internal ingestion can consume the structured message;
customers continue to use MAF's richer App Insights spans for detailed usage analysis.

## 3. Goals / Non-goals

**Goals**
- Emit exactly one metadata-only usage record in-process for each MAF `Agent.run()` attempt that
  actually starts.
- Cover non-streaming, streaming, chat-time delegated, and Dynamic Workflow Sub Agent calls.
- Emit records only through the existing shared `azure.functions.AgentRuntime` logger.
- Normalize MAF's canonical input, output, total, cache, and reasoning token fields.
- Log the effective model/deployment and configured transport provider (`foundry`, `azure_openai`,
  or `openai`).
- Distinguish complete and unavailable usage without fabricating zeroes.
- Include workflow/node ids for Durable activity attempts without logging chat session identifiers.
- Keep usage extraction and emission non-fatal: telemetry must never change an agent result.
- Keep usage metadata free of prompts, responses, tool arguments, and secrets regardless of
  `ENABLE_SENSITIVE_DATA`.

**Non-goals**
- Rolling specialist usage into a coordinator or top-level request total.
- Adding token usage to REST, MCP, SSE, or `AgentResult` response contracts.
- Emitting one record per inner model call; MAF's existing `gen_ai.*` spans remain that surface.
- Estimating tokens when a provider does not report them.
- Calculating price, currency, quotas, budgets, or chargeback.
- Inferring model publisher from model/deployment names such as `gpt-*` or `claude-*`.
- Logging model API hosts or other endpoint-derived resource identifiers.
- Calling the Foundry control plane from the invocation path to discover deployment metadata.
- Supporting direct Anthropic transport.
- Requiring customer configuration solely to enrich internal telemetry.
- Adding or changing spans, span attributes, metrics, or customer observability behavior.
- Adding token metrics, persistence, dashboards, alerts, or a new config/front-matter switch.
- Parsing provider-specific raw response payloads or private MAF telemetry internals.

## 4. Proposed design

This is an internal execution-log extension. Discovery, translation, registration, and the existing
OpenTelemetry surface do not change.

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| discover | none | No change. |
| translate | none | No schema or resolved-config change. |
| register | none | No change. |
| execute | `runner.py` | Extract final MAF usage for successful non-streaming/streaming calls, carry the resolved inference target, and emit once per top-level or leaf invocation attempt. |
| client construction | `client_manager.py` | Return the chat client with an immutable inference-target descriptor containing authoritative transport and effective model/deployment. |
| internal logging | `runner.py` | Normalize usage and write structured JSON through the imported shared `logger`; `_logger.py` remains unchanged. |
| Dynamic Workflow activity | `workflows/engine.py` | Pass workflow and node correlation into leaf-agent usage logging. |

### 4.1 Source of truth and normalization

The runtime uses only MAF's public response contracts:

- non-streaming: `AgentResponse.usage_details` after `await Agent.run(...)`; and
- successful streaming: `await ResponseStream.get_final_response()` after normal iteration, then
  its `AgentResponse.usage_details`.

Interrupted streams do not have a final `AgentResponse`, and provider/MAF versions do not expose a
single documented cross-provider contract stating whether each intermediate `UsageContent` is an
increment or a cumulative snapshot. The runtime therefore does not emit token counts from
intermediate usage updates. A timeout, cancellation, exception, or generator teardown emits the
invocation record with `usage_available=false` and `usage_complete=false`. This intentionally
prefers an explicit gap over a plausible but incorrect count.

The normalized token fields are:

| MAF `UsageDetails` key | Runtime record field |
| --- | --- |
| `input_token_count` | `input_tokens` |
| `output_token_count` | `output_tokens` |
| `total_token_count` | `total_tokens` |
| `cache_creation_input_token_count` | `cache_creation_input_tokens` |
| `cache_read_input_token_count` | `cache_read_input_tokens` |
| `reasoning_output_token_count` | `reasoning_output_tokens` |

Reported integer zeroes are valid. Missing or malformed values are omitted. The runtime does not
derive `total_tokens` from input/output when MAF omits it because the provider may report additional
token categories. A record has `usage_available=true` when at least one canonical count is valid.

A successful stream uses only final `AgentResponse.usage_details`, avoiding duplicate counting
between intermediate updates and the final response. Usage content remains ignored by the runtime's
SSE mapper.

### 4.2 Inference target metadata

`client_manager.py` adds an immutable `InferenceTarget` value with `provider` and `model`. A backward-compatible
`ClientManager.build_chat_client_with_target()` method returns the client plus this descriptor;
custom managers that implement only the existing abstract methods continue to work and receive a
best-effort descriptor with unavailable fields. `MAFClientManager` overrides the method so provider
selection and model resolution happen once and cannot drift between the built client and log data.

The exact value type is:

```python
@dataclass(frozen=True)
class InferenceTarget:
  provider: str | None = None
  model: str | None = None
```

Fields are optional because a custom manager may not have an authoritative value. The built-in MAF
manager always supplies `provider` and effective `model`. The value remains valid for the lifetime
of the client returned beside it.

`build_chat_client()` remains an abstract, supported API with its existing signature. The new method
is concrete on the ABC: its default calls `self.build_chat_client(model)` exactly once and returns
`tuple[Any, InferenceTarget]` containing that client and `InferenceTarget()` (all fields unavailable
rather than guessed). Runner call sites migrate to the new method and unpack both values, so an
existing custom manager that implements only `resolve_model()` and `build_chat_client()` remains
source-compatible and always receives the empty fallback descriptor. Custom managers may override
the new method to return authoritative metadata.

`MAFClientManager` overrides both paths around private helpers: it selects the provider once,
resolves the effective model against that provider once, dispatches the matching client-builder
branch, and creates the descriptor from those same local values. Its legacy `build_chat_client()`
delegates to the MAF override and returns only the client. It must not independently call
`_provider()` or `resolve_model()` after client construction. Tests assert the descriptor provider
matches the client-builder branch that actually executed. Concretely,
`MAFClientManager.build_chat_client_with_target()` caches `_provider()` and model resolution results
as local variables in one method scope before invoking the matching `_build_openai()`,
`_build_azure_openai()`, or `_build_foundry()` branch and constructing `InferenceTarget` from those
same cached values. The legacy method performs no independent provider or model resolution.

The frozen descriptor is a construction-time snapshot. Later environment, endpoint, credential, or
manager-state changes do not mutate it. All fields remain optional in the shared type for custom
manager compatibility. For `MAFClientManager`, `provider` and `model` are always populated;
consumers still treat both fields as nullable and do not infer availability from provider type.
Endpoint-derived host metadata and model publisher are not collected or included in the descriptor.

For built-in providers:

| Configured transport | `provider` |
| --- | --- |
| Microsoft Foundry | `foundry` |
| Azure OpenAI | `azure_openai` |
| OpenAI | `openai` |

The effective `model` is the same resolved model/deployment passed to the MAF client.

Direct Anthropic transport remains unsupported: `ANTHROPIC_API_KEY` is not read and does not
participate in provider precedence. If it is present alongside a supported provider's settings, the
supported provider selection is unchanged; if it is the only provider credential, existing
"No MAF provider configured" behavior remains unchanged.

### 4.3 One record per invocation attempt

An invocation attempt begins only when the runtime calls MAF `Agent.run()`. Session/history/agent-
build failures that happen before that point do not emit a misleading agent-call record. Once begun,
an in-process idempotent recorder emits exactly once across success, timeout, exception,
cancellation, and stream-generator teardown paths.

Dynamic Workflow Activities are delivered at least once. If Durable retries an activity and the
activity calls MAF again, that is another real model invocation attempt with new token consumption
and therefore produces another record. The runtime does not suppress or deduplicate those records.
`workflow_id` plus `workflow_node_id` groups attempts for the same logical workflow node; each log
item represents one actual invocation attempt.

The record contains:

- identity: `event_name=agent_token_usage`, `agent_name` (the resolved agent slug),
  `execution_role`;
- correlation: optional `workflow_id` and `workflow_node_id`;
- accounting: `outcome`, `usage_scope=agent_run_local`, nullable `provider`, nullable
  effective `model`;
- quality: `usage_available`, `usage_complete`, `usage_source`; and
- any valid normalized token fields from section 4.1.

`execution_role` is `primary`, `delegate`, or `workflow_subagent`. `usage_source` is
`final_response` or `unavailable`. Normal completion with valid final response metadata sets
`usage_complete=true`. Interrupted calls and completed calls without valid provider metadata emit
`usage_available=false` and `usage_complete=false`. The record outcome is `success`, `error`,
`timeout`, or `cancelled`.

Each coordinator and specialist emits an independent local record. Chat-time delegation records
carry the specialist's agent name. Workflow activities also include explicit workflow/node ids.
Internal consumers can sum local records when a request-level total is needed; the runtime does not
claim that sum as a coordinator total.

The exact JSON target field names are `provider` and `model`. They are always present with a JSON
string or `null`, keeping the record shape stable for built-in and custom managers alike. No
endpoint-derived host or publisher field is emitted.

### 4.4 Internal logging

`runner.py` continues to import the repository's shared `logger` from `_logger.py`. The recorder
writes one INFO message whose payload is deterministic JSON:

```text
Agent token usage: {"agent_name":"main",...,"total_tokens":321}
```

The implementation uses parameterized logging (`logger.info("Agent token usage: %s", payload)`) and
serializes with stable key ordering so internal ingestion can recognize the prefix and parse the JSON
without relying on logger-specific custom-dimension handling. `_logger.py` and the
`azure.functions.AgentRuntime` category remain unchanged, preserving all existing routing,
filtering, and visibility behavior.

`run_leaf_agent_task()` adds required
`execution_role: Literal["delegate", "workflow_subagent"]` plus optional `workflow_id` and
`workflow_node_id` keyword parameters. `_build_delegate_tool()` passes `delegate` with no workflow
ids. `agents_workflow_run_sub_agent()` passes `workflow_subagent` plus `task["workflow_id"]` and
`task["id"]`.

### 4.5 Failure and streaming behavior

For a successful stream, `run_agent_stream()` calls `get_final_response()` after update iteration
and before emitting the existing `done` event. Usage content remains ignored and does not expand
the SSE vocabulary. For timeout, cancellation, exception, or client disconnect, existing
`_finalize_maf_stream()` behavior remains in place; the usage recorder emits at most once with
usage unavailable. Tests assert ordering across normal completion, timeout, cancellation, and
`GeneratorExit`, including that `get_final_response()` does not duplicate cleanup/finalization.

Extraction and emission catch their own errors. Malformed provider metadata, logger failures,
or a test/custom stream without `get_final_response()` never fail or alter the application call.

### Authoring / API surface

There is no new authoring or wire API surface. No `*.agent.md`, `agents.config.yaml`, endpoint,
`AgentResult`, or SSE fields change. The structured system-log message is an internal runtime
contract; no new customer observability surface is introduced.

### Compatibility

- Existing agent behavior and response contracts are backward compatible.
- The new INFO record increases telemetry volume by one item per actual agent invocation attempt.
- Existing MAF `gen_ai.*` usage spans continue unchanged and can be used for model-call detail.
- The log remains under `azure.functions.AgentRuntime`; no logger, category, exporter, span, or
  metric configuration changes.
- Providers that omit usage still produce a record with `usage_available=false`; they are not
  treated as zero-token calls.
- Existing custom `ClientManager` implementations remain source-compatible; target fields they do
  not authoritatively provide are omitted.
- Internal usage attribution requires no customer-only configuration.
- The implementation targets the declared `agent-framework-*==1.3.*` public API and includes
  compatibility tests for `usage_details` and `ResponseStream.get_final_response()`.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Telemetry destination | customer log only / span attributes only / both | Emit a customer-visible structured log and matching runtime span attributes. | Human | 2026-08-05 |
| 2 | Multi-agent accounting scope | top-level rollup / one local record per `Agent.run()` / both | One local record per coordinator or specialist call; correlate and aggregate in the backend. | Human | 2026-08-05 |
| 3 | Calls without provider usage | omit / emit unavailable | Always emit after invocation starts and mark usage unavailable. | Human | 2026-08-05 |
| 4 | Usage source | provider raw payloads / MAF public response / MAF spans | Use final `AgentResponse.usage_details`; do not parse provider raw payloads or MAF spans. | Agent | 2026-08-05 |
| 5 | Existing logger | rename it / use it / add dedicated logger | Retain the system logger and add a non-`azure.functions.*` customer usage logger. | Agent | 2026-08-05 |
| 6 | Missing totals | derive input + output / preserve absent | Preserve absent fields; never invent counts the provider did not report. | Agent | 2026-08-05 |
| 7 | Public response changes | add usage to API/SSE / telemetry only | Telemetry only; no REST, MCP, SSE, or `AgentResult` change. | Agent | 2026-08-05 |
| 8 | Sensitive-data policy | follow `ENABLE_SENSITIVE_DATA` / always metadata-only | Always metadata-only; never include model or tool content. | Agent | 2026-08-05 |
| 9 | Interrupted-stream counts | sum usage updates / snapshot or max / report unavailable | Report unavailable; intermediate usage semantics are not a stable cross-provider contract. | Agent (architecture review) | 2026-08-05 |
| 10 | Durable retry cardinality | logical-call exactly once / invocation-attempt exactly once | Emit once in-process per actual MAF attempt; retries that call the model again get distinct records. | Agent (architecture review) | 2026-08-05 |
| 11 | Log visibility guarantee | guarantee `AppTraces` / standard-log contract with deployment-dependent export | Emit a standard customer-category INFO record; treat spans as canonical and validate both supported logging paths. | Agent (architecture review; Human selected log + span) | 2026-08-05 |
| 12 | Shared logger convention | shared system logger only / narrow dedicated logger exception | Add only the usage logger because the shared `azure.functions.*` namespace cannot produce customer telemetry. | Agent (architecture review; Human selected customer log) | 2026-08-05 |
| 13 | Re-scope telemetry destination | dedicated customer logger + spans / existing system logger only | **Supersedes #1, #5, #11, and #12:** emit only an internal structured INFO record through the existing `azure.functions.AgentRuntime` logger; add no logger or span surface. | Human | 2026-08-05 |
| 14 | Revenue responsibility | calculate money in runtime / log billable inputs for downstream analysis | Log effective model/deployment and reported token categories; downstream systems own pricing and revenue calculations. | Human motivation, Agent boundary | 2026-08-05 |
| 15 | Keep revenue inputs within runner's authoritative data | strengthen `ClientManager` to guarantee provider/model / omit provider/model and join downstream | **Supersedes the model portion of #14:** log only authoritative token categories and invocation context; internal accounting joins provider/model/deployment pricing context outside the runtime. | Agent (internal-only scope review) | 2026-08-05 |
| 16 | Finalize internal-only design | approve / revise | Approved for implementation. | Human | 2026-08-05 |
| 17 | Identify where inference is hosted | model API hostname / Function App deployment / both | Log the sanitized model API hostname. | Human | 2026-08-06 |
| 18 | Separate transport from publisher | one overloaded provider field / separate fields | Log configured inference transport and model publisher separately; a Foundry-hosted Anthropic model is `provider=foundry`, `model_publisher=anthropic`. | Human | 2026-08-06 |
| 19 | Resolve Foundry publisher | infer from model name / control-plane lookup / exact-model environment map / unavailable | Use an exact-model JSON environment map; never infer names or add an invocation-path control-plane request. | Human | 2026-08-06 |
| 20 | Reintroduce authoritative provider/model metadata | keep #15 downstream-only / extend `ClientManager` target contract | **Supersedes #15:** extend `ClientManager` with a backward-compatible inference-target descriptor so the runner logs only metadata used to construct the client. | Agent | 2026-08-06 |
| 21 | Finalize inference-target amendment | approve / revise | Approved for implementation. | Human | 2026-08-06 |
| 22 | Minimize endpoint-derived metadata | keep sanitized host / remove host | **Supersedes #17:** do not collect or emit the model API host; provider, model/deployment, and publisher are sufficient for internal accounting. | Human | 2026-08-06 |
| 23 | Per-attempt identifier | runtime-generated UUID / rely on one log item per attempt | Remove `invocation_id`; it has no external correlation source, and each emitted log item already represents one invocation attempt. | Human | 2026-08-06 |
| 24 | Transport field name | `inference_provider` / `provider` | Use the concise JSON and descriptor field name `provider`; its values continue to identify the configured inference transport. | Human | 2026-08-06 |
| 25 | Chat session identifier | retain for conversation aggregation / remove for data minimization | Do not include `session_id` in token usage logs; session-level cost analysis is not required for internal provider/model accounting. | Human | 2026-08-06 |
| 26 | Customer setup for internal attribution | exact-model publisher map / publisher inference / provider and model only | **Supersedes #18, #19, and the publisher clause of #22:** internal usage logging must require no customer-facing setup. Remove `model_publisher` and `AZURE_FUNCTIONS_AGENTS_MODEL_PUBLISHERS`; retain only authoritative `provider` and effective `model`, with no publisher inference or control-plane lookup. | Human | 2026-08-06 |
| 27 | Log schema marker | retain `schema_version` / remove it | Remove `schema_version`; this internal log does not need an explicit schema field. | Human | 2026-08-06 |

## 6. Test plan

- [x] Unit: usage normalization preserves valid zeroes and canonical optional fields while omitting
  missing, boolean, negative, and malformed counts.
- [x] Unit: stable JSON message shape/prefix through the existing shared logger, metadata-only
  privacy, INFO level, and emitter idempotence.
- [x] Unit: non-streaming success with/without usage and timeout/exception/cancellation emit exactly
  once per attempt; pre-invocation build failure emits none.
- [x] MAF contract: one non-streaming `Agent.run()` with multiple model/tool turns reports the
  aggregate expected usage; a fully iterated stream followed by `get_final_response()` reports the
  same totals.
- [x] Unit: streaming final-response usage, ignored usage SSE content, unavailable usage on
  interruption, no-usage completion, custom-stream compatibility, and exactly-once teardown
  behavior.
- [x] Unit: delegated and workflow specialists emit independent records with the correct role and
  workflow correlation; coordinator usage remains local.
- [x] Testing review: independently inspect duplicate-emission paths, cancellation/`GeneratorExit`,
  stream finalization order, and local-vs-rollup assertions.
- [x] Full gate: ruff, mypy, and CI-equivalent pytest/coverage commands from `AGENTS.md`.
- [x] Smoke test: normal, streaming, delegated, unavailable, and workflow calls each write the
  expected `azure.functions.AgentRuntime` line and contain no sensitive content.
- [x] Unit: built-in providers return the effective model and configured transport without
  collecting endpoint-derived host or publisher metadata.
- [x] Unit: custom `ClientManager` implementations remain compatible and omit target fields they do
  not provide.
- [x] Unit: primary, streaming, delegated, and workflow usage records carry the target associated
  with their own built client; target metadata remains present when usage itself is unavailable.
- [x] Testing review: independently verify client/metadata cannot drift, endpoint-derived metadata
  and publisher configuration are absent, custom-manager compatibility, and no new customer
  telemetry surface.
- [x] Full gate after inference-target amendment: ruff, mypy, and CI-equivalent pytest/coverage.

The unchecked amendment tests above are mandatory Phase 4 merge gates. Per `AGENTS.md`, they remain
unchecked during Phase 2 architecture review and are marked complete only after product
implementation exists and the tests pass.

No config scenario fixture is required because authoring/config interpretation does not change.

## 7. Docs impact

- [x] `docs/architecture.md` - note that `runner.py` emits internal per-attempt token usage records.
- [x] `docs/architecture.md` - in the module map and extension-point/custom-inference-client
  discussion, document the `build_chat_client_with_target()` tuple contract, `InferenceTarget`
  fields and provider-specific availability, absence of endpoint-derived and publisher metadata,
  and that `runner.py` consumes this descriptor rather than duplicating provider selection.
- [x] `docs/observability.md` - no change; customer token detail remains MAF's existing App Insights
  span surface, and the internal logger behavior described there remains unchanged.
- [x] `docs/frds/0007-multi-agent-delegation.md` - no change; customer-facing MAF span and no-rollup
  behavior remains accurate.
- [x] `README.md` - no customer setup is required for the internal usage record.
- [x] `AGENTS.md` - no change; the implementation follows the shared-logger convention.
- [x] `docs/front-matter-spec.md` - no change; there is no authoring surface.
- [x] `docs/triggers.md` - no change; trigger behavior is unchanged.

## 8. Status & sign-off

- **Architecture review (phase 2):** The initial broader customer-log/span design completed review,
  then the Human superseded it with the internal-only existing-logger requirement (Decision #13).
  Review of that reduced design found one blocker: guaranteeing provider/model would require a
  broader `ClientManager` contract change. Decision #15 resolves it by limiting the record to
  authoritative invocation/token data and leaving provider/model/pricing joins downstream. The
  review also requested `schema_version=1`, subsequently removed by Decision #27. A final
  independent follow-up review found no blocker, major, or minor findings and returned **APPROVE**
  on 2026-08-05.
- **Human sign-off:** victoriahall, 2026-08-05. **Finalized.**
- **Inference-target amendment (2026-08-06):** Reopened for architecture review after the Human
  requested model API host plus separate transport/publisher metadata and selected an exact-model
  Foundry publisher map. Independent planning-only review found no unresolved design findings and
  returned **APPROVE**. Human sign-off: victoriahall, 2026-08-06. Decision #22 subsequently removes
  host collection to minimize potentially identifying endpoint metadata. Decision #26 subsequently
  removes publisher metadata and its customer-facing configuration. **Finalized.**