# Hybrid sandbox leadership demo storyboard

| Time | Scene | Claim | Narration focus | Evidence |
| --- | --- | --- | --- | --- |
| 0:00-0:24 | Opening | Hosted Skills can pair serverless orchestration with isolated tool execution. | Functions orchestrates; APIM governs; ACA Sandbox isolates. | Retained deployment inventory |
| 0:24-0:56 | Deployed topology | Model/MCP remain outside the local-tool sandbox boundary. | Four governed surfaces and one request path. | FRD 0009 architecture |
| 0:56-1:28 | Live Function App | The Hosted Skill chat, chatstream, MCP, and reaper functions are deployed; the portal evidence request returned HTTP 200 and terminal `done`. | Walk the real Functions blade and highlight `agent_main_builtin_chatstream`. | Live Function App capture plus operation `f8e1f7aec373b17ab9d1ae1d730b2105` |
| 1:28-2:00 | Live APIM governance | The retained model API enforces backend/token policies; the MCP API enforces rate limiting and streaming forward. | Walk the real policy design surfaces; distinguish live configuration from qualification telemetry. | Live `larohra-ai-gateway` model and MCP policy captures |
| 2:00-2:34 | Live ACA lifecycle | The portal shows zero inventory after execution; the earlier qualification observation proves a transient Running sandbox. | Keep the two provenances explicit because current polling did not catch the short-lived sandbox. | Live Sandbox Group portal plus typed inventory observations |
| 2:34-3:12 | Live App Insights waterfall | The exact portal evidence request appears as HTTP 200, 18.3 seconds, with two correlated trace items. | Walk the real end-to-end transaction and the content-free correlation window. | Operation `f8e1f7aec373b17ab9d1ae1d730b2105` |
| 3:12-3:42 | Enterprise boundaries | Functions, APIM, and Sandbox identities remain separate. | Narrow RBAC, deny egress, no ingress, no bodies. | Resource and API diagnostic configuration |
| 3:42-4:16 | Measured qualification | Optimized runtime average is 48.03% lower across 21 clean-window requests. | Explain create, upload, readiness, tool, and handoff costs. | Durable qualification results JSON |
| 4:16-4:44 | Leadership takeaway | Feasibility is proven; productization is the next decision. | Authoring simplicity, centralized governance, isolated execution. | Combined evidence |
| 4:44-5:08 | Close | The complete live path worked and inventory converged. | One-line capability statement. | Manifest and final inventory |

Scenes 3-6 use redacted, account-header-free captures from the authenticated
Azure product blades. No request or response body, key, token, connection
string, sandbox identifier, subscription identifier, account identity,
InvocationId, or HostInstanceId appears in the rendered media.
