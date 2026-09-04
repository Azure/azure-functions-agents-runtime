# Hybrid ACA Sandbox/APIM leadership demo

This package demonstrates one real Hosted Skills request flowing through Azure
Functions, API Management, Microsoft Learn MCP, and invocation-scoped ACA
Sandbox execution before returning a governed result and zero sandbox inventory.
The primary cut is a 2:36 narrated walkthrough built around the recorded live
chat rather than a slide-only presentation.

The implementation remains experimental and private. This demo supports a
productization decision; it does not present the retained environment as a
supported production service.

## Primary assets

| Asset | Purpose |
| --- | --- |
| `hybrid-sandbox-leadership-demo.pptx` | Six-slide executive deck with speaker notes and embedded live evidence |
| `storyboard.md` | Timestamped 2:36 scene plan and narration |
| `record_live_flow.py` | Bounded live recorder with pre/post inventory gates |
| `demo.html` | Supporting topology, lifecycle, trace, APIM, and result scenes |
| `record_scenes.py` | Reproducible supporting-scene recorder |
| `narration.json` | Narration source |
| `manifest.json` | Correlation window, claims, provenance, integrity, and paths |
| `evidence/live-chat-multitool.png` | Returned MCP takeaway plus `ALPHA_COMPLETE` and `BETA_COMPLETE` |
| `evidence/appinsights-agent-trace.png` | Same-run 68-span Application Insights trace visualization |
| `evidence/aca-sandbox-live-run.png` | Live sandbox product view plus same-run create/tool/delete/zero timeline |
| `evidence/apim-foundry-mcp.png` | Live APIM policy configuration for model and MCP lanes |

The final MP4, silent master, contact sheet, raw Playwright recording, and
redacted telemetry exports are intentionally kept out of git:

```text
C:\Users\larohra\.copilot\session-state\e29c49e0-41eb-4fab-be8c-6e9a671f8008\files\hybrid-sandbox-leadership-video\live-flow-v3\
```

Open `hybrid-sandbox-leadership-live-e2e.mp4` for the leadership cut.

## Correlated live run

The bounded invocation started at `2026-09-04T16:45:30.1047464Z` and completed
with HTTP 200 at `2026-09-04T16:46:01.1258946Z`.

- Function request operation: `a241847fb7325d57b8516c2bd52f2f08`
- Runtime trace operation: `3d57819cb9fda4dcfe439fc457a93133`
- Function duration: 31.021 seconds
- Runtime span duration: 30.949 seconds
- Tool results: one Microsoft Learn MCP takeaway, `ALPHA_COMPLETE`, and
  `BETA_COMPLETE`
- Application Insights dependencies: 68 spans
- Sandbox lifecycle: create 2.942 seconds, cleanup handoff 65.9 milliseconds,
  delete 1.685 seconds
- Final typed inventory: zero at `2026-09-04T17:01:45.7843079Z`; no operator
  cleanup was used

The model surfaced three tool calls in one plan. The two local calls shared one
invocation sandbox and were safely queued by the same-sandbox file journal.
This is **parallel model fan-out with safe same-sandbox queuing**, not a claim
that local commands executed simultaneously.

## Claim-to-evidence map

| Claim | Evidence |
| --- | --- |
| A real Hosted Skills request completed | Raw Playwright recording, Function request operation, and `live-chat-multitool.png` |
| The agent used MCP plus two local tools | Live UI three-tool counter, returned markers, and same-run dependency spans |
| Foundry model and MCP traffic crossed APIM | Same-window APIM-role dependencies: two successful model POSTs and five successful MCP POSTs |
| Only executable local tools entered ACA Sandbox | Same-run sandbox, file-journal, and shell spans; the model and MCP spans remain outside the sandbox lane |
| One fresh sandbox was cleaned up | Same-run create/delete spans and final typed inventory of zero |
| The request is inspectable as one agent lifecycle | `appinsights-agent-trace.png` and the redacted 68-span dependency export |
| The optimized path reduced measured runtime by 48% | `docs/decisions/0009-hybrid-sandbox-tool-execution-results.json` and the demo-results scene |

Two unsuccessful MCP `GET` probes in the same window are expected stream
close/teardown behavior; the five MCP `POST` requests and both model `POST`
requests succeeded. No request or response bodies were queried or recorded.

## Recording provenance and limitations

- The Hosted Skills interaction is an actual 1920×1080 Playwright recording
  against `func-hybrid-sbx-0902`; it is not a mock or reconstructed animation.
- The Function key was held only in process memory and injected after page
  load. Credential entry, key values, session identifiers, and detail payloads
  are absent from the recording.
- The APIM and sandbox images are genuine Azure portal captures of the retained
  resources. Same-run behavior is established by the correlated telemetry and
  typed inventory, not by exposing resource or sandbox identifiers on screen.
- `appinsights-agent-trace.png` is a content-free visualization generated from
  real Application Insights `AppDependencies` spans. It is not a screenshot of
  the portal Agent Trace blade because the authenticated Edge session was in
  active use and automation failed closed rather than taking over the session.
- Telemetry exports in the external artifact directory are redacted. They omit
  prompts, arguments, results, host targets, subscription IDs, sandbox IDs, and
  custom properties.

## Safe rerun

Prerequisites:

1. Azure CLI authenticated to the subscription containing the retained resource
   group.
2. Python environment with this repository installed with development extras,
   Playwright, and an installed Chromium or Chrome executable.
3. Read access to the existing Function App and ACA Sandbox Group.
4. Empty sandbox inventory before starting. The recorder refuses to run
   otherwise and fails if final inventory is not zero.

From the repository root in PowerShell, use an artifact directory outside git.
The command below reads the existing resource IDs and key without printing the
key or placing it on the command line:

```powershell
$rg = "larohra-test-adc-tools-hosted-skill"
$functionApp = "func-hybrid-sbx-0902"
$sandboxGroup = "sbg-hybrid-tools-0902"
$output = Join-Path $env:TEMP "hybrid-sandbox-live-flow"
$chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
$groupId = az resource show `
  --resource-group $rg `
  --resource-type "Microsoft.App/sandboxGroups" `
  --name $sandboxGroup `
  --query id -o tsv
$env:HYBRID_SPIKE_FUNCTION_KEY = az functionapp keys list `
  --resource-group $rg `
  --name $functionApp `
  --query "functionKeys.default" -o tsv

try {
  python docs\demo\hybrid-sandbox-leadership\record_live_flow.py `
    --output-root $output `
    --chrome $chrome `
    --sandbox-group $groupId `
    --region westus2
} finally {
  Remove-Item Env:\HYBRID_SPIKE_FUNCTION_KEY -ErrorAction SilentlyContinue
  Remove-Variable groupId -ErrorAction SilentlyContinue
}
```

This authorizes exactly one request. Do not enable body logging, change resource
settings, reprovision, or clean up resources manually. If either inventory gate
fails, stop and investigate rather than sending another request.

Supporting scenes can be regenerated without Azure access:

```powershell
python docs\demo\hybrid-sandbox-leadership\record_scenes.py `
  --output-root "$env:TEMP\hybrid-sandbox-support"
```

Use the supplied `playwright-demo-video.skill` scripts to trim the raw browser
recording, stitch the scene manifest, synthesize narration, mix overlays and
audio, create the contact sheet, and inspect the final media. Exact output
hashes and codecs are recorded in `manifest.json`.
