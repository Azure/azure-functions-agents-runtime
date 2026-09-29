# Local Copilot preview

This **default-off, local-only** Functions sample runs `main.agent.md` through
discovery, HTTP registration and the Copilot SDK (1.0.14, native 1.0.85,
protocol 3, stdio). It supports non-streaming built-in chat, the authored
`/preview` route, one `make_receipt` tool and SDK-owned local sessions.
Use Python 3.13/3.14, Core Tools 4 and an approved Foundry deployment with
Azure sign-in. Model calls incur charges; setup does not prove project access.

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

Expect `Agent harness selected: harness=copilot`. For safe version diagnostics,
run `func --version` and
`.\.venv\Scripts\python.exe -c "import importlib.metadata as m; print(m.version('github-copilot-sdk'))"`
from the repository root.

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

No Azure hosting, multiple workers, MAF history import, streaming, MCP,
skills, delegation, workflows, or interrupted-turn recovery guarantee.
Configured `max_output_tokens` is rejected instead of silently ignored.

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
