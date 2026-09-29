# Local Copilot harness preview

This **default-off, local-only** Azure Functions sample uses markdown discovery
and HTTP registration, not a standalone SDK script or `azd up` deployment.
It pins `github-copilot-sdk==1.0.14`, native `1.0.85`/protocol 3, external stdio;
MAF stays default. Use Python 3.13/3.14, Functions Core Tools 4, and an approved
Foundry deployment/Azure sign-in. Model calls incur charges; setup does not prove access.

## Scope and limits

- Non-streaming built-in `POST /agents/main/chat` (JSON), authored `POST /preview`
  (plain text), `make_receipt` tool, and SDK-owned local native-session continuity.
- One local worker only: no Azure hosting, Blob/MAF history import, interrupted
  turn recovery guarantee or fallback. Built-in stream/history routes return 501.
- No MCP, skills, debug UI, web/ACA tools, non-HTTP triggers, delegation,
  workflows, MAF/native compaction or special tool policies; `web_request` is off.
- **No output cap:** `agent_configuration.max_output_tokens` is rejected for
  this opt-in. The SDK owns provider requests; the host does not force
  `store:false` or a generation cap. Model-call count/billing can vary.

## Windows PowerShell setup (repository root)

Install **this checkout** with `[copilot]`; isolate native files and keep secrets out of local settings.

```powershell
py -3.13 -m venv .venv
Push-Location samples\copilot-preview\src
& ..\..\..\.venv\Scripts\python.exe -m pip install -r requirements.txt
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
```

No `py` launcher? Replace the first four lines above with this `uv` equivalent
from the repository root (the venv stays at the root):

```powershell
uv venv .venv --python 3.13
Push-Location samples\copilot-preview\src
uv pip install --python ..\..\..\.venv\Scripts\python.exe -r requirements.txt
Pop-Location
```

The `[copilot]` extra installs the Python SDK, not native assets. On the first
Copilot-enabled request, the SDK automatically downloads its pinned native
runtime if it is not cached; this can add cold-start time. Later requests with
access to the same cache reuse it. The flag-off MAF path does not download it.
No separate CLI setup or download setting is needed for this sample.

```powershell
Push-Location samples\copilot-preview\src
func start --port 7071
```

## Verify (only against an approved deployment)

In a **second terminal at repo root**, the verifier checks a real built-in HTTP
reply, one `make_receipt` call and public ID. Restart the host with the **same
environment/state** between phases; follow-up recalls without a tool. Negatives use no model.

```powershell
.\.venv\Scripts\python.exe samples\copilot-preview\verify.py `
  --phase first --evidence .preview-evidence.json
# Ctrl+C host; in setup terminal: Pop-Location; Push-Location samples\copilot-preview\src
# Run func start --port 7071 again.
.\.venv\Scripts\python.exe samples\copilot-preview\verify.py `
  --phase followup --evidence .preview-evidence.json
.\.venv\Scripts\python.exe samples\copilot-preview\verify.py --phase negative
```

Expect `PASS first`, `PASS followup`, `PASS negative` (unknown session fails;
stream/history 501). For automatic restart, with no host/evidence file, run
`.\.venv\Scripts\python.exe samples\copilot-preview\verify.py --restart-host --evidence .preview-evidence.json`
in the setup terminal.

While the host runs, this cross-route example shows a built-in tool call and an
authored-trigger follow-up (plain text and `x-ms-session-id`, not JSON tool calls):

```powershell
$first = Invoke-RestMethod http://127.0.0.1:7071/agents/main/chat `
  -Method Post -ContentType application/json `
  -Body '{"prompt":"Call make_receipt exactly once with tag ''manual-demo''. Reply only with its result."}'
$first.tool_calls # Inspect the single make_receipt call and result.
$followup = Invoke-WebRequest http://127.0.0.1:7071/preview -UseBasicParsing `
  -Method Post -ContentType application/json `
  -Headers @{"x-ms-session-id"=$first.session_id} `
  -Body '{"prompt":"Recall the previous receipt without calling any tool."}'
$followup.Content
$followup.Headers["x-ms-session-id"] # Same ID as $first.session_id.
```

## Flag off and targeted cleanup

Stop the host with Ctrl+C; restart with flag off in the setup terminal.
MAF history/streaming returns: `GET /agents/main/history` is no longer 501
(no inference needed). Never reuse a Copilot ID for a MAF conversation.

```powershell
$env:AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT = "false"
Push-Location samples\copilot-preview\src
func start --port 7071
# Ctrl+C; then Pop-Location to return to the repository root.
Pop-Location
if ($run -notmatch '^[a-f0-9]{32}$' -or
    -not (Test-Path -LiteralPath .\samples\copilot-preview\src\main.agent.md)) {
    throw "Cleanup requires the original run ID at this sample's repository root."
}
$repo = (Get-Location).ProviderPath
$state = Join-Path $repo ".preview-state\$run"
$parent = Get-Item -LiteralPath (Split-Path $state) -ErrorAction SilentlyContinue
$target = Get-Item -LiteralPath $state -ErrorAction SilentlyContinue
$target | Select-Object FullName, LinkType, Target
$items = @($parent) + @($target)
if ($items | Where-Object {
    $null -ne $_ -and (-not $_.PSIsContainer -or
        ($_.Attributes -band [IO.FileAttributes]::ReparsePoint))
}) {
    throw "Refusing to recursively remove a file or linked directory."
}
```

After inspecting the printed full path, remove only this run's session
directory (never the SDK cache or the parent `.preview-state`):

```powershell
Remove-Item -LiteralPath $state -Recurse -ErrorAction SilentlyContinue
Remove-Item -LiteralPath .preview-evidence.json, samples\copilot-preview\src\local.settings.json `
  -ErrorAction SilentlyContinue
Remove-Item Env:\AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT, Env:\AZURE_FUNCTIONS_AGENTS_SESSION_DIR `
  -ErrorAction SilentlyContinue
```

Do not remove shared SDK caches, MAF `agent-sessions` or another run's state.
At `14c9c956` (2026-09-28; Windows/Python 3.13.15, Core Tools 4.13.0,
SDK 1.0.14/native 1.0.85), an approved uncapped Foundry/Entra `gpt-5.4`
run passed the receipt tool, cold host restart, value-free follow-up, and
negative cases. Native events showed three model calls, one tool call and
two user turns; no native process remained. This local result does not
qualify Blob, compaction, Azure hosting or production parity.
