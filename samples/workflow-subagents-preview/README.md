# Parallel PR status portfolio report

This sample receives pull requests from Azure Storage Queue, reviews every pull
request in parallel with an isolated specialist, combines the summaries into an
HTML portfolio report, and publishes the report to Azure Blob Storage.

The pull-request tools use deterministic synthetic data, so no GitHub token is
required. A model provider is required to run the PR analysts and report writer.

## Workflow shape

```text
Queue message
  +-- PR analyst: pull request A --+
  +-- PR analyst: pull request B --+-- HTML report writer -- publish Blob
  +-- PR analyst: pull request C --+
```

Each PR analyst uses the fake `get_pull_request_status` and
`get_pull_request_activity` tools plus the `pr-status-analysis` skill. The
report writer receives only the compact analyst summaries and returns one
responsive HTML document. The publisher writes to the exact `report_blob`
provided by the queue message with overwrite enabled, so submitting the same
destination again updates one stable Blob instead of creating duplicates.

Example queue message:

```json
{
  "report_title": "Functions team PR status",
  "report_blob": "reports/functions-pr-status.html",
  "pull_requests": [
    {
      "url": "https://github.com/Azure/azure-functions-host/pull/123",
      "last_checked_at": "2026-07-22T17:00:00Z"
    },
    {
      "url": "https://github.com/Azure/azure-functions-python-worker/pull/456",
      "last_checked_at": "2026-07-22T17:00:00Z"
    }
  ]
}
```

## Run locally

Create the environment and local settings:

```powershell
Set-Location samples\workflow-subagents-preview\src
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item local.settings.template.json local.settings.json
```

Set `FOUNDRY_PROJECT_ENDPOINT` and `FOUNDRY_MODEL` in
`local.settings.json`, then authenticate with `az login`.

Start Azurite for the input queue, output Blob, and Functions host storage:

```powershell
docker run --rm --name workflow-subagents-azurite `
  -p 10000:10000 -p 10001:10001 -p 10002:10002 `
  mcr.microsoft.com/azure-storage/azurite:latest `
  azurite --silent --skipApiVersionCheck `
  --blobHost 0.0.0.0 --queueHost 0.0.0.0 --tableHost 0.0.0.0
```

Azurite must still be running when this sample uses the Durable Task Scheduler
(DTS), because the queue, report Blob, and Functions host storage remain in
Azurite.

In another terminal, start the DTS emulator. Dynamic Task Hubs let it create
the configured `prstatusreports` hub automatically:

```powershell
docker run --rm --name workflow-subagents-dts `
  -e DTS_USE_DYNAMIC_TASK_HUBS=true `
  -p 8080:8080 -p 8082:8082 `
  mcr.microsoft.com/dts/dts-emulator:latest
```

Port `8080` is the scheduler endpoint used by the Functions host. Open the DTS
dashboard at <http://localhost:8082> to inspect workflow instances and their
progress.

With both emulators running, start the Functions host from the activated
environment:

```powershell
func start
```

Create the queue and submit the JSON message with Azure Storage Explorer, Azure
CLI, or the Azure Storage SDK. Use:

- queue: `pr-status-requests`
- storage connection: `UseDevelopmentStorage=true`
- output container: `workflow-reports`

After processing completes, download
`workflow-reports/reports/functions-pr-status.html`. Submit the same message a
second time and confirm that the same Blob is replaced.

## Adapt for production

Replace the two fake PR tools with GitHub API implementations and use managed
identity for Azure Storage. Keep the same shape: one isolated analyst per pull
request, a final report writer that receives the analyst summaries, and one
stable output destination.
