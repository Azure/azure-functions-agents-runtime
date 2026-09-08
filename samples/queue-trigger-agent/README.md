# Queue Trigger Agent

A non-HTTP agent that processes Azure Storage Queue messages. Each message triggers an
agent invocation with a structured JSON payload containing the message body and queue
metadata.

| Trigger | Custom Tools | Connectors | MCP Servers | Skills | Sandbox | Chat UI |
|---|---|---|---|---|---|---|
| Azure Storage Queue | | | | | Yes | |

## Features

- **Queue trigger** - Processes messages placed on the `agent-input` Azure Storage Queue.
- **Structured trigger payload** - The agent receives JSON-safe queue data such as `body`,
  `body_encoding`, `id`, and `dequeue_count`, rather than a Python binding representation.
- **Microsoft Foundry** - Provisions an AI Services account, Foundry project, and `gpt-5.4`
  deployment.
- **Flex Consumption** - Deploys the Function App on the FC1 Flex Consumption plan.

Queue triggers are fully supported on Flex Consumption. Unlike a blob trigger on Flex,
this sample does not require `source=EventGrid` or an Event Grid system topic; it needs
only the storage queue.

## Prerequisites

- [Azure Developer CLI (`azd`)](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd)
- [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local)
- An Azure subscription

## Deploy

1. Set up the environment:

   ```bash
   cd samples/queue-trigger-agent
   azd init
   azd env set AZURE_LOCATION eastus2
   ```

   `AZURE_LOCATION` is restricted to regions that support both Azure Functions Flex
   Consumption and the sample's default Microsoft Foundry `gpt-5.4` Global Standard
   deployment: `brazilsouth`, `canadacentral`, `canadaeast`, `centralus`, `eastus`,
   `eastus2`, `northcentralus`, `southcentralus`, `westus`, and `westus3`.

2. Deploy:

   ```bash
   azd up
   ```

   The deployment provisions the `agent-input` queue and exposes its name as
   `AZURE_STORAGE_QUEUE_NAME`.

3. Send a test message:

   ```bash
   storage_account=$(azd env get-value AZURE_STORAGE_ACCOUNT_NAME)
   az storage message put \
     --account-name "$storage_account" \
     --queue-name agent-input \
     --auth-mode login \
     --content 'Summarize this: order #42 for 3 widgets'
   ```

   The signed-in principal needs the **Storage Queue Data Contributor** role on the
   storage account to use `--auth-mode login`.

## Run Locally

Follow the [shared local development guide](../README.md#run-locally). This sample uses
`AzureWebJobsStorage=UseDevelopmentStorage=true`, so start Azurite before the Functions
host:

```bash
azurite
```

Copy `src/local.settings.template.json` to `src/local.settings.json`, then set
`FOUNDRY_PROJECT_ENDPOINT` and `FOUNDRY_MODEL`. Start the host from `src`:

```bash
func start
```

In another terminal, create the local queue and add a message:

```bash
az storage queue create \
  --name agent-input \
  --connection-string 'UseDevelopmentStorage=true'

az storage message put \
  --queue-name agent-input \
  --connection-string 'UseDevelopmentStorage=true' \
  --content 'Summarize this: order #42 for 3 widgets'
```

## What To Expect

The `queue_processor` function runs once a message is available. The runtime places a
serialized queue payload in the agent prompt instead of a Python `QueueMessage` repr.
For a UTF-8 message, the prompt includes data shaped like:

```json
{
  "body": "Summarize this: order #42 for 3 widgets",
  "body_encoding": "utf-8",
  "id": "<queue-message-id>",
  "dequeue_count": 1,
  "insertion_time": "<timestamp>",
  "expiration_time": "<timestamp>",
  "time_next_visible": "<timestamp>",
  "pop_receipt": "<receipt>"
}
```

If the message body is valid JSON, the payload can also include `body_json`. The agent
uses the body and metadata to produce a concise structured summary and implied action;
view the Function App logs or Application Insights for the result.

## How It Works

- [`queue_processor.agent.md`](src/queue_processor.agent.md) declares the
  `queue_trigger` for `agent-input` using `AzureWebJobsStorage`.
- The Bicep template creates the queue alongside the Function App, Microsoft Foundry,
  storage, monitoring, and ACA Dynamic Sessions resources from the basic-chat template.
- The Function App's managed identity already receives **Storage Queue Data Contributor**
  through the copied storage RBAC module.
