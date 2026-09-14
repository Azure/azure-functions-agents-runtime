param(
    [Parameter(Mandatory = $true)]
    [string]$Scope
)

$token = az account get-access-token --scope $Scope --query accessToken -o tsv
if (-not $token) {
    throw "Failed to acquire token. Ensure 'az login' is complete and scope is valid."
}

$token
