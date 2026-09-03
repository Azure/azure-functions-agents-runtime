# Hybrid ACA Sandbox/APIM leadership demo

This package presents live evidence from the retained hybrid spike without
changing or tearing down any Azure resource. The edited video follows the
request path from the Azure Functions Hosted Skill through isolated APIM model
and MCP APIs, into a fresh ACA Sandbox for local tool execution, and back through
content-free Application Insights telemetry and lifecycle cleanup.

## Assets

- `storyboard.md` - 5:08 scene and narration plan.
- `manifest.json` - exact request, telemetry window, trace IDs, stage metrics,
  inventory observations, and asset provenance.
- `evidence/*.png` - 1920x1080 stills for Functions, APIM, ACA lifecycle, and
  Application Insights.
- `demo.html` and `record_scenes.py` - deterministic Playwright render source.
- `narration.json` - narration text used for the enhanced cut.
- Final video, silent master, and contact sheet are retained in the session
  artifact directory named in `manifest.json` because generated video is not
  suitable for this repository.

## Recording provenance

The four evidence frames are not Azure Portal mockups. They are deterministic
Playwright-rendered evidence panels populated from the retained deployment's
read-only Azure CLI, Application Insights, APIM diagnostic, and typed ACA SDK
query results. The exact values and correlation window are preserved in
`manifest.json`. This approach avoided exporting or reusing authenticated portal
SSO state and removed portal chrome, tenant identifiers, unrelated activity,
and any chance of recording credential entry.

The final MP4 was recorded as ten independent 1920x1080 Playwright scenes,
stitched into a silent H.264 master, then enhanced non-destructively with Windows
SAPI narration, visible captions/callouts, and a generated license-free ambient
bed. Asset hashes, codecs, dimensions, duration, and audio levels are in the
manifest.

## Safe rerun

Prerequisites are Azure CLI authenticated to `Private Test Sub LAROHRA`, `uv`,
Chrome, FFmpeg/FFprobe, and Windows SAPI voices. The deployment must already
exist; this package does not provision or configure it.

1. Verify typed Sandbox Group inventory is zero with
   `AcaSandboxAdapter.list_sandboxes(labels={})`.
2. Retrieve the Function host key into process memory only. Do not print or
   persist it.
3. Run exactly one streaming request against
   `https://func-hybrid-sbx-0902.azurewebsites.net/agents/main/chatstream` using
   prompt ID `customer-probe-leadership-demo-repeat-3`; discard body content and
   retain only status, timing, and terminal event.

   ```powershell
   $key = az functionapp keys list `
     --resource-group larohra-test-adc-tools-hosted-skill `
     --name func-hybrid-sbx-0902 --query "functionKeys.default" -o tsv
   $env:HYBRID_SPIKE_FUNCTION_KEY = $key.Trim()
   try {
     uv run python eng\scripts\hybrid_spike_benchmark.py `
       --url "https://func-hybrid-sbx-0902.azurewebsites.net/agents/main/chatstream" `
       --prompt "Call customer_probe with message leadership-demo and repeat 3. Return its JSON." `
       --output "<session-artifact-folder>\invocation.json" `
       --concurrency 1 --requests 1 --stream --timeout 180
   }
   finally {
     Remove-Item Env:HYBRID_SPIKE_FUNCTION_KEY -ErrorAction SilentlyContinue
     $key = $null
   }
   ```
4. Query Application Insights by the exact UTC window in `manifest.json`.
   Project only request status, correlation, durations, APIM API IDs, and hybrid
   metrics. Do not query or enable body fields.
5. Poll typed Sandbox Group inventory without deleting anything until it returns
   to zero. A failed nonblocking delete initiation must rely on the already
   configured lifecycle/reaper backstop.
6. Render with:

   ```powershell
   uv run --with playwright python docs\demo\hybrid-sandbox-leadership\record_scenes.py `
     --output-root <session-artifact-folder> `
     --chrome "C:\Program Files\Google\Chrome\Application\chrome.exe"
   ```

7. Stitch the scene manifest, generate narration and the ambient bed, mix a
   separate enhanced output, then inspect a contact sheet and FFprobe output.
   Never overwrite the silent master.

## Claim map

| Claim | Evidence |
| --- | --- |
| Hosted Skill request succeeded | `manifest.json` live request status, terminal event, Function operation |
| Foundry model and MCP used APIM | Live APIM request counts in the exact telemetry window; `apim-foundry-mcp.png` |
| Local tool ran through ACA Sandbox | Runtime tool metric plus sandbox create/upload/readiness/tool spans |
| Scheduled reaper returned inventory to zero | Typed pre/active/final observations in `manifest.json` |
| Telemetry is content-free | Projected metrics and API diagnostic configuration; no body fields in assets |
| Optimized runtime average fell 48.03% | `docs/decisions/0009-hybrid-sandbox-tool-execution-results.json`, n=21 clean window |

The live request revealed one failed nonblocking delete initiation. The media
states this explicitly and attributes eventual zero to the existing
ownership-scoped reaper after its 20-minute orphan threshold, not to prompt
cleanup or an unobserved lifecycle transition. Resource names and correlation
IDs are intentional evidence; keys,
tokens, connection strings, request/response bodies, sandbox IDs, and
subscription/tenant identifiers are excluded.
