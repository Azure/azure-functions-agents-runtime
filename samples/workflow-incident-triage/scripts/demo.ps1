<#
.SYNOPSIS
    Pre-flight check + narration for the workflow-incident-triage demo.

.DESCRIPTION
    Verifies that Azurite, the Durable backend, the Functions host, and
    the workflow tools are reachable, then walks the presenter through
    the stakeholder demo one step at a time. The script auto-detects
    whether the sample is currently configured for Azure Storage or
    Durable Task Scheduler (DTS) by inspecting host.json, and adapts
    pre-flight + narration accordingly. The script is interactive
    (pauses between steps) and does NOT drive the chat UI itself —
    pasting the prompt and watching the live-progress card is
    intentionally a manual step so the audience sees the agent author
    the plan in real time.

    This is a presenter aid, not a test. Functional validation lives in
    the pytest suite under ``tests/``.

.PARAMETER BaseUrl
    Base URL of the running Functions host. Defaults to
    http://localhost:7071.

.PARAMETER NoBrowser
    Skip auto-opening the chat UI in the default browser.

.PARAMETER SkipPause
    Run all steps without waiting for Enter between them. Useful when
    rehearsing or when re-running pre-flight only.

.EXAMPLE
    ./demo.ps1
    Run the full demo dry-run with default settings.

.EXAMPLE
    ./demo.ps1 -BaseUrl http://localhost:7071 -NoBrowser
    Pre-flight + narration only; do not open a browser.
#>

[CmdletBinding()]
param(
    [string]$BaseUrl = "http://localhost:7071",
    [switch]$NoBrowser,
    [switch]$SkipPause
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$SrcDir = Resolve-Path (Join-Path $PSScriptRoot "..\src")
$DtsDashboardUrl = "http://localhost:8082"
$AgentUrl = "$($BaseUrl.TrimEnd('/'))/agents/main"

function Write-Step {
    param([string]$Title, [string]$Body)
    Write-Host ""
    Write-Host "── $Title ──" -ForegroundColor Cyan
    if ($Body) { Write-Host $Body }
}

function Write-Ok {
    param([string]$Message)
    Write-Host "  [ok] $Message" -ForegroundColor Green
}

function Write-Fail {
    param([string]$Message)
    Write-Host "  [!!] $Message" -ForegroundColor Red
}

function Write-Note {
    param([string]$Message)
    Write-Host "       $Message" -ForegroundColor DarkGray
}

function Pause-If-Interactive {
    param([string]$Prompt = "Press Enter to continue")
    if ($SkipPause) { return }
    Write-Host ""
    Read-Host -Prompt $Prompt | Out-Null
}

function Test-Tcp {
    param([string]$HostName, [int]$Port, [int]$TimeoutMs = 1500)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne($TimeoutMs)) { return $false }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Get-ActiveBackend {
    # Detect the Durable backend from host.json. Anything mentioning the
    # "azureManaged" storage provider is treated as DTS; otherwise we
    # default to "storage" (Azure Storage / Azurite).
    $hostPath = Join-Path $SrcDir "host.json"
    if (-not (Test-Path $hostPath)) { return "storage" }
    try {
        $hj = Get-Content -Raw -Path $hostPath | ConvertFrom-Json
        $providerType = $hj.extensions.durableTask.storageProvider.type
        if ($providerType -and $providerType.ToString().ToLower() -eq "azuremanaged") {
            return "dts"
        }
    } catch {
        # Malformed host.json — fall through and return storage; the
        # functions host will fail with its own error and we'll catch
        # that on the BaseUrl probe.
    }
    return "storage"
}

function Get-DtsEndpoint {
    # Best-effort parse of DURABLE_TASK_SCHEDULER_CONNECTION_STRING from
    # local.settings.json. Returns @{ HostName = ...; Port = ...; Raw = ... }
    # or $null if anything is missing or unparseable. We fall back to
    # localhost:8080 (the emulator default) at the call site.
    $settingsPath = Join-Path $SrcDir "local.settings.json"
    if (-not (Test-Path $settingsPath)) { return $null }
    try {
        $settings = Get-Content -Raw -Path $settingsPath | ConvertFrom-Json
        $connStr = $settings.Values.DURABLE_TASK_SCHEDULER_CONNECTION_STRING
        if (-not $connStr) { return $null }
        # Extract Endpoint=<url>; from a semicolon-delimited connection string.
        $endpointPart = ($connStr -split ';') | Where-Object { $_ -match '^\s*Endpoint\s*=' } | Select-Object -First 1
        if (-not $endpointPart) { return $null }
        $url = ($endpointPart -split '=', 2)[1].Trim()
        $uri = [System.Uri]::new($url)
        return @{
            HostName = $uri.Host
            Port = $uri.Port
            Raw = $url
        }
    } catch {
        return $null
    }
}

# Step counter so labels stay correct whether or not we add the
# DTS-only dashboard step.
$script:stepNum = 0
function Next-StepLabel {
    param([string]$Title)
    $script:stepNum += 1
    return "Step ${script:stepNum}: $Title"
}

# ---------- pre-flight ----------

$activeBackend = Get-ActiveBackend
$backendLabel = if ($activeBackend -eq "dts") { "Durable Task Scheduler (DTS)" } else { "Azure Storage / Azurite" }

Write-Step -Title "Pre-flight" -Body "Active backend: $backendLabel. Checking that prerequisites are reachable."

$azuriteOk = Test-Tcp -HostName "127.0.0.1" -Port 10000
if ($azuriteOk) {
    Write-Ok "Azurite blob endpoint reachable (127.0.0.1:10000)"
} else {
    Write-Fail "Azurite is not reachable on 127.0.0.1:10000"
    Write-Note "Start it with:  azurite --silent --location ./.azurite"
    Write-Note "The Functions runtime requires AzureWebJobsStorage regardless of the"
    Write-Note "Durable backend, so Azurite (or a real Storage account) must be running."
    exit 1
}

if ($activeBackend -eq "dts") {
    $dts = Get-DtsEndpoint
    if ($dts) {
        $dtsHost = $dts.HostName
        $dtsPort = $dts.Port
        $dtsLabel = "$($dts.HostName):$($dts.Port) (from local.settings.json)"
    } else {
        $dtsHost = "localhost"
        $dtsPort = 8080
        $dtsLabel = "localhost:8080 (default; DURABLE_TASK_SCHEDULER_CONNECTION_STRING not found in local.settings.json)"
    }
    if (Test-Tcp -HostName $dtsHost -Port $dtsPort) {
        Write-Ok "DTS gRPC endpoint reachable ($dtsLabel)"
    } else {
        Write-Fail "DTS gRPC endpoint not reachable at $dtsHost`:$dtsPort"
        Write-Note "Start the emulator with:"
        Write-Note "  docker run -d --name dts-emulator -p 8080:8080 -p 8082:8082 \``"
        Write-Note "    -e DTS_USE_DYNAMIC_TASK_HUBS=true \``"
        Write-Note "    mcr.microsoft.com/dts/dts-emulator:latest"
        Write-Note "Or swap back to Azure Storage with:  ./scripts/swap-backend.ps1 storage"
        exit 1
    }
}

try {
    $rootResp = Invoke-WebRequest -Uri "$AgentUrl/" -Method Get -TimeoutSec 4 -UseBasicParsing
    if ($rootResp.StatusCode -eq 200) {
        Write-Ok "Functions host serving the chat UI at $AgentUrl/"
    } else {
        Write-Fail "Unexpected status from $AgentUrl/ : $($rootResp.StatusCode)"
        exit 1
    }
} catch {
    Write-Fail "Cannot reach the Functions host at $AgentUrl/"
    Write-Note "Start it from the sample's src/ directory with:  func start"
    Write-Note "Make sure your venv is active in the same shell so the worker"
    Write-Note "picks up azure-functions-durable. (See sample README.)"
    exit 1
}

try {
    $probe = Invoke-WebRequest -Uri "$AgentUrl/workflows" -Method Get -TimeoutSec 4 -UseBasicParsing -ErrorAction Stop
    if ($probe.StatusCode -eq 200) {
        Write-Ok "Workflow tools are wired (GET /agents/main/workflows -> 200)"
    } else {
        Write-Fail "Workflow capability probe returned status $($probe.StatusCode)"
        Write-Note "Expected 200. If you get 404/501, workflows.enabled is not set in main.agent.md."
        exit 1
    }
} catch {
    $status = $_.Exception.Response.StatusCode.value__
    if ($status -eq 404 -or $status -eq 501) {
        Write-Fail "Workflow capability probe returned $status — workflows are not enabled."
        Write-Note "Confirm main.agent.md contains 'workflows.enabled: true' and restart func host."
    } else {
        Write-Fail "Workflow capability probe failed: $($_.Exception.Message)"
    }
    exit 1
}

Write-Host ""
Write-Host "Pre-flight passed. You are ready to demo." -ForegroundColor Green
Pause-If-Interactive -Prompt "Press Enter to start narration"

# ---------- narration ----------

Write-Step -Title (Next-StepLabel "open the chat UI") -Body @"
The chat UI is served by the Functions host itself at:
  $AgentUrl/

It auto-detects whether the agent has workflow tools enabled. When the
session is visible it polls GET /agents/main/workflows on a 2–5s cadence and
renders a per-workflow live-progress card inline with the chat thread.
"@
if (-not $NoBrowser) {
    try {
        Start-Process "$AgentUrl/" | Out-Null
        Write-Note "Opened in your default browser."
    } catch {
        Write-Note "Could not auto-open the browser. Open $AgentUrl/ manually."
    }
}
Pause-If-Interactive

Write-Step -Title (Next-StepLabel "paste this prompt") -Body @"
Copy the line below into the chat input. The agent will author a five-task
DAG: three parallel fetches, a 30-second durable timer, and a final
summarize step.
"@
Write-Host ""
Write-Host '  We''re seeing latency spikes and intermittent 502s on the orders-api ' -ForegroundColor Yellow
Write-Host '  service for the last 20 minutes. Pull recent logs, metrics, and the   ' -ForegroundColor Yellow
Write-Host '  deploy history in parallel; let in-flight work drain for 30 seconds;  ' -ForegroundColor Yellow
Write-Host '  then summarize what you find.                                         ' -ForegroundColor Yellow
Pause-If-Interactive

Write-Step -Title (Next-StepLabel "watch the workflow card render") -Body @"
Within a few seconds the agent should call start_workflow and a card
should appear in the chat thread. Things to point out:

  * Five tasks: fetch_logs, fetch_metrics, fetch_deploys, cooldown (wait),
    summarize_findings.
  * The three fetches transition to 'running' immediately and 'completed'
    in roughly the same wave — that is real Durable fan-out, not a
    sequential loop.
  * The cooldown wait sits at 'running' for ~30s. This is a durable
    timer: if you killed func.exe right now and restarted it, the timer
    would resume and the workflow would still complete.
"@
Pause-If-Interactive

if ($activeBackend -eq "dts") {
    Write-Step -Title (Next-StepLabel "open the operator dashboard (DTS only)") -Body @"
While the workflow is still running, open the DTS dashboard in a second
browser tab:

  $DtsDashboardUrl

You should see the same orchestration listed under task hub 'default'
(or whatever TASKHUB_NAME you set). Click into it and point out:

  * The execution history — every activity invocation, timer fired, and
    output payload, captured by the scheduler. This is the operator
    surface that the chat UI doesn't expose.
  * The instance is identical whether the agent or a human kicked it off,
    so on-call tooling and the chat experience converge on the same
    backing store.

This is the DTS headline: the same workflow you saw in chat is also a
first-class object in the operator's world. Same status envelope is
available programmatically via get_workflow_status — the dashboard
isn't doing anything the API can't do, it's just an out-of-the-box
view.
"@
    Pause-If-Interactive
}

Write-Step -Title (Next-StepLabel "terminal state and auto-notification") -Body @"
Around the 35-second mark the card should flip to 'Completed'. A
moment later, the chat UI will auto-inject a synthetic user message
labeled 'Automatic workflow notification' (subtle styling, expandable
to show the raw injected prompt). That message tells the agent the
workflow finished; the agent calls get_workflow_status once and
writes a short natural-language summary inline as a normal Copilot
turn — closing the loop without you typing anything.

Things to call out for stakeholders:

  * Fire-and-forget loop: the agent ended its turn after
    start_workflow returned. It did not poll. It only re-engaged
    because the chat client posted the synthetic notification on
    completion.
  * Token cost: the agent only sees the FINAL summary envelope (one
    get_workflow_status call), not every per-task output, because
    templating happens inside the orchestrator. This is
    Anthropic-style programmatic tool calling with durability.
  * Observability: the same status envelope the UI polls is also
    available outside the chat via get_workflow_status — operator
    dashboards, on-call tooling, and MCP Tasks clients all read the
    same shape.
"@
Pause-If-Interactive

Write-Step -Title (Next-StepLabel "(optional) demo cooperative cancel") -Body @"
If you have time, run the demo a second time and during the 30-second
wait, send a follow-up message:

  actually cancel that

The agent should call cancel_workflow. The orchestration unwinds at the
next wave boundary, the live card flips to 'Canceled', and partial
results gathered before the cancel are still visible. The auto-
notification then fires, prompting the agent to acknowledge the
cancellation in a final turn. Contrast this with terminate_workflow,
which stops abruptly; the auto-notification still fires (the chat UI
sees the terminal state regardless of cooperativity), and the agent
will say plainly that no usable result is available.
"@

Write-Host ""
Write-Host "Demo dry-run complete." -ForegroundColor Green
Write-Note "If anything was wrong, fix it now and re-run this script before the live demo."
Write-Note "To clean up stale workflows between rehearsals, restart func.exe; the per-session"
Write-Note "owner key changes when the chat UI generates a new session, so old runs are hidden."
