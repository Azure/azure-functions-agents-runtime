# Hybrid sandbox leadership demo storyboard

| Time | Scene | Visible action and claim | Evidence |
| --- | --- | --- | --- |
| 0:00-0:04 | Capability title | Hosted Skills can pair serverless orchestration with isolated customer tool execution. | Retained deployment |
| 0:04-0:12 | Request path | Functions orchestrates, APIM governs, ACA Sandbox isolates, and Application Insights correlates. | FRD 0009 architecture |
| 0:12-0:28 | Live request | Type and send the real multi-tool prompt in Hosted Skills. | Raw Playwright recording; Function operation `b4ea9ff6…` |
| 0:28-0:40 | Expanded tool calls | Open the live `3 tool calls` bubble; safe tool names and one running sandbox appear together. | `live-chat-tool-calls.png`; typed inventory at `17:43:25Z` |
| 0:40-0:54 | Returned result | The live response returns the Microsoft Learn takeaway plus `ALPHA_COMPLETE` and `BETA_COMPLETE`; post-run inventory is overlaid as zero. | Raw Playwright recording; independent `final-inventory.json` |
| 0:54-1:10 | Sandbox lifecycle | One fresh sandbox contains both local calls, cleanup deletes it, and inventory remains zero. | Runtime trace `a34b1843…`; typed final inventory |
| 1:10-1:28 | Native Agent Trace | Application Insights Agents (Preview) renders a retained live Agent Trace with agent, LLM, sandbox, and journal spans. | `appinsights-agent-trace-portal.png`; identifiers redacted |
| 1:28-1:48 | Same-run trace analysis | The 68.81-second capture run separately correlates model, MCP, sandbox, shared journal, and cleanup spans. | Runtime trace `a34b1843…` |
| 1:48-2:06 | AI Gateway | The capture window contains two model posts and successful MCP posts through APIM. | APIM-role dependencies; live policy capture |
| 2:06-2:24 | Demo results | HTTP 200/zero inventory plus the latest complete n=21 optimized qualification from 2026-09-03 and its 48% reduction. | Live request plus durable results JSON; the incomplete 2026-09-04 rerun is excluded |
| 2:24-2:34 | Takeaway | The complete governed path works and cleans up. | Combined evidence |

Transitions are hard cuts or sub-second fades; there are no long presentation
gaps. The former implementation-details scene is removed, and the duplicated
results/close material is consolidated into **Demo results**.

The live chat records safe prompt and answer content. The two 25-second shell
holds exist only to make the active sandbox observable and are not treated as a
service benchmark. Telemetry scenes exclude prompt text, tool arguments/results,
function keys, account identity, subscription/tenant IDs, sandbox IDs, and
runtime host identifiers.

The 2026-09-04 streaming rerun stopped after its inventory safety gate and did
not produce a replacement c1/c10 comparison. The existing recorded scene stays
bound to the complete 2026-09-03 qualification.
