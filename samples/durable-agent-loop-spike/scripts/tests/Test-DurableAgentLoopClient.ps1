#requires -Version 7.0
<#
.SYNOPSIS
Exercises Invoke-DurableAgentLoop.ps1 against a local source-contract mock.

.DESCRIPTION
The mock implements only the current private Durable Agent Loop HTTP contract.
It records header presence, never header values, and does not contact Azure or
start a model run.
#>
[CmdletBinding()]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute(
    'PSUseUsingScopeModifierInNewRunspaces',
    '',
    Justification = 'The mock job receives values through its explicit param block.'
)]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-AvailableLoopbackPort {
    [CmdletBinding()]
    param()

    $listener = [System.Net.Sockets.TcpListener]::new(
        [System.Net.IPAddress]::Loopback,
        0
    )
    try {
        $listener.Start()
        return ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
    }
    finally {
        $listener.Stop()
    }
}

function Wait-ForMockReady {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$ReadyPath,

        [Parameter(Mandatory)]
        [System.Management.Automation.Job]$Job
    )

    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    while (-not (Test-Path -LiteralPath $ReadyPath)) {
        if ($Job.State -in @('Failed', 'Stopped', 'Completed')) {
            $reason = $Job.ChildJobs[0].JobStateInfo.Reason
            throw "The local mock did not start: $reason"
        }
        if ([DateTime]::UtcNow -ge $deadline) {
            throw 'The local mock did not become ready within 10 seconds.'
        }
        Start-Sleep -Milliseconds 50
    }
}

$clientScript = Join-Path $PSScriptRoot '..\Invoke-DurableAgentLoop.ps1'
$clientScript = (Resolve-Path -LiteralPath $clientScript).Path
$routeBase = '/api/experimental/durable-agent-runs'
$testRoot = Join-Path ([System.IO.Path]::GetTempPath()) (
    "durable-agent-loop-client-$([guid]::NewGuid().ToString('N'))"
)
$readyPath = Join-Path $testRoot 'ready'
$stopPath = Join-Path $testRoot 'stop'
[void](New-Item -ItemType Directory -Path $testRoot -Force)
$port = Get-AvailableLoopbackPort

$mockJob = Start-Job -ScriptBlock {
    param(
        [int]$Port,
        [string]$ReadyPath,
        [string]$StopPath
    )

    Set-StrictMode -Version Latest
    $ErrorActionPreference = 'Stop'
    $routeBase = '/api/experimental/durable-agent-runs'
    $runFirst = 'run-11111111111111111111111111111111'
    $runSecond = 'run-22222222222222222222222222222222'
    $runWait = 'run-33333333333333333333333333333333'
    $runHuman = 'run-44444444444444444444444444444444'
    $runHumanJson = 'run-88888888888888888888888888888888'
    $runCancel = 'run-55555555555555555555555555555555'
    $runFailed = 'run-66666666666666666666666666666666'
    $runAmbiguous = 'run-77777777777777777777777777777777'
    $humanRequestId = 'human-1-0123456789abcdef'
    $humanJsonRequestId = 'human-2-fedcba9876543210'
    $requests = [System.Collections.Generic.List[object]]::new()
    $waitStatusRequests = 0
    $listener = [System.Net.HttpListener]::new()
    $listener.Prefixes.Add("http://127.0.0.1:$Port/")

    function Send-MockJson {
        param(
            [Parameter(Mandatory)]
            [System.Net.HttpListenerContext]$Context,

            [Parameter(Mandatory)]
            [int]$StatusCode,

            [Parameter(Mandatory)]
            [hashtable]$Document
        )

        $content = [Text.Encoding]::UTF8.GetBytes(
            ($Document | ConvertTo-Json -Depth 16 -Compress)
        )
        $Context.Response.StatusCode = $StatusCode
        $Context.Response.ContentType = 'application/json; charset=utf-8'
        $Context.Response.ContentLength64 = $content.Length
        $Context.Response.OutputStream.Write($content, 0, $content.Length)
        $Context.Response.Close()
    }

    function ConvertTo-MockAcceptedRun {
        param(
            [Parameter(Mandatory)]
            [string]$RunId,

            [Parameter(Mandatory)]
            [string]$SessionId
        )

        return @{
            cancel_url = "$routeBase/$RunId/cancel"
            result_url = "$routeBase/$RunId/result"
            run_id = $RunId
            session_id = $SessionId
            status = 'Pending'
            status_url = "$routeBase/$RunId"
        }
    }

    try {
        $listener.Start()
        [System.IO.File]::WriteAllText($ReadyPath, 'ready')
        $pendingContext = $listener.GetContextAsync()
        $deadline = [DateTime]::UtcNow.AddSeconds(15)
        while ($requests.Count -lt 13) {
            if (Test-Path -LiteralPath $StopPath) {
                break
            }
            if (-not $pendingContext.Wait(200)) {
                if ([DateTime]::UtcNow -ge $deadline) {
                    throw 'The local mock timed out before receiving the expected requests.'
                }
                continue
            }
            $context = $pendingContext.GetAwaiter().GetResult()
            $pendingContext = $listener.GetContextAsync()
            $request = $context.Request
            $bodyText = [System.IO.StreamReader]::new($request.InputStream).ReadToEnd()
            $body = if ([string]::IsNullOrWhiteSpace($bodyText)) {
                @{}
            }
            else {
                $bodyText | ConvertFrom-Json -AsHashtable -Depth 16 -NoEnumerate
            }
            $record = [pscustomobject]@{
                method = $request.HttpMethod
                path = $request.Url.AbsolutePath
                has_function_key = -not [string]::IsNullOrWhiteSpace(
                    $request.Headers['x-functions-key']
                )
                has_code_query = -not [string]::IsNullOrWhiteSpace(
                    $request.Url.Query
                )
                has_submission_id = -not [string]::IsNullOrWhiteSpace(
                    $request.Headers['Idempotency-Key']
                )
                body = $body
            }
            $requests.Add($record)

            switch ("$($request.HttpMethod) $($request.Url.AbsolutePath)") {
                "POST $routeBase" {
                    $prompt = [string]$body['prompt']
                    $sessionId = [string]$body['session_id']
                    if ($prompt -eq 'first turn') {
                        Send-MockJson $context 202 (ConvertTo-MockAcceptedRun $runFirst $sessionId)
                    }
                    elseif ($prompt -eq 'second turn') {
                        Send-MockJson $context 202 (ConvertTo-MockAcceptedRun $runSecond $sessionId)
                    }
                    elseif ($prompt -eq 'ambiguous turn') {
                        Send-MockJson $context 202 @{
                            error = 'run_start_acknowledgement_lost'
                            possibly_committed = $true
                            run_id = $runAmbiguous
                        }
                    }
                    else {
                        Send-MockJson $context 400 @{ error = 'invalid_prompt' }
                    }
                    break
                }
                "GET $routeBase/$runSecond/sandbox" {
                    Send-MockJson $context 200 @{
                        sandbox_instance_alias = 'sandbox-1234abcd'
                        generation = 1
                        state = 'Stopped'
                        workspace_checkpoint_present = $true
                    }
                    break
                }
                "GET $routeBase/$runWait" {
                    $waitStatusRequests++
                    if ($waitStatusRequests -eq 1) {
                        Send-MockJson $context 200 @{
                            run_id = $runWait
                            session_id = 'unused-by-client'
                            status = 'Running'
                            phase = 'model_step'
                            model_steps = 1
                        }
                    }
                    else {
                        Send-MockJson $context 200 @{
                            run_id = $runWait
                            session_id = 'unused-by-client'
                            status = 'Completed'
                            phase = 'completed'
                            model_steps = 2
                        }
                    }
                    break
                }
                "GET $routeBase/$runWait/result" {
                    Send-MockJson $context 200 @{
                        run_id = $runWait
                        session_id = 'unused-by-client'
                        status = 'Completed'
                        response = 'mock-complete'
                    }
                    break
                }
                "GET $routeBase/$runHuman/input/$humanRequestId" {
                    Send-MockJson $context 200 @{
                        run_id = $runHuman
                        session_id = 'unused-by-client'
                        request_id = $humanRequestId
                        question = 'Pick a color.'
                        choices = @('Blue', 'Green')
                        allow_free_text = $false
                        expires_at = '2026-09-11T20:00:00+00:00'
                        response_schema = $null
                    }
                    break
                }
                "POST $routeBase/$runHuman/input/$humanRequestId" {
                    Send-MockJson $context 202 @{
                        run_id = $runHuman
                        request_id = $humanRequestId
                        status = 'accepted'
                        delivery = 'delivered'
                    }
                    break
                }
                "POST $routeBase/$runHumanJson/input/$humanJsonRequestId" {
                    Send-MockJson $context 202 @{
                        run_id = $runHumanJson
                        request_id = $humanJsonRequestId
                        status = 'accepted'
                        delivery = 'delivered'
                    }
                    break
                }
                "POST $routeBase/$runCancel/cancel" {
                    Send-MockJson $context 202 @{
                        run_id = $runCancel
                        status = 'Cancelled'
                        delivery = 'retry_pending'
                    }
                    break
                }
                "GET $routeBase/$runFailed" {
                    Send-MockJson $context 200 @{
                        run_id = $runFailed
                        session_id = 'unused-by-client'
                        status = 'Failed'
                        phase = 'run'
                        error = 'tool_outcome_ambiguous'
                    }
                    break
                }
                "GET $routeBase/run-99999999999999999999999999999999" {
                    Send-MockJson $context 200 @{
                        run_id = 'run-99999999999999999999999999999999'
                        session_id = 'unused-by-client'
                        status = 'Pending'
                        phase = 'durable'
                    }
                    break
                }
                default {
                    Send-MockJson $context 404 @{ error = 'run_not_found' }
                    break
                }
            }
        }
        return $requests
    }
    finally {
        if ($listener.IsListening) {
            $listener.Stop()
        }
        $listener.Close()
    }
} -ArgumentList $port, $readyPath, $stopPath

try {
    Wait-ForMockReady -ReadyPath $readyPath -Job $mockJob
    $keyText = [guid]::NewGuid().ToString('N')
    $key = [System.Security.SecureString]::new()
    foreach ($character in $keyText.ToCharArray()) {
        $key.AppendChar($character)
    }
    $key.MakeReadOnly()
    $keyText = $null
    $common = @{
        BaseUri = "http://127.0.0.1:$port"
        FunctionKey = $key
        AllowInsecureLocalHttp = $true
        RequestTimeoutSeconds = 10
    }

    $session = & $clientScript -NewLogicalSession
    if (
        $session.server_call_made -or
        $session.session_id -notmatch '^[0-9a-f]{32}$'
    ) {
        throw 'NewLogicalSession must create a local 32-character hexadecimal ID.'
    }

    $first = & $clientScript -StartRun @common `
        -SessionId $session.session_id `
        -Prompt 'first turn' `
        -SandboxProfile retained_session
    if ($first.run_id -ne 'run-11111111111111111111111111111111' -or $first.continuation) {
        throw 'StartRun did not return the accepted first-turn contract.'
    }

    $second = & $clientScript -ContinueSession @common `
        -SessionId $session.session_id `
        -Prompt 'second turn' `
        -SandboxProfile retained_session
    if (
        $second.run_id -ne 'run-22222222222222222222222222222222' -or
        -not $second.continuation
    ) {
        throw 'ContinueSession did not return the accepted continuation contract.'
    }

    $sandbox = & $clientScript -GetRetainedSandbox @common -RunId $second.run_id
    if (
        $sandbox.lifecycle_action -ne 'inspect_only' -or
        $sandbox.state -ne 'Stopped' -or
        -not $sandbox.workspace_checkpoint_present
    ) {
        throw 'GetRetainedSandbox did not return the inspection-only contract.'
    }

    $wait = & $clientScript -WaitRun @common `
        -RunId 'run-33333333333333333333333333333333' `
        -PollIntervalSeconds 0.5 `
        -TimeoutSeconds 10
    if (
        $wait.outcome -ne 'Completed' -or
        $wait.result.response -ne 'mock-complete' -or
        $wait.attempts -ne 2
    ) {
        throw 'WaitRun did not poll through completion and retrieve the terminal result.'
    }

    $human = & $clientScript -GetHumanInput @common `
        -RunId 'run-44444444444444444444444444444444' `
        -HumanRequestId 'human-1-0123456789abcdef'
    if ($human.question -ne 'Pick a color.' -or $human.choices.Count -ne 2) {
        throw 'GetHumanInput did not return the source contract.'
    }

    $answer = & $clientScript -SubmitHumanInput @common `
        -RunId 'run-44444444444444444444444444444444' `
        -HumanRequestId 'human-1-0123456789abcdef' `
        -Answer 'Blue'
    if ($answer.status -ne 'accepted' -or $answer.delivery -ne 'delivered') {
        throw 'SubmitHumanInput did not return the accepted-delivery contract.'
    }

    $jsonAnswer = & $clientScript -SubmitHumanInput @common `
        -RunId 'run-88888888888888888888888888888888' `
        -HumanRequestId 'human-2-fedcba9876543210' `
        -AnswerJson '{"choice":"Blue"}'
    if ($jsonAnswer.status -ne 'accepted' -or $jsonAnswer.delivery -ne 'delivered') {
        throw 'SubmitHumanInput did not serialize an AnswerJson value.'
    }

    $cancel = & $clientScript -CancelRun @common -RunId 'run-55555555555555555555555555555555'
    if (
        -not $cancel.cancellation_requested -or
        $cancel.delivery -ne 'retry_pending'
    ) {
        throw 'CancelRun did not retain the durable delivery status.'
    }

    $terminalFailureRaised = $false
    try {
        & $clientScript -WaitRun @common `
            -RunId 'run-66666666666666666666666666666666' `
            -PollIntervalSeconds 0.5 `
            -TimeoutSeconds 10
    }
    catch {
        $terminalFailureRaised = $_.FullyQualifiedErrorId -like 'DurableAgentLoopTerminalFailure*'
    }
    if (-not $terminalFailureRaised) {
        throw 'WaitRun must raise a terminating error for Failed runs by default.'
    }

    $ambiguous = & $clientScript -StartRun @common `
        -SessionId $session.session_id `
        -Prompt 'ambiguous turn'
    if (
        -not $ambiguous.possibly_committed -or
        -not $ambiguous.requires_status_check
    ) {
        throw 'StartRun must report a possibly committed acknowledgement loss.'
    }

    $env:DURABLE_LOOP_FUNCTION_KEY = [guid]::NewGuid().ToString('N')
    try {
        $environmentKeyStatus = & $clientScript `
            -GetStatus `
            -BaseUri "http://127.0.0.1:$port" `
            -AllowInsecureLocalHttp `
            -RunId 'run-99999999999999999999999999999999'
    }
    finally {
        Remove-Item Env:DURABLE_LOOP_FUNCTION_KEY -ErrorAction SilentlyContinue
    }
    if ($environmentKeyStatus.status -ne 'Pending') {
        throw 'GetStatus did not accept a process-environment Function key.'
    }

    if (-not (Wait-Job -Job $mockJob -Timeout 10)) {
        throw 'The local mock did not receive the expected client requests.'
    }
    $requests = @(Receive-Job -Job $mockJob -ErrorAction Stop)
    if ($requests.Count -ne 13) {
        throw "Expected 13 HTTP requests; received $($requests.Count)."
    }
    if ($requests | Where-Object { -not $_.has_function_key -or $_.has_code_query }) {
        throw 'The client must use the Function key header and must not add a URL query.'
    }
    if (
        $requests[0].body['session_id'] -ne $session.session_id -or
        $requests[1].body['session_id'] -ne $session.session_id
    ) {
        throw 'StartRun and ContinueSession must send the same supplied logical session ID.'
    }
    if (
        $requests[0].body['sandbox_profile'] -ne 'retained_session' -or
        $requests[1].body['sandbox_profile'] -ne 'retained_session'
    ) {
        throw 'Retained sandbox selection was not serialized into both turn payloads.'
    }
    if (
        -not $requests[7].has_submission_id -or
        $requests[7].body['answer'] -ne 'Blue'
    ) {
        throw 'SubmitHumanInput must send an idempotency key and the supplied answer.'
    }
    $jsonAnswerRequest = @($requests | Where-Object {
        $_.path -eq "$routeBase/run-88888888888888888888888888888888/input/human-2-fedcba9876543210"
    })
    if (
        $jsonAnswerRequest.Count -ne 1 -or
        $jsonAnswerRequest.body['answer']['choice'] -ne 'Blue'
    ) {
        throw 'AnswerJson must be embedded as one JSON answer value.'
    }

    'Durable Agent Loop PowerShell client source-contract mock: passed'
}
finally {
    if ($mockJob.State -eq 'Running' -and -not (Test-Path -LiteralPath $stopPath)) {
        [System.IO.File]::WriteAllText($stopPath, 'stop')
        [void](Wait-Job -Job $mockJob -Timeout 5)
    }
    if ($mockJob.State -eq 'Running') {
        Stop-Job -Job $mockJob
    }
    Remove-Job -Job $mockJob -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
}
