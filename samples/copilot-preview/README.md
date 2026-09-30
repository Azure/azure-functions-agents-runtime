# Local Copilot preview

This **default-off, local-only** Functions sample exercises non-streaming
Copilot chat and the authored `/preview` HTTP route with `make_receipt` and
`web_request` (limited to `example.com`). It preserves completed-turn sessions
across a local restart. ACA `execute_python` is disabled in the checked-in
configuration.

Requires Python 3.13/3.14, Azure Functions Core Tools 4, and `uv`. Model calls
incur charges. You need one approved target: an OpenAI API key/model, an Azure OpenAI resource
and deployment, or a Foundry project and deployment. Azure Entra callers need
the target data-plane role (for example, Cognitive Services OpenAI User for
Azure OpenAI or the project role approved by your Foundry administrator).

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
$env:FUNCTIONS_WORKER_PROCESS_COUNT = "1"
$run = [Guid]::NewGuid().ToString("N")
$env:AZURE_FUNCTIONS_AGENTS_SESSION_DIR = "$PWD\.preview-state\$run"
if (Test-Path samples\copilot-preview\src\local.settings.json) {
  throw "Do not overwrite local settings."
}
Copy-Item samples\copilot-preview\src\local.settings.template.json `
  samples\copilot-preview\src\local.settings.json
```

Choose exactly one provider block. Values entered with `Read-Host -MaskInput`
remain process environment values; prefer your organization's approved local
secret injection when available. Never commit them.
Provider settings, API keys, and Azure OpenAI API-key-vs-Entra mode are captured
when the Functions app starts; restart the host to rotate them.

### OpenAI API key

```powershell
$env:AZURE_FUNCTIONS_AGENTS_PROVIDER = "openai"
$env:AZURE_FUNCTIONS_AGENTS_MODEL = "<approved-model>"
$env:OPENAI_API_KEY = Read-Host -MaskInput "OpenAI API key"
Remove-Item Env:AZURE_OPENAI_ENDPOINT, Env:AZURE_OPENAI_API_KEY, `
  Env:AZURE_OPENAI_DEPLOYMENT, Env:AZURE_OPENAI_API_VERSION, `
  Env:FOUNDRY_PROJECT_ENDPOINT, Env:FOUNDRY_MODEL -ErrorAction SilentlyContinue
```

### Azure OpenAI API key

The endpoint must be host-only; do not include `/openai`, deployment paths,
query strings, or credentials. Host-only custom HTTPS domains, such as APIM,
are intentionally supported.

```powershell
$env:AZURE_FUNCTIONS_AGENTS_PROVIDER = "azure_openai"
$env:AZURE_OPENAI_ENDPOINT = "https://<resource>.openai.azure.com"
$env:AZURE_OPENAI_DEPLOYMENT = "<approved-deployment>"
$env:AZURE_OPENAI_API_KEY = Read-Host -MaskInput "Azure OpenAI API key"
# Optional only when the resource requires a dated API version:
# $env:AZURE_OPENAI_API_VERSION = "2024-10-21"
Remove-Item Env:OPENAI_API_KEY, Env:FOUNDRY_PROJECT_ENDPOINT, Env:FOUNDRY_MODEL `
  -ErrorAction SilentlyContinue
```

### Azure OpenAI Entra

Use an approved developer credential. `DefaultAzureCredential` supports the
Azure CLI login below; `AZURE_CLIENT_ID` may select an approved identity where
your credential policy requires it. The Entra scope targets public Azure cloud
only; sovereign clouds are unsupported.

```powershell
az login --tenant "<tenant-id>"
az account set --subscription "<subscription-id>"
$env:AZURE_FUNCTIONS_AGENTS_PROVIDER = "azure_openai"
$env:AZURE_OPENAI_ENDPOINT = "https://<resource>.openai.azure.com"
$env:AZURE_OPENAI_DEPLOYMENT = "<approved-deployment>"
Remove-Item Env:AZURE_OPENAI_API_KEY, Env:OPENAI_API_KEY, `
  Env:FOUNDRY_PROJECT_ENDPOINT, Env:FOUNDRY_MODEL -ErrorAction SilentlyContinue
```

### Foundry project Entra

```powershell
az login --tenant "<tenant-id>"
az account set --subscription "<subscription-id>"
$env:AZURE_FUNCTIONS_AGENTS_PROVIDER = "foundry"
$env:FOUNDRY_PROJECT_ENDPOINT = "https://<resource>.services.ai.azure.com/api/projects/<project>"
$env:FOUNDRY_MODEL = "<approved-deployment>"
Remove-Item Env:OPENAI_API_KEY, Env:AZURE_OPENAI_ENDPOINT, `
  Env:AZURE_OPENAI_API_KEY, Env:AZURE_OPENAI_DEPLOYMENT, `
  Env:AZURE_OPENAI_API_VERSION -ErrorAction SilentlyContinue
```

Start the host:

```powershell
Push-Location samples\copilot-preview\src
func start --port 7071
```

Startup logs `harness=copilot`. Invalid provider settings fail startup, and
request-time credential failures return a sanitized `error` response; neither
prints credentials.

## Verify

In a **second terminal at the repository root**:

```powershell
.\.venv\Scripts\python.exe samples\copilot-preview\verify.py --phase first --evidence .preview-evidence.json
# Stop/restart `func start --port 7071`, preserving provider variables and state.
.\.venv\Scripts\python.exe samples\copilot-preview\verify.py --phase followup --evidence .preview-evidence.json
.\.venv\Scripts\python.exe samples\copilot-preview\verify.py --phase negative
```

Expect `PASS first`, `PASS followup`, and `PASS negative`. To exercise the
authored route while the host runs:

```powershell
$first = Invoke-RestMethod http://127.0.0.1:7071/agents/main/chat -Method Post `
  -ContentType application/json `
  -Body '{"prompt":"Call make_receipt once with tag ''demo''. Reply only with its result."}'
$first.tool_calls
$again = Invoke-WebRequest http://127.0.0.1:7071/preview -UseBasicParsing -Method Post `
  -ContentType application/json -Headers @{"x-ms-session-id"=$first.session_id} `
  -Body '{"prompt":"Recall the previous receipt without calling any tool."}'
$again.Content
$again.Headers["x-ms-session-id"]
```

To exercise the configured `web_request` tool:

```powershell
$web = Invoke-RestMethod http://127.0.0.1:7071/agents/main/chat -Method Post `
  -ContentType application/json `
  -Body '{"prompt":"Call web_request exactly once with method GET and URL https://example.com/. Summarize its status."}'
$web.tool_calls | ConvertTo-Json -Depth 8
```

This local preview does not support streaming, MCP, skills, delegation,
workflows, Azure hosting, or MAF history import. Custom `ClientManager`
instances are MAF-only and are rejected when Copilot is on. See
[the architecture guide](../../docs/architecture.md#bounded-copilot-migration-preview)
for the capability boundary.

## Flag off and cleanup

Stop the host; set the flag to `false` and restart to restore MAF. Do not
reuse a Copilot session ID in MAF. After stopping MAF, run `Pop-Location`
to return to the repository root. Review `$run` and the cleanup block before
running it; it removes only that run's state and sample evidence:

```powershell
# Stop the host.
$env:AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT = "false"
func start --port 7071
# Stop the host again.
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
Remove-Item -LiteralPath .preview-evidence.json, `
  samples\copilot-preview\src\local.settings.json -ErrorAction SilentlyContinue
Remove-Item Env:OPENAI_API_KEY, Env:AZURE_OPENAI_API_KEY -ErrorAction SilentlyContinue
```

Do not delete SDK caches or MAF history.
