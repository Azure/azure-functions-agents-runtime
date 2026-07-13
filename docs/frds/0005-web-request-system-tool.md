---
frd: 0005
title: web_request system tool
status: Finalized            # Draft → In review → Finalized  (→ Implemented after merge)
author: larohra
created: 2026-07-08
updated: 2026-07-13
issues: [https://github.com/Azure/azure-functions-bucees-planning/issues/1176]
pull_requests: [https://github.com/Azure/azure-functions-agents-runtime/pull/96, https://github.com/Azure/azure-functions-agents-runtime/pull/87]
branch: larohra-http-call-system-tool
---

# FRD 0005 — `web_request` system tool

## 1. Summary

Add a built-in, default-on **`web_request` system tool** so an agent can invoke an HTTP
endpoint **directly** instead of generating and running code to make the request.
It is the runtime's second system tool and mirrors the existing
`dynamic_sessions_code_interpreter` (sandbox) across the full config → merge →
impl → wiring → telemetry pipeline. The model calls
`web_request(method, url, headers?, query?, body?|json?)` and receives a structured
JSON result (`status`, final `url` with query/userinfo stripped, `content_type`, a
redaction-filtered `response_headers` subset, parsed `body`, `body_truncated`,
`redirect_count`). Because the tool can reach arbitrary **public** hosts, it adds
an always-on **SSRF security floor** (globally-routable-unicast-only IP validation
+ DNS-rebind IP-pinning) that no configuration can switch off, plus optional
operator controls (exact-host allowlist, https-only, size and time caps) and
opt-in telemetry. **It is on by default** for every agent — the reduced,
SSRF-floored public-fetch scope makes it safe to ship as a standard built-in — so a
`system_tools.web_request` block *configures* it rather than enabling it. Set
`web_request: false` (globally or per-agent), or `tools: false`, to turn it off.

**v1 is deliberately minimal — a public, *unauthenticated* fetch primitive.**
Governed per-host credential injection, redirect following, per-agent override
objects, and wildcard host matching are **deferred to v2** (see *Phased delivery*,
§3). **v1 is also public-only** — destinations that resolve to private/internal
ranges are blocked by the floor.

## 2. Motivation / problem

Calling an HTTP API is the single most common integration need for an agent, yet
there is no first-class, safe primitive for it today. The agent's only options are
to **generate and run code** in the Dynamic Sessions sandbox (slow, non-
deterministic, token-hungry, and forcing a heavyweight ACA dependency onto an app
whose only need is a web request) or to **hand-write a custom Python tool per
endpoint** (doesn't scale; re-implements the same request/response plumbing each
time).

`web_request` makes it a declarative, deterministic primitive with **one line of
config and zero code generation** — enrich answers from public / partner APIs,
read or write an allow-listed SaaS / line-of-business endpoint, fire a webhook, or
chain services without glue code.

**Why built-in.** Making outbound HTTP calls *safely* is genuinely hard — SSRF,
IMDS token theft (`169.254.169.254`), and DNS rebinding are easy to get wrong.
Centralizing it as a governed system tool means **every agent inherits the security
floor** (internal-range blocklist, IP-pinning, optional allowlist) for free instead
of each customer re-implementing it, usually incorrectly. It is deterministic,
faster, and cheaper than code-gen, and needs no sandbox.

> **v1 is public-only and unauthenticated.** The always-on SSRF floor blocks
> private / internal ranges even for allow-listed hosts, and v1 injects no
> credentials (the model supplies any auth header itself). Operator-controlled
> private-range access and governed per-host credential injection are **v2** (see
> §3 *Phased delivery*).

## 3. Goals / Non-goals

**Goals (v1)**
- A single `web_request` tool the model invokes with `method`/`url`/`headers`/
  `query`/`body`|`json`, returning a structured JSON result.
- **On by default** for every agent (unlike the opt-in sandbox): the reduced
  public-fetch scope + always-on SSRF floor make it safe as a standard built-in.
  A `system_tools.web_request` block *configures* it; `web_request: false` (global or
  per-agent) or `tools: false` disables it.
- An **always-on SSRF floor** that no config can disable: globally-routable-
  unicast-only IP validation (internal-range blocklist + evasion hardening) and
  DNS-rebind IP-pinning.
- Operator guardrails: optional **exact-host** allowlist, an https-only floor
  (`require_https: true` by default; set `require_https: false` to allow plaintext
  http), and caps on timeout / response size / request size.
- **Per-agent enablement** via front matter: opt out (`false`) or inherit the
  global config (`true`/absent). No per-agent override *object* in v1 (see
  Non-goals / *Phased delivery*).
- Opt-in telemetry: a per-call span, a metric counter, a new fault domain, and
  the `system_tools_used` indexing summary key — with basic redaction (no query
  string, headers, or bodies in logs/spans).
- No new hard dependency (`aiohttp` is already a runtime dependency).

**Non-goals (v1)** — deferred items, tagged **(v2)** / **(v3+)** where they appear
in §4; see *Phased delivery* (below) for the exact cut line.

- **Per-host authentication** — static-header auth profiles + `{env: VAR}` secret
  injection are **(v2)**; managed-identity / Key Vault refs are **(v3+, #1037)**. v1
  is unauthenticated (the model may still pass its own `headers`).
- **Redirect following (v2)** — v1 issues one request and returns a 3xx as-is
  (`redirect_count` always `0`).
- **Per-agent override *object* (v2)** — v1 front matter is only `false` (opt out)
  or `true`/absent (inherit); the field-level object, the agent-vs-operator trust
  model, and a `allow_agent_override` ceiling (v3+) are deferred.
- **Wildcard / IDNA host matching (v2)** — v1 `allowed_hosts` is exact-host only.
- **Private / internal-network destinations** — blocked by the floor in **every**
  version; a v2 operator-controlled private-range allowlist is the planned path.
- **Full telemetry parity (v2)** — v1 ships a minimal set (one span, one counter,
  `FaultDomain.WEB_REQUEST`, the indexing key, basic redaction).
- Retries / backoff (left to the agent), response streaming (v2), cookie jar /
  cross-call HTTP state, non-HTTP(S) schemes, and a generalized system-tools
  registry / shared base.

### Phased delivery (v1 / v2 / v3+)

v1 is deliberately a **minimal, public, unauthenticated fetch** primitive. The
security floor (SSRF validation + caps) ships in full from day one; everything
that adds *credential handling* or *destination flexibility* is staged later, so
v1 carries the smallest possible threat surface.

| Capability | v1 | v2 | v3+ |
| --- | --- | --- | --- |
| Tool surface `web_request(method, url, headers?, query?, body?/json?)` | Yes | Yes | Yes |
| Structured JSON response (`status` / `response_headers` / `body` / truncation) | Yes | Yes | Yes |
| Default-on enablement + per-agent `false` / inherit | Yes | Yes | Yes |
| Always-on SSRF floor (blocklist + global-unicast + DNS-rebind pin) | Yes | Yes | Yes |
| `require_https` floor (default true) + `false` escape hatch | Yes | Yes | Yes |
| Caps (timeout / request bytes / response bytes) | Yes | Yes | Yes |
| Static request/response header denylist (hop-by-hop / cookie / auth) | Yes | Yes | Yes |
| `allowed_hosts` **exact-host** allowlist | Yes | Yes | Yes |
| Minimal telemetry (span + counter + `WEB_REQUEST` + indexing key) | Yes | Yes | Yes |
| **Per-host auth** (static headers + `{env: VAR}`) + secret redaction | — | Yes | Yes |
| **Redirect following** (per-hop re-validation) | — (returns 3xx) | Yes | Yes |
| **Per-agent override object** (field-level) + trust model | — | Yes | Yes |
| **Wildcard / IDNA** host matching | — | Yes | Yes |
| Full telemetry parity (per-outcome metrics / attributes) | — | Yes | Yes |
| Operator private-range allowlist (not agent-overridable) | — | Yes | Yes |
| Reflection / exfiltration mitigations (path-method allowlist, etc.) | n/a | Yes | Yes |
| Managed identity / Key Vault secret refs (#1037) | — | — | Yes |
| Global-ceiling mode (`allow_agent_override: false`) | — | — | Yes |
| Out-of-process secret broker / egress-proxy custody (Tier 2) | — | — | Yes |
| Response DLP; streaming; retries; cookie jar; tools registry | — | — | Yes |

## 4. Proposed design

> **Reading guide (v1 scope).** This section documents the **full target design**.
> Items tagged **(v2)** or **(v3+)** are deferred (see §3 *Phased delivery*); the
> **v1** surface is everything **untagged** — a public, *unauthenticated* fetch with
> the always-on SSRF floor. In particular, v1 ships **no** `auth` injection, **no**
> redirect following (a 3xx is returned as-is), **no** per-agent override object
> (only `false` / inherit), and **exact-host** allowlist matching only.

`web_request` is a **system tool**, so it rides the existing four-stage pipeline
(`docs/architecture.md` §2: discover → translate → register → execute) exactly
where the sandbox does. A new `system_tools/web_request.py` module owns the tool
factory and the SSRF validator; `config/schema.py` and `config/merge.py` gain the
config models and merge rule; `registration/capabilities.py` builds the tool once
per agent onto a new `AgentCapabilities.web_request_tools` field and `runner.py`
carries it through a dedicated `web_request_tools=` channel; `_observability.py` and
`app.py` add telemetry.

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| translate | `config/schema.py` | New `WebRequestAuthProfile`, `WebRequestConfig`, `WebRequestAgentOverride` models. `SystemToolsConfig.web_request: WebRequestConfig \| bool \| None` (**default-on**: absent/`None`/`True` → enabled with defaults, `False` → disabled app-wide, object → enabled + configured); `SystemToolsAgentOverride.web_request: bool \| WebRequestAgentOverride \| None`; `ResolvedAgent.web_request_config: WebRequestConfig \| None` (`None` ⇒ disabled for this agent). |
| translate | `config/merge.py` | New `_resolve_web_request(spec, global_config)`. **Global** absent/`True`/object → enabled (default `WebRequestConfig()` unless an object is given), `False` → disabled app-wide. **Per-agent** `False` → disabled; `True`/absent → inherit the (default-on) global; override object → field-level replacement over global **(v2)**. Unlike `_resolve_sandbox` (opt-in), the default when unset is **enabled**. |
| register | `registration/capabilities.py`, `registration/_handlers.py`, `runner.py` | `build_capabilities(...)` builds the tool **once per agent** (stateless — no session id) via `create_web_request_tools(resolved.web_request_config)` and stores it on a new `AgentCapabilities.web_request_tools` field, suppressed when `resolved.tools_disabled` (mirrors `filtered_user_tools`). A new dedicated `web_request_tools=` runner channel — parallel to the existing `sandbox_tools=` channel (`runner.py:242`) — carries it through **every** registration path (HTTP/non-HTTP triggers, built-in chat + SSE endpoints) so all entry points behave identically. |
| execute | **new** `system_tools/web_request.py` | `create_web_request_tools(config)` returns a `FunctionTool` via `@tool` + a Pydantic param schema; async HTTP resources (connector/session) are created **lazily** on first invocation. Per invocation: canonicalize the URL, run the SSRF validator (parse/normalize → allowlist → resolve → validate every resolved IP as global-unicast → **pin**), issue the `aiohttp` request with `allow_redirects=False` (v1 returns any 3xx as-is), enforce caps + incremental read, shape the JSON result, and emit a span + counter. **(v2)** resolves `{env: VAR}` auth into a closure-local structure (never on `ResolvedAgent`), attaches per-host auth after validation, and follows redirects with per-hop re-validation. |
| bootstrap / telemetry | `_observability.py`, `app.py` | New `FaultDomain.WEB_REQUEST`; `record_web_request(...)` counter(s); a `web_request` span with redaction. `app.py` adds `"web_request"` to the `system_tools_used` indexing summary (global block + per-agent when enabled). |

**Boundary note.** Unlike the sandbox (rebuilt per request because it needs the
runtime `session_id` for REPL state), `web_request` is stateless, so its
`FunctionTool` is built **once per agent** at registration and its closure
captures the agent's resolved `WebRequestConfig`. Each invocation performs its own
validated request. This keeps registration the only Azure-aware stage untouched —
`web_request` reaches arbitrary *public* hosts and needs no Azure resource.

**Enablement note.** Because it needs no Azure resource and is SSRF-floored,
`web_request` is **on by default** — diverging from the opt-in sandbox
(`dynamic_sessions_code_interpreter`), which stays presence-enabled because it
provisions an ACA session pool. Operators disable `web_request` with
`system_tools.web_request: false` (app-wide) or per-agent `web_request: false`; the
`tools: false` kill-switch suppresses it too (parity with the sandbox).

### Authoring / API surface

**`web_request` is on by default** (see the *Enablement note* above). The
`system_tools.web_request` block is therefore for **configuration**, not enablement —
and every field in it is optional.

**Global config (`agents.config.yaml`)** — the v1 surface:

```yaml
system_tools:
  web_request:                  # optional — omit this block and web_request is still ON
    allowed_hosts:            # optional; unset = any PUBLIC host reachable
      - api.example.com       # exact host only in v1 (wildcards are v2)
    require_https: true       # https floor by default; set false to allow http
    timeout_seconds: 30       # clamped to an absolute operational max
    max_response_bytes: 5000000
    max_request_bytes: 1000000
```

To **turn the tool off app-wide**, set the key to `false`:

```yaml
system_tools:
  web_request: false            # disable web_request for every agent
```

**Per-agent (`*.agent.md` front matter)** — v1 supports two shapes:

```yaml
system_tools:
  web_request: false            # (1) opt this agent out (overrides the default-on)
```
```yaml
# (2) key absent, or `true` → inherit the global config (on by default)
```

> The field-level per-agent **override object** (its own `allowed_hosts` / `auth` /
> caps) is a **v2** feature; see *Target authoring surface (once all versions ship)*
> at the end of this section. In v1, a per-agent value is only `false` or `true`.

**Tool surface seen by the model:**
`web_request(method, url, headers?, query?, body?|json?)` where `method` is a
`Literal["GET","POST","PUT","PATCH","DELETE","HEAD","OPTIONS"]` (default `GET`),
`body` is a raw string and `json` is any JSON value (mutually exclusive; `json`
sets `Content-Type: application/json`). Timeout and size limits are **not** model
parameters — they are operator config. The result is a JSON string:

```json
{
  "status": 200,
  "url": "https://api.example.com/v1/thing",
  "content_type": "application/json",
  "redirect_count": 0,
  "response_headers": { "...": "redaction-filtered subset (see below) ..." },
  "body": { "parsed": "JSON when content-type is JSON, else raw text" },
  "body_truncated": false,
  "body_omitted_reason": null
}
```

`response_headers` is a **redaction-filtered subset**, not the raw header set:
hop-by-hop, cookie, and auth headers (`Set-Cookie`, `Authorization`,
`Proxy-Authorization`, `WWW-Authenticate`, `Proxy-Authenticate`, `Cookie`) are
stripped (see *Header & scheme policy*). **(v2)** additionally redacts configured
secret values from whatever remains. The returned `url` has its **query string and userinfo stripped**,
and `redirect_count` reports how many hops were followed (**v1: not followed** — a
3xx is returned as-is with its `Location` header, so `redirect_count` is always
`0`; see *Redirect handling*). Binary bodies are **not**
returned (`body: null`, `body_omitted_reason: "binary"`, with `content_type` +
`response_bytes` still reported); a `HEAD` returns `body: null,
body_omitted_reason: "head"`. A response exceeding `max_response_bytes` is
**truncated** with `body_truncated: true` (never a hard error), and a truncated
JSON body is returned as **raw text**, not parsed.

### SSRF validator contract

A single URL parser runs before every request (and, in **v2**, before every
redirect hop):

1. **Parse & reject** malformed URLs: missing scheme/host, embedded userinfo
   (`user:pass@host`), non-`http(s)` scheme, invalid/out-of-range port.
2. **Normalize the host**: lowercase, strip a single trailing dot, IDNA-encode to
   ASCII (punycode) so IDN and mixed-case hosts match the allowlist canonically.
3. **Enforce the allowlist** (if configured) on the normalized host *before* any
   DNS lookup.
4. **Resolve** via an **injectable async resolver** (so tests never touch real
   DNS/network) and validate **every** resolved IP with a *global-unicast-only*
   predicate (`ipaddress`): reject loopback, link-local, private (RFC1918), ULA,
   unspecified (`0.0.0.0`/`::`), reserved, multicast, CGNAT (`100.64.0.0/10`), and
   IMDS (`169.254.169.254`); explicitly unwrap IPv4-mapped IPv6
   (`::ffff:169.254.169.254`). Reject non-canonical numeric IP literals (decimal
   `2130706433`, octal `0177.0.0.1`, hex `0x7f000001`).
5. **Pin** the connection to the exact validated IP(s) via a custom aiohttp
   resolver/connector, preserving the original `Host` header and TLS SNI, with DNS
   caching disabled so a later rebind cannot outlive the validation decision.

### Host normalization & matching

The normalized host (lowercased, one trailing `.` stripped) feeds allowlist
matching. **v1 matches the exact host** — `api.example.com` matches only that host.
Wildcard (`*.example.com`, subdomains only, not the apex) and IDNA / punycode
canonicalization are **v2** (see §5 C10 / J5).

### Header & scheme policy

- **Request denylist** (caller-supplied headers dropped so they can't break framing
  or hijack routing): `Host`, `Content-Length`, `Transfer-Encoding`, `Connection`,
  `Upgrade`, `TE`, `Trailer`, `Proxy-Authorization`, `Proxy-Connection`.
- **Response** headers are returned as the redaction-filtered subset described above
  (auth / cookie / hop-by-hop stripped).
- **Port / scheme:** any TCP port on an allow-listed public host; the scheme floor
  is `https` unless `require_https: false`.
- **(v2)** per-host `auth` headers are applied after validation and take precedence
  over model-supplied headers of the same name.

### Redirect handling (v1: not followed)

v1 issues a single request (`allow_redirects=False`) and **returns** any 3xx
response as-is (status + `Location` header) for the agent to act on;
`redirect_count` is always `0`. Transparent per-hop-revalidated following is **v2**
(see §5 C5 / C8 / J3).

### Execution-surface invariant

The model **never executes code in the worker process.** The runtime's only
code-execution tool is the ACA Dynamic Sessions sandbox, which dispatches model
code to a separate container over HTTPS; there is **no in-worker `exec` / `eval`
fallback**, so with no sandbox configured the model simply cannot run code
anywhere. The one in-worker author-code path (`tools/*.py`, loaded once at startup
and cached) is deploy-time code the model cannot add or modify at runtime. Standing
invariant: **never `exec` model output in the worker.** Authors who need to run
model-generated code must use the ACA sandbox — **never** an in-worker `tools/*.py`
that executes model-supplied commands (that re-creates the sandbox without its
isolation). See §5 I1.

### Deferred to v2 / v3+ (design notes)

These are **out of v1** (see §3 *Phased delivery* and the §5 rows noted); they are
sketched only so the v1 shape stays forward-compatible.

- **Per-host auth + secret custody (v2 — D1–D5 / J2).** Static-header profiles with a
  typed `{env: VAR}` secret-reference resolved only at tool-build into a
  closure-local structure (never persisted on `ResolvedAgent`), plus defense-in-depth
  value redaction. Extends to Key Vault / managed-identity refs (#1037, v3+).
- **Per-agent override object + trust model (v2 — A2 / A4 / J4).** A field-level
  per-agent object that may *widen* the global config under a **single trust-domain**
  assumption (global is a default, not a ceiling); a `allow_agent_override: false`
  ceiling for less-trusted authors is v3+. v1 has only `false` / inherit, so this
  question does not arise. The always-on SSRF floor is **never** overridable.
- **Redirect following (v2 — C5 / C8 / J3)** — per-hop re-validation; auth never
  carried cross-host; `301/302/303` → GET, `307/308` preserve method + body.
- **Wildcard / IDNA host matching (v2 — C10 / J5).**
- **Reflection / confused-deputy residual (v2 — I2).** Once auth ships, a credentialed
  request to a model-controlled destination-within-allowlist whose response the model
  reads can bounce the injected header back into context. Mitigation ladder (by
  leverage): short-lived least-privilege creds (#1037) → path/method allowlist →
  value redaction (best-effort) → response DLP → governance ceiling. Inherent to any
  authenticated tool; **not** closed by *where* the secret is stored.
- **Out-of-process secret custody (v2 opt-in / v3+ — I3 / I4).** The ACA egress proxy
  offers true out-of-process, destination-bound credential injection, but it mandates
  the sandbox (contradicting the "no sandbox required" motivation), is preview, adds
  latency, does **not** fix reflection, and removes the runtime's redaction backstop
  — so it is a deferred opt-in "Tier 2" backend, not a v1 replacement. Re-hosting the
  runtime's compute on ACA Sandboxes is **rejected** (category mismatch; a
  product-level decision outside this FRD). `web_request` stays a plain Functions
  `FunctionTool` independent of these features.
- **Full telemetry parity (v2 — F1–F4 / J6)** — richer per-outcome metrics /
  attributes and secret-value redaction across surfaces.

### Target authoring surface (once all versions ship)

> **Illustrative — not v1.** The blocks below show the **full** configuration
> surface after the v2/v3+ features land (per-host `auth`, wildcard hosts, the
> per-agent override object, redirect following). In v1 these portions are **not
> accepted**; the v1 surface is the minimal example under *Authoring / API surface*
> above.

```yaml
# agents.config.yaml — full (future) surface
system_tools:
  web_request:
    allowed_hosts:
      - api.example.com
      - "*.partner.com"       # (v2) wildcard: subdomains only, not the apex
    require_https: true
    timeout_seconds: 30
    max_response_bytes: 5000000
    max_request_bytes: 1000000
    max_redirects: 5          # (v2) redirect following
    auth:                     # (v2) per-host static-header profiles
      - host: api.example.com
        headers:
          Authorization: { env: API_TOKEN }   # typed secret-ref; resolved at
                                               # tool-build, never persisted
      - host: "*.partner.com"
        headers:
          X-API-Key: { env: PARTNER_KEY }
```

```yaml
# *.agent.md — full (future) per-agent override object (v2). `auth` fully
# replaces the global auth set (A5); unspecified fields fall back to global.
system_tools:
  web_request:
    allowed_hosts:
      - api.crm.example.com
    auth:
      - host: api.crm.example.com
        headers:
          Authorization: { env: CRM_TOKEN }
```

### Compatibility

- **Additive, but default-on (behavior change on upgrade).** The sandbox is
  untouched, but once the runtime ships `web_request`, every agent **gains it
  enabled** — an app that sets no `system_tools.web_request` block now has an
  outbound-HTTP tool it did not have before. The always-on SSRF floor + https-only
  keep this safe from internal-resource attacks (IMDS / private ranges); operators
  who require **zero outbound egress** must set `system_tools.web_request: false`, and
  those who want a hard destination boundary should set `allowed_hosts`.
- **No new hard dependency** — `aiohttp` is already a runtime dependency (used by
  the sandbox).
- The per-agent shapes (`false` / inherit in v1; `+ object` in v2) match the
  established `bool | Filter | None` override idiom used for `mcp` / `skills` /
  `tools`.
- The auth-profile model is forward-compatible with #1037: today only `headers`
  is supported; `managed_identity` / `key_vault_ref` can be added per profile
  later without breaking the shape.

## 5. Decisions log

> Ported from the pre-plan design discussion (`files/1176-design-questions.md`).
> Append-only.
>
> **Naming:** rows below use the original `http_call` / `allow_http` identifiers.
> **J8** renamed the tool (and config key) to `web_request` and the scheme flag to
> `require_https` (default `true`); §1–§4 and §6–§8 use the current names.

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| A1 | What enables the tool | `http_call: true` flag / config-object presence / default-on | Presence of the `system_tools.http_call` config object enables it (`http_call: {}` = defaults; absent/`false` = off), mirroring the sandbox; default **off** | Human | 2026-07-08 |
| A2 | Per-agent override model | no override / narrow-only (global = ceiling) / full object replacement | **Full object replacement:** `http_call: bool \| HttpCallAgentOverride \| None`; `false` = opt out; `true`/absent = inherit; **object replaces any of the config fields**, unspecified fall back to global | Human | 2026-07-08 |
| A3 | The always-on floor | everything overridable / non-overridable SSRF floor | Floor = request-time **SSRF validation** (internal blocklist C2 + DNS-rebind IP-pinning C3) — validation logic, not config, so it always applies; timeout/sizes additionally **clamped to an absolute operational max** (worker resource-safety). Everything else is per-agent replaceable | Human | 2026-07-08 |
| A4 | Can a per-agent override *widen* global? | narrow-only (least-privilege) / global is a pure default | **Single trust domain** — global config + front matter share one author/review/deploy, so per-agent `allowed_hosts`/`auth` **may exceed** global; global is a **default, not a ceiling** (narrow-only would force widening the global list to serve one agent) | Human | 2026-07-08 |
| A5 | Per-agent `auth` merge semantics | union with global / full replace | Per-agent `auth` **fully replaces** the global auth set for that agent; secrets still resolve only via env-substitution (front matter holds **references, never values**) | Human | 2026-07-08 |
| E1 | Config shape | sibling field (mirror sandbox) / general system-tools registry now | **Sibling `http_call` field** on `SystemToolsConfig`; structured `HttpCallConfig` + `HttpCallAgentOverride`; registry/shared-base generalization **deferred** | Human | 2026-07-08 |
| B1 | Request params | verb-specific tools / single tool; `body` object vs raw+json | Single tool: `method` (Literal, default GET), `url`, `headers`, `query` (`dict[str,str]`), mutually-exclusive `body` (raw str) / `json` (Any → sets JSON content-type). Timeout/size are **not** model params | Human | 2026-07-08 |
| B2 | Response shape | status+headers+body / richer object | JSON string: `status`, final `url`, `content_type`, `headers` (all), `body` (parsed), `body_truncated` | Human | 2026-07-08 |
| B3 | Body handling | auto-parse / always text; error vs truncate on cap; binary | Auto JSON-vs-text by content-type; **binary omitted** (content_type + length + note); size-cap **truncates** with `body_truncated: true` (no hard error); single tool | Human | 2026-07-08 |
| C1 | Host allowlist model | deny-by-default required / optional allowlist / allow-all+blocklist | **Optional `allowed_hosts`**: unset = any PUBLIC host; set = only those. Matching = **exact host + wildcard/suffix** (`*.example.com` matches subdomains, **not** the apex — list both to include it) | Human | 2026-07-08 |
| C2 | Internal-range blocklist | none / always-on | **Always-on, non-configurable (v1):** IMDS `169.254.169.254`, loopback `127.0.0.0/8`+`::1`, link-local `169.254/16`+`fe80::/10`, RFC1918 private + IPv6 ULA `fc00::/7` | Human | 2026-07-08 |
| C3 | DNS-rebind defense | hostname-only checks / resolve+validate+pin | Resolve host, validate **every** resolved IP against the blocklist, and **pin the connection to the validated IP** (prevents TOCTOU rebind) | Human | 2026-07-08 |
| C4 | Scheme policy | https-only / allow http | **https-only by default**; optional `allow_http` flag (default `false`) | Human | 2026-07-08 |
| C5 | Redirects | don't follow / follow+re-validate | Follow up to `max_redirects` (default 5), **re-validating each hop** through the full SSRF check; return the final URL | Human | 2026-07-08 |
| C6 | Header policy | allow all / denylist | Deny `Host` and computed `Content-Length`; config-injected auth headers **win** over model-supplied headers of the same name | Human | 2026-07-08 |
| C7 | Caps | none / global-config caps + hard max | Global caps `timeout_seconds` (default 30, absolute max ~120), `max_response_bytes`, `max_request_bytes`, `max_redirects` | Human | 2026-07-08 |
| D1 | Auth model | single global header set / per-host profiles | **Per-host profiles** `auth: [{host, headers}]`; credentials attached only to the matching host and **only after SSRF validation** | Human | 2026-07-08 |
| D2 | Secret sourcing | model-supplied / config env-substitution | Secrets via existing `substitute_env_vars_in_value` / `has_unresolved_placeholders`; **never** in model context; **redacted** in telemetry/logs; never echoed in responses | Human | 2026-07-08 |
| D3 | Config vs model header precedence | model wins / config wins | Config auth headers **override** model-supplied headers of the same name (consistent with C6) | Human | 2026-07-08 |
| D4 | Auth extensibility + host match | headers-only fixed / extensible shape | Profile shape designed to **extend for #1037** (`managed_identity` / `key_vault_ref` later); v1 = `headers` only; host match = exact + wildcard/suffix (aligns with C1) | Human | 2026-07-08 |
| F1 | Telemetry scope | minimal / full sandbox parity | **Full parity:** add `"http_call"` to `app.py` `system_tools_used`; per-call `http_call.request` span; counter(s) via new `record_http_call(...)`; new `FaultDomain.HTTP_CALL` | Human | 2026-07-08 |
| F2 | Span attributes | url+status / structured non-sensitive set | `method`, `host`, `path`, `status`, `duration_ms`, `response_bytes`, `body_truncated`, `redirect_count`, `outcome` (`success`/`blocked`/`timeout`/`error`) | Human | 2026-07-08 |
| F3 | Redaction | log everything / strict redaction | **Strict:** never log query string, auth/`Authorization` headers, cookies, or request/response bodies; blocked requests log only the **reason category** (`imds`/`loopback`/`private`/`not-in-allowlist`) + host, no raw resolved IPs | Human | 2026-07-08 |
| G1 | Wiring cadence | per-request (like sandbox) / build-once per agent | **Build-once per agent** at registration (stateless); the `FunctionTool` closure captures the agent's resolved config | Human | 2026-07-08 |
| G2 | Request mechanics | shared session / per-call; IP-pin approach | Each invocation performs its own validated request (custom DNS resolver + IP-pin per C3); `aiohttp.ClientSession` lifecycle (app-shared vs per-call) is an impl detail (see §"Open implementation details") | Human | 2026-07-08 |
| H1 | v1 scope IN | — | B-schema tool; presence enablement (A1) + object replacement (A2); SSRF guardrails incl. **wildcard** matching (C1–C7); per-host auth (D1–D4); sibling config (E1); full telemetry (F1–F3); build-once wiring (G1) | Human | 2026-07-08 |
| H2 | v1 scope OUT | — | MI + Key Vault secret refs (#1037); retries/backoff (agent-driven); cookie jar / cross-call state; registry generalization; non-HTTP schemes | Human | 2026-07-08 |
| H3 | Streaming | v1 streaming / defer | **Deferred to v2** (doesn't fit single-result tool call); v1 still reads incrementally up to `max_response_bytes` as a guardrail | Human | 2026-07-08 |
| A6 | Trust-model boundary (revisit A2) | reverse A2 → global ceiling / keep A2 widening + document boundary | **Keep A2 widening** (honors A2/A4); add an explicit trust-model subsection — single trust domain, secret-ref in front matter ⇒ config access; a v2 `allow_agent_override: false` ceiling is noted for less-trusted authors | Agent (arch review) | 2026-07-08 |
| B4 | Internal-LOB motivation vs SSRF floor | promise internal APIs / public-only v1 | **v1 public-only** — the always-on floor blocks private ranges, so internal-LOB is reframed as public SaaS/LOB; operator-controlled private-range access is a v2 follow-up | Agent (arch review) | 2026-07-08 |
| G3 | Wiring channel (build-once → runner) | reuse sandbox per-session channel / dedicated build-once channel | New `AgentCapabilities.http_call_tools` field (suppressed when `tools_disabled`, like `filtered_user_tools`) + a dedicated `http_call_tools=` runner param parallel to `sandbox_tools=`; reaches **all** registration paths uniformly | Agent (arch review) | 2026-07-08 |
| D5 | Secret-reference shape | inline `${VAR}` string / typed `{env: VAR}` dict | **Typed `{env: VAR}` dict** — the loader's eager substitution is str-only (`config/env.py`), so a dict passes through and the value never lands on `ResolvedAgent`; resolved only at tool-build; extends to #1037 (`{key_vault: …}`) | Agent (arch review) | 2026-07-08 |
| F4 | Response header exposure | return all headers / redaction-filtered subset | Return `response_headers` **subset** (strip Set-Cookie / Authorization / Proxy-Authorization / WWW-Authenticate / Proxy-Authenticate / Cookie + configured auth names); redact known secret values from headers/body/`url`/errors; returned `url` has query + userinfo stripped | Agent (arch review) | 2026-07-08 |
| C8 | Redirect mechanics | aiohttp auto-follow / manual per-hop | **Manual** loop (`allow_redirects=False`): re-validate + rebuild headers/auth for each hop; auth never carried cross-host; `301/302/303`→GET, `307/308` preserve method+body | Agent (arch review) | 2026-07-08 |
| C9 | SSRF validator hardening | basic private-range block / global-unicast predicate + evasion coverage | Reject userinfo; reject non-canonical numeric IPs (decimal/octal/hex); unwrap IPv4-mapped IPv6; add CGNAT `100.64.0.0/10`; validate **every** resolved IP as global-unicast; **injectable resolver** so tests avoid real DNS/network | Agent (arch review) | 2026-07-08 |
| C10 | Host normalization | ad-hoc / canonical | Lowercase + strip one trailing dot + IDNA-encode before allowlist/auth matching; wildcard matches any sub-label depth but **not** the apex | Agent (arch review) | 2026-07-08 |
| C11 | Request-header denylist expansion | `Host`+`Content-Length` only / full hop-by-hop set | Also drop `Transfer-Encoding`, `Connection`, `Upgrade`, `TE`, `Trailer`, `Proxy-Authorization`, `Proxy-Connection` | Agent (arch review) | 2026-07-08 |
| C12 | Port policy (v1) | restrict to 80/443 / any port on allowed host | Any TCP port on an allow-listed public host; `https` floor unless `allow_http` | Agent (arch review) | 2026-07-08 |
| B5 | Binary / HEAD / truncated-JSON result semantics | implicit / explicit | Binary → `body: null` + `body_omitted_reason: "binary"` + `response_bytes`; `HEAD` → `body: null` + reason `"head"`; truncated JSON returned as **raw text**, not parsed | Agent (arch review) | 2026-07-08 |
| G4 | `aiohttp` session lifecycle | app-shared pool / lazy per-agent closure | Build the closure **synchronously** at registration; create connector/session **lazily** on first invocation (mirrors the sandbox's lazy async resources) so no event loop is required at build time | Agent (arch review) | 2026-07-08 |
| G5 | `tools: false` and system tools | separate kill-switch now / reuse `tools_disabled` | `tools: false` also suppresses `http_call` (parity with the sandbox); a dedicated `system_tools: false` kill-switch is deferred to v2 | Agent (arch review) | 2026-07-08 |
| I1 | Execution-surface boundary & invariant | rely on prose / make it explicit | Model code runs **only** in the ACA sandbox (separate container over HTTPS), **never** the worker, with **no in-worker fallback** (no sandbox config ⇒ no code execution at all); `tools/*.py` (`exec_module`, startup-only, cached) is the sole in-worker author-code path — in-trust-domain, not model-reachable at runtime. Standing invariant: **never `exec` model output in the worker**. Guideline: use the ACA sandbox for LLM code; **never** author an in-worker LLM-exec tool | Human | 2026-07-09 |
| I2 | Reflection / confused-deputy residual | leave implicit / name it + mitigation ladder | Name it as the sharpest residual (model controls credentialed destination-within-allowlist **and** reads the response; prompt-injection in untrusted bodies amplifies). Mitigations, by leverage: short-lived/least-privilege creds (#1037) → path/method allowlist (v2) → value redaction (v1, best-effort) → response DLP (future) → governance/ceiling (A6) | Human | 2026-07-09 |
| I3 | ACA egress-proxy custody | adopt for v1 / reject / defer as opt-in tier | **Defer to a v2 opt-in "Tier 2" backend.** True out-of-process custody + destination-bound injection, but it mandates the sandbox (contradicts the "no sandbox" motivation §2), is **preview**, adds latency/cost, does **not** solve reflection, and **removes the runtime's response-redaction backstop**. v1 stays a plain Functions `FunctionTool`, independent of this feature | Human | 2026-07-09 |
| I4 | Re-host compute on ACA Sandboxes | re-platform / reject | **Rejected** — category mismatch (Sandboxes are a call-in exec service, not an app host; no ingress/trigger model to host a `FunctionApp`); compute host is a **product-level decision outside this FRD**; and it still wouldn't fix reflection. The real custody path without leaving Functions is an out-of-process **egress broker** (Firewall / APIM / forward-proxy sidecar) in the outbound path = Tier 2 | Human | 2026-07-09 |
| J1 | **v1 scope reduction** (supersedes H1/H2) | keep full v1 / cut to a minimal primitive | **v1 = public, *unauthenticated* fetch primitive.** IN: single-tool schema (B1/B2/B3/B5); default-on enablement (J7) + per-agent `false`/inherit (J4); **always-on SSRF floor** (C2/C3/C9/C12) + https floor (C4) + caps (C7) + request-header denylist (C6/C11) + response-header subset (F4, static part) + **exact-host** allowlist (J5) + minimal telemetry (J6) + build-once wiring (G1/G3/G4/G5). Everything else → v2/v3+ (J2–J6) | Human | 2026-07-10 |
| J2 | Auth / credential injection (revisit D1–D5) | ship in v1 / defer | **→ v2.** v1 injects **no** credentials; `auth`, secret custody/redaction, and the reflection residual (I2) are deferred. Removes the sharpest v1 risk and lets v1 ship without #1037 | Human | 2026-07-10 |
| J3 | Redirect following (revisit C5/C8) | follow+revalidate / don't follow | **→ v2.** v1 does **not** follow redirects — a 3xx is returned as-is with its `Location` header; `redirect_count` is always `0`. The per-hop-revalidated follower is v2 | Human | 2026-07-10 |
| J4 | Per-agent override object (revisit A2/A4/A5/A6) | full object replacement / bool-only | **→ v2.** v1 per-agent value is **only `false` or `true`/inherit** (`SystemToolsAgentOverride.http_call: bool \| None` in v1). The field-level `HttpCallAgentOverride` object, the widen-vs-ceiling trust model, and the `allow_agent_override` ceiling are deferred | Human | 2026-07-10 |
| J5 | Host matching (revisit C1/C10) | exact + wildcard + IDNA / exact-only | **→ exact-host only in v1.** `allowed_hosts` matches the exact normalized (lowercased, trailing-dot-stripped) host; wildcard `*.example.com` and IDNA/punycode canonicalization are **v2** | Human | 2026-07-10 |
| J6 | Telemetry & redaction depth (revisit F1–F4) | full sandbox parity / minimal | **→ minimal in v1.** v1 emits one `http_call.request` span (method, host, status, duration_ms, outcome), `FaultDomain.HTTP_CALL`, `"http_call"` in `system_tools_used`, and the **static** response-header subset stripping (F4). Deferred to **v2**: the full counter set (F1), the complete structured attribute set (F2), and **secret-value** redaction across surfaces (F3 / F4) — inert in v1 since no secrets are injected | Human | 2026-07-10 |
| J7 | Tool enablement (supersedes A1) | opt-in presence / default-on | **Default-on.** Absent/`None`/`True` on `SystemToolsConfig.http_call` ⇒ enabled with defaults; `False` ⇒ disabled app-wide; object ⇒ enabled + configured. Per-agent `false` opts out; `tools: false` suppresses it. The reduced, SSRF-floored public-fetch scope makes default-on safe; diverges from the opt-in sandbox (which needs an Azure resource) | Human | 2026-07-10 |
| J8 | Naming: tool + scheme flag | keep `http_call`/`allow_http` / rename | **Rename.** Tool + config key `http_call` → **`web_request`** (and `HttpCallConfig`→`WebRequestConfig`, module `system_tools/web_request.py`, `AgentCapabilities.web_request_tools`, `FaultDomain.WEB_REQUEST`, `record_web_request`, span `web_request`, `"web_request"` in `system_tools_used`); scheme flag `allow_http` (default `false`) → **`require_https`** (default `true`, inverted polarity — set `require_https: false` to allow plaintext http). Rationale: "http" was overloaded (tool name vs URL scheme), and `allow_http: false` read like it disabled a now-default-on tool | Human | 2026-07-10 |
| K1 | FRD sign-off + PR consolidation | keep #87 FRD-only (stacked impl PR) / consolidate FRD + impl into one PR | **Finalized.** Human signed off on the FRD — including all Agent-decided rows (A6, B4, B5, C8–C12, D5, F4, G3–G5) — after the v1 implementation and a gpt-5.5 rubber-duck validation passed the full gate (ruff / mypy-strict / **587 tests**). FRD 0005 + the v1 implementation were consolidated into a single feature PR **#96** to `main`; the FRD-only PR **#87** was closed as superseded | Human | 2026-07-13 |

## 6. Test plan

> **v1 vs v2.** Bullets tagged **(v2)** are written now but land with those
> features; everything untagged is the **v1** suite.

- [ ] Unit: `tests/test_config_merge.py` — `_resolve_web_request`: **default-on**
  (absent/`True` ⇒ enabled with defaults, global `False` ⇒ disabled app-wide) and
  per-agent `False` opt-out / inherit; **(v2)** override object replaces specified
  fields and falls back to global for the rest, `auth` full-replace (A5); caps
  fallback.
- [ ] Fixture scenario: `tests/fixtures/config_scenarios/<nn_web_request>/` —
  default-on (no block ⇒ enabled), global `web_request: false`, per-agent opt-out;
  **(v2)** per-agent object replacement / auth override.
- [ ] Unit: `tests/test_config_schema.py` (or loader test) — **config** models
  parse; `extra="forbid"` rejects unknown keys; the `{env: VAR}` secret-reference
  parses as a typed ref (not a plain string). Note: `method` Literal validation and
  `body`/`json` mutual exclusion are **tool-parameter** schema, so they live in
  `tests/test_web_request.py`, not the config-schema test.
- [ ] **(v2) Secret-reference / no-leak** (`tests/test_config_loader.py` +
  `test_web_request.py`): a `{env: VAR}` auth value **passes through** the loader's
  eager env-substitution untouched and is **never** present on the resolved
  `GlobalConfig` / `AgentSpec` / `ResolvedAgent`; it is dereferenced only at
  tool-build; the resolved secret value never appears in the tool result
  (`response_headers`/`body`/`url`/error), in span attributes, or in logs.
- [ ] Unit: `tests/test_web_request.py` (new) — tool-param schema validation
  (`method` Literal; `body`/`json` mutual exclusion); JSON-vs-text parse; **binary →
  `body: null` + `body_omitted_reason: "binary"` + `response_bytes`**; **HEAD →
  `body: null` + reason `"head"`**; truncation + `body_truncated` with **truncated
  JSON returned as raw text**; `response_headers` **subset** (auth/cookie/hop-by-hop
  stripped); returned `url` has query + userinfo stripped; `redirect_count` present;
  request-header denylist (expanded set); config-auth-wins precedence.
- [ ] **SSRF suite** (`tests/test_web_request.py`), using an **injectable async
  resolver** so no test touches real DNS/network:
  - IMDS / loopback / link-local / private (RFC1918) / ULA / CGNAT
    (`100.64.0.0/10`) / unspecified blocked, **with and without** an allowlist.
  - **Evasion vectors:** IPv4-mapped IPv6 (`::ffff:169.254.169.254`), non-canonical
    numeric IPs (decimal `2130706433`, octal `0177.0.0.1`, hex `0x7f000001`),
    embedded userinfo (`user@host`), trailing-dot host; **(v2)** IDN/punycode host.
  - DNS-rebind IP-pin: host resolves to a blocked IP → refused; multi-record host
    with one blocked IP → refused.
  - Allowlist **exact host (v1)**; **(v2)** wildcard (subdomain matches at any depth,
    apex does **not**).
  - `https` floor + `require_https: false` override; non-`http(s)` scheme rejected.
- [ ] **Redirect handling — (v1)** a 3xx is returned unfollowed (`redirect_count:
  0`, `Location` preserved). **(v2) suite:** redirect to a blocked host → refused (each hop
  re-validated); **auth is not carried across hosts** (redirect to a different host
  → no `Authorization`; redirect to a profile host → its auth attached);
  `301/302/303` → GET (body dropped), `307/308` preserve method + body;
  `max_redirects` enforced.
- [ ] **(v2)** Auth: per-host profile attached only **after** validation; wildcard host
  match; config auth header overrides a model-supplied header of the same name.
- [ ] Telemetry: **(v1)** the `web_request` span is emitted with the basic
  attributes (method/host/status/duration/outcome) and the static response-header
  subset is applied. **(v2)** full counter set, complete F2 attributes, and F3/F4
  **secret-value** redaction (no query/auth/body/secret values in spans or results).
- [ ] Regression: empty `GlobalConfig` dump includes the new `web_request` key path
  as expected (mirror the observability-key regression in `test_config_loader`).

## 7. Docs impact

> **v1 docs = public-fetch primitive.** Document the **v1** surface (default-on,
> exact-host allowlist, https floor, caps) as shipping behavior; present auth,
> redirects, the override object, and wildcard hosts as **v2** so users aren't told
> to configure things the runtime rejects.

- [ ] `docs/front-matter-spec.md` — document that `web_request` is **on by default**
  and that the `system_tools.web_request` block **configures / disables** it (set
  `web_request: false`, globally or per-agent, to turn it off). Cover the **v1** fields
  only (exact-host `allowed_hosts`, `require_https`, caps); the per-agent value is
  **`bool` in v1** (the `object` override, `auth` / `{env: VAR}` secrets, wildcard
  hosts, and redirect following are **v2**). Security warning: v1 is **public-only**
  (private ranges are always blocked) and **default-on** (operators requiring zero
  egress must opt out with `web_request: false`), and **response bodies are
  untrusted**. Also state the **execution-surface** guidance from §4: the model can
  only run code in the ACA sandbox (never the worker), and authors must **never
  write an in-worker `tools/*.py` that executes model-supplied code/commands** (use
  the ACA sandbox instead).
- [ ] `docs/architecture.md` — add `system_tools/web_request.py` to the §3 module
  map, note the new `AgentCapabilities.web_request_tools` field + `web_request_tools=`
  runner channel, and describe the second system tool in the pipeline description.
- [ ] `docs/front-matter-reference.md` (**auto-generated**) — regenerate with
  `python eng/scripts/generate_config_reference.py` so the new `web_request` schema
  keys appear; CI runs the script with `--check`, so the committed reference must
  match the Pydantic models or the build fails.
- [ ] `README.md` — add a `web_request` example under the system-tools / quickstart
  section. Note it is **on by default** (show `web_request: false` to disable) and use
  the **v1** surface (exact-host allowlist), with the same public-only + untrusted-
  body caveats.
- [ ] `docs/triggers.md` — no change (not a trigger).

## 8. Status & sign-off

- **Status:** `Finalized` (2026-07-13). Human sign-off received; the Agent-decided
  rows were confirmed and the v1 implementation has landed. Becomes `Implemented`
  when PR #96 merges.
- **Agent-decided rows confirmed.** Decisions **A6, B4, B5, C8–C12, D5, F4, G3–G5**
  (made by the Agent during architecture review, recorded in §5 as *Agent (arch
  review)*) were confirmed by the human at sign-off — see row **K1**.
- **Reviews complete (phase 2).** Two independent architecture reviews and a
  human-led security deep-dive were incorporated; the execution-surface / custody
  discussion is captured in §5 rows **I1–I4**. v1 was subsequently narrowed to a
  public, unauthenticated fetch primitive and made default-on (rows **J1–J8**). The
  full rationale and options considered live in the Decisions log and git history.
- **Implementation (phases 3–5) complete.** Delivered on branch
  `larohra-web-request-system-tool`, validated by a gpt-5.5 rubber-duck pass plus a
  full green gate (**587 tests**), and shipped via PR **#96**.

### Residual implementation details (settle during implementation)

- **Shared host-match helper.** One normalized wildcard/suffix matcher (per **C10**)
  reused by both the `allowed_hosts` allowlist (C1) and the per-host `auth`
  profiles (D4) — a single implementation, exercised by shared tests.
- **Custom-resolver wiring in `aiohttp`.** The exact connector/resolver classes and
  how the validated-IP set is threaded into the pinned connection (per **C3/C9/G4**)
  are a coding detail; the contract (pin to validated IPs, preserve Host/SNI, no DNS
  cache) is fixed above.
- **Absolute operational caps.** Confirm the concrete ceiling values
  (`timeout_seconds` max, `max_response_bytes`, `max_request_bytes`, `max_redirects`)
  against worker limits during implementation (defaults in C7 stand unless data says
  otherwise).
