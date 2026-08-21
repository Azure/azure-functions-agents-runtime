# Provision Azure resources and deploy the Serverless Agent Portal to Azure
# Container Apps using the Azure Developer CLI (azd).
#
# Prerequisites: azd, Docker, and `az login` (or `azd auth login`).
#
# Usage:
#   ./deploy.ps1                      # defaults: env serverless-portal, westus3, target sub
#   ./deploy.ps1 -Location eastus2    # override region
#   ./deploy.ps1 -EnvName my-portal   # override azd environment name

[CmdletBinding()]
param(
    [string]$EnvName = 'serverless-portal',
    [string]$Location = 'eastus2',
    [string]$SubscriptionId = '0b894477-1614-4c8d-8a9b-a697a24596b8',
    [string]$MsalClientId = 'e5b70676-8224-4421-ad61-d1926b5c0952',
    [string]$MsalAuthority = 'https://login.microsoftonline.com/organizations'
)

$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    if (-not (Get-Command azd -ErrorAction SilentlyContinue)) {
        throw 'azd (Azure Developer CLI) is not installed. See https://aka.ms/azd-install.'
    }

    # Build the SPA locally so the Dockerfile can COPY its prebuilt dist.
    # The @coreai/fluentui-react feed is private and unreachable from ACR.
    Write-Host 'Building SPA (npm run build in app/frontend)...' -ForegroundColor Cyan
    Push-Location (Join-Path $PSScriptRoot 'app/frontend')
    try {
        npm run build
        if ($LASTEXITCODE -ne 0) { throw "SPA build failed with exit $LASTEXITCODE." }
    }
    finally { Pop-Location }

    # Stage runtime samples so /api/samples returns them from the container.
    # The Dockerfile can't COPY outside its build context; this bundle sits
    # inside serverless-portal/ so the image picks it up.
    Write-Host 'Staging samples bundle...' -ForegroundColor Cyan
    $bundle = Join-Path $PSScriptRoot 'samples-bundle'
    if (Test-Path $bundle) { Remove-Item $bundle -Recurse -Force }
    $sourceSamples = (Resolve-Path (Join-Path $PSScriptRoot '..\samples')).Path
    if (Test-Path $sourceSamples) {
        $skipDirs = @('.venv', '__pycache__', '.python_packages', '.azure')
        $skipFiles = @('local.settings.json', 'local.settings.template.json')
        New-Item -ItemType Directory -Path $bundle -Force | Out-Null
        $srcLen = $sourceSamples.Length
        Get-ChildItem -Path $sourceSamples -Recurse -Force -File | Where-Object {
            $relPath = $_.FullName.Substring($srcLen).TrimStart('\','/')
            if (-not $relPath) { return $false }
            $segments = $relPath -split '[\\/]'
            $inSkipDir = ($segments | Where-Object { $skipDirs -contains $_ }).Count -gt 0
            -not $inSkipDir -and -not ($skipFiles -contains $_.Name)
        } | ForEach-Object {
            $rel = $_.FullName.Substring($srcLen).TrimStart('\','/')
            $target = Join-Path $bundle $rel
            $parent = Split-Path $target -Parent
            if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
            Copy-Item -Path $_.FullName -Destination $target -Force
        }
        $bundleCount = (Get-ChildItem -Path $bundle -Recurse -File).Count
        Write-Host "  bundled $bundleCount files" -ForegroundColor DarkGray
    }

    # Create the azd environment if it does not exist yet (idempotent).
    $existing = azd env list --output json 2>$null | ConvertFrom-Json
    if (-not ($existing | Where-Object { $_.Name -eq $EnvName })) {
        azd env new $EnvName --subscription $SubscriptionId --location $Location --no-prompt
    }
    azd env select $EnvName

    # Pin subscription/location and portal configuration for the Bicep templates.
    azd env set AZURE_SUBSCRIPTION_ID $SubscriptionId
    azd env set AZURE_LOCATION $Location
    azd env set PORTAL_SUBSCRIPTION_ID $SubscriptionId
    azd env set MSAL_CLIENT_ID $MsalClientId
    azd env set MSAL_AUTHORITY $MsalAuthority

    # Provision infra + build/push image + deploy the container app.
    azd up --no-prompt

    Write-Host ''
    Write-Host 'Deployed. Portal URL:' -ForegroundColor Green
    azd env get-values | Select-String '^PORTAL_URI='
    Write-Host ''
    Write-Host 'Reminder: add the PORTAL_URI origin above as a SPA redirect URI on app' -ForegroundColor Yellow
    Write-Host "  $MsalClientId  so browser sign-in works." -ForegroundColor Yellow
}
finally {
    Pop-Location
}
