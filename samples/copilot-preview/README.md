# Local Copilot preview

This **default-off** Functions sample exercises non-streaming Copilot chat and
the authored `/preview` HTTP route with `make_receipt` and `web_request`
(limited to `example.com`). It preserves completed-turn sessions across a local
restart, storing each session as one `state.json` file under
`$env:AZURE_FUNCTIONS_AGENTS_SESSION_DIR`. Run it locally only: Azure Blob
session storage has real-service protocol and replacement-process
qualification, but deployed Functions hosting is not qualified. ACA
`execute_python` is disabled in the checked-in configuration; real
Copilot-to-ACA execution is not qualified.

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
$env:AZURE_FUNCTIONS_AGENTS_COPILOT_SESSION_STORAGE = "local"
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
```

Expect `PASS first` and `PASS followup`. Run the negative checks directly:

```powershell
.\.venv\Scripts\python.exe samples\copilot-preview\verify.py --phase negative
```

Expect `PASS negative`; the unknown session check requires HTTP 409 with
`Native session has no completed turn to resume.`.

## Optional disposable Blob / Azurite verification

These commands are **opt-in** and are not part of the local-only walkthrough.
Use a pre-existing, dedicated disposable container and an explicitly selected
storage target: **either** a connection string (Azurite is fine) **or** an
`https` Blob service URI authenticated with Microsoft Entra ID through
`DefaultAzureCredential` (for accounts with shared-key access disabled). Never
point them at customer session storage. The tests and verifier never create
containers. The integration tests use a fresh random app namespace on each run,
create only session `state.json` objects, and delete only those exact objects;
storage versions, soft-deleted copies and snapshots may be retained by the
service. No native SDK process, model or Functions host is needed for these
storage-protocol tests:

Connection string (for example Azurite):

```powershell
$env:AZURE_FUNCTIONS_AGENTS_TEST_DISPOSABLE_BLOB = "1"
$env:AZURE_FUNCTIONS_AGENTS_TEST_BLOB_CONNECTION_STRING = $env:AzureWebJobsStorage
$env:AZURE_FUNCTIONS_AGENTS_TEST_BLOB_CONTAINER = "<pre-existing-disposable-container>"
.\.venv\Scripts\python.exe -m pytest tests\test_copilot_session_fs_integration.py -q -rs
```

Microsoft Entra ID (no keys or SAS): sign in with an identity that has
**Storage Blob Data Contributor** on the disposable container or account
(`DefaultAzureCredential` picks up `az login`), and set exactly one target:

```powershell
az login
Remove-Item Env:AZURE_FUNCTIONS_AGENTS_TEST_BLOB_CONNECTION_STRING -ErrorAction SilentlyContinue
$env:AZURE_FUNCTIONS_AGENTS_TEST_DISPOSABLE_BLOB = "1"
$env:AZURE_FUNCTIONS_AGENTS_TEST_BLOB_SERVICE_URI = "https://<account>.blob.core.windows.net"
$env:AZURE_FUNCTIONS_AGENTS_TEST_BLOB_CONTAINER = "<pre-existing-disposable-container>"
.\.venv\Scripts\python.exe -m pytest tests\test_copilot_session_fs_integration.py -q -rs
```

Setting both target variables, an `http` URI or a URI with a query string
(SAS) fails the tests instead of guessing.

For a **real SDK/model completed-turn cold restore**, configure the same
disposable container for the sample host and run the verifier with a fresh
evidence filename. This restarts the local Functions host (and its native
process) between turns; the first turn uses a real tool and the second recalls
its result without restating it. It incurs model charges. Keep the Foundry
or other chosen provider settings from the setup above; set `AzureWebJobsStorage` in the process
environment to your disposable storage connection and remove the **blank**
`AzureWebJobsStorage` entry from the untracked
`src\local.settings.json` if it shadows that environment variable. Do not
commit credentials or populate the checked-in template.

```powershell
$env:AZURE_FUNCTIONS_AGENTS_COPILOT_SESSION_STORAGE = "blob"
$env:AZURE_FUNCTIONS_AGENTS_SESSION_CONTAINER = $env:AZURE_FUNCTIONS_AGENTS_TEST_BLOB_CONTAINER
.\.venv\Scripts\python.exe samples\copilot-preview\verify.py --restart-host `
  --evidence .preview-blob-evidence.json
```

For Entra ID instead, use the identity-based Functions setting and **no**
connection string (a connection string always wins over `blobServiceUri`).
The verifier requires exactly one of the two and rejects a non-blank
`AzureWebJobsStorage` or a different `AzureWebJobsStorage__blobServiceUri`
in `src\local.settings.json`; the host and worker authenticate with
`DefaultAzureCredential` (your `az login`):

```powershell
Remove-Item Env:AzureWebJobsStorage -ErrorAction SilentlyContinue
$env:AzureWebJobsStorage__blobServiceUri = "https://<account>.blob.core.windows.net"
$env:AZURE_FUNCTIONS_AGENTS_TEST_DISPOSABLE_BLOB = "1"
$env:AZURE_FUNCTIONS_AGENTS_COPILOT_SESSION_STORAGE = "blob"
$env:AZURE_FUNCTIONS_AGENTS_SESSION_CONTAINER = "<pre-existing-disposable-container>"
.\.venv\Scripts\python.exe samples\copilot-preview\verify.py --restart-host `
  --evidence .preview-blob-evidence.json
```

This driver does **not** delete the model conversation: use the saved
`session_id` from the evidence file and `state_name(route, "main", session_id)`
as in the inspection example below to identify that *one* test-owned blob,
then remove it manually only after the host stops. Native compaction is not
forced by this two-turn driver. Separate #1335 qualification demonstrated
semantic compacted-summary reuse across fresh Python/native processes; its
forced threshold was qualification-only and is not user-facing configuration.

To inspect the stored session (read-only) after `PASS first`, from a terminal
with the same `AZURE_FUNCTIONS_AGENTS_SESSION_DIR`:

```powershell
.\.venv\Scripts\python.exe -c @"
from pathlib import Path
from azure_functions_agents._native_session_identity import resolve_route, state_name
route = resolve_route(Path('samples/copilot-preview/src').resolve())
print(route.local_dir / state_name(route, 'main', '<session-id>'))
"@
```

Read that file with `Get-Content ... | ConvertFrom-Json`; `state` should be
`ready` after a completed turn. Do not hand-edit it — a broken integrity digest
or version field makes the session unresumable by design.

To exercise the authored route while the host runs:

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
workflows, or MAF history import. Blob storage is qualified for the documented
#1335 protocol and replacement-process flows; Azure Functions hosting remains
unqualified. Custom `ClientManager`
instances are MAF-only and are rejected when Copilot is on. See
[the architecture guide](../../docs/architecture.md#bounded-copilot-migration-preview)
for the capability boundary.

## Flag off and cleanup

The flag is read once per app, so stop the host, set the flag to `false` and
restart to restore MAF. Switching harnesses migrates nothing: a session ID with
native state is rejected on the MAF path (and vice versa), so use fresh session
IDs after switching. The runtime never expires or prunes sessions for you and
exposes no delete API; remove a session as a whole unit (its `state.json` and
`state.lock`) while no turn is running. After stopping MAF, run `Pop-Location`
to return to the repository root. Review `$run` and the cleanup block before
running it; it removes only that run's native session state and sample evidence:

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

Do not delete SDK caches or MAF history. The default walkthrough uses local
storage; for the optional Blob flow, storage settings, error codes, versioning
and retention, see
[`docs/copilot-preview-operations.md`](../../docs/copilot-preview-operations.md).
