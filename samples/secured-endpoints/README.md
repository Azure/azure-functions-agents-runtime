# Secured Endpoints

Demonstrates inbound authentication for built-in agent endpoints: **API key** (Azure Functions function/host keys) and **Entra ID** (Azure AD) for production. Two agents run side by side so you can test both modes against the same deployed app.

| Trigger | Built-in Endpoints | Custom Tools | Connectors | MCP Servers | Skills | Sandbox | Chat UI |
|---|---|---|---|---|---|---|---|
| | ✅ HTTP + MCP | | | | | | |

## What this sample shows

| Agent | Endpoint | `auth` mode | How to call it |
|---|---|---|---|
| `apikey` | `POST /agents/apikey/chat` | `function` | Requires a valid **function/host key** (`x-functions-key` header or `?code=`). |
| `entra` | `POST /agents/entra/chat` | `entra` | Requires a valid **Entra ID token**. App Service Authentication (Easy Auth) validates the token and injects a client principal; the runtime enforces that principal in-app. |

The `apikey` agent also registers an MCP tool on the shared `/runtime/webhooks/mcp` webhook. See [MCP endpoints](#mcp-endpoints) for how that is secured.

## Resources created

`azd up` provisions everything in a new resource group:

- **Azure Function App** (Flex Consumption, Python 3.13) — hosts both agents
- **Storage account** — deployment package + session history
- **Microsoft Foundry** AI Services account, project, and `gpt-5.4` deployment
- **Application Insights + Log Analytics** — observability
- **User-assigned managed identity** + RBAC role assignments (storage, Foundry, monitoring)

No Entra app registration is created by the template. You create one yourself and pass its client ID as `ENTRA_CLIENT_ID`; the template then configures **App Service Authentication (Easy Auth)** with the Microsoft identity provider so the platform validates Entra tokens. See [Test 2](#test-2--entra-id-agent).

## Prerequisites

- [Azure Developer CLI (`azd`)](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd)
- [Azure CLI (`az`)](https://learn.microsoft.com/cli/azure/install-azure-cli)
- [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local) (for local runs)
- An Azure subscription

## Deploy

1. **Initialize and pick a region:**

   ```bash
   cd samples/secured-endpoints
   azd init
   azd env set AZURE_LOCATION eastus2
   ```

   `AZURE_LOCATION` must support both Azure Functions Flex Consumption and the default Microsoft Foundry `gpt-5.4` Global Standard deployment: `brazilsouth`, `canadacentral`, `canadaeast`, `centralus`, `eastus`, `eastus2`, `northcentralus`, `southcentralus`, `westus`, `westus3`.

2. *(For the Entra agent)* create an app registration and point the template at it so Easy Auth is configured:

   ```bash
   APP_ID=$(az ad app create --display-name "agents-secured" --query appId -o tsv)
   az ad app update --id "$APP_ID" --identifier-uris "api://agents-secured"
   azd env set ENTRA_CLIENT_ID "$APP_ID"
   azd env set ENTRA_ALLOWED_AUDIENCES "api://agents-secured"
   ```

   Leave `ENTRA_CLIENT_ID` unset to skip Easy Auth setup — the `entra` agent then rejects every request (fail closed) until Easy Auth is configured. The `apikey` agent is unaffected.

3. **Deploy:**

   ```bash
   azd up
   ```

   When it finishes, note these outputs (also visible via `azd env get-values`):
   - `AZURE_FUNCTION_NAME` — the Function App name
   - `AZURE_FUNCTIONS_AGENTS_ENTRA_TENANT_ID` — the tenant the entra agent trusts
   - `AZURE_FUNCTIONS_AGENTS_EASY_AUTH_ENABLED` — whether Easy Auth was configured

## Test 1 — API key agent

The `apikey` agent uses `auth: function`, so the chat route requires a function key.

1. **Get the app name and default function key:**

   ```bash
   APP=$(azd env get-value AZURE_FUNCTION_NAME)
   RG=$(azd env get-value AZURE_RESOURCE_GROUP)
   KEY=$(az functionapp keys list -g "$RG" -n "$APP" --query functionKeys.default -o tsv)
   ```

2. **Call without a key → `401 Unauthorized`:**

   ```bash
   curl -i -X POST "https://$APP.azurewebsites.net/agents/apikey/chat" \
     -H "Content-Type: application/json" \
     -d '{"prompt": "Say hello in one sentence"}'
   ```

3. **Call with the key → `200 OK`:**

   ```bash
   curl -i -X POST "https://$APP.azurewebsites.net/agents/apikey/chat" \
     -H "Content-Type: application/json" \
     -H "x-functions-key: $KEY" \
     -d '{"prompt": "Say hello in one sentence"}'
   ```

   The key can also be passed as a query string: `.../agents/apikey/chat?code=$KEY`.

> Use `auth: admin` instead of `function` to require the **master key** (`AuthLevel.ADMIN` maps to the Functions `_master` key — the most privileged app credential, distinct from an extension system key). Use `auth: anonymous` to disable key checks entirely.

## Test 2 — Entra ID agent

The `entra` agent uses `auth: mode: entra`. The route is anonymous at the Functions
key layer; **App Service Authentication (Easy Auth)** validates the incoming Entra
token at the platform and injects an `x-ms-client-principal` header. The runtime
reads that principal, confirms it is an Entra (`aad`) identity, and applies the
configured tenant / audience / client-id allowlists. If no validated principal is
present, the request is rejected with `401` (fail closed) — the runtime never
validates tokens itself.

> Because the route is anonymous at the Functions key layer, the runtime trusts the
> injected principal **only** when it has non-spoofable evidence that Easy Auth is
> enforced: the platform-injected `WEBSITE_AUTH_ENABLED`, or the
> `AZURE_FUNCTIONS_AGENTS_ENTRA_EASY_AUTH` app setting (the template sets this to
> `true` when you deploy with `ENTRA_CLIENT_ID`). Without that evidence, a
> caller-supplied `x-ms-client-principal` header is rejected.

> Easy Auth is configured only when you deployed with `ENTRA_CLIENT_ID` set (step 2
> above). Without it, every call to the entra agent returns `401`.

Easy Auth runs in "allow anonymous" mode so the API-key agent keeps working:
unauthenticated requests still reach the function, but any token they carry is
validated by the platform first.

### Call the agent

Because Easy Auth intercepts the request, you send the bearer token exactly as
before — the platform (not the runtime) validates it and forwards the principal:

```bash
APP=$(azd env get-value AZURE_FUNCTION_NAME)
TOKEN=$(az account get-access-token --resource "api://agents-secured" --query accessToken -o tsv)

# Without a token → 401 (no validated principal)
curl -i -X POST "https://$APP.azurewebsites.net/agents/entra/chat" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Say hello in one sentence"}'

# With a token for the configured audience → 200
curl -i -X POST "https://$APP.azurewebsites.net/agents/entra/chat" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"prompt": "Say hello in one sentence"}'
```

A token whose `aud` is not in `AZURE_FUNCTIONS_AGENTS_ENTRA_AUDIENCES` returns
`403 Forbidden`; a token from a different tenant returns `403`; a missing or
platform-rejected token returns `401`.

To also require a specific **caller application**, set
`AZURE_FUNCTIONS_AGENTS_ENTRA_CLIENT_IDS` (comma-separated `appid`/`azp` values) as
an app setting, or add `allowed_client_ids` to the agent frontmatter.

### Browser / delegated sign-in

The same Easy Auth configuration also supports interactive sign-in: point a browser
at the app and the Microsoft identity provider handles the login flow, after which
the injected principal is read by the runtime the same way. See
[App Service Authentication](https://learn.microsoft.com/azure/app-service/overview-authentication-authorization)
for details.


## MCP endpoints

`/runtime/webhooks/mcp` is owned by the Azure Functions MCP extension, so its HTTP surface is authenticated at the **platform** layer, not in-app:

- **API key:** protect it with the runtime **system key** (`az functionapp keys list --query systemKeys`).
- **Entra ID:** enable App Service Easy Auth on the Function App (see [Test 2](#test-2--entra-id-agent)).

Because the MCP handler never sees HTTP headers in-process, `auth.mode: entra` cannot be enforced in-app for MCP. When an agent enables both `mcp` and `auth: entra`, the runtime logs a one-time note pointing to platform Easy Auth.

## Configuration reference

`builtin_endpoints.auth` accepts a shorthand string (`auth: function`) or an object:

```yaml
builtin_endpoints:
  chat_api: true
  auth:
    mode: entra            # function (default) | admin | anonymous | entra
    entra:                 # only used when mode == entra
      tenant_id: "<guid>"                  # or env AZURE_FUNCTIONS_AGENTS_ENTRA_TENANT_ID
      allowed_audiences: ["api://agents"]  # or env AZURE_FUNCTIONS_AGENTS_ENTRA_AUDIENCES
      allowed_client_ids: ["<app-id>"]     # or env AZURE_FUNCTIONS_AGENTS_ENTRA_CLIENT_IDS
```

| Mode | Behavior |
|---|---|
| `function` (default) | Requires a function/host key (`AuthLevel.FUNCTION`). |
| `admin` | Requires the master key (`AuthLevel.ADMIN` maps to the Functions `_master` key — the most privileged app credential, distinct from an extension system key). |
| `anonymous` | No auth. |
| `entra` | Route is anonymous at the Functions key layer; App Service Authentication (Easy Auth) validates the Entra token and the runtime enforces the injected client principal with optional tenant/audience/client-id allowlists. Requires Easy Auth to be configured. |

See [`docs/front-matter-spec.md`](../../docs/front-matter-spec.md#auth--endpoint-authentication) for the full schema.

## Run locally

Follow the [shared local development guide](../README.md#run-locally). Set these in `src/local.settings.json`:

- `AZURE_FUNCTIONS_AGENTS_PROVIDER`: `foundry`
- `FOUNDRY_PROJECT_ENDPOINT`, `FOUNDRY_MODEL`: your Foundry project and model deployment
- `AZURE_FUNCTIONS_AGENTS_ENTRA_TENANT_ID`, `AZURE_FUNCTIONS_AGENTS_ENTRA_AUDIENCES`: to exercise the entra agent locally

Locally, `func start` serves function keys from the emulator for the `apikey` agent. Easy Auth is not available locally, so the `entra` agent has no platform-injected principal and returns `401`. To exercise the entra path locally, set `AZURE_FUNCTIONS_AGENTS_ENTRA_EASY_AUTH=true` (asserting Easy Auth) and send a mock principal header (`x-ms-client-principal`, base64-encoded JSON with `auth_typ: aad`) — the runtime reads it exactly as it reads the platform-injected one. Only do this locally; never set that assertion in a deployment that lacks Easy Auth.

## Clean up

```bash
azd down --purge
```
