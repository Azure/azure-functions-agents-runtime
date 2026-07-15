---
frd: 0006
title: Endpoint authentication (API key + Entra ID)
status: Finalized            # Draft → In review → Finalized  (→ Implemented after merge)
author: victoriahall
created: 2026-07-14
updated: 2026-07-14
issues: []
pull_requests: []
branch: victoriahall/endpoint-auth
---

# FRD 0006 — Endpoint authentication (API key + Entra ID)

## 1. Summary

Add first-class, configurable **inbound authentication** for an agent's built-in
HTTP endpoints (`/agents/{slug}/chat`, `/agents/{slug}/chatstream`) and its MCP
tool surface (`/runtime/webhooks/mcp`). Authoring gains a single new key —
`builtin_endpoints.auth` — that selects one of four modes: `function` (Azure
Functions **API key**, the default), `admin` (system key), `anonymous`
(unauthenticated, dev-only), or `entra` (Microsoft **Entra ID**). In `entra`
mode the runtime accepts a request only when it carries a validated identity:
either an App Service Authentication (**Easy Auth**) `X-MS-CLIENT-PRINCIPAL`
header injected by the platform, or a **validated bearer token** (`Authorization:
Bearer <jwt>`) checked in-app against Entra ID. Optional `tenant_id`,
`allowed_audiences`, and `allowed_client_ids` allow-lists narrow which callers
are accepted. This makes the built-in endpoints safe to expose in production
without hand-rolling auth per app.

## 2. Motivation / problem

The built-in chat API and MCP tool endpoints are the primary way to call an agent
over HTTP. Today their protection is implicit and non-configurable:

- The chat routes inherit the app-level `AuthLevel.FUNCTION`, so they *happen* to
  require a function key, but authors cannot see, choose, or change that, and
  cannot opt into a stronger or weaker level per agent.
- There is no way to require **Entra ID** — the standard for production
  service-to-service and user auth on Azure. Teams that need OAuth2 bearer tokens
  or SSO have to fork the runtime or front it with a custom gateway.
- Guidance is scattered: the README mentions the MCP system key in passing, but
  there is no coherent authoring story for "how do I secure this agent?".

Anyone shipping an agent beyond a local demo hits this immediately: a reviewer or
security gate asks "how is `/agents/main/chat` authenticated?" and the honest
answer is "implicitly, and you can't change it." This feature makes the answer
explicit and configurable.

## 3. Goals / Non-goals

**Goals**
- One authoring key, `builtin_endpoints.auth`, controlling built-in endpoint auth.
- **API key** support via native Azure Functions auth levels (`function`, `admin`).
- **Entra ID** support for the chat API via two accepted proofs of identity:
  Easy Auth principal header **and** in-app bearer-token (JWT) validation.
- Optional claim allow-lists: `tenant_id`, `allowed_audiences`,
  `allowed_client_ids`, each with an environment-variable fallback.
- Clear, documented behavior for the MCP endpoint, whose HTTP surface is owned by
  the Functions MCP extension and therefore authenticated at the platform layer.
- Backward compatible: no `auth` key ⇒ today's behavior (`function` / API key).

**Non-goals**
- In-app JWT validation for the MCP webhook. `/runtime/webhooks/mcp` is a single
  host-owned endpoint; the runtime only registers `mcp_tool_trigger` tools on it
  and never sees its HTTP request/headers, so it cannot validate bearer tokens
  there. MCP Entra auth is delegated to Easy Auth (platform) and documented.
- Per-agent-trigger `http_trigger` auth changes — that already has `auth_level`.
- Authorization / RBAC beyond simple issuer/audience/appid allow-lists (roles,
  scopes, per-tool policy) — deferred to a future FRD.
- Managing/rotating function or system keys, or provisioning the Easy Auth app
  registration. Those are deployment concerns, not runtime concerns.

## 4. Proposed design

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| translate | `config/schema.py` | New `EntraAuthConfig` + `EndpointAuthConfig` models; `BuiltinEndpointsConfig.auth` field with string-shorthand coercion. |
| translate | `config/merge.py` | No behavior change — `auth` flows through `_resolve_builtin_endpoints` as part of `BuiltinEndpointsConfig`. |
| translate | `config/validation.py` | No change needed — the `entra`+`mcp` guidance is surfaced as a one-time log at registration (see below), not a translate-stage warning. |
| register | `registration/_auth.py` (new) | `resolve_endpoint_auth_level()` (mode → `func.AuthLevel`) and `authorize_entra_request()` (Easy Auth principal or validated bearer token, plus allow-list checks). |
| register | `registration/endpoints.py` | Apply the resolved `auth_level` to the chat routes, gate the chat/chatstream handlers with `authorize_entra_request()` in `entra` mode, and emit a one-time log at registration when `auth.mode == "entra"` and `mcp` is enabled (MCP Entra enforcement is platform-level Easy Auth). |
| execute | — | No runner change; auth is enforced before the runner is invoked. |

### Auth modes

`builtin_endpoints.auth.mode` ∈ `{function, admin, anonymous, entra}`:

| mode | Functions `auth_level` on chat routes | In-app enforcement | Intended use |
| --- | --- | --- | --- |
| `function` (default) | `FUNCTION` | none (platform key check) | API key (function/host keys) |
| `admin` | `ADMIN` | none (platform key check) | API key (system/master key) |
| `anonymous` | `ANONYMOUS` | none | local dev / already-fronted |
| `entra` | `ANONYMOUS` | Entra identity required | production Entra ID / SSO |

`entra` sets the Functions level to `ANONYMOUS` deliberately: the function-key
gate is *replaced* by the runtime's identity check, so a caller presents a bearer
token (or arrives through Easy Auth) rather than a key.

### Entra identity resolution (chat API)

For each request in `entra` mode, `authorize_entra_request()` accepts the request
if **either** proof succeeds, else returns `401`:

1. **Easy Auth principal** — if `X-MS-CLIENT-PRINCIPAL` is present, the App
   Service Authentication layer has already validated the token; the runtime
   base64-decodes the principal JSON and reads its claims.
2. **Bearer token** — otherwise, if `Authorization: Bearer <jwt>` is present, the
   runtime validates the JWT signature (Entra JWKS for the tenant), `iss`, `aud`,
   and `exp` via PyJWT.

After a proof succeeds, optional allow-lists are enforced (a configured list must
contain the claim): `tenant_id` vs `tid`, `allowed_audiences` vs `aud`,
`allowed_client_ids` vs `appid`/`azp`. Each config value falls back to an env var
(`AZURE_FUNCTIONS_AGENTS_ENTRA_TENANT_ID`, `…_ENTRA_AUDIENCES`,
`…_ENTRA_CLIENT_IDS`) so credentials stay out of source.

### MCP endpoint

`/runtime/webhooks/mcp` is owned by the Functions MCP extension. The runtime
registers tools on it but never handles its HTTP request, so:

- **API key:** the extension already requires the MCP **system key**
  (`x-functions-key`). This is unchanged and now documented.
- **Entra ID:** enforced at the platform via Easy Auth (App Service
  Authentication). The runtime cannot validate a bearer token inside a
  `mcp_tool_trigger` handler. `entra` + `mcp` therefore logs a one-time info
  message pointing to the Easy Auth guidance; it is not an error.

### Authoring / API surface

```yaml
builtin_endpoints:
  chat_api: true
  mcp: true
  auth:
    mode: entra              # function (default) | admin | anonymous | entra
    entra:                   # only used when mode: entra
      tenant_id: <tenant-guid>
      allowed_audiences: ["api://<app-id>"]
      allowed_client_ids: ["<caller-app-id>"]   # optional
```

Shorthand: `auth: function` (a bare string) is accepted and coerced to
`{ mode: function }`. Omitting `auth` entirely keeps the current default
(`function` / API key).

### Compatibility

Fully backward compatible. Existing agents with no `auth` key keep `function`
(API-key) protection on their chat routes — the same effective behavior as today,
now explicit. `builtin_endpoints: true` shorthand continues to enable all
endpoints with default (`function`) auth.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Where does auth config live? | New top-level `auth:` block / on `builtin_endpoints` / per-trigger | On `builtin_endpoints.auth` (these are the endpoints being secured) | Agent | 2026-07-14 |
| 2 | How is API key auth expressed? | New abstraction / reuse Functions `AuthLevel` | Reuse native `AuthLevel` (`function`/`admin`) — the platform already implements key auth | Agent | 2026-07-14 |
| 3 | Entra enforcement mechanism | Easy Auth only / in-app JWT only / both | Both — accept Easy Auth principal *or* validated bearer token | Agent | 2026-07-14 |
| 4 | Functions `auth_level` in `entra` mode | keep `FUNCTION` / `ANONYMOUS` | `ANONYMOUS` — identity check replaces the key gate | Agent | 2026-07-14 |
| 5 | MCP webhook Entra enforcement | in-app / platform (Easy Auth) | Platform (Easy Auth) — extension owns the HTTP surface; runtime can't see it | Agent | 2026-07-14 |
| 6 | JWT library | hand-rolled / `pyjwt[crypto]` | `pyjwt[crypto]` (already transitively present via `azure-identity`→`msal`); declared explicitly | Agent | 2026-07-14 |
| 7 | Secrets in config | inline only / env fallback | Allow inline **and** env fallback for tenant/audience/client-id | Agent | 2026-07-14 |

## 6. Test plan

- [ ] Unit `tests/test_config_schema.py`: `auth` parses (string shorthand, full
      object, default), rejects unknown modes and extra keys.
- [ ] Unit `tests/test_registration_auth.py` (new): `resolve_endpoint_auth_level`
      mapping; `authorize_entra_request` — Easy Auth principal happy path,
      bearer-token happy path (RS256 with an injected signing key), missing
      credential ⇒ 401, tenant/audience/appid allow-list mismatch ⇒ 401,
      expired/invalid token ⇒ 401.
- [ ] Unit `tests/test_registration_endpoints.py`: chat routes carry the resolved
      `auth_level` per mode; `entra` handler returns 401 without identity and 200
      with a valid principal; chatstream returns an SSE `error` frame on 401.
- [ ] Fixture scenario: `tests/fixtures/config_scenarios/` agent with
      `builtin_endpoints.auth` to exercise merge/validation end to end.
- [ ] Validation: `entra` + `mcp` logs guidance and still registers.

## 7. Docs impact

- [ ] `docs/architecture.md` — add `registration/_auth.py` to the module map and
      note the auth step in the endpoint-registration path.
- [ ] `docs/front-matter-spec.md` — document `builtin_endpoints.auth` with
      examples for API key and Entra ID.
- [ ] `docs/triggers.md` — note built-in endpoint auth modes.
- [ ] `README.md` — brief "securing endpoints" note for chat API and MCP.
- [ ] `docs/frds/README.md` — index this FRD.

## 8. Status & sign-off

- **Architecture review (phase 2):** Self-review against `docs/architecture.md`
  pipeline boundaries — auth is a *registration*-stage concern (the only
  Azure-aware stage), enforced before the lazy runner is invoked; discovery and
  translation are untouched except for the additive schema field. Public surface
  stays consistent with `docs/front-matter-spec.md` (`builtin_endpoints` object).
- **Human sign-off:** Requested and approved by the user via the task instruction
  to implement endpoint authentication (API key + Entra ID). → `status: Finalized`.
