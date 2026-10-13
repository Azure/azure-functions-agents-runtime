# Hybrid HostedSkill

An ordinary HTTP Function that injects an endpoint-less markdown agent and
invokes it explicitly with `skill.run()`.

| Trigger | HostedSkill | Built-in Endpoints | Custom Tools | Sandbox |
|---|---|---|---|---|
| HTTP | summarizer | | | |

## Prerequisites

- Python 3.13+
- [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local)
- Azure credentials with access to a Microsoft Foundry project (`az login`)

## Run locally

1. Follow the [shared local setup](../README.md#run-locally) from this sample's
   `src` directory.
2. Start Azurite with `azurite --skipApiVersionCheck`. This compatibility flag
   lets Azurite accept the API version used by the current Azure Storage SDK.
3. Set `FOUNDRY_PROJECT_ENDPOINT` in `local.settings.json`.
4. Optionally set `FOUNDRY_MODEL` to override the runtime's `gpt-4o-mini` fallback.
5. Start the host with `func start`.
6. Send a request:

   ```bash
   curl -X POST http://localhost:7071/api/summarize \
     -H "Content-Type: application/json" \
     -d '{"prompt":"Azure Functions runs event-driven code without managing servers."}'
   ```

The response contains the generated `session_id` and summary. Pass the same
session ID in a later request to continue that agent conversation:

```json
{"prompt":"Make it even shorter.","session_id":"<session-id>"}
```

The route uses the app's default Function-key authorization when deployed, but
a format-valid `session_id` is only a conversation-continuity key. It does not
prove conversation ownership. Callers that share endpoint access can resume a
conversation when they know its ID, so applications that require per-user or
per-tenant isolation must authorize session continuation themselves.

The `summarizer.agent.md` file has no trigger or built-in endpoints. It is an
inert catalog entry selected by the `summarizer` filename slug. This sample is
local-first and does not provision Azure resources; use the repository's
deployable samples as a hosting baseline when moving it to Azure.