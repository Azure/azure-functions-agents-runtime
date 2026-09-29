# Local Copilot preview

This **default-off, local-only** Functions sample runs `main.agent.md` through
discovery, HTTP registration and the Copilot SDK (1.0.14, native 1.0.85,
protocol 3, stdio). It supports the primary direct, non-streaming built-in chat
and authored `/preview` routes; filtered explicit tools; configured
`web_request`; optional session-bound ACA `execute_python` adapter wiring;
host-owned structured validation on authored HTTP-trigger routes; and SDK-owned
local completed-turn continuity. The checked-in policy exposes deterministic
`make_receipt` plus `web_request` restricted to `example.com`; ACA remains opted
out. The ACA catalog/session wiring is unit-qualified, but no repository test
currently makes a real Copilot-to-ACA call. Use Python 3.13/3.14, Core Tools 4
and an approved Foundry deployment with Azure sign-in. Model and optional ACA
calls incur charges; setup does not prove project or pool access.

Copilot empty mode exposes only the host's explicit catalog. Ambient SDK
shell/file/web/todo/task/human-input tools, config discovery and tool search are
disabled. Duplicate names and unsupported MAF-only tool policy fail before
native startup. Tool exceptions are recoverable to the model; request
cancellation propagates.

## Run (PowerShell, repository root)

`requirements.txt` installs this checkout with `[copilot]`. It installs the
Python SDK, **not** native assets: the SDK downloads its pinned runtime on first
enabled client construction if uncached, so the first request may be slower.
There is no separate CLI setting or download step.

```powershell
uv venv .venv --python 3.13
Push-Location samples\copilot-preview\src
uv pip install --python ..\..\..\.venv\Scripts\python.exe -r requirements.txt
Pop-Location
$env:VIRTUAL_ENV = "$PWD\.venv"
$env:PATH = "$env:VIRTUAL_ENV\Scripts;$env:PATH"
$env:AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT = "true"
$env:AZURE_FUNCTIONS_AGENTS_PROVIDER = "foundry"
$env:FOUNDRY_PROJECT_ENDPOINT = "https://<resource>.services.ai.azure.com/api/projects/<project>"
$env:FOUNDRY_MODEL = "<approved-deployment-name>"
$env:FUNCTIONS_WORKER_PROCESS_COUNT = "1"
$run = [Guid]::NewGuid().ToString("N")
$env:AZURE_FUNCTIONS_AGENTS_SESSION_DIR = "$PWD\.preview-state\$run"
if (Test-Path samples\copilot-preview\src\local.settings.json) { throw "Do not overwrite local settings." }
Copy-Item samples\copilot-preview\src\local.settings.template.json samples\copilot-preview\src\local.settings.json
Push-Location samples\copilot-preview\src
func start --port 7071
```

Expect `Agent harness selected: harness=copilot`, followed on first request by
`Copilot tool catalog verified: custom_tool_count=2`. For safe version
diagnostics, run `func --version` and
`.\.venv\Scripts\python.exe -c "import importlib.metadata as m; print(m.version('github-copilot-sdk'))"`
from the repository root. Do not print environment variables, tokens, request
content or native session files for diagnostics.

## Verify

In a **second terminal at the repository root**, use an approved deployment:

```powershell
.\.venv\Scripts\python.exe samples\copilot-preview\verify.py --phase first --evidence .preview-evidence.json
# Stop and restart `func start --port 7071` in the first terminal, keeping its environment and state.
.\.venv\Scripts\python.exe samples\copilot-preview\verify.py --phase followup --evidence .preview-evidence.json
.\.venv\Scripts\python.exe samples\copilot-preview\verify.py --phase negative
```

Expect `PASS first` (one `make_receipt` call), `PASS followup` (same session,
no tool), `PASS negative` (unknown ID fails; stream/history return 501).
To exercise the authored route while the host runs:

```powershell
$first = Invoke-RestMethod http://127.0.0.1:7071/agents/main/chat -Method Post `
  -ContentType application/json -Body '{"prompt":"Call make_receipt once with tag ''demo''. Reply only with its result."}'
$first.tool_calls
$again = Invoke-WebRequest http://127.0.0.1:7071/preview -UseBasicParsing -Method Post `
  -ContentType application/json -Headers @{"x-ms-session-id"=$first.session_id} `
  -Body '{"prompt":"Recall the previous receipt without calling any tool."}'
$again.Content
$again.Headers["x-ms-session-id"] # Matches $first.session_id.
```

The authored `/preview` route keeps its existing body and `x-ms-session-id`
behavior. On authored HTTP-trigger routes only, `input_schema`,
`response_example`, and `response_schema` parsing/validation remain host-owned;
the SDK does not replace that validation. The built-in
`/agents/main/chat` route instead validates its own `prompt` envelope and
returns `{session_id, response, tool_calls}`. It does **not** apply the agent's
authored input or response schemas.

### Host-tool requests and opt-outs

The checked-in `agents.config.yaml` restricts `web_request` to
`https://example.com`. To collect optional real web-tool evidence while the host
runs:

```powershell
$web = Invoke-RestMethod http://127.0.0.1:7071/agents/main/chat -Method Post `
  -ContentType application/json `
  -Body '{"prompt":"Call web_request exactly once with method GET and URL https://example.com/. Summarize its status."}'
$web.tool_calls | ConvertTo-Json -Depth 8
# Expect exactly web_request; another host is rejected by the configured policy.
```

`web_request` is normally default-on. Set `system_tools.web_request: false` in
`agents.config.yaml` to disable it app-wide, or set the same Boolean in one
agent's front matter to opt out there. `tools: false` disables explicit user
tools and both host system tools for that agent.

This slice wires the existing ACA tool into the Copilot catalog and unit-tests
that its closure receives the public HTTP session ID. Existing sandbox tests
stub ACA execution; the agentic E2E suite does not exercise Copilot-to-ACA.
Real ACA authentication, transport, result, and error behavior therefore remain
a separately gated acceptance item and are not production-qualified here.

For an exploratory manual check with an existing Dynamic Sessions pool, change
the agent's `dynamic_sessions_code_interpreter` override to `true`, add the
following app-level configuration, restart, and ask the built-in chat endpoint
to calculate `6 * 7` with `execute_python`:

```yaml
system_tools:
  dynamic_sessions_code_interpreter:
    endpoint: https://<pool>.<region>.dynamicsessions.io
    client_id: <optional-user-assigned-managed-identity-client-id>
  web_request:
    allowed_hosts: [example.com]
```

If the exploratory call succeeds, inspect its `tool_calls` for one
`execute_python` result containing `42`. The host log's `aca_session` field
should match the public `x-ms-session-id` without exposing code or credentials.
This manual observation does not close the gated ACA acceptance item. Restore
the checked-in `false` override afterward. This sample neither creates nor
deletes the ACA pool.

Streaming, debug UI/history projection, non-HTTP triggers, MCP, skills,
delegation, Workflow Sub Agents, workflows-enabled agents and workflow
management remain explicitly unsupported. Azure hosting, multiple workers,
Azure Blob/distributed persistence, MAF history import, native compaction and
interrupted-turn recovery are also unqualified. Stream/history return 501;
unsupported configured capabilities fail before inference. Configured
`max_output_tokens` is rejected instead of silently ignored.

## Flag off and cleanup

Stop the host; in its terminal set the flag to `false` and start it again.
MAF is restored (history no longer returns 501). Do not reuse a Copilot ID.
After stopping MAF, run `Pop-Location` to return to the repository root.

```powershell
$env:AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT = "false"
func start --port 7071
# Stop the host.
Pop-Location
if ($run -notmatch '^[a-f0-9]{32}$' -or
    -not (Test-Path -LiteralPath .\samples\copilot-preview\src\main.agent.md)) {
    throw "Cleanup requires the original run ID at this sample's repository root."
}
$state = Join-Path (Get-Location).ProviderPath ".preview-state\$run"
$parent = Get-Item -LiteralPath (Split-Path $state) -ErrorAction SilentlyContinue
$target = Get-Item -LiteralPath $state -ErrorAction SilentlyContinue
$target | Select-Object FullName, LinkType, Target
if (@($parent) + @($target) | Where-Object {
    $null -ne $_ -and (-not $_.PSIsContainer -or
        ($_.Attributes -band [IO.FileAttributes]::ReparsePoint))
}) { throw "Refusing to remove a file or linked directory." }
# Inspect the printed path before removing only this run's state.
Remove-Item -LiteralPath $state -Recurse -ErrorAction SilentlyContinue
Remove-Item -LiteralPath .preview-evidence.json, samples\copilot-preview\src\local.settings.json `
  -ErrorAction SilentlyContinue
```

Do not delete SDK caches or MAF history. At `14c9c956` on Windows, an approved
Foundry/Entra run passed the receipt, full host restart and negative checks;
this does not qualify Azure hosting or production use.
