# Provision a Linux Consumption Azure Function App and publish the static
# Serverless Agent Portal mockups to it, producing a shareable public URL.
#
# Prerequisites: Azure CLI (az), Azure Functions Core Tools (func), and a
# signed-in session (`az login`).
#
# Usage:
#   ./deploy.ps1                                   # defaults below
#   ./deploy.ps1 -Location westus3
#   ./deploy.ps1 -ResourceGroup my-rg -NamePrefix portal

[CmdletBinding()]
param(
    [string]$ResourceGroup = 'rg-serverless-portal-mocks',
    [string]$Location = 'westus2',
    [string]$SubscriptionId = '1a839f1f-10b2-4613-95ad-0800a22abbf2',
    [ValidateLength(3, 12)]
    [string]$NamePrefix = 'mocks'
)

$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    foreach ($tool in 'az', 'func') {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
            throw "'$tool' is required but was not found on PATH. See https://aka.ms/azd-install and https://aka.ms/azfunc-core-tools."
        }
    }

    # 1. Refresh the static content from the mockups (kept out of source control).
    $content = Join-Path $PSScriptRoot 'content'
    if (Test-Path $content) { Remove-Item $content -Recurse -Force }
    Copy-Item -Path (Join-Path $PSScriptRoot '..' 'mocks') -Destination $content -Recurse
    Write-Host "Copied mockups into $content" -ForegroundColor Green

    # 2. Target the subscription and resource group.
    az account set --subscription $SubscriptionId
    if ($LASTEXITCODE -ne 0) { throw "Failed to select subscription $SubscriptionId." }
    az group create --name $ResourceGroup --location $Location --output none
    if ($LASTEXITCODE -ne 0) { throw "Failed to create resource group $ResourceGroup." }

    # 3. Provision the Function App and its dependencies.
    Write-Host 'Provisioning Azure resources...' -ForegroundColor Cyan
    $outputs = az deployment group create `
        --resource-group $ResourceGroup `
        --template-file (Join-Path $PSScriptRoot 'infra' 'main.bicep') `
        --parameters location=$Location namePrefix=$NamePrefix `
        --query properties.outputs --output json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or -not $outputs) { throw 'Infrastructure provisioning failed (see the error above).' }
    $appName = $outputs.functionAppName.value
    $appUrl = $outputs.functionAppUrl.value

    # 4. Publish the function code + static content (remote/Oryx build).
    Write-Host "Publishing to $appName..." -ForegroundColor Cyan
    func azure functionapp publish $appName --build remote
    if ($LASTEXITCODE -ne 0) { throw "Publishing to $appName failed." }

    Write-Host ''
    Write-Host 'Deployed. Share this URL:' -ForegroundColor Green
    Write-Host "  $appUrl" -ForegroundColor Green
}
finally {
    Pop-Location
}
