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
    [string]$SubscriptionId = '1a839f1f-10b2-4613-95ad-0800a22abbf2',
    [string]$MsalClientId = '0ceccceb-9c05-4953-9193-d94f9daa18d3',
    [string]$MsalAuthority = 'https://login.microsoftonline.com/organizations'
)

$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    if (-not (Get-Command azd -ErrorAction SilentlyContinue)) {
        throw 'azd (Azure Developer CLI) is not installed. See https://aka.ms/azd-install.'
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
