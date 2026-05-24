# Outlook Reply Agent

A connector-triggered agent that listens for new Office 365 Outlook email, checks whether the message is addressed to a configured mailbox, and drafts a reply instead of sending one.

| Trigger | Custom Tools | Connectors | MCP Servers | Skills | Sandbox | Chat UI |
|---|---|---|---|---|---|---|
| Office 365 Outlook | | ✅ Office 365 Outlook | ✅ Office 365 Outlook | | ✅ | |

## Features

- **Connector trigger** — uses the preview Azure Functions Connector Extension through the experimental extension bundle
- **Office 365 Outlook v2 connection** — provisions a connection under a Connector Gateway
- **Office 365 Outlook MCP server** — exposes the `DraftEmail` action so the agent can create drafts without sending messages
- **Microsoft Foundry** — provisions an AI Services account, Foundry project, and `gpt-5.4` deployment
- **ACA Dynamic Sessions** — gives the agent access to a Python session pool for code execution
- **Variable substitution** — watched mailbox address configured via `$WATCHED_SENDER_EMAIL`

## Status

This sample is intentionally experimental. The Function binding shape is based on the public [`Azure/azure-functions-connector-extension`](https://github.com/Azure/azure-functions-connector-extension) preview docs and the extension bundle `4.6.0-Experimental`. The trigger config Bicep follows [`Azure/azure-rest-api-specs` PR #41935](https://github.com/Azure/azure-rest-api-specs/pull/41935), which adds `Microsoft.Web/connectorGateways/triggerConfigs` to the `2026-05-01-preview` API surface.

The sample keeps the Function App, Foundry resources, storage, and session pool in `AZURE_LOCATION`. The Connector Gateway defaults to `westcentralus`, where `OnNewEmailV3` trigger config creation has been validated while the preview rolls out.

## Deploy

1. **Set environment variables:**

   ```bash
   cd samples/outlook-reply-agent
   azd init
   azd env set AZURE_LOCATION eastus2
   azd env set WATCHED_SENDER_EMAIL recipient@example.com
   ```

2. **Provision and deploy:**

   ```bash
   azd up
   ```

   This provisions the Function App, Microsoft Foundry, storage, ACA session pool, Connector Gateway, Office 365 Outlook connection, connection access policies, and draft-email MCP server config.

3. **Authenticate the Office 365 Outlook connection:**

   Open the deployed Office 365 Outlook connection and complete authentication. The deployment output includes `O365_CONNECTION_ID`; after signing in, the connection status should be `Connected`:

   ```bash
   az resource show --ids "$(azd env get-value O365_CONNECTION_ID)" --query properties.overallStatus -o tsv
   ```

4. **Create the trigger config:**

   The connector trigger callback URL needs the Function App system key named `connector_extension`. That key is created by the experimental connector extension after the Function host loads the app, so trigger config creation is a second step.

   ```bash
   function_name=$(azd env get-value AZURE_FUNCTION_NAME)
   gateway_name=$(azd env get-value O365_CONNECTOR_GATEWAY_NAME)
   trigger_config_name=$(azd env get-value CONNECTOR_TRIGGER_CONFIG_NAME)
   key=$(az functionapp keys list \
     -g rg-$(azd env get-value AZURE_ENV_NAME) \
     -n "$function_name" \
     --query "systemKeys.connector_extension" -o tsv)

   az deployment group create \
     -g rg-$(azd env get-value AZURE_ENV_NAME) \
     --template-file infra/app/trigger-config.bicep \
     --parameters \
       connectorGatewayName="$gateway_name" \
       connectionName=office365-outlook \
       triggerConfigName="$trigger_config_name" \
       folderPath=Inbox \
       callbackUrl="https://${function_name}.azurewebsites.net/runtime/webhooks/connector?functionName=OnNewEmail&code=${key}"
   ```

   The trigger config monitors the connected mailbox Inbox. The agent filters messages by the `To` field in its instructions.

## How It Works

- [`OnNewEmail.agent.md`](src/OnNewEmail.agent.md) registers an agent using `generic_trigger` with `type="connectorTrigger"`.
- [`host.json`](src/host.json) uses `Microsoft.Azure.Functions.ExtensionBundle.Experimental` version `[4.6.0, 5.0.0)` so the connector trigger extension is available to Python.
- The connector extension receives callbacks at `/runtime/webhooks/connector?functionName=OnNewEmail&code=<connector_extension_key>`.
- [`trigger-config.bicep`](infra/app/trigger-config.bicep) creates an `OnNewEmailV3` trigger config with `folderPath=Inbox`.
- [`connector-gateway.bicep`](infra/app/connector-gateway.bicep) grants Office 365 connection access to the Function App identity, the deployer, and the Connector Gateway identity. The Connector Gateway identity access policy is required for trigger polling.
- The agent checks the incoming email `To` field against `$WATCHED_SENDER_EMAIL`. For matching mail, it uses the Office 365 MCP `DraftEmail` tool to create a draft and never sends the message.
- For true reply drafts, the agent passes `messageId`, `draftType: "Reply"`, plain-text `comment`, and a matching `draftMessage` envelope. The connector renders `comment` literally, so it should not contain HTML tags.

## Run Locally

Follow the [shared local development guide](../README.md#run-locally) in the samples directory. Connector trigger callbacks require Azure-hosted Connector Gateway resources, so local testing is usually limited to the Function app code and MCP configuration.

Required settings for a deployed or locally configured app:

- `FOUNDRY_PROJECT_ENDPOINT`: your Microsoft Foundry project endpoint
- `FOUNDRY_MODEL`: model deployment name, such as `gpt-5.4`
- `O365_MCP_SERVER_URL`: Office 365 Outlook MCP server URL
- `WATCHED_SENDER_EMAIL`: mailbox recipient address that should produce reply drafts

Optional:

- `O365_MCP_CLIENT_ID`: managed identity client ID for the Office 365 Outlook MCP server; defaults to the app-wide identity selection
- `ACA_SESSION_POOL_ENDPOINT`: if set, enables code execution features

## Notes

- The trigger config resource stores `notificationDetails.callbackUrl`. Avoid dumping full trigger config or run resources in logs because callback URLs include the Function connector system key.
- This sample does not expose a web search or weather MCP tool by default. If an email asks for live external information, the agent should draft a follow-up asking for the missing context rather than inventing facts.