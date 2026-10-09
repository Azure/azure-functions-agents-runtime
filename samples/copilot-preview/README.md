# Local Copilot preview

This **default-off** Functions sample exercises Copilot HTTP chat and
the authored `/preview` HTTP route with `make_receipt` and `web_request`
(limited to `example.com`). It also includes the public Microsoft Learn MCP
server and a `preview-check` skill with a reference file. The SDK owns sessions and their opaque files;
the host supplies filesystem callbacks backed by local files or Blob.
Run locally with one worker only. ACA `execute_python` is disabled in the
checked-in configuration.

Requires Python 3.13/3.14, Azure Functions Core Tools 4, and `uv`. Model calls
incur charges. You need one approved target: an OpenAI API key/model, an Azure OpenAI resource
and deployment, or a Foundry project and deployment. Azure Entra callers need
the target data-plane role (for example, Cognitive Services OpenAI User for
Azure OpenAI or the project role approved by your Foundry administrator).

`requirements.txt` installs this checkout with `[copilot]`, which pins
`github-copilot-sdk==1.0.16`. The SDK downloads its native runtime on first
use if uncached. On Windows, same-drive `session-state` callbacks use the
configured session storage, not a physical directory at the drive root.
The sample needs outbound HTTPS access to `learn.microsoft.com`. This public
MCP server needs no API key. The skill check reads a local file and runs no script.

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

If port 7071 is in use, choose a free port and use it in every test URL.
Do not stop another host to free the port.

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

### Check the skill and MCP

The checked-in agent enables both capabilities. No extra fixture or frontmatter
edit is needed. Each check creates a separate conversation. Model calls incur
charges; the MCP check also calls the public Microsoft Learn service.

In the second terminal at the repository root, use the same Python environment:

```powershell
python samples\copilot-preview\verify.py --base-url http://127.0.0.1:7071 --phase capabilities
```

The verifier requires successful tool evidence, not only a model response:

- **Skill:** native `skill` and `view` both succeed. `view` reads this checkout's
  `skills/preview-check/references/check.txt` and returns `REFERENCE_READ_7C42A9`.
  The response contains that marker and `SKILL_LOADED_PREVIEW_CHECK`.
- **MCP:** `microsoft_docs_search` succeeds and returns a Microsoft Learn link.
  The response includes a link from the search result.

Use `--phase skill` or `--phase mcp` to run one check. The existing `--phase all`
still runs the receipt, follow-up, and negative checks.

For direct HTTP inspection:

```powershell
$body = @{
  prompt = "Run the preview-check skill test. Load the skill, then use view to read its references/check.txt. Return the skill marker and the exact file marker. Do not guess or use other tools."
} | ConvertTo-Json
$skill = Invoke-RestMethod http://127.0.0.1:7071/agents/main/chat -Method Post `
  -ContentType application/json -Body $body -TimeoutSec 180
$skill | ConvertTo-Json -Depth 12

$body = @{
  prompt = "For an MCP check, use microsoft_docs_search on microsoft-learn to search for the Azure Functions Python programming model. Include a Microsoft Learn link from the result. Do not use other tools."
} | ConvertTo-Json
$mcp = Invoke-RestMethod http://127.0.0.1:7071/agents/main/chat -Method Post `
  -ContentType application/json -Body $body -TimeoutSec 180
$mcp | ConvertTo-Json -Depth 12
```

The reference marker appears only in the file, not in the test prompt or skill
instructions. Do not accept a guessed marker or missing/failed tool evidence.
For a large MCP search result, the verifier permits `view` of only the saved
file reported by a successful search under `/session-state/temp/`. All tool
calls must succeed. A failed first search can be followed by a successful
retry, but that still fails this check. See
[qualification scope](../../docs/copilot-preview-operations.md#qualification-scope).
Keep MCP queries limited to public documentation. Do not send local files,
conversation content, or credentials to the public server.

**Temporary MCP mitigation:** the sample instructs the model to retry a failed
read-only Learn search once with the same arguments. This is a sample-only
mitigation for the SDK/native catalog failure tracked in
[#261](https://github.com/Azure/azure-functions-agents-runtime/issues/261).
The failure is reported by the SDK's native runtime; its cause and ownership
are not yet confirmed. The instruction does not guarantee a retry or success.
Other MCP tools must not be retried. Failed calls remain visible, and the
strict verifier still fails if the first call fails, even when the retry succeeds.

The receipt and `web_request` behavior remains unchanged. To run without the
public MCP service, set the top-level `mcp: false` in `src/main.agent.md` and
restart the host. Set `skills: false` to disable the skill check. Do not change
the nested `builtin_endpoints.mcp` field: that field controls an inbound endpoint,
not the outbound server. See the [MCP and scoped skills guide](../../docs/copilot-preview-operations.md#mcp-and-scoped-skills).

The local adapter also supports SSE chat, declared chat delegates, Workflow
Sub Agents, and Dynamic Workflow management when authored in an agent app.
This sample remains a single-agent tool demonstration and does not configure
those optional capabilities. Deployed hosting remains unsupported in this
preview. The built-in debug chat UI and non-HTTP triggers are enabled but not
live-qualified; the UI does not restore earlier transcript messages when you
resume a session. Custom `ClientManager`
instances are MAF-only and are rejected when Copilot is on. See
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
