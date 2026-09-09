param(
    [Parameter(Mandatory = $true)]
    [string]$Jwt
)

$parts = $Jwt.Split('.')
if ($parts.Length -lt 2) {
    throw "Invalid JWT format."
}

$payload = $parts[1].Replace('-', '+').Replace('_', '/')
switch ($payload.Length % 4) {
    2 { $payload += '==' }
    3 { $payload += '=' }
}

$bytes = [System.Convert]::FromBase64String($payload)
$json = [System.Text.Encoding]::UTF8.GetString($bytes)
$json | ConvertFrom-Json
