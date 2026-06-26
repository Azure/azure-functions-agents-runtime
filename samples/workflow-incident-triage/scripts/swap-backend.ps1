<#
.SYNOPSIS
    Swap the workflow-incident-triage sample between Durable Functions
    backends.

.DESCRIPTION
    The Functions host reads the live host.json. To run the sample on a
    different Durable backend, the right host.json variant must be in
    place when ``func start`` boots. This script swaps in the canonical
    variant for the requested backend; no in-place editing.

    Backends:
        storage   - classic Azure Storage / Azurite (the committed default)
        dts       - Durable Task Scheduler (host.dts.json + DTS app settings)

    The DTS path also reminds you to copy local.settings.dts.json.template
    to local.settings.json (or merge the new keys in) and to start the
    DTS emulator. See the sample README for the full setup.

.PARAMETER Backend
    Which backend to switch to: 'storage' or 'dts'.

.PARAMETER Status
    Show the currently-active backend without changing anything.

.EXAMPLE
    ./swap-backend.ps1 storage
    Restore the committed Azure Storage host.json.

.EXAMPLE
    ./swap-backend.ps1 dts
    Apply the Durable Task Scheduler host.json variant.

.EXAMPLE
    ./swap-backend.ps1 -Status
    Print which backend is currently active.
#>

[CmdletBinding(DefaultParameterSetName = "Switch")]
param(
    [Parameter(Position = 0, ParameterSetName = "Switch", Mandatory = $true)]
    [ValidateSet("storage", "dts")]
    [string]$Backend,

    [Parameter(ParameterSetName = "Status")]
    [switch]$Status
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SrcDir = (Resolve-Path (Join-Path $ScriptDir "..\src")).Path

function Get-CurrentBackend {
    $hostJson = Join-Path $SrcDir "host.json"
    if (-not (Test-Path $hostJson)) { return "missing" }
    $contents = Get-Content -Raw -LiteralPath $hostJson
    if ($contents -match '"type"\s*:\s*"azureManaged"') {
        return "dts"
    }
    return "storage"
}

function Write-Status {
    $current = Get-CurrentBackend
    switch ($current) {
        "storage" { Write-Host "current backend: " -NoNewline; Write-Host "storage" -ForegroundColor Green -NoNewline; Write-Host "  (Azure Storage / Azurite -- host.json default)" }
        "dts"     { Write-Host "current backend: " -NoNewline; Write-Host "dts"     -ForegroundColor Cyan  -NoNewline; Write-Host "      (Durable Task Scheduler -- host.dts.json applied)" }
        "missing" { Write-Host "current backend: " -NoNewline; Write-Host "missing" -ForegroundColor Red   -NoNewline; Write-Host "  (host.json not found in $SrcDir -- run ./swap-backend.ps1 storage|dts)" }
        default   { Write-Host "current backend: $current" }
    }
}

if ($PSCmdlet.ParameterSetName -eq "Status") {
    Write-Status
    return
}

switch ($Backend) {
    "storage" {
        # Restore the committed default. host.json is always-tracked,
        # so `git checkout HEAD -- host.json` is the canonical undo.
        # We use `HEAD --` (not bare `--`) so the restore is from the
        # last commit, not the index — otherwise a contributor who
        # accidentally staged a swapped host.json would have storage
        # silently no-op while claiming success.
        git -C $SrcDir rev-parse --is-inside-work-tree 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "error: " -ForegroundColor Red -NoNewline
            Write-Host "not inside a git work tree; cannot restore the storage host.json this way."
            Write-Host "  remediation: re-clone the sample, or commit the storage host.json yourself before swapping back." -ForegroundColor DarkGray
            exit 1
        }
        git -C $SrcDir checkout HEAD -- host.json
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        Write-Host "OK  " -ForegroundColor Green -NoNewline
        Write-Host "switched to " -NoNewline
        Write-Host "storage " -ForegroundColor Green -NoNewline
        Write-Host "(host.json restored from HEAD)"
        Write-Host "    don't forget: Azurite must be running (or AzureWebJobsStorage must point at a real Storage account)." -ForegroundColor DarkGray
    }
    "dts" {
        $dtsHost = Join-Path $SrcDir "host.dts.json"
        if (-not (Test-Path $dtsHost)) {
            Write-Host "error: $dtsHost not found" -ForegroundColor Red
            exit 1
        }
        Copy-Item -LiteralPath $dtsHost -Destination (Join-Path $SrcDir "host.json") -Force
        Write-Host "OK  " -ForegroundColor Green -NoNewline
        Write-Host "switched to " -NoNewline
        Write-Host "dts " -ForegroundColor Cyan -NoNewline
        Write-Host "(host.dts.json copied to host.json)"
        Write-Host "    next:" -ForegroundColor DarkGray
        Write-Host "      1. start the DTS emulator:" -ForegroundColor DarkGray
        Write-Host "         docker run -d --name dts-emulator -p 8080:8080 -p 8082:8082 ``" -ForegroundColor DarkGray
        Write-Host "             -e DTS_USE_DYNAMIC_TASK_HUBS=true ``" -ForegroundColor DarkGray
        Write-Host "             mcr.microsoft.com/dts/dts-emulator:latest" -ForegroundColor DarkGray
        Write-Host "      2. ensure local.settings.json has DURABLE_TASK_SCHEDULER_CONNECTION_STRING and TASKHUB_NAME" -ForegroundColor DarkGray
        Write-Host "         (see local.settings.dts.json.template)" -ForegroundColor DarkGray
        Write-Host "      3. Azurite still needs to be running -- AzureWebJobsStorage is required by the Functions runtime regardless of Durable backend" -ForegroundColor DarkGray
        Write-Host "      4. open the dashboard at http://localhost:8082" -ForegroundColor DarkGray
        Write-Host "      to swap back: ./scripts/swap-backend.ps1 storage" -ForegroundColor DarkGray
    }
}
