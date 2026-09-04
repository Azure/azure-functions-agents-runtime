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
- `evidence/*.png` - 1920x1080 redacted live product views for Functions, APIM,
  ACA lifecycle, and Application Insights.
- `demo.html` and `record_scenes.py` - deterministic Playwright render source.
- `narration.json` - narration text used for the enhanced cut.
- Final video, silent master, and contact sheet are retained in the session
  artifact directory named in `manifest.json` because generated video is not
  suitable for this repository.

## Recording provenance

The four evidence frames are not Azure Portal mockups. Playwright used a
session-local copy of the already authenticated, device-compliant Edge Work
profile to capture the retained Function App, APIM policy designer, ACA Sandbox
Group, and Application Insights end-to-end transaction. No credential entry was
recorded. The account banner was excluded at capture time. Subscription IDs,
build hashes, InvocationId, ProcessId, HostInstanceId, and other internal
identifiers were then cropped or visibly redacted before the screenshots entered
the repository.

The Application Insights view is correlated to the portal evidence request from
`2026-09-04T00:00:39.9521489Z` through
`2026-09-04T00:01:08.9715532Z`, operation
`f8e1f7aec373b17ab9d1ae1d730b2105`. The exact window produced no APIM
diagnostic row, so the package does not mislabel unrelated gateway traffic as
same-run evidence. APIM screenshots prove the retained live governance
configuration; request counts and lifecycle timings remain tied to the earlier
qualification run identified separately in `manifest.json`.

The enhanced MP4 was recorded as ten independent 1920x1080 Playwright scenes.
Scenes 3-6 animate the redacted product captures with a restrained push-in; the
other scenes preserve the architecture, boundary, and measured qualification
story. The scenes were stitched into a new silent H.264 master and mixed
non-destructively with Windows SAPI narration, visible callouts, and a generated
license-free ambient bed. The original presentation-led cut remains preserved.

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
6. For authenticated portal captures, copy the Edge profile to a session-local
   temporary directory, launch system Edge through Playwright
   `launch_persistent_context`, and exclude the top 70 pixels containing the
   account banner. Never commit or retain the temporary profile. Crop or redact
   subscription IDs, account identity, build hashes, InvocationId, ProcessId, and
   HostInstanceId before publication.
7. Render with:

   ```powershell
   uv run --with playwright python docs\demo\hybrid-sandbox-leadership\record_scenes.py `
     --output-root <session-artifact-folder> `
     --chrome "C:\Program Files\Google\Chrome\Application\chrome.exe"
   ```

8. Stitch the scene manifest, generate narration and the ambient bed, mix a
   separate enhanced output, then inspect a contact sheet and FFprobe output.
   Never overwrite the silent master.

## Claim map

| Claim | Evidence |
| --- | --- |
| Hosted Skill surface is deployed | Live Function App capture; `function-hosted-skill.png` |
| Portal evidence request succeeded | Operation `f8e1f7aec373b17ab9d1ae1d730b2105`; `appinsights-waterfall.png` |
| Foundry model and MCP are governed by APIM | Live APIM policy designer; qualification request counts remain separately attributed; `apim-foundry-mcp.png` |
| Local tool ran through ACA Sandbox | Runtime tool metric plus sandbox create/upload/readiness/tool spans |
| Sandbox inventory returned to zero | Live Sandbox Group portal plus typed final observation in `manifest.json` |
| Telemetry is content-free | Projected metrics and API diagnostic configuration; no body fields in assets |
| Optimized runtime average fell 48.03% | `docs/decisions/0009-hybrid-sandbox-tool-execution-results.json`, n=21 clean window |

The live request revealed one failed nonblocking delete initiation. The media
states this explicitly and attributes eventual zero to the existing
ownership-scoped reaper after its 20-minute orphan threshold, not to prompt
cleanup or an unobserved lifecycle transition. Resource names and correlation
IDs are intentional evidence; keys,
tokens, connection strings, request/response bodies, sandbox IDs, and
subscription/tenant identifiers are excluded.
