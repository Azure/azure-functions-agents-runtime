# Hybrid ACA Sandbox/APIM leadership demo

This package explains and demonstrates the experimental hybrid execution path:

- Azure Functions hosts the agent endpoint, Hosted Skill, and Microsoft Agent
  Framework model loop.
- Azure API Management governs Foundry model traffic and remote MCP traffic
  through separate APIs.
- One fresh, customer-owned Azure Container Apps Sandbox executes every local
  customer tool call for the top-level invocation.
- Application Insights correlates sandbox creation, package delivery, executor
  readiness, tool execution, and cleanup without recording prompts, tool
  arguments, results, credentials, or sandbox identifiers.

The implementation remains experimental and private. This demo supports a
productization decision; it does not present the retained environment as a
supported production service.

## Assets

- `hybrid-sandbox-leadership-demo.pptx` - six-slide executive deck with speaker
  notes and embedded live-evidence stills.
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

## Presentation package

The six slides move from problem to decision:

1. **Customer-owned sandboxes for every tool call** - the outcome in one
   sentence.
2. **Powerful agents need a safer place to act** - why worker-local customer
   tools create an avoidable blast radius.
3. **One request. Two governed execution planes.** - the end-to-end visual
   architecture.
4. **One fresh sandbox, one bounded invocation** - the measured lifecycle and
   trust controls.
5. **The live proof is visible across Azure** - four views from the same
   correlated invocation.
6. **The optimization cut runtime latency in half** - measured impact and the
   proposed leadership decision.

Speaker notes contain concise narration for every slide.

## Recording provenance

The four evidence frames are not fabricated values or portal mockups. They are
deterministic
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

## Five-minute presentation sequence

| Time | View | What to show |
| --- | --- | --- |
| 0:00-0:30 | Slides 1-2 | State the problem and the split-plane bet. |
| 0:30-1:10 | Slide 3 | Trace the request through Functions, APIM, Foundry/MCP, and ACA Sandbox. |
| 1:10-1:40 | Hosted Skill evidence | Show the successful streaming Function request. |
| 1:40-2:15 | APIM evidence | Show model and MCP traffic through protected APIs. |
| 2:15-2:50 | ACA evidence | Show inventory moving from zero to one and back to zero. |
| 2:50-3:35 | Application Insights | Show the correlated lifecycle waterfall. |
| 3:35-5:08 | Slides 4-6 | Close on controls, measured improvement, and the decision. |

Use the prerecorded run as the primary leadership presentation. Keep the live
environment available for questions and use a fresh request only as a
drill-down. This avoids spending the meeting on portal latency or telemetry
ingestion delay while every recorded claim remains grounded in retained live
evidence.

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

## Demonstration safety

- Use only the retained nonproduction spike resources.
- Start from typed Sandbox Group inventory zero and verify eventual zero after
  the invocation.
- Never record credential entry, subscription keys, tokens, connection strings,
  managed-identity token values, request headers, or environment settings.
- Keep APIM request and response body capture at zero bytes.
- Use the deterministic `customer_probe` prompt so the result contains no
  customer data.
- Prefer resource names and a short request time window over full resource IDs.
- Crop tenant, subscription, user, and machine-specific identifiers that are
  not needed to prove the flow.

## Measured results used in the deck

| Measure | Baseline | Optimized | Change |
| --- | ---: | ---: | ---: |
| Runtime request average | 20.493 s | 10.649 s | 48.03% lower |
| Cold request | 27.706 s | 17.743 s | 35.96% lower |
| c1 p50 | 19.721 s | 9.108 s | 53.81% lower |
| c10 p50 | 27.560 s | 18.468 s | 32.99% lower |
| Package upload average | 5.510 s | 0.135 s | 97.55% lower |
| Executor readiness average | 2.145 s | 0.333 s | 84.47% lower |

All 21 optimized benchmark requests succeeded. The clean window recorded 21
sandbox creates, package uploads, package verifications, local tool calls,
lifecycle handoffs, and accepted delete requests; 42 model calls; zero hybrid
failures; and final typed sandbox inventory zero. APIM model p50 gateway
overhead was approximately 1.3 ms.

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
IDs are intentional evidence. Keys, tokens, connection strings, request or
response bodies, sandbox IDs, and subscription or tenant identifiers are
excluded.
