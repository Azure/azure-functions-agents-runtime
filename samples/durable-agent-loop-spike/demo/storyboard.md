# Durable Agent Loop leadership demo storyboard

Target: 6–8 minutes, 1440×900, light portal theme, segmented real browser
capture. All live runs call the deployed Function App. Browser scenes render
only content-free metadata; the Function key stays in the loopback proxy
process environment.

| Scene | Claim | Surface / action | Expected evidence | Correlation / fallback |
| --- | --- | --- | --- | --- |
| Opening | The design separates orchestration from untrusted execution. | Original title card. | Trusted brain / isolated hands / durable recovery. | No fallback needed. |
| Architecture | Four governed Azure surfaces cooperate without putting local executable tools in the Functions worker. | Original architecture card. | Functions+DTS, APIM, ACA Sandbox, Blob+Insights. | Source: FRD 0010 and deployed topology. |
| Live chapter | The demo uses real endpoints and real resources. | Original live chapter card. | Four scenarios and secret-handling statement. | No fallback needed. |
| Retained create — start | A new retained run starts a DTS orchestration and creates a customer-owned sandbox. | Local control room → **New retained session**. | Pending/Running status, safe run/session aliases, sandbox inventory becomes one. | Run ID stored server-side and in machine-local browser state. |
| Retained create — completion | Individual model and tool steps complete and the workspace checkpoint is exported. | Reopen the same control room after completion. | Completed, 3 model steps, 2 tool calls, sandbox inventory present. | Use content-free status if result text is hidden. |
| Retained stop and reuse — start | ACA auto-suspend stops the sandbox; the next turn resumes the same sandbox. | Wait outside capture for state `Stopped`, reopen controller, trigger **Reuse same session**. | Same safe sandbox alias, state changes Stopped → Running, same session alias. | Lifecycle JSONL is retained as evidence. |
| Retained reuse — completion | Workspace state persists across turns. | Reopen controller after completion. | Completed, 2 model steps, 1 tool call; exact deterministic marker was validated by the run. | If response content is hidden, show counters plus same sandbox/session aliases. |
| Fault — start | Recovery is activity-scoped. | Trigger **Recover injected fault**. | DTS run starts with fixed `tool_activity_ack_loss_once` profile. | No arbitrary fault payloads. |
| Fault — completion | A checkpointed prior step is not replayed from zero. | Reopen after terminal status. | Completed, 7 model steps, 6 logical tool calls; deterministic terminal marker validated. | DTS Timeline is the authoritative retry evidence. |
| HITL — start | Human clarification is a runtime-owned tool call. | Trigger **Pause for a human**. | Run starts and progresses to `Waiting`. | Fixed two-choice prompt only. |
| HITL — wait/resume | The orchestration parks without continuing model/tool compute and resumes from the same checkpoint. | Reopen at Waiting, select **Beta**. | Waiting card, external-event answer, same run becomes Completed. | Authenticated detail contents remain hidden. |
| Observability chapter | One safe run alias crosses every view. | Original observability card. | DTS, App Insights, APIM, ACA. | No fallback needed. |
| DTS timeline | DTS exposes orchestration and activity boundaries directly. | Real DTS dashboard, completed fault run, Timeline. | Orchestrator, model/tool activities, attempts, duration, completion. | Full IDs and entity hashes are visually replaced with safe aliases. |
| App Insights | Agent and service telemetry is content-free and measured. | Real Application Insights **Agents (Preview)** dashboard. | Agent runs, tools, model, token and operational metrics. | If a waterfall is unavailable, do not imply one; DTS remains the activity timeline. |
| APIM | The model/control/MCP routes are governed independently. | Real APIM resource page. | Dedicated child APIs and gateway management surface. | Portal identity/tenant details are overlaid as redacted. |
| ACA | The isolation resource is customer-owned and lifecycle bounded. | Real ACA Sandbox Group resource page. | Sandbox Group overview and policy context. | Live inventory is shown in the control room because the portal lacks a richer inventory view. |
| Lifecycle deletion | ACA automatically deletes retained inventory after the bounded inactivity policy. | Reopen control room after off-camera wait. | Inventory reaches zero; lifecycle log contains Running → Stopped → zero timestamps. | Explicitly label this as ACA automatic inactivity policy. |
| Results | The spike completed bounded chains and real failure/HITL scenarios with measured timings. | Original results card. | c10 10/10, 18/17 chain, current p50/p95 component timings. | Label client-observed versus service telemetry; no SLA claim. |
| Recommendation | Feasibility is proven; production readiness is not. | Original closing card. | Time-boxed productization design recommendation. | No fallback needed. |

## Security and capture rules

- Dedicated machine-local portal profile:
  `%LOCALAPPDATA%\ms-playwright-demo-video\hybrid-sandbox-azure-portal`.
- Separate disposable control-room recording profile outside the deliverable
  folder.
- Never render Function/APIM keys, tokens, connection strings, publishing
  profiles, raw prompts, tool arguments/results, provider response IDs, or
  sandbox resource IDs.
- Replace full run IDs with eight-character aliases in portal DOM before
  recording. Hash sandbox IDs before returning them to the browser.
- Keep `ENABLE_SENSITIVE_DATA=false`.
- Record independent clips and remove model, telemetry-ingestion, auto-suspend,
  and auto-delete waits in the final edit.
