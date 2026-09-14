# User identity propagation

This document describes how the runtime carries end-user identity through the
request lifecycle and forwards it to downstream MCP servers.

---

## Overview

The runtime acts as a **transparent identity proxy**: it never validates the
incoming user token itself. Instead it relies on EasyAuth to authenticate the
session before the request reaches Python code, and it forwards the
already-authenticated identity artifacts to downstream MCP servers, adding a
managed identity token so the downstream can verify *which* app is calling.

Two authentication modes are supported depending on what is present in the
inbound request headers:

| Mode | Trigger condition | Description |
|---|---|---|
| **BigMac hook-session** | Both `X-MS-Access-Token` and `X-MS-Hooks-Session-Token` present | User identity forwarded unchanged; `Authorization` is the function app's managed identity token. |
| **OBO (On-Behalf-Of)** | `X-MS-Access-Token` (or `X-MS-TOKEN-AAD-ACCESS-TOKEN`) present, no hooks session token | MSAL exchanges the inbound token for a new downstream-scoped token. |
| **Managed identity fallback** | No user token present | The function app's own managed identity is used for downstream calls. |

---

## End-to-end flow

### BigMac hook-session mode (production path)

```
┌───────────────────┐
│  Browser / Client │
└────────┬──────────┘
         │  POST /agents/<name>/chat
         │  X-MS-Access-Token: <user access token>
         │  X-MS-Hooks-Session-Token: <session token>
         ▼
┌────────────────────────────────────────────────────────────────────┐
│  EasyAuth (App Service Authentication)                             │
│                                                                    │
│  • Validates X-MS-Hooks-Session-Token (opaque session managed by   │
│    EasyAuth).                                                      │
│  • If X-MS-Access-Token is expired, refreshes it automatically     │
│    via the EasyAuth /.auth/refresh endpoint before passing the     │
│    request on.                                                     │
│  • Injects or preserves the two headers for downstream code.       │
└────────────────────────────┬───────────────────────────────────────┘
                             │  (same headers, now validated by EasyAuth)
                             ▼
┌────────────────────────────────────────────────────────────────────┐
│  Azure Functions Agent Runtime                                     │
│                                                                    │
│  1. _build_user_context_from_request()                             │
│     • extract_user_token_from_headers()                            │
│       Priority: X-MS-Access-Token > X-MS-TOKEN-AAD-ACCESS-TOKEN   │
│               > X-MS-TOKEN-AAD-ID-TOKEN > Authorization: Bearer   │
│     • extract_hooks_session_token_from_headers()                   │
│     • Both values stored in UserContext (no validation performed). │
│                                                                    │
│  2. runner.run_agent() / run_agent_stream()                        │
│     • Sets UserContext in a contextvar for the duration of the run.│
│     • Passes UserContext to all MCP tool calls.                    │
│                                                                    │
│  3. discovery/mcp.py — obo_header_provider()                       │
│     • Detects: hooks_session_token IS set AND access_token IS set. │
│     • Acquires a fresh managed identity (MI) token for the         │
│       configured scope (e.g. https://graph.microsoft.com/.default).│
│     • Builds the outbound header set:                              │
│       Authorization:            Bearer <MI token>  (new)           │
│       X-MS-Access-Token:        <forwarded unchanged>              │
│       X-MS-Hooks-Session-Token: <forwarded unchanged>              │
└────────────────────────────┬───────────────────────────────────────┘
                             │  HTTP request to MCP server
                             │  Authorization: Bearer <MI token>
                             │  X-MS-Access-Token: <user token>
                             │  X-MS-Hooks-Session-Token: <session token>
                             ▼
┌────────────────────────────────────────────────────────────────────┐
│  Downstream MCP Server                                             │
│                                                                    │
│  The downstream server decides what to validate. Typical checks:  │
│  • Authorization — verify the MI token; the azp claim identifies  │
│    which function app is the caller.                               │
│  • X-MS-Access-Token — decode the user's identity (oid, upn, tid, │
│    scp) if the server wants to act on behalf of the user.          │
│  • X-MS-Hooks-Session-Token — can be used to call EasyAuth's      │
│    /.auth/refresh if the access token needs refreshing.            │
└────────────────────────────────────────────────────────────────────┘
```

### OBO (On-Behalf-Of) mode

Activated when an inbound access token is present but **no hooks session token**
is included. The runtime exchanges the user token for a new token scoped to the
downstream API using MSAL and the configured `auth.obo` credentials.

```
┌───────────────────┐
│  Browser / Client │
└────────┬──────────┘
         │  X-MS-Access-Token: <user access token>
         ▼
┌────────────────────────────────────────────────────────────────────┐
│  EasyAuth  (validates session)                                     │
└────────────────────────────┬───────────────────────────────────────┘
                             ▼
┌────────────────────────────────────────────────────────────────────┐
│  Azure Functions Agent Runtime                                     │
│                                                                    │
│  1. UserContext built with access_token only (no hooks token).     │
│                                                                    │
│  2. obo_header_provider() — OBO branch:                            │
│     • hooks_session_token is None → OBO path selected.            │
│     • OboTokenProvider.acquire_token_on_behalf_of() calls MSAL.   │
│     • MSAL exchanges user token for a downstream-scoped token.    │
│     • Token cached in-process (keyed by token hash + scope).      │
│     • If exchange fails with interaction_required, the request     │
│       returns HTTP 401 with WWW-Authenticate containing the claims │
│       challenge so the client can re-authenticate.                 │
│     • Outbound header:                                             │
│       Authorization: Bearer <OBO downstream token>                 │
└────────────────────────────┬───────────────────────────────────────┘
                             │  Authorization: Bearer <OBO token>
                             ▼
┌────────────────────────────┐
│  Downstream MCP Server     │
└────────────────────────────┘
```

### Managed identity fallback

Activated when no user identity headers are present at all (e.g. a timer
trigger or a background request without EasyAuth).

```
┌───────────────────────────────────────────┐
│  Azure Functions Agent Runtime            │
│                                           │
│  UserContext.access_token = None          │
│  obo_header_provider() — fallback branch: │
│  • build_credential() → DefaultAzure...   │
│    (Managed Identity in Azure; az CLI     │
│     or env vars locally)                  │
│  • Token acquired for configured scope.   │
│  • Outbound header:                       │
│    Authorization: Bearer <MI token>       │
└───────────────────────┬───────────────────┘
                        ▼
┌──────────────────────────┐
│  Downstream MCP Server   │
└──────────────────────────┘
```

---

## Trust boundaries

| Boundary | Who is responsible | What the runtime does |
|---|---|---|
| Inbound token authenticity | **EasyAuth** | Trusts the headers as-is; no JWT decode or signature check |
| Outbound caller identity | **Runtime** (via MI token) | Mints a fresh MI token; downstream can verify `azp` claim |
| Downstream access control | **Downstream MCP server** | Runtime makes no assertions about what the downstream should accept |

**Security implication:** without EasyAuth in front of the function app, the
inbound `X-MS-Access-Token` and `X-MS-Hooks-Session-Token` headers are
completely unauthenticated. EasyAuth is a **hard requirement** in production for
this model to be secure.

---

## Header extraction priority

The runtime checks inbound headers in this order when looking for a user access
token (implemented in `_obo.py: extract_user_token_from_headers`):

1. `X-MS-Access-Token` — BigMac explicit access token header
2. `X-MS-TOKEN-AAD-ACCESS-TOKEN` — EasyAuth AAD access token header
3. `X-MS-TOKEN-AAD-ID-TOKEN` — EasyAuth AAD ID token (fallback when access token is absent)
4. `Authorization: Bearer <token>` — standard bearer token

The hooks session token is extracted separately from `X-MS-Hooks-Session-Token`
(case-insensitive lookup).

---

## Configuration

### BigMac / managed identity fallback

No special configuration is required. The runtime uses `DefaultAzureCredential`
for the outbound MI token. In Azure this resolves to the function app's managed
identity; locally it falls back to `az login` credentials.

The MCP server entry in `mcp.json` must set `auth.type: obo` to enable the
identity-forwarding path:

```json
{
  "servers": {
    "my-api": {
      "url": "https://my-api.example.com/mcp",
      "auth": {
        "type": "obo",
        "scope": "https://graph.microsoft.com/.default"
      }
    }
  }
}
```

The `scope` value is used when acquiring the outbound MI token (fallback path)
or the OBO downstream token.

### OBO mode

Requires `auth.obo` in `agents.config.yaml` with a valid Entra app registration
that has been pre-consented for the downstream scopes:

```yaml
auth:
  obo:
    enabled: true
    client_id: $AZURE_CLIENT_ID
    client_secret: $AZURE_CLIENT_SECRET
    tenant_id: $AZURE_TENANT_ID
    downstream_scopes:
      my_api: "api://<downstream-app-id>/.default"
```

The `client_id` / `client_secret` / `tenant_id` must be in the **same tenant**
as the downstream API. If they are in a different tenant from the Azure
subscription hosting the function app, MSAL will fail with a cross-tenant
identity error.

---

## Runtime implementation pointers

| Concern | Module | Symbol |
|---|---|---|
| Header extraction | `_obo.py` | `extract_user_token_from_headers()`, `extract_hooks_session_token_from_headers()` |
| UserContext construction | `registration/_handlers.py`, `registration/endpoints.py` | `_build_user_context_from_request()` |
| Context propagation into tools | `discovery/mcp.py` | `set_current_user_context()`, `_current_user_context` contextvar |
| BigMac / OBO / MI header dispatch | `discovery/mcp.py` | `_build_obo_header_provider()` → `obo_header_provider()` closure |
| OBO token exchange and caching | `_obo.py` | `OboTokenProvider.acquire_token_on_behalf_of()`, `_token_cache` |
| Interaction-required handling | `registration/endpoints.py` | `_interaction_required_error()` |
