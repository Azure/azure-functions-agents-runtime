param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Sample
)

$scriptPath = Join-Path $PSScriptRoot "sync_sample_wheels.py"
$arguments = @($scriptPath)

foreach ($sampleName in $Sample) {
    $arguments += "--sample"
    $arguments += $sampleName
}

$repoRoot = Split-Path $PSScriptRoot -Parent
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"

if (Test-Path $venvPython) {
    & $venvPython @arguments
} else {
    & python @arguments
}

if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
