# OBO WhoAmI MCP Server (Downstream Test Target)

Minimal downstream MCP server for validating OBO pass-through from
`samples/obo-e2e`.

This server exposes one tool:

- `whoami` - reads inbound `Authorization: Bearer` token and returns decoded
  claims (`oid`, `sub`, `aud`, `azp`, `appid`, etc.).

Use this server as the `<downstream-mcp-endpoint>` target in
`samples/obo-e2e/src/mcp.json`.

## Quick start (local)

```powershell
cd samples/obo-whoami-mcp-server
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python server.py
```

Server starts at:

- `http://localhost:8000/mcp`

## Plug into obo-e2e sample

In `samples/obo-e2e/src/mcp.json`:

```json
{
  "servers": {
    "whoami-api": {
      "url": "http://localhost:8000/mcp",
      "auth": {
        "type": "obo",
        "scope": "api://<downstream-api-app-id>/.default"
      }
    }
  }
}
```

For real delegated auth validation, use an HTTPS deployment with Entra auth in
front of this MCP server and set `auth.scope` to that API's audience.

## Deploy notes (non-production)

You can deploy this sample to any environment that can run a Python ASGI app
(for example, Azure Container Apps, App Service for Containers, or another
container host).

Container startup command:

```text
python server.py
```

Then use:

- `https://<your-host>/mcp`

as `<downstream-mcp-endpoint>` in `samples/obo-e2e/src/mcp.json`.

## Validation expectation

When your upstream OBO sample calls `whoami`:

- User A request should show User A `oid/sub`.
- User B request should show User B `oid/sub`.
- No user token should show fallback identity behavior (or missing auth,
  depending on downstream auth config).
