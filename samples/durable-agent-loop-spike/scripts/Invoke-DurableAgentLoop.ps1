#requires -Version 7.0
<#
.SYNOPSIS
Calls the private Durable Agent Loop spike routes without placing a Function key in a URL.

.DESCRIPTION
Creates logical session IDs locally and calls the deployed Durable Agent Loop HTTP
surface through the x-functions-key header. The script never writes a Function key
to output, disk, or a request URL.

.EXAMPLE
$session = .\Invoke-DurableAgentLoop.ps1 -NewLogicalSession
$key = Read-Host -AsSecureString 'Function key'
$run = .\Invoke-DurableAgentLoop.ps1 -StartRun -SessionId $session.session_id `
    -Prompt 'Use the approved local tool.' -SandboxProfile retained_session -FunctionKey $key

.EXAMPLE
.\Invoke-DurableAgentLoop.ps1 -StartRun -Prompt 'Start a bounded test.' `
    -FunctionAppName func-durable-loop-0904 `
    -ResourceGroup larohra-durable-agent-loop -RetrieveFunctionKey
#>
[CmdletBinding(DefaultParameterSetName = 'NewLogicalSession')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute(
    'PSReviewUnusedParameter',
    '',
    Justification = 'Script parameters are consumed by nested helper functions.'
)]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute(
    'PSUseApprovedVerbs',
    '',
    Justification = 'The internal helper throws a terminating client error.'
)]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute(
    'PSUseShouldProcessForStateChangingFunctions',
    '',
    Justification = 'New helpers construct in-memory output objects and do not change state.'
)]
param(
    [Parameter(Mandatory, ParameterSetName = 'NewLogicalSession')]
    [switch]$NewLogicalSession,

    [Parameter(Mandatory, ParameterSetName = 'StartRun')]
    [switch]$StartRun,

    [Parameter(Mandatory, ParameterSetName = 'ContinueSession')]
    [switch]$ContinueSession,

    [Parameter(Mandatory, ParameterSetName = 'GetStatus')]
    [switch]$GetStatus,

    [Parameter(Mandatory, ParameterSetName = 'WaitRun')]
    [switch]$WaitRun,

    [Parameter(Mandatory, ParameterSetName = 'GetResult')]
    [switch]$GetResult,

    [Parameter(Mandatory, ParameterSetName = 'GetRetainedSandbox')]
    [switch]$GetRetainedSandbox,

    [Parameter(Mandatory, ParameterSetName = 'GetHumanInput')]
    [switch]$GetHumanInput,

    [Parameter(Mandatory, ParameterSetName = 'SubmitHumanInput')]
    [switch]$SubmitHumanInput,

    [Parameter(Mandatory, ParameterSetName = 'CancelRun')]
    [switch]$CancelRun,

    [Parameter(Mandatory, ParameterSetName = 'StartRun')]
    [Parameter(Mandatory, ParameterSetName = 'ContinueSession')]
    [ValidateNotNull()]
    [string]$Prompt,

    [Parameter(ParameterSetName = 'StartRun')]
    [Parameter(Mandatory, ParameterSetName = 'ContinueSession')]
    [ValidateLength(1, 128)]
    [string]$SessionId,

    [Parameter(ParameterSetName = 'StartRun')]
    [Parameter(ParameterSetName = 'ContinueSession')]
    [ValidateLength(1, 128)]
    [string]$RequestId,

    [Parameter(ParameterSetName = 'StartRun')]
    [Parameter(ParameterSetName = 'ContinueSession')]
    [ValidateSet('per_call', 'retained_session')]
    [string]$SandboxProfile = 'per_call',

    [Parameter(ParameterSetName = 'StartRun')]
    [Parameter(ParameterSetName = 'ContinueSession')]
    [ValidateSet(
        'none',
        'model_apim_429_once',
        'model_timeout_once',
        'tool_activity_ack_loss_once',
        'sandbox_loss_after_checkpoint',
        'cleanup_failure_once',
        'commit_ack_loss_once'
    )]
    [string]$FaultProfile = 'none',

    [Parameter(Mandatory, ParameterSetName = 'GetStatus')]
    [Parameter(Mandatory, ParameterSetName = 'WaitRun')]
    [Parameter(Mandatory, ParameterSetName = 'GetResult')]
    [Parameter(Mandatory, ParameterSetName = 'GetRetainedSandbox')]
    [Parameter(Mandatory, ParameterSetName = 'GetHumanInput')]
    [Parameter(Mandatory, ParameterSetName = 'SubmitHumanInput')]
    [Parameter(Mandatory, ParameterSetName = 'CancelRun')]
    [ValidatePattern('^run-[0-9a-f]{32}$')]
    [string]$RunId,

    [Parameter(Mandatory, ParameterSetName = 'GetHumanInput')]
    [Parameter(Mandatory, ParameterSetName = 'SubmitHumanInput')]
    [ValidatePattern('^human-[0-9]+-[0-9a-f]{16}$')]
    [string]$HumanRequestId,

    [Parameter(ParameterSetName = 'SubmitHumanInput')]
    [AllowEmptyString()]
    [string]$Answer,

    [Parameter(ParameterSetName = 'SubmitHumanInput')]
    [AllowEmptyString()]
    [string]$AnswerJson,

    [Parameter(ParameterSetName = 'SubmitHumanInput')]
    [ValidateLength(1, 128)]
    [string]$SubmissionId,

    [Parameter(ParameterSetName = 'WaitRun')]
    [ValidateRange(0.5, 60.0)]
    [double]$PollIntervalSeconds = 2.0,

    [Parameter(ParameterSetName = 'WaitRun')]
    [ValidateRange(1, 21600)]
    [int]$TimeoutSeconds = 600,

    [Parameter(ParameterSetName = 'WaitRun')]
    [switch]$AllowNonSuccessTerminal,

    [string]$BaseUri,

    [SecureString]$FunctionKey,

    [ValidateNotNullOrEmpty()]
    [string]$FunctionKeyEnvironmentVariable = 'DURABLE_LOOP_FUNCTION_KEY',

    [switch]$RetrieveFunctionKey,

    [ValidatePattern('^[A-Za-z0-9-]+$')]
    [string]$FunctionAppName,

    [ValidatePattern('^[A-Za-z0-9._()-]+$')]
    [string]$ResourceGroup,

    [ValidateRange(1, 600)]
    [int]$RequestTimeoutSeconds = 120,

    [switch]$AllowInsecureLocalHttp
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:FunctionKeyWasProvided = $PSBoundParameters.ContainsKey('FunctionKey')
$script:AnswerWasProvided = $PSBoundParameters.ContainsKey('Answer')
$script:AnswerJsonWasProvided = $PSBoundParameters.ContainsKey('AnswerJson')
$script:DefaultFunctionAppName = 'func-durable-loop-0904'
$script:RouteBase = '/api/experimental/durable-agent-runs'
$script:MaxPromptBytes = 256KB
$script:MaxHumanAnswerBytes = 64KB
$script:MaxResponseBytes = 1MB
$script:RunStatuses = @('Pending', 'Running', 'Waiting', 'Completed', 'Failed', 'Cancelled')

function Throw-DurableAgentLoopError {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Message,

        [Parameter(Mandatory)]
        [string]$ErrorId,

        [Parameter(Mandatory)]
        [System.Management.Automation.ErrorCategory]$Category,

        [object]$TargetObject
    )

    $exception = [System.InvalidOperationException]::new($Message)
    $record = [System.Management.Automation.ErrorRecord]::new(
        $exception,
        $ErrorId,
        $Category,
        $TargetObject
    )
    $PSCmdlet.ThrowTerminatingError($record)
}

function ConvertFrom-DurableSecureString {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [SecureString]$Value
    )

    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        if ($pointer -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        }
    }
}

function Resolve-DurableFunctionKey {
    [CmdletBinding()]
    param()

    if ($script:FunctionKeyWasProvided) {
        return ConvertFrom-DurableSecureString -Value $FunctionKey
    }

    if ($RetrieveFunctionKey) {
        if (
            [string]::IsNullOrWhiteSpace($FunctionAppName) -or
            [string]::IsNullOrWhiteSpace($ResourceGroup)
        ) {
            Throw-DurableAgentLoopError `
                -Message 'RetrieveFunctionKey requires FunctionAppName and ResourceGroup.' `
                -ErrorId 'DurableAgentLoopMissingAzureResource' `
                -Category InvalidArgument
        }
        if ($null -eq (Get-Command az -CommandType Application -ErrorAction SilentlyContinue)) {
            Throw-DurableAgentLoopError `
                -Message 'RetrieveFunctionKey requires the Azure CLI executable on PATH.' `
                -ErrorId 'DurableAgentLoopAzureCliRequired' `
                -Category ObjectNotFound
        }

        $azOutput = @(
            & az functionapp keys list `
                --name $FunctionAppName `
                --resource-group $ResourceGroup `
                --only-show-errors `
                --query 'functionKeys.default' `
                --output tsv 2>&1
        )
        if ($LASTEXITCODE -ne 0) {
            Throw-DurableAgentLoopError `
                -Message "Azure CLI could not retrieve the Function key (exit code $LASTEXITCODE)." `
                -ErrorId 'DurableAgentLoopFunctionKeyRetrievalFailed' `
                -Category ConnectionError
        }

        $key = ($azOutput | ForEach-Object { [string]$_ } | Where-Object { $_ } | Select-Object -Last 1)
        if ([string]::IsNullOrWhiteSpace($key)) {
            Throw-DurableAgentLoopError `
                -Message 'Azure CLI returned an empty Function key.' `
                -ErrorId 'DurableAgentLoopFunctionKeyUnavailable' `
                -Category SecurityError
        }
        return $key
    }

    $environmentKey = [Environment]::GetEnvironmentVariable($FunctionKeyEnvironmentVariable)
    if ([string]::IsNullOrWhiteSpace($environmentKey)) {
        Throw-DurableAgentLoopError `
            -Message (
                "Provide FunctionKey, set $FunctionKeyEnvironmentVariable in the process environment, " +
                'or use RetrieveFunctionKey with FunctionAppName and ResourceGroup.'
            ) `
            -ErrorId 'DurableAgentLoopFunctionKeyRequired' `
            -Category SecurityError
    }
    return $environmentKey
}

function Resolve-DurableBaseUri {
    [CmdletBinding()]
    param()

    if ([string]::IsNullOrWhiteSpace($BaseUri)) {
        $appName = if ([string]::IsNullOrWhiteSpace($FunctionAppName)) {
            $script:DefaultFunctionAppName
        }
        else {
            $FunctionAppName
        }
        $candidate = "https://$appName.azurewebsites.net"
    }
    else {
        $candidate = $BaseUri
    }

    $uri = $null
    if (-not [Uri]::TryCreate($candidate, [UriKind]::Absolute, [ref]$uri)) {
        Throw-DurableAgentLoopError `
            -Message 'BaseUri must be an absolute Function App origin.' `
            -ErrorId 'DurableAgentLoopInvalidBaseUri' `
            -Category InvalidArgument `
            -TargetObject $candidate
    }
    if (
        $uri.UserInfo -or
        $uri.Query -or
        $uri.Fragment -or
        ($uri.AbsolutePath -ne '/' -and $uri.AbsolutePath -ne '')
    ) {
        Throw-DurableAgentLoopError `
            -Message 'BaseUri must not include credentials, a query, a fragment, or a path.' `
            -ErrorId 'DurableAgentLoopUnsafeBaseUri' `
            -Category InvalidArgument `
            -TargetObject $candidate
    }
    if ($uri.Scheme -eq 'https') {
        return $uri.GetLeftPart([UriPartial]::Authority)
    }
    if ($uri.Scheme -eq 'http' -and $AllowInsecureLocalHttp -and $uri.IsLoopback) {
        return $uri.GetLeftPart([UriPartial]::Authority)
    }

    Throw-DurableAgentLoopError `
        -Message 'BaseUri must use HTTPS. HTTP is permitted only for an explicit loopback mock.' `
        -ErrorId 'DurableAgentLoopInsecureBaseUri' `
        -Category SecurityError `
        -TargetObject $candidate
}

function ConvertTo-DurableJson {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Value,

        [Parameter(Mandatory)]
        [int]$MaximumBytes,

        [Parameter(Mandatory)]
        [string]$Purpose
    )

    $json = $Value | ConvertTo-Json -Depth 32 -Compress
    $byteCount = [Text.Encoding]::UTF8.GetByteCount($json)
    if ($byteCount -gt $MaximumBytes) {
        Throw-DurableAgentLoopError `
            -Message "$Purpose exceeds the $MaximumBytes-byte route limit." `
            -ErrorId 'DurableAgentLoopRequestTooLarge' `
            -Category LimitsExceeded
    }
    return $json
}

function Read-DurableResponseText {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [System.Net.Http.HttpContent]$Content
    )

    if (
        $null -ne $Content.Headers.ContentLength -and
        $Content.Headers.ContentLength -gt $script:MaxResponseBytes
    ) {
        Throw-DurableAgentLoopError `
            -Message "The server response exceeds the $script:MaxResponseBytes-byte client limit." `
            -ErrorId 'DurableAgentLoopResponseTooLarge' `
            -Category LimitsExceeded
    }

    $stream = $Content.ReadAsStream()
    $memory = [System.IO.MemoryStream]::new()
    $buffer = [byte[]]::new(8192)
    try {
        while (($read = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            $memory.Write($buffer, 0, $read)
            if ($memory.Length -gt $script:MaxResponseBytes) {
                Throw-DurableAgentLoopError `
                    -Message "The server response exceeds the $script:MaxResponseBytes-byte client limit." `
                    -ErrorId 'DurableAgentLoopResponseTooLarge' `
                    -Category LimitsExceeded
            }
        }
        return [Text.Encoding]::UTF8.GetString($memory.ToArray())
    }
    finally {
        $memory.Dispose()
        $stream.Dispose()
    }
}

function ConvertFrom-DurableResponse {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [System.Net.Http.HttpResponseMessage]$Response
    )

    $contentType = $Response.Content.Headers.ContentType
    if ($null -eq $contentType -or $contentType.MediaType -ne 'application/json') {
        Throw-DurableAgentLoopError `
            -Message 'The server response was not application/json.' `
            -ErrorId 'DurableAgentLoopUnexpectedContentType' `
            -Category InvalidData
    }

    $text = Read-DurableResponseText -Content $Response.Content
    try {
        $document = $text | ConvertFrom-Json -AsHashtable -Depth 32 -NoEnumerate
    }
    catch {
        Throw-DurableAgentLoopError `
            -Message 'The server response was not a JSON object.' `
            -ErrorId 'DurableAgentLoopInvalidJsonResponse' `
            -Category InvalidData
    }
    if ($document -isnot [System.Collections.IDictionary]) {
        Throw-DurableAgentLoopError `
            -Message 'The server response was not a JSON object.' `
            -ErrorId 'DurableAgentLoopInvalidJsonResponse' `
            -Category InvalidData
    }
    return $document
}

function Get-DurableServerErrorCode {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [System.Collections.IDictionary]$Document
    )

    $serverErrorValue = $Document['error']
    if (
        $serverErrorValue -is [string] -and
        $serverErrorValue -match '^[a-z0-9_:-]{1,128}$'
    ) {
        return $serverErrorValue
    }
    return 'server_error'
}

function ConvertTo-DurableOutputValue {
    [CmdletBinding()]
    param(
        [object]$Value
    )

    if ($Value -is [System.Collections.IDictionary]) {
        $properties = [ordered]@{}
        foreach ($key in $Value.Keys) {
            $properties[[string]$key] = ConvertTo-DurableOutputValue -Value $Value[$key]
        }
        return [pscustomobject]$properties
    }
    if ($Value -is [System.Collections.IEnumerable] -and $Value -isnot [string]) {
        return @($Value | ForEach-Object { ConvertTo-DurableOutputValue -Value $_ })
    }
    return $Value
}

function New-DurableOperationResult {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Operation,

        [Parameter(Mandatory)]
        [int]$HttpStatusCode,

        [Parameter(Mandatory)]
        [double]$LatencyMs,

        [Parameter(Mandatory)]
        [System.Collections.IDictionary]$Document
    )

    $properties = [ordered]@{
        operation = $Operation
        http_status = $HttpStatusCode
        latency_ms = [Math]::Round($LatencyMs, 1)
    }
    foreach ($key in $Document.Keys) {
        if ($properties.Contains($key)) {
            Throw-DurableAgentLoopError `
                -Message "The server response contains a reserved client field: $key." `
                -ErrorId 'DurableAgentLoopReservedResponseField' `
                -Category InvalidData
        }
        $properties[[string]$key] = ConvertTo-DurableOutputValue -Value $Document[$key]
    }

    $result = [pscustomobject]$properties
    $result.PSObject.TypeNames.Insert(0, "DurableAgentLoop.$Operation")
    return $result
}

function Invoke-DurableAgentLoopRequest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateSet('GET', 'POST')]
        [string]$Method,

        [Parameter(Mandatory)]
        [string]$Path,

        [string]$BodyJson,

        [string]$IdempotencyKey,

        [Parameter(Mandatory)]
        [int[]]$ExpectedStatusCodes,

        [Parameter(Mandatory)]
        [string]$Operation,

        [Parameter(Mandatory)]
        [string]$ResolvedBaseUri,

        [Parameter(Mandatory)]
        [string]$ResolvedFunctionKey,

        [Parameter(Mandatory)]
        [int]$Timeout
    )

    $requestUri = "$ResolvedBaseUri$Path"
    $client = [System.Net.Http.HttpClient]::new()
    $request = [System.Net.Http.HttpRequestMessage]::new(
        [System.Net.Http.HttpMethod]::$Method,
        $requestUri
    )
    $response = $null
    $stopwatch = [Diagnostics.Stopwatch]::StartNew()
    try {
        $client.Timeout = [TimeSpan]::FromSeconds($Timeout)
        [void]$request.Headers.Accept.ParseAdd('application/json')
        [void]$request.Headers.TryAddWithoutValidation('Cache-Control', 'no-store')
        [void]$request.Headers.TryAddWithoutValidation('x-functions-key', $ResolvedFunctionKey)
        if (-not [string]::IsNullOrWhiteSpace($IdempotencyKey)) {
            [void]$request.Headers.TryAddWithoutValidation('Idempotency-Key', $IdempotencyKey)
        }
        if ($PSBoundParameters.ContainsKey('BodyJson')) {
            $request.Content = [System.Net.Http.StringContent]::new(
                $BodyJson,
                [Text.Encoding]::UTF8,
                'application/json'
            )
        }

        try {
            $response = $client.SendAsync($request).GetAwaiter().GetResult()
        }
        catch [System.Threading.Tasks.TaskCanceledException] {
            Throw-DurableAgentLoopError `
                -Message "The $Operation request exceeded its $Timeout-second timeout." `
                -ErrorId 'DurableAgentLoopRequestTimeout' `
                -Category OperationTimeout `
                -TargetObject $Operation
        }
        catch {
            Throw-DurableAgentLoopError `
                -Message "The $Operation request failed before a response was received." `
                -ErrorId 'DurableAgentLoopNetworkFailure' `
                -Category ConnectionError `
                -TargetObject $Operation
        }

        $document = ConvertFrom-DurableResponse -Response $response
        $statusCode = [int]$response.StatusCode
        $stopwatch.Stop()
        if ($statusCode -notin $ExpectedStatusCodes) {
            $serverError = Get-DurableServerErrorCode -Document $document
            Throw-DurableAgentLoopError `
                -Message "The $Operation request returned HTTP $statusCode ($serverError)." `
                -ErrorId 'DurableAgentLoopHttpFailure' `
                -Category InvalidResult `
                -TargetObject $Operation
        }

        return [pscustomobject]@{
            document = $document
            http_status = $statusCode
            latency_ms = $stopwatch.Elapsed.TotalMilliseconds
        }
    }
    finally {
        $stopwatch.Stop()
        if ($null -ne $response) {
            $response.Dispose()
        }
        $request.Dispose()
        $client.Dispose()
    }
}

function Get-DurableRunPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Id,

        [string]$Suffix
    )

    $encodedId = [Uri]::EscapeDataString($Id)
    return "$script:RouteBase/$encodedId$Suffix"
}

function Invoke-DurableStartOperation {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [bool]$IsContinuation,

        [Parameter(Mandatory)]
        [string]$ResolvedBaseUri,

        [Parameter(Mandatory)]
        [string]$ResolvedFunctionKey
    )

    if ([string]::IsNullOrWhiteSpace($Prompt)) {
        Throw-DurableAgentLoopError `
            -Message 'Prompt is required and cannot be blank.' `
            -ErrorId 'DurableAgentLoopPromptRequired' `
            -Category InvalidArgument
    }
    if ([Text.Encoding]::UTF8.GetByteCount($Prompt) -gt $script:MaxPromptBytes) {
        Throw-DurableAgentLoopError `
            -Message "Prompt exceeds the $script:MaxPromptBytes-byte route limit." `
            -ErrorId 'DurableAgentLoopPromptTooLarge' `
            -Category LimitsExceeded
    }

    $resolvedRequestId = if ([string]::IsNullOrWhiteSpace($RequestId)) {
        "client-turn-$([guid]::NewGuid().ToString('N'))"
    }
    else {
        $RequestId
    }
    $payload = [ordered]@{
        prompt = $Prompt
        request_id = $resolvedRequestId
        sandbox_profile = $SandboxProfile.ToLowerInvariant()
        fault_profile = $FaultProfile.ToLowerInvariant()
    }
    if (-not [string]::IsNullOrWhiteSpace($SessionId)) {
        $payload['session_id'] = $SessionId
    }
    $operation = if ($IsContinuation) { 'ContinueSession' } else { 'StartRun' }
    $request = Invoke-DurableAgentLoopRequest `
        -Method POST `
        -Path $script:RouteBase `
        -BodyJson (ConvertTo-DurableJson -Value $payload -MaximumBytes ($script:MaxPromptBytes + 1KB) -Purpose 'Start request') `
        -ExpectedStatusCodes @(200, 202) `
        -Operation $operation `
        -ResolvedBaseUri $ResolvedBaseUri `
        -ResolvedFunctionKey $ResolvedFunctionKey `
        -Timeout $RequestTimeoutSeconds
    $result = New-DurableOperationResult `
        -Operation $operation `
        -HttpStatusCode $request.http_status `
        -LatencyMs $request.latency_ms `
        -Document $request.document
    $result | Add-Member -NotePropertyName request_id -NotePropertyValue $resolvedRequestId
    $result | Add-Member -NotePropertyName continuation -NotePropertyValue $IsContinuation
    $result | Add-Member `
        -NotePropertyName requires_status_check `
        -NotePropertyValue ($request.document['possibly_committed'] -eq $true)
    return $result
}

function Invoke-DurableStatusOperation {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$ResolvedBaseUri,

        [Parameter(Mandatory)]
        [string]$ResolvedFunctionKey,

        [int]$Timeout = $RequestTimeoutSeconds
    )

    $request = Invoke-DurableAgentLoopRequest `
        -Method GET `
        -Path (Get-DurableRunPath -Id $RunId) `
        -ExpectedStatusCodes @(200) `
        -Operation 'GetStatus' `
        -ResolvedBaseUri $ResolvedBaseUri `
        -ResolvedFunctionKey $ResolvedFunctionKey `
        -Timeout $Timeout
    $result = New-DurableOperationResult `
        -Operation 'GetStatus' `
        -HttpStatusCode $request.http_status `
        -LatencyMs $request.latency_ms `
        -Document $request.document
    $status = [string]$result.status
    if ($status -notin $script:RunStatuses) {
        Throw-DurableAgentLoopError `
            -Message "The status route returned an unsupported run status: $status." `
            -ErrorId 'DurableAgentLoopInvalidRunStatus' `
            -Category InvalidData `
            -TargetObject $RunId
    }
    return $result
}

function Invoke-DurableResultOperation {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$ResolvedBaseUri,

        [Parameter(Mandatory)]
        [string]$ResolvedFunctionKey,

        [int]$Timeout = $RequestTimeoutSeconds
    )

    $request = Invoke-DurableAgentLoopRequest `
        -Method GET `
        -Path (Get-DurableRunPath -Id $RunId -Suffix '/result') `
        -ExpectedStatusCodes @(200, 202) `
        -Operation 'GetResult' `
        -ResolvedBaseUri $ResolvedBaseUri `
        -ResolvedFunctionKey $ResolvedFunctionKey `
        -Timeout $Timeout
    $result = New-DurableOperationResult `
        -Operation 'GetResult' `
        -HttpStatusCode $request.http_status `
        -LatencyMs $request.latency_ms `
        -Document $request.document
    $result | Add-Member -NotePropertyName result_available -NotePropertyValue ($request.http_status -eq 200)
    return $result
}

function New-DurableWaitResult {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Outcome,

        [Parameter(Mandatory)]
        [int]$Attempts,

        [Parameter(Mandatory)]
        [double]$ElapsedSeconds,

        [Parameter(Mandatory)]
        [object]$StatusSnapshot,

        [object]$Result
    )

    $properties = [ordered]@{
        outcome = $Outcome
        run_id = $RunId
        attempts = $Attempts
        elapsed_seconds = [Math]::Round($ElapsedSeconds, 2)
        status_snapshot = $StatusSnapshot
    }
    if ($null -ne $Result) {
        $properties['result'] = $Result
    }
    $waitResult = [pscustomobject]$properties
    $waitResult.PSObject.TypeNames.Insert(0, 'DurableAgentLoop.WaitResult')
    return $waitResult
}

function Invoke-DurableWaitOperation {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$ResolvedBaseUri,

        [Parameter(Mandatory)]
        [string]$ResolvedFunctionKey
    )

    $stopwatch = [Diagnostics.Stopwatch]::StartNew()
    $attempts = 0
    while ($true) {
        $remainingSeconds = $TimeoutSeconds - $stopwatch.Elapsed.TotalSeconds
        if ($remainingSeconds -le 0) {
            Throw-DurableAgentLoopError `
                -Message "Polling exceeded the $TimeoutSeconds-second timeout." `
                -ErrorId 'DurableAgentLoopPollTimeout' `
                -Category OperationTimeout `
                -TargetObject $RunId
        }
        $perRequestTimeout = [Math]::Min(
            $RequestTimeoutSeconds,
            [Math]::Max(1, [int][Math]::Ceiling($remainingSeconds))
        )
        $snapshot = Invoke-DurableStatusOperation `
            -ResolvedBaseUri $ResolvedBaseUri `
            -ResolvedFunctionKey $ResolvedFunctionKey `
            -Timeout $perRequestTimeout
        $attempts++
        $status = [string]$snapshot.status

        if ($status -eq 'Waiting') {
            return New-DurableWaitResult `
                -Outcome 'Waiting' `
                -Attempts $attempts `
                -ElapsedSeconds $stopwatch.Elapsed.TotalSeconds `
                -StatusSnapshot $snapshot
        }
        if ($status -eq 'Completed') {
            $result = Invoke-DurableResultOperation `
                -ResolvedBaseUri $ResolvedBaseUri `
                -ResolvedFunctionKey $ResolvedFunctionKey `
                -Timeout $perRequestTimeout
            if ($result.result_available) {
                return New-DurableWaitResult `
                    -Outcome 'Completed' `
                    -Attempts $attempts `
                    -ElapsedSeconds $stopwatch.Elapsed.TotalSeconds `
                    -StatusSnapshot $snapshot `
                    -Result $result
            }
        }
        elseif ($status -in @('Failed', 'Cancelled')) {
            $terminal = New-DurableWaitResult `
                -Outcome $status `
                -Attempts $attempts `
                -ElapsedSeconds $stopwatch.Elapsed.TotalSeconds `
                -StatusSnapshot $snapshot
            if ($AllowNonSuccessTerminal) {
                return $terminal
            }
            Throw-DurableAgentLoopError `
                -Message "Run $RunId reached terminal status $status. Use AllowNonSuccessTerminal to return its status object." `
                -ErrorId 'DurableAgentLoopTerminalFailure' `
                -Category InvalidResult `
                -TargetObject $terminal
        }

        $remainingSeconds = $TimeoutSeconds - $stopwatch.Elapsed.TotalSeconds
        if ($remainingSeconds -le 0) {
            Throw-DurableAgentLoopError `
                -Message "Polling exceeded the $TimeoutSeconds-second timeout." `
                -ErrorId 'DurableAgentLoopPollTimeout' `
                -Category OperationTimeout `
                -TargetObject $RunId
        }
        $sleepMilliseconds = [int]([Math]::Min($PollIntervalSeconds, $remainingSeconds) * 1000)
        Start-Sleep -Milliseconds ([Math]::Max(1, $sleepMilliseconds))
    }
}

function Invoke-DurableSandboxOperation {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$ResolvedBaseUri,

        [Parameter(Mandatory)]
        [string]$ResolvedFunctionKey
    )

    $request = Invoke-DurableAgentLoopRequest `
        -Method GET `
        -Path (Get-DurableRunPath -Id $RunId -Suffix '/sandbox') `
        -ExpectedStatusCodes @(200) `
        -Operation 'GetRetainedSandbox' `
        -ResolvedBaseUri $ResolvedBaseUri `
        -ResolvedFunctionKey $ResolvedFunctionKey `
        -Timeout $RequestTimeoutSeconds
    $result = New-DurableOperationResult `
        -Operation 'GetRetainedSandbox' `
        -HttpStatusCode $request.http_status `
        -LatencyMs $request.latency_ms `
        -Document $request.document
    $result | Add-Member -NotePropertyName lifecycle_action -NotePropertyValue 'inspect_only'
    return $result
}

function Invoke-DurableHumanDetailOperation {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$ResolvedBaseUri,

        [Parameter(Mandatory)]
        [string]$ResolvedFunctionKey
    )

    $suffix = "/input/$([Uri]::EscapeDataString($HumanRequestId))"
    $request = Invoke-DurableAgentLoopRequest `
        -Method GET `
        -Path (Get-DurableRunPath -Id $RunId -Suffix $suffix) `
        -ExpectedStatusCodes @(200) `
        -Operation 'GetHumanInput' `
        -ResolvedBaseUri $ResolvedBaseUri `
        -ResolvedFunctionKey $ResolvedFunctionKey `
        -Timeout $RequestTimeoutSeconds
    return New-DurableOperationResult `
        -Operation 'GetHumanInput' `
        -HttpStatusCode $request.http_status `
        -LatencyMs $request.latency_ms `
        -Document $request.document
}

function Resolve-DurableHumanAnswer {
    [CmdletBinding()]
    param()

    $hasAnswer = $script:AnswerWasProvided
    $hasAnswerJson = $script:AnswerJsonWasProvided
    if ($hasAnswer -eq $hasAnswerJson) {
        Throw-DurableAgentLoopError `
            -Message 'SubmitHumanInput requires exactly one of Answer or AnswerJson.' `
            -ErrorId 'DurableAgentLoopHumanAnswerRequired' `
            -Category InvalidArgument
    }
    if ($hasAnswer) {
        return $Answer
    }

    try {
        $wrapper = ('{"answer":' + $AnswerJson + '}') |
            ConvertFrom-Json -AsHashtable -Depth 32 -NoEnumerate
    }
    catch {
        Throw-DurableAgentLoopError `
            -Message 'AnswerJson must be one valid JSON value.' `
            -ErrorId 'DurableAgentLoopInvalidHumanAnswerJson' `
            -Category InvalidArgument
    }
    if ($wrapper -isnot [System.Collections.IDictionary] -or -not $wrapper.Contains('answer')) {
        Throw-DurableAgentLoopError `
            -Message 'AnswerJson must be one valid JSON value.' `
            -ErrorId 'DurableAgentLoopInvalidHumanAnswerJson' `
            -Category InvalidArgument
    }
    return $wrapper['answer']
}

function Invoke-DurableHumanAnswerOperation {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$ResolvedBaseUri,

        [Parameter(Mandatory)]
        [string]$ResolvedFunctionKey
    )

    $answerValue = Resolve-DurableHumanAnswer
    $payload = [ordered]@{ answer = $answerValue }
    $bodyJson = ConvertTo-DurableJson `
        -Value $payload `
        -MaximumBytes ($script:MaxHumanAnswerBytes + 1KB) `
        -Purpose 'Human answer'
    $resolvedSubmissionId = if ([string]::IsNullOrWhiteSpace($SubmissionId)) {
        "client-answer-$([guid]::NewGuid().ToString('N'))"
    }
    else {
        $SubmissionId
    }
    $suffix = "/input/$([Uri]::EscapeDataString($HumanRequestId))"
    $request = Invoke-DurableAgentLoopRequest `
        -Method POST `
        -Path (Get-DurableRunPath -Id $RunId -Suffix $suffix) `
        -BodyJson $bodyJson `
        -IdempotencyKey $resolvedSubmissionId `
        -ExpectedStatusCodes @(202) `
        -Operation 'SubmitHumanInput' `
        -ResolvedBaseUri $ResolvedBaseUri `
        -ResolvedFunctionKey $ResolvedFunctionKey `
        -Timeout $RequestTimeoutSeconds
    $result = New-DurableOperationResult `
        -Operation 'SubmitHumanInput' `
        -HttpStatusCode $request.http_status `
        -LatencyMs $request.latency_ms `
        -Document $request.document
    $result | Add-Member -NotePropertyName submission_id -NotePropertyValue $resolvedSubmissionId
    return $result
}

function Invoke-DurableCancelOperation {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$ResolvedBaseUri,

        [Parameter(Mandatory)]
        [string]$ResolvedFunctionKey
    )

    $request = Invoke-DurableAgentLoopRequest `
        -Method POST `
        -Path (Get-DurableRunPath -Id $RunId -Suffix '/cancel') `
        -ExpectedStatusCodes @(202) `
        -Operation 'CancelRun' `
        -ResolvedBaseUri $ResolvedBaseUri `
        -ResolvedFunctionKey $ResolvedFunctionKey `
        -Timeout $RequestTimeoutSeconds
    $result = New-DurableOperationResult `
        -Operation 'CancelRun' `
        -HttpStatusCode $request.http_status `
        -LatencyMs $request.latency_ms `
        -Document $request.document
    $result | Add-Member -NotePropertyName cancellation_requested -NotePropertyValue $true
    return $result
}

if ($PSCmdlet.ParameterSetName -eq 'NewLogicalSession') {
    $logicalSession = [pscustomobject]@{
        operation = 'NewLogicalSession'
        session_id = [guid]::NewGuid().ToString('N')
        server_call_made = $false
    }
    $logicalSession.PSObject.TypeNames.Insert(0, 'DurableAgentLoop.LogicalSession')
    return $logicalSession
}

$resolvedFunctionKey = $null
try {
    $resolvedBaseUri = Resolve-DurableBaseUri
    $resolvedFunctionKey = Resolve-DurableFunctionKey

    switch ($PSCmdlet.ParameterSetName) {
        'StartRun' {
            Invoke-DurableStartOperation `
                -IsContinuation $false `
                -ResolvedBaseUri $resolvedBaseUri `
                -ResolvedFunctionKey $resolvedFunctionKey
            break
        }
        'ContinueSession' {
            Invoke-DurableStartOperation `
                -IsContinuation $true `
                -ResolvedBaseUri $resolvedBaseUri `
                -ResolvedFunctionKey $resolvedFunctionKey
            break
        }
        'GetStatus' {
            Invoke-DurableStatusOperation `
                -ResolvedBaseUri $resolvedBaseUri `
                -ResolvedFunctionKey $resolvedFunctionKey
            break
        }
        'WaitRun' {
            Invoke-DurableWaitOperation `
                -ResolvedBaseUri $resolvedBaseUri `
                -ResolvedFunctionKey $resolvedFunctionKey
            break
        }
        'GetResult' {
            Invoke-DurableResultOperation `
                -ResolvedBaseUri $resolvedBaseUri `
                -ResolvedFunctionKey $resolvedFunctionKey
            break
        }
        'GetRetainedSandbox' {
            Invoke-DurableSandboxOperation `
                -ResolvedBaseUri $resolvedBaseUri `
                -ResolvedFunctionKey $resolvedFunctionKey
            break
        }
        'GetHumanInput' {
            Invoke-DurableHumanDetailOperation `
                -ResolvedBaseUri $resolvedBaseUri `
                -ResolvedFunctionKey $resolvedFunctionKey
            break
        }
        'SubmitHumanInput' {
            Invoke-DurableHumanAnswerOperation `
                -ResolvedBaseUri $resolvedBaseUri `
                -ResolvedFunctionKey $resolvedFunctionKey
            break
        }
        'CancelRun' {
            Invoke-DurableCancelOperation `
                -ResolvedBaseUri $resolvedBaseUri `
                -ResolvedFunctionKey $resolvedFunctionKey
            break
        }
        default {
            Throw-DurableAgentLoopError `
                -Message "Unsupported operation: $($PSCmdlet.ParameterSetName)." `
                -ErrorId 'DurableAgentLoopUnsupportedOperation' `
                -Category InvalidArgument
        }
    }
}
finally {
    $resolvedFunctionKey = $null
}
