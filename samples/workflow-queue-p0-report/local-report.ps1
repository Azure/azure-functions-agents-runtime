[CmdletBinding()]
param(
    [ValidateSet("Submit", "Download")]
    [string] $Action = "Submit",

    [string[]] $Repositories = @(
        "Azure/azure-functions-host",
        "Azure/azure-functions-python-worker",
        "Azure/azure-functions-durable-python"
    ),

    [string] $ReportBlob = "reports/p0-issues.html",
    [string] $OutputFile = ".\p0-issues.html"
)

$ErrorActionPreference = "Stop"

$connection = "DefaultEndpointsProtocol=http;" +
    "AccountName=devstoreaccount1;" +
    # [SuppressMessage("Microsoft.Security", "CS002:SecretInNextLine", Justification="Azurite uses a public emulator account key")]
    "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;" +
    "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;" +
    "QueueEndpoint=http://127.0.0.1:10001/devstoreaccount1;" +
    "TableEndpoint=http://127.0.0.1:10002/devstoreaccount1;"

if ($Action -eq "Submit") {
    if ($Repositories.Count -eq 0) {
        throw "At least one repository is required."
    }

    az storage queue create `
        --name issue-report-requests `
        --connection-string $connection `
        --only-show-errors `
        --output none
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create the Azurite input queue."
    }

    $request = @{
        repositories = $Repositories
        report_blob = $ReportBlob
    } | ConvertTo-Json -Compress
    $encodedRequest = [Convert]::ToBase64String(
        [Text.Encoding]::UTF8.GetBytes($request)
    )

    az storage message put `
        --queue-name issue-report-requests `
        --connection-string $connection `
        --content $encodedRequest `
        --only-show-errors `
        --output none
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to submit the report request."
    }

    Write-Host "Submitted report request for $($Repositories.Count) repositories."
    Write-Host "Blob destination: workflow-reports/$ReportBlob"
    return
}

az storage blob download `
    --container-name workflow-reports `
    --name $ReportBlob `
    --file $OutputFile `
    --connection-string $connection `
    --overwrite `
    --only-show-errors `
    --output none
if ($LASTEXITCODE -ne 0) {
    throw "Failed to download the generated report."
}

$resolvedOutput = (Resolve-Path $OutputFile).Path
Write-Host "Downloaded report to $resolvedOutput"
Start-Process $resolvedOutput
