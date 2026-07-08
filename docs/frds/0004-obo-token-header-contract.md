---
frd: 0003
title: OBO token and header pass-through contract
status: Finalized
author: victoriahall
created: 2026-07-06
updated: 2026-07-06
issues: []
pull_requests: []
branch: victoriahall/obo-token-header-contract
---

# FRD 0003 - OBO token and header pass-through contract

## 1. Summary

Define the runtime contract for passing authenticated user identity from inbound
HTTP requests to downstream MCP server calls using OAuth 2.0 On-Behalf-Of
(OBO). The contract covers input headers, config shape, context propagation,
outbound auth headers, fallback behavior, and error responses.

## 2. Motivation / problem

Before this contract, agents could authenticate downstream calls only with
application credentials (managed identity). Web app scenarios require delegated
authorization so tool calls reflect end-user permissions.

Without a clear contract, client apps and runtime modules can disagree on:

1. Which inbound header carries the user token.
2. How token context is propagated across async boundaries.
3. What outbound authorization behavior is expected for OBO versus fallback.
4. How consent or claims challenges are surfaced to clients.

## 3. Goals / Non-goals

**Goals**
- Define canonical inbound token/header extraction behavior.
- Define runtime configuration contract for OBO enablement.
- Define MCP auth contract for OBO-protected downstream services.
- Define deterministic fallback and error behavior.
- Keep behavior backward compatible for apps that do not enable OBO.

**Non-goals**
- Browser interactive sign-in orchestration.
- Runtime-managed consent UX.
- Certificate credential support for OBO client auth in this revision.
- Changes to trigger model or non-HTTP identity sources.

## 4. Proposed design

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| discover | `src/azure_functions_agents/discovery/mcp.py` | Add OBO-aware MCP header provider and request-scoped user context access |
| translate | `src/azure_functions_agents/config/schema.py` | Add global auth schema with OBO settings |
| register | `src/azure_functions_agents/registration/_handlers.py`, `src/azure_functions_agents/registration/endpoints.py` | Extract inbound user token/identity and map interaction-required errors to HTTP 401 |
| execute | `src/azure_functions_agents/runner.py`, `src/azure_functions_agents/_obo.py` | Thread user context through run paths and perform OBO token exchange with caching |

### Authoring / API surface

Global config in agents.config.yaml:

- `auth.obo.enabled` boolean.
- `auth.obo.client_id` string.
- `auth.obo.client_secret` string.
- `auth.obo.tenant_id` string.
- `auth.obo.downstream_scopes` map of scope aliases to scope URIs.

MCP server auth in mcp.json:

- `auth.type: obo` enables OBO header provider for that server.
- `auth.scope` is required and identifies the downstream resource scope.

Inbound request header contract:

1. Primary token source: `X-MS-TOKEN-AAD-ACCESS-TOKEN`.
2. Secondary token source: `Authorization: Bearer <token>`.
3. User id source: `X-MS-CLIENT-PRINCIPAL-ID` when present.

Outbound MCP header contract:

1. Preserve configured static headers from mcp.json.
2. Add `Authorization: Bearer <access_token>` where token comes from:
   - OBO exchange when user context and OBO config are available.
   - Managed identity fallback when OBO is unavailable or fails.

### Runtime behavior contract

1. Request handlers create `UserContext` from inbound headers.
2. Runner stores context in a request-scoped context variable for both
   non-streaming and streaming agent execution.
3. MCP header provider reads current user context at request time.
4. OBO token provider caches tokens in-memory by token hash and scope until
   near expiry.
5. On OBO interaction-required conditions, handlers return HTTP 401 and include
   `WWW-Authenticate` with error metadata and claims challenge.

### Error and claims challenge contract

For interaction-required failures (`interaction_required`, `consent_required`,
`login_required`):

- HTTP status: 401.
- Response body: JSON with `error`, `error_description`, and optional `claims`.
- Response header: `WWW-Authenticate` containing Bearer error metadata.
- Claims in `WWW-Authenticate` are base64-encoded.

### Compatibility

- Backward compatible by default: OBO is inactive unless `auth.obo.enabled` is
  true and MCP server auth type is explicitly set to obo.
- Existing managed identity behavior remains default for non-OBO MCP entries.
- Missing or malformed user token does not break request handling; runtime can
  continue via managed identity fallback.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Inbound token precedence | Authorization first / EasyAuth first | EasyAuth first, then Authorization | Human | 2026-06-19 |
| 2 | Context propagation mechanism | Explicit argument threading only / context variable | Context variable with reset token | Human | 2026-06-19 |
| 3 | Token cache strategy | No cache / distributed cache / in-memory cache | In-memory cache keyed by token hash and scope | Human | 2026-06-19 |
| 4 | Fallback mode | Fail closed when OBO unavailable / managed identity fallback | Managed identity fallback | Human | 2026-06-19 |
| 5 | Claims challenge behavior | 500 with internal error / 401 with challenge details | 401 with WWW-Authenticate and claims | Human | 2026-06-19 |
| 6 | OBO scope location | Global single scope / per-MCP auth scope | Per-MCP `auth.scope` with optional global alias map | Human | 2026-06-19 |

## 6. Test plan

- [x] Unit: OBO config validation and defaults in `tests/test_obo.py`.
- [x] Unit: Header extraction precedence and case-insensitive lookup in
      `tests/test_obo.py`.
- [x] Unit: OBO provider success, interaction-required, and generic error paths
      in `tests/test_obo.py`.
- [x] Unit: Token caching behavior in `tests/test_obo.py`.
- [x] Unit: Global provider lifecycle tests in `tests/test_obo.py`.
- [x] Integration-adjacent: Runner streaming tests pass with context threading
      changes in `tests/test_runner_streaming.py`.

## 7. Docs impact

- [ ] `docs/architecture.md` - add explicit OBO dataflow notes in module map.
- [ ] `docs/front-matter-spec.md` - no expected changes.
- [ ] `docs/triggers.md` - no expected changes.
- [ ] `README.md` - add OBO configuration and mcp.json examples.

## 8. Status & sign-off

- **Architecture review (phase 2):** Contract aligns with implemented runtime
  boundaries (discovery -> registration -> execute) and preserves backward
  compatibility.
- **Human sign-off:** Victoria Hall, 2026-07-06.
