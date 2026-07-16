---
frd: 0006
title: Endpoint authentication (API key + Entra ID)
status: Finalized            # Draft → In review → Finalized  (→ Implemented after merge)
author: victoriahall
created: 2026-07-14
updated: 2026-07-16
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
mode the runtime accepts a request only when it carries a validated identity: an
App Service Authentication (**Easy Auth**) `X-MS-CLIENT-PRINCIPAL` header
injected by the platform. Optional `tenant_id`, `allowed_audiences`, and
`allowed_client_ids` allow-lists narrow which callers are accepted. This makes
the built-in endpoints safe to expose in production without hand-rolling auth per
app.

> **Amendment A (§9, 2026-07-16):** the original design also validated raw bearer
> tokens in-app against Entra JWKS. That path was removed — the runtime now
> delegates all Entra token validation to Easy Auth and fails closed when no
> platform-validated principal is present.

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

> **Superseded by Amendment A (§9).** The two-proof model below was replaced: the
> runtime no longer validates bearer tokens in-app. Entra tokens are validated by
> App Service Authentication (Easy Auth) at the platform, and the runtime trusts
> only the injected `X-MS-CLIENT-PRINCIPAL`. Missing/invalid principal ⇒ `401`
> (fail closed). The allow-list behavior below still applies to the principal's
> claims.

For each request in `entra` mode, `authorize_entra_request()` requires a
validated Easy Auth principal, else returns `401`:

1. **Easy Auth principal** — if `X-MS-CLIENT-PRINCIPAL` is present, the App
   Service Authentication layer has already validated the token; the runtime
   base64-decodes the principal JSON, confirms `auth_typ` is Entra (`aad`), and
   reads its claims.

After the principal is accepted, optional allow-lists are enforced (a configured
list must contain the claim): `tenant_id` vs `tid`, `allowed_audiences` vs `aud`,
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
| 8 | Entra bearer-token validation (revises #3) | keep in-app JWT / **Easy Auth only** | **Easy Auth only** — the platform validates Entra bearer tokens and injects `X-MS-CLIENT-PRINCIPAL`; the runtime never validates JWTs itself. See Amendment A. | Human + Agent | 2026-07-16 |
| 9 | JWT library (revises #6) | keep `pyjwt[crypto]` / drop it | **Drop** the explicit `pyjwt[crypto]` dependency — no in-app token validation remains | Human + Agent | 2026-07-16 |
| 10 | No validated principal in `entra` mode | allow / **fail closed** | **Fail closed** — with `auth_level: ANONYMOUS`, a missing/invalid principal returns `401`; Easy Auth is a required deployment step for `entra` | Human + Agent | 2026-07-16 |

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

## 9. Amendment A — Standardize Entra bearer tokens on Easy Auth (2026-07-16)

### Context

The original design (Decisions #3, #6) accepted **two** proofs of an Entra
identity in `entra` mode: an Easy Auth `X-MS-CLIENT-PRINCIPAL` header **or** an
in-app validated bearer token (`Authorization: Bearer <jwt>`, verified with
`pyjwt[crypto]` against Entra JWKS). PR review raised that hand-rolling
bearer-token validation is risky and does not meet the bar enterprises (and 1P
workloads in particular) expect from Entra integration.

The concern is well-founded, and the shipped in-app path already showed the
hazard: it verified `exp` and signature but **never validated `iss`**, and when
`allowed_audiences` was unset it disabled audience verification
(`verify_aud: False`) — so any RS256 token resolvable from the tenant's JWKS
(e.g. a token minted for a different resource) would authenticate. App Service
Authentication (**Easy Auth**) already validates Entra-issued bearer tokens at
the platform, is security-hardened, audited, and is the sanctioned mechanism for
1P workloads; it injects the same `X-MS-CLIENT-PRINCIPAL` the runtime already
trusts as proof (1).

### Decision

Standardize **all** Entra enforcement on Easy Auth. Remove in-app JWT validation
entirely. In `entra` mode the runtime trusts only the platform-injected
`X-MS-CLIENT-PRINCIPAL` header, then applies the existing claim allow-lists as
defense-in-depth. This also unifies chat with MCP, which already delegated Entra
enforcement to Easy Auth (Decision #5).

### Flow change

Before (per request, `entra` mode):

```
Functions host (ANONYMOUS) → handler → authorize_entra_request:
    (a) X-MS-CLIENT-PRINCIPAL present? → decode + allow-lists
    (b) else Authorization: Bearer?    → in-app JWKS/JWT validate + allow-lists
    (c) else                           → 401
```

After:

```
App Service Easy Auth (validates bearer/cookie, injects X-MS-CLIENT-PRINCIPAL,
    401s unauthenticated per config)
  → Functions host (ANONYMOUS) → handler → authorize_entra_request:
      principal present? → decode + auth_typ == aad + allow-lists
      else               → 401 (fail closed; Easy Auth required)
```

### Scope of changes

- **`registration/_auth.py`** — delete `_validate_bearer_token`,
  `_get_signing_key`, `_jwks_uri`, and the module-global `_jwks_clients`; remove
  the bearer branch from `authorize_entra_request`. Keep the principal decode,
  `auth_typ` check, and `_check_allowlists`. Missing/invalid principal ⇒ `401`
  (fail closed, per Decision #10).
- **`pyproject.toml`** — drop the explicit `pyjwt[crypto]` dependency
  (Decision #9).
- **Schema / env** — no change. `EntraAuthConfig` (`tenant_id`,
  `allowed_audiences`, `allowed_client_ids`) and env fallbacks remain as
  allow-lists layered over the Easy-Auth-validated claims.
- **Infra / sample** — `samples/secured-endpoints` gains a
  `Microsoft.Web/sites/config@authsettingsV2` resource (Entra provider,
  `allowedAudiences`, `unauthenticatedClientAction`) so the deployable sample
  demonstrates the sanctioned path instead of implying direct bearer calls.
- **Docs** — update §4 "Entra identity resolution", `front-matter-spec.md`,
  `architecture.md` (note `_auth.py` no longer validates JWTs), and the README
  "securing endpoints" note. Messaging: *the runtime trusts the
  platform-validated principal; enabling Entra is an Easy Auth deployment step.*

### Revised Non-goals

- **In-app JWT / bearer-token validation for any endpoint.** Entra tokens are
  validated by Easy Auth at the platform for both chat and MCP. The runtime never
  parses or verifies a JWT.

### Tradeoff (recorded)

`entra` now **requires** Easy Auth, which is a cloud/App Service capability not
available under the local Core Tools host. Local exercise of `entra` therefore
relies on injecting a mock `X-MS-CLIENT-PRINCIPAL` header (tests) or using
`anonymous` mode for local runs. This is an accepted cost of removing the
hand-rolled path.

### Test-plan delta

- Remove the RS256 signing-key / bearer-token cases from
  `tests/test_registration_auth.py`; keep principal happy-path, `auth_typ != aad`
  ⇒ 401, allow-list mismatch ⇒ 401, and missing principal ⇒ 401 (fail closed).
- In `tests/test_registration_endpoints.py`, drop bearer variants; keep
  principal-based 200 and no-identity 401 for chat, chatstream, and the workflow
  routes.

### Sign-off

- **Raised by:** PR reviewer. **Decided by:** Human + Agent, 2026-07-16.
- Status remains `Finalized`; this amendment supersedes Decisions #3 and #6.
