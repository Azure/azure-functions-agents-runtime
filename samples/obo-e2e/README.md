# OBO E2E Test Kit

This sample validates On-Behalf-Of (OBO) token/header pass-through behavior in
Azure Functions Agents Runtime.

It verifies four paths:

1. User A request -> downstream sees User A identity.
2. User B request -> downstream sees User B identity.
3. No user token -> managed identity fallback.
4. Missing consent/MFA -> HTTP 401 with `WWW-Authenticate` claims challenge.

## Important clarification

You do **not** need to deploy to production to test this.

You can run this scenario in either:

1. **Local dev** (`func start`) against test Entra app registrations and test
   downstream APIs.
2. **Non-production Azure environment** (recommended for team validation)
   such as dev/test subscription or staging slot.

Production deployment is optional and should only happen after validation in
one of the environments above.

## Included sample app files

This folder now includes a runnable sample app under `src/`:

- `src/function_app.py`
- `src/main.agent.md`
- `src/agents.config.yaml`
- `src/mcp.json`
- `src/host.json`
- `src/local.settings.template.json`
- `src/requirements.txt`

The root templates are still provided for quick copy/reference:

- `agents.config.obo.sample.yaml`
- `mcp.obo.sample.json`
- `test.http`
- helper scripts (`get-user-token.ps1`, `decode-jwt-payload.ps1`)

## Prerequisites

- Azure Functions Core Tools (for local run)
- Python 3.13+
- Runtime configured with OBO (see `src/agents.config.yaml`)
- MCP server configured with `auth.type: obo` (see `src/mcp.json`)
- A downstream MCP tool (for example `whoami`) that returns bearer token claims
  (`oid`, `sub`, `aud`, and `azp/appid`)
- Azure CLI logged in to test users (User A and User B)

## Quick local setup

From repo root:

```powershell
cd samples/obo-e2e/src
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item local.settings.template.json local.settings.json
```

Set values in `local.settings.json`:

- `FOUNDRY_PROJECT_ENDPOINT`
- `FOUNDRY_MODEL`
- `AZURE_CLIENT_ID`
- `AZURE_CLIENT_SECRET`
- `AZURE_TENANT_ID`

Update `mcp.json` values:

- `url`: `https://<downstream-mcp-endpoint>/mcp`
- `scope` (default first-run option): `https://graph.microsoft.com/.default`

What `<downstream-mcp-endpoint>` should be:

- The host name of a downstream MCP server that exposes a `whoami`-style tool.
- Example with the included downstream sample:
  - Run `samples/obo-whoami-mcp-server`
  - Use `http://localhost:8000/mcp` for local tests
  - Or `https://<deployed-host>/mcp` after non-prod deployment

Scope guidance:

- Start with `https://graph.microsoft.com/.default` for the easiest OBO smoke
  test path.
- Switch to `api://<downstream-api-app-id>/.default` when validating strict
  custom downstream audience behavior.

Then start the host:

```powershell
func start
```

## Copy-paste sequence (PowerShell)

Run these commands from `samples/obo-e2e`.

```powershell
# 0) Set these values for your environment
$FunctionBaseUrl = "https://<your-function-app>.azurewebsites.net"
$AgentName = "main"

# 1) Acquire User A token (for the function app audience)
# If you use EasyAuth in front of the app, use your app's audience scope.
$FunctionAudienceScope = "api://<function-app-app-id>/.default"
$UserAToken = az account get-access-token --scope $FunctionAudienceScope --query accessToken -o tsv

# 2) Call chat endpoint as User A
$Body = @{ prompt = "Call the downstream whoami MCP tool and return raw JSON" } | ConvertTo-Json
$RespA = Invoke-WebRequest -Method Post `
  -Uri "$FunctionBaseUrl/agents/$AgentName/chat" `
  -Headers @{ "X-MS-TOKEN-AAD-ACCESS-TOKEN" = $UserAToken } `
  -ContentType "application/json" `
  -Body $Body

$RespA.StatusCode
$RespA.Content

# 3) Sign in as User B, then acquire User B token
# az login
$UserBToken = az account get-access-token --scope $FunctionAudienceScope --query accessToken -o tsv

# 4) Call chat endpoint as User B
$RespB = Invoke-WebRequest -Method Post `
  -Uri "$FunctionBaseUrl/agents/$AgentName/chat" `
  -Headers @{ "X-MS-TOKEN-AAD-ACCESS-TOKEN" = $UserBToken } `
  -ContentType "application/json" `
  -Body $Body

$RespB.StatusCode
$RespB.Content

# 5) Fallback check (no user token)
$RespFallback = Invoke-WebRequest -Method Post `
  -Uri "$FunctionBaseUrl/agents/$AgentName/chat" `
  -ContentType "application/json" `
  -Body $Body

$RespFallback.StatusCode
$RespFallback.Content

# 6) Optional: claims challenge check
# Use a scope requiring consent/MFA and inspect 401 + WWW-Authenticate.
try {
  Invoke-WebRequest -Method Post `
    -Uri "$FunctionBaseUrl/agents/$AgentName/chat" `
    -Headers @{ "X-MS-TOKEN-AAD-ACCESS-TOKEN" = $UserAToken } `
    -ContentType "application/json" `
    -Body (@{ prompt = "Call MCP tool for protected scope" } | ConvertTo-Json)
} catch {
  $_.Exception.Response.StatusCode.value__
  $_.Exception.Response.Headers["WWW-Authenticate"]
}
```

## What to validate

- User A response shows downstream `oid/sub` for User A.
- User B response shows downstream `oid/sub` for User B.
- Fallback response shows app identity (managed identity/service principal).
- Claims challenge path returns HTTP 401 and `WWW-Authenticate`.

## Local REST client option

You can also use [test.http](test.http) and paste User A/User B tokens manually.
