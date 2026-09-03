# Hybrid sandbox leadership demo storyboard

| Time | Scene | Claim | Narration focus | Evidence |
| --- | --- | --- | --- | --- |
| 0:00-0:24 | Opening | Hosted Skills can pair serverless orchestration with isolated tool execution. | Functions orchestrates; APIM governs; ACA Sandbox isolates. | Retained deployment inventory |
| 0:24-0:56 | Deployed topology | Model/MCP remain outside the local-tool sandbox boundary. | Four governed surfaces and one request path. | FRD 0009 architecture |
| 0:56-1:28 | Function Hosted Skill | One live stream returned HTTP 200 and terminal `done`. | Safe status, timing, and correlation only. | Function request operation `c9cfb8d52497c15862277558e1a96a9c` |
| 1:28-2:00 | APIM Foundry + MCP | The same bounded window contains two successful model calls and four successful MCP POSTs. | Managed identity, W3C correlation, no body capture. | App Insights APIM request telemetry |
| 2:00-2:34 | ACA lifecycle | Typed inventory moved from zero to one Running and back to zero at 23:00:08 UTC. | Honest failed delete-initiation path and scheduled ownership-scoped reaper. | Typed `AcaSandboxAdapter` inventory |
| 2:34-3:12 | App Insights waterfall | Runtime lifecycle stages are measurable without content. | Live stage durations on `hybrid.invocation`. | Runtime trace `fc2ce69d3b9c95984da4c48e15b8d48f` and custom metrics |
| 3:12-3:42 | Enterprise boundaries | Functions, APIM, and Sandbox identities remain separate. | Narrow RBAC, deny egress, no ingress, no bodies. | Resource and API diagnostic configuration |
| 3:42-4:16 | Measured qualification | Optimized runtime average is 48.03% lower across 21 clean-window requests. | Explain create, upload, readiness, tool, and handoff costs. | Durable qualification results JSON |
| 4:16-4:44 | Leadership takeaway | Feasibility is proven; productization is the next decision. | Authoring simplicity, centralized governance, isolated execution. | Combined evidence |
| 4:44-5:08 | Close | The complete live path worked and inventory converged. | One-line capability statement. | Manifest and final inventory |

The visible scene captions are intentionally shorter than narration. No request
or response body, key, token, connection string, sandbox identifier, or
subscription identifier appears in the rendered media.
