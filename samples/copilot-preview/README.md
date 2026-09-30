# Local Copilot preview

This **default-off, local-only** Functions sample exercises non-streaming
Copilot chat and the authored `/preview` HTTP route with `make_receipt` and
`web_request` (limited to `example.com`). It preserves completed-turn sessions
across a local restart. ACA `execute_python` is disabled in the checked-in
configuration; real Copilot-to-ACA execution is not qualified.

Requires Python 3.13/3.14, Azure Functions Core Tools 4, `uv`, an approved
Foundry deployment and Azure sign-in. Model calls incur charges.

## Run (PowerShell, repository root)

`requirements.txt` installs this checkout with `[copilot]`. The SDK downloads
its native runtime on first use if uncached.

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

## Verify

In a **second terminal at the repository root**:

```powershell
.\.venv\Scripts\python.exe samples\copilot-preview\verify.py --phase first --evidence .preview-evidence.json
# Stop and restart `func start --port 7071` in the first terminal, keeping its environment and state.
.\.venv\Scripts\python.exe samples\copilot-preview\verify.py --phase followup --evidence .preview-evidence.json
.\.venv\Scripts\python.exe samples\copilot-preview\verify.py --phase negative
```

Expect `PASS first`, `PASS followup`, and `PASS negative`. To exercise the
authored route while the host runs:

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

To exercise the configured `web_request` tool:

```powershell
$web = Invoke-RestMethod http://127.0.0.1:7071/agents/main/chat -Method Post `
  -ContentType application/json `
  -Body '{"prompt":"Call web_request exactly once with method GET and URL https://example.com/. Summarize its status."}'
$web.tool_calls | ConvertTo-Json -Depth 8
```

This local preview does not support streaming, MCP, skills, delegation,
workflows, Azure hosting, or MAF history import. See
[the architecture guide](../../docs/architecture.md#bounded-copilot-migration-preview)
for the capability boundary.

## Flag off and cleanup

Stop the host; set the flag to `false` and restart to restore MAF. Do not
reuse a Copilot session ID in MAF. After stopping MAF, run `Pop-Location`
to return to the repository root. Review `$run` and the cleanup block before
running it; it removes only that run's state and sample evidence:

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
Remove-Item -LiteralPath $state -Recurse -ErrorAction SilentlyContinue
Remove-Item -LiteralPath .preview-evidence.json, samples\copilot-preview\src\local.settings.json `
  -ErrorAction SilentlyContinue
```

Do not delete SDK caches or MAF history.
