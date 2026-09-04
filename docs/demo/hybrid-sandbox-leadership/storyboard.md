# Hybrid sandbox leadership demo storyboard

| Time | Scene | Visible action and claim | Evidence |
| --- | --- | --- | --- |
| 0:00-0:04 | Capability title | Hosted Skills can pair serverless orchestration with isolated customer tool execution. | Retained deployment |
| 0:04-0:12 | Request path | Functions orchestrates, APIM governs, ACA Sandbox isolates, and Application Insights correlates. | FRD 0009 architecture |
| 0:12-0:50 | Live Hosted Skills chat | Submit one real prompt; the live tool counter reaches three; Microsoft Learn grounding and both shell markers return. | Recorded `/agents/main/` UI; Function operation `a241847f…` |
| 0:50-1:10 | Sandbox lifecycle | One fresh sandbox contains both local calls, delete succeeds, and inventory returns to zero. | Runtime trace `3d57819c…`; typed final inventory |
| 1:10-1:45 | Agent trace | The same run contains model planning/synthesis, MCP tool, sandbox, shared file-journal, and cleanup spans. | 68 Application Insights dependency spans |
| 1:45-2:05 | AI Gateway | The same UTC window contains two model posts and five successful MCP posts through APIM. | APIM-role Application Insights dependencies; live policy capture |
| 2:05-2:25 | Demo results | Live HTTP 200/31.02 seconds/zero inventory plus the n=21 optimized qualification and 48% reduction. | Live request plus durable results JSON |
| 2:25-2:37 | Takeaway | The complete governed path works and cleans up. | Combined evidence |

Transitions are hard cuts or sub-second fades; there are no long presentation
gaps. The former implementation-details scene is removed, and the duplicated
results/close material is consolidated into **Demo results**.

The live chat records safe prompt and answer content. Telemetry scenes exclude
prompt text, tool arguments/results, function keys, account identity,
subscription/tenant IDs, sandbox IDs, and runtime host identifiers.
