# Local Copilot preview

This **default-off** Functions sample exercises Copilot HTTP chat and
the authored `/preview` HTTP route with `make_receipt` and `web_request`
(limited to `example.com`). The SDK owns sessions and their opaque files;
the host supplies filesystem callbacks backed by local files or Blob.
Run locally with one worker only. ACA `execute_python` is disabled in the
checked-in configuration.

Requires Python 3.13/3.14, Azure Functions Core Tools 4, and `uv`. Model calls
incur charges. You need one approved target: an OpenAI API key/model, an Azure OpenAI resource
and deployment, or a Foundry project and deployment. Azure Entra callers need
the target data-plane role (for example, Cognitive Services OpenAI User for
Azure OpenAI or the project role approved by your Foundry administrator).

`requirements.txt` installs this checkout with `[copilot]`. The SDK downloads
its native runtime on first use if uncached.

For the local-file walkthrough, use a terminal without `AzureWebJobsStorage`
or `AzureWebJobsStorage__blobServiceUri` configured and leave those settings
unconfigured in the sample's `local.settings.json`. Either existing setting
automatically selects Blob, even locally; failures do not fall back to disk.
Optional approved Blob setup is in the
[operations guide](../../docs/copilot-preview-operations.md#storage-selection).

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
$first = Invoke-RestMethod http://127.0.0.1:7071/agents/main/chat -Method Post `
  -ContentType application/json `
  -Body '{"prompt":"Call make_receipt once with tag ''demo''. Reply only with its result."}'
$first | ConvertTo-Json -Depth 8
```

Check that `tool_calls` includes `make_receipt` and save the returned
`session_id` in `$first`. Stop and restart `func start --port 7071` in the
host terminal, preserving its provider settings and
`AZURE_FUNCTIONS_AGENTS_SESSION_DIR`. Then, in the same second terminal,
exercise the authored route with that session:

```powershell
$again = Invoke-WebRequest http://127.0.0.1:7071/preview -UseBasicParsing -Method Post `
  -ContentType application/json -Headers @{"x-ms-session-id"=$first.session_id} `
  -Body '{"prompt":"Recall the previous receipt without calling any tool."}'
$again.Content
$again.Headers["x-ms-session-id"]
```

Check for the previous receipt in the response and the same session ID in the
header. This exercises SDK continuation; it is not a host recovery or
compaction guarantee.

To exercise the configured `web_request` tool:

```powershell
$web = Invoke-RestMethod http://127.0.0.1:7071/agents/main/chat -Method Post `
  -ContentType application/json `
  -Body '{"prompt":"Call web_request exactly once with method GET and URL https://example.com/. Summarize its status."}'
$web.tool_calls | ConvertTo-Json -Depth 8
```

Optional outbound MCP servers and project skills use the existing authoring
and per-agent filters; see the [MCP and scoped skills guide](../../docs/copilot-preview-operations.md#mcp-and-scoped-skills)
before enabling them. This walkthrough requires neither and keeps the receipt
and `web_request` behavior unchanged.

The local adapter also supports SSE chat, declared chat delegates, Workflow
Sub Agents, and Dynamic Workflow management when authored in an agent app.
This sample remains a single-agent tool demonstration and does not configure
those optional capabilities. Deployed hosting remains unsupported in this
preview. The built-in debug chat UI and non-HTTP triggers are enabled but not
live-qualified; the UI does not restore earlier transcript messages when you
resume a session. Provider selection still comes from the shared
`harness/_provider_config.py` rules, while SDK client construction stays inside
the selected harness adapter. See
[the architecture guide](../../docs/architecture.md#bounded-copilot-migration-preview)
for the capability boundary.

## Flag off and cleanup

The flag is read once per app, so stop the host, set the flag to `false` and
restart to use MAF and its own history provider. When finished with the sample,
keep the host stopped. In the original host terminal, return to the repository
root and inspect only this run's files before cleanup:

```powershell
# Stop the host.
Pop-Location
$env:AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT = "false"
if (Test-Path -LiteralPath $env:AZURE_FUNCTIONS_AGENTS_SESSION_DIR) {
  Get-ChildItem -LiteralPath $env:AZURE_FUNCTIONS_AGENTS_SESSION_DIR -Recurse -File |
    Select-Object FullName, Length, LastWriteTimeUtc
}
Remove-Item -LiteralPath .\samples\copilot-preview\src\local.settings.json
Remove-Item Env:OPENAI_API_KEY, Env:AZURE_OPENAI_API_KEY -ErrorAction SilentlyContinue
```

Remove only the sample-owned files after reviewing the dedicated run directory;
do not delete SDK caches, MAF history or shared Blob containers. Native files
may contain conversation content, so do not publish them in diagnostics.
For storage configuration, errors and targeted cleanup, see
[`docs/copilot-preview-operations.md`](../../docs/copilot-preview-operations.md).
