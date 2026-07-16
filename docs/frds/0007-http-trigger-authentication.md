---
frd: 0007
title: HTTP trigger authentication (shared auth model)
status: Finalized
author: victoriahall
created: 2026-07-16
updated: 2026-07-16
issues: []
pull_requests: []
branch: victoriahall/endpoint-auth
---

# FRD 0007 — HTTP trigger authentication (shared auth model)

## 1. Summary

FRD 0006 added an inbound `auth` policy (API key / admin / anonymous / Entra ID)
to the runtime's **built-in** endpoints (chat API + MCP). This FRD extends the
exact same policy to **arbitrary `http_trigger` agents**. HTTP-triggered agents
now accept the nested `auth:` object (the same `EndpointAuthConfig` model,
including `entra`), reusing the shared `_auth` guard so identity enforcement is
identical across surfaces. The legacy flat `auth_level` string is deprecated but
still honored for backward compatibility.

## 2. Motivation / problem

Before this change only the built-in endpoints could require Entra ID. A regular
`http_trigger` agent could only pick a Functions `auth_level`
(`anonymous`/`function`/`admin`) — there was no way to put App Service
Authentication (Easy Auth) identity enforcement in front of a custom agent route.
Authors who wanted a secured REST agent had to fall back to the built-in chat API
or wire auth manually. The auth model and the `authorize_entra_request` guard
already existed and were surface-agnostic (they operate on an
`EndpointAuthConfig` + a header getter), so the gap was purely that
`http_trigger` registration never consumed them.

## 3. Goals / Non-goals

**Goals**
- Let `http_trigger` agents declare the same nested `auth:` policy as
  `builtin_endpoints.auth`, including `entra` with tenant/audience/client-id
  allow-lists.
- Reuse the shared `_auth` module (`resolve_endpoint_auth_level`,
  `authorize_entra_request`) — one enforcement code path for all HTTP surfaces.
- Keep the flat `auth_level` working (deprecated, warns) so existing agents are
  unaffected.

**Non-goals**
- No new schema fields on `TriggerSpec` (auth is parsed from the free-form
  `trigger.args`), so `front-matter-reference.md` is unchanged.
- No changes to the Entra enforcement semantics themselves (defined in FRD 0006).
- No sample additions/changes.

## 4. Proposed design

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| discover | — | none |
| translate | — | none (auth read from existing free-form `trigger.args`) |
| register | `registration/triggers.py` | New `_resolve_http_trigger_auth()` maps nested `auth` (preferred) / flat `auth_level` (deprecated) → `EndpointAuthConfig`; route registered with `resolve_endpoint_auth_level(auth)`. |
| register | `registration/_handlers.py` | `make_http_agent_handler()` gains an `auth` param and enforces `authorize_entra_request()` at the top of the request handler (fail-closed before any processing). |
| execute | `runner.py` | unchanged — guard runs before the runner is invoked. |

### Authoring / API surface

`http_trigger.args.auth` — same model as `builtin_endpoints.auth`
(`EndpointAuthConfig`): string shorthand (`function` | `admin` | `anonymous` |
`entra`) or object (`{ mode, entra: { tenant_id, allowed_audiences,
allowed_client_ids } }`). Default `function`. Documented in
`docs/front-matter-spec.md#http-trigger` and `docs/triggers.md`.

### Compatibility

- Flat `auth_level` (`anonymous`/`function`/`admin`) still accepted; emits a
  deprecation warning and maps to the equivalent `auth.mode`.
- If both `auth` and `auth_level` are set, `auth` wins and `auth_level` is
  ignored with a warning.
- Agents with no auth declared keep the default `function` key check — no
  behavior change.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Where to store `auth` for http_trigger | (A) add fields to `TriggerSpec` schema (B) parse from free-form `trigger.args` | B — avoids a generic-trigger schema change and `front-matter-reference.md` regen; surgical | Human | 2026-07-16 |
| 2 | Reuse vs. new extraction of the guard | (A) extract a new shared helper (B) reuse existing `_auth` as-is | B — `_auth` is already surface-agnostic (`EndpointAuthConfig` + `HeaderGetter`) | Agent | 2026-07-16 |
| 3 | Flat `auth_level` handling | (A) hard-remove (B) keep + deprecate (C) silently alias | B — backward compatible with a warning | Human | 2026-07-16 |
| 4 | Conflict resolution when both set | (A) error (B) `auth` wins + warn (C) `auth_level` wins | B — least disruptive, steers authors to `auth` | Human | 2026-07-16 |

## 6. Test plan

- [x] Unit (`tests/test_registration_triggers.py`): nested `auth` string
  shorthand for each mode maps to the correct route `AuthLevel` (incl.
  `entra`→anonymous); nested object `auth` with `entra` allow-lists is passed to
  the handler; `auth` wins over `auth_level` (+warning); flat `auth_level`
  deprecation warning; invalid nested `auth` and invalid flat `auth_level` raise
  `ValueError`; existing valid `auth_level` levels still map correctly.
- [x] Unit (`tests/test_registration_handlers.py`): `entra` http handler returns
  401 without Easy Auth evidence, 401 without a principal, 200 with a valid
  principal when `WEBSITE_AUTH_ENABLED` is set; default (non-entra) handler does
  not gate requests.
- [x] Regression: existing http_trigger registration tests unchanged in behavior.

## 7. Docs impact

- [x] `docs/architecture.md` — module map rows for `_handlers.py` / `triggers.py`
  and the execute-stage HTTP agent note the shared `_auth` guard on routes.
- [x] `docs/front-matter-spec.md` — `#http-trigger` documents nested `auth` +
  deprecates `auth_level`.
- [x] `docs/triggers.md` — HTTP trigger auth section.
- [ ] `README.md` — no change (no new quickstart surface).

## 8. Status & sign-off

- **Architecture review (phase 2):** confirmed the shared `_auth` module is
  already surface-agnostic, so the work is reuse (not new extraction); parsing
  `auth` from the free-form `trigger.args` avoids a schema change. Extends FRD
  0006 without altering its Entra semantics.
- **Human sign-off:** victoriahall, 2026-07-16 → `status: Finalized`.
