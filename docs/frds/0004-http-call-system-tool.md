---
frd: 0004
title: http_call system tool
status: In review            # Draft → In review → Finalized  (→ Implemented after merge)
author: larohra
created: 2026-07-08
updated: 2026-07-08
issues: [https://github.com/Azure/azure-functions-bucees-planning/issues/1176]
pull_requests: []
branch: larohra-http-call-system-tool
---

# FRD 0004 — `http_call` system tool

## 1. Summary

Add a built-in, opt-in **`http_call` system tool** so an agent can invoke an HTTP
endpoint **directly** instead of generating and running code to make the request.
It is the runtime's second system tool and mirrors the existing
`dynamic_sessions_code_interpreter` (sandbox) across the full config → merge →
impl → wiring → telemetry pipeline. The model calls
`http_call(method, url, headers?, query?, body?|json?)` and receives a structured
JSON result (`status`, final `url` with query/userinfo stripped, `content_type`, a
redaction-filtered `response_headers` subset, parsed `body`, `body_truncated`,
`redirect_count`). Because the tool can reach arbitrary **public** hosts, it adds
an always-on **SSRF security floor** (globally-routable-unicast-only IP validation
+ DNS-rebind IP-pinning) that no configuration can switch off, plus optional
operator controls (exact-host allowlist, https-only, size and time caps) and
opt-in telemetry. It is enabled by adding a `system_tools.http_call` block to
`agents.config.yaml`; the default is **off**.

**v1 is deliberately minimal — a public, *unauthenticated* fetch primitive.**
Governed per-host credential injection, redirect following, per-agent override
objects, and wildcard host matching are **deferred to v2** (see *Phased delivery*,
§3). **v1 is also public-only** — destinations that resolve to private/internal
ranges are blocked by the floor.

## 2. Motivation / problem

**The pain today.** An agent that needs to call a REST API has two poor options:

1. **Generate + run code** in the Dynamic Sessions code interpreter. This is
   slow, non-deterministic, burns tokens, can mis-parse responses, and forces a
   heavyweight Azure dependency (an ACA sessions resource + a managed identity)
   onto an app whose only need is a web request.
2. **Hand-write a custom Python tool per endpoint.** This doesn't scale — a new
   tool for every API — and re-implements request/response plumbing each time.

Calling an HTTP API is the single most common integration need for an agent, and
today there is no first-class, safe primitive for it.

**What `http_call` unlocks.** A declarative, deterministic HTTP primitive with
**one line of config and zero code generation**. Concrete customer scenarios:

- Enrich answers from public / partner APIs (weather, geocoding, pricing, status).
- Read/write a SaaS or line-of-business API exposed on a **public** endpoint
  (allowlisted host) — ticket lookups, inventory, records, RAG-over-API.
- Fire a webhook / post to a downstream system (Teams/Slack webhook, CRM).
- Chain services (call A, use the result to call B) without glue code.

> **v1 is public-only and unauthenticated.** Line-of-business APIs that resolve to
> private (RFC1918 / ULA / link-local) addresses are **out of scope for v1** — the
> always-on SSRF floor blocks private ranges even when the host is allow-listed.
> Operator-controlled private-network access is a planned v2 follow-up (see
> Non-goals and the trust model in §4). Likewise, **v1 injects no credentials**:
> the model supplies any auth header itself, so v1 targets public or
> model-authenticated endpoints. Governed per-host credential injection (secret
> never in model context) is **v2** (see *Phased delivery*, §3).

**Why built-in rather than left to customers.** Making outbound HTTP calls
*safely* is genuinely hard — SSRF, IMDS token theft (`169.254.169.254`), DNS
rebinding, and credential leakage are easy to get wrong. Centralizing it as a
governed system tool means **every agent inherits the security floor** (internal blocklist, IP-pinning,
allowlist) for free instead of each
customer re-implementing it, usually incorrectly. It is deterministic, faster,
and cheaper than code-gen, and needs no sandbox. The existing sandbox already
does deny-by-default host allowlisting to protect its MI token; `http_call`
applies the same rigor to protect the whole worker.

**Enterprise fit.** An operator-controlled **exact-host allowlist** + opt-in
telemetry let a security team govern exactly which hosts agents may reach, with
visibility, from **v1**. **Governed per-host credential injection** (env / Key
Vault / managed identity — so a security team also controls *with which
credentials*, with the secret never entering model context) lands in **v2** (see
*Phased delivery*, §3); until then v1 targets public or model-authenticated
endpoints. Together they are a prerequisite for production / regulated
deployments.

## 3. Goals / Non-goals

**Goals (v1)**
- A single `http_call` tool the model invokes with `method`/`url`/`headers`/
  `query`/`body`|`json`, returning a structured JSON result.
- **Opt-in**, default off; enabled by the presence of the
  `system_tools.http_call` config object (mirrors the sandbox's config-presence
  enablement).
- An **always-on SSRF floor** that no config can disable: globally-routable-
  unicast-only IP validation (internal-range blocklist + evasion hardening) and
  DNS-rebind IP-pinning.
- Operator guardrails: optional **exact-host** allowlist, https-only with an
  `allow_http` escape hatch, and caps on timeout / response size / request size.
- **Per-agent enablement** via front matter: opt out (`false`) or inherit the
  global config (`true`/absent). No per-agent override *object* in v1 (see
  Non-goals / *Phased delivery*).
- Opt-in telemetry: a per-call span, a metric counter, a new fault domain, and
  the `system_tools_used` indexing summary key — with basic redaction (no query
  string, headers, or bodies in logs/spans).
- No new hard dependency (`aiohttp` is already a runtime dependency).

**Non-goals**
- Managed-identity token acquisition and Key Vault secret references — deferred
  to #1037; the auth-profile shape and the `{env: VAR}` secret-reference are
  designed to extend to them (e.g. `{key_vault: ...}`, `{managed_identity: ...}`).
- **Private / internal-network destinations** (RFC1918 / ULA / link-local) — out
  of scope for v1; the always-on floor blocks them. A v2 operator-controlled
  private-range allowlist (not agent-overridable) is the planned path.
- **A per-agent-widening opt-out** (`allow_agent_override` / global-ceiling mode)
  — v1 assumes a single trust domain (§4 trust model); a future flag can lock it
  down for deployments where agent authors are less trusted than operators.
- Retries / backoff — intentionally left to the agent, not the tool.
- Response streaming — deferred to v2 (does not fit a single-result tool call);
  v1 still reads incrementally up to `max_response_bytes` as a guardrail.
- Cookie jar / cross-call HTTP state; non-HTTP(S) schemes.
- A generalized system-tools registry / shared base — deferred; `http_call` is
  added as a sibling field like the sandbox.

## 4. Proposed design

`http_call` is a **system tool**, so it rides the existing four-stage pipeline
(`docs/architecture.md` §2: discover → translate → register → execute) exactly
where the sandbox does. A new `system_tools/http_call.py` module owns the tool
factory and the SSRF validator; `config/schema.py` and `config/merge.py` gain the
config models and merge rule; `registration/capabilities.py` builds the tool once
per agent onto a new `AgentCapabilities.http_call_tools` field and `runner.py`
carries it through a dedicated `http_call_tools=` channel; `_observability.py` and
`app.py` add telemetry.

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| translate | `config/schema.py` | New `HttpCallAuthProfile`, `HttpCallConfig`, `HttpCallAgentOverride` models. `SystemToolsConfig.http_call: HttpCallConfig \| None`; `SystemToolsAgentOverride.http_call: bool \| HttpCallAgentOverride \| None`; `ResolvedAgent.http_call_config: HttpCallConfig \| None`. |
| translate | `config/merge.py` | New `_resolve_http_call(spec, global_config)`: `False` → disabled; override object → field-level replacement over the global `HttpCallConfig` (unspecified fields fall back to global); `True`/absent → global. Mirrors `_resolve_sandbox`. |
| register | `registration/capabilities.py`, `registration/_handlers.py`, `runner.py` | `build_capabilities(...)` builds the tool **once per agent** (stateless — no session id) via `create_http_call_tools(resolved.http_call_config)` and stores it on a new `AgentCapabilities.http_call_tools` field, suppressed when `resolved.tools_disabled` (mirrors `filtered_user_tools`). A new dedicated `http_call_tools=` runner channel — parallel to the existing `sandbox_tools=` channel (`runner.py:242`) — carries it through **every** registration path (HTTP/non-HTTP triggers, built-in chat + SSE endpoints) so all entry points behave identically. |
| execute | **new** `system_tools/http_call.py` | `create_http_call_tools(config)` resolves `{env: VAR}` auth secrets into a **closure-local** structure (never on `ResolvedAgent`) and returns a `FunctionTool` via `@tool` + a Pydantic param schema; async HTTP resources (connector/session) are created **lazily** on first invocation. Per invocation: canonicalize the URL, run the SSRF validator (parse/normalize → allowlist → resolve → validate every resolved IP as global-unicast → **pin**), attach the matching per-host auth **after** validation, issue the `aiohttp` request with `allow_redirects=False` and a **manual, per-hop-revalidated** redirect loop that rebuilds headers/auth from scratch for each hop, enforce caps + incremental read, shape the redaction-filtered JSON result, and emit a span + counter. |
| bootstrap / telemetry | `_observability.py`, `app.py` | New `FaultDomain.HTTP_CALL`; `record_http_call(...)` counter(s); an `http_call.request` span with redaction. `app.py` adds `"http_call"` to the `system_tools_used` indexing summary (global block + per-agent when enabled). |

**Boundary note.** Unlike the sandbox (rebuilt per request because it needs the
runtime `session_id` for REPL state), `http_call` is stateless, so its
`FunctionTool` is built **once per agent** at registration and its closure
captures the agent's resolved `HttpCallConfig`. Each invocation performs its own
validated request. This keeps registration the only Azure-aware stage untouched —
`http_call` reaches arbitrary *public* hosts and needs no Azure resource.

### Authoring / API surface

**Global config (`agents.config.yaml`)** — presence enables the tool:

```yaml
system_tools:
  http_call:
    allowed_hosts:            # optional; unset = any PUBLIC host reachable
      - api.example.com
      - "*.partner.com"       # wildcard: subdomains only, not the apex
    allow_http: false         # https-only floor unless true (default false)
    timeout_seconds: 30       # clamped to an absolute operational max
    max_response_bytes: 5000000
    max_request_bytes: 1000000
    max_redirects: 5
    auth:                     # per-host static-header profiles
      - host: api.example.com
        headers:
          Authorization: { env: API_TOKEN }   # typed secret-ref; resolved at
                                               # tool-build, never persisted
      - host: "*.partner.com"
        headers:
          X-API-Key: { env: PARTNER_KEY }
```

`http_call: {}` enables the tool with defaults; the key being **absent** (or
`false`) leaves it off.

**Per-agent override (`*.agent.md` front matter)** — three shapes:

```yaml
system_tools:
  http_call: false            # (1) opt this agent out entirely
```
```yaml
# (2) key absent or `true` → inherit the global config
```
```yaml
system_tools:
  http_call:                  # (3) object = field-level override of global defaults
    allowed_hosts:
      - api.crm.example.com
    auth:                     # `auth` fully replaces the global auth set (A5)
      - host: api.crm.example.com
        headers:
          Authorization: { env: CRM_TOKEN }
    # other fields not specified here fall back to the global http_call config
```

**Tool surface seen by the model:**
`http_call(method, url, headers?, query?, body?|json?)` where `method` is a
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
`Proxy-Authorization`, `WWW-Authenticate`, `Proxy-Authenticate`, `Cookie`, plus
every configured auth header name) are stripped, and any configured secret values
are redacted from whatever remains (see *Header policy* and *Secret handling &
redaction*). The returned `url` has its **query string and userinfo stripped**,
and `redirect_count` reports how many hops were followed. Binary bodies are **not**
returned (`body: null`, `body_omitted_reason: "binary"`, with `content_type` +
`response_bytes` still reported); a `HEAD` returns `body: null,
body_omitted_reason: "head"`. A response exceeding `max_response_bytes` is
**truncated** with `body_truncated: true` (never a hard error), and a truncated
JSON body is returned as **raw text**, not parsed.

### Trust model / security boundary

v1 assumes a **single trust domain**: whoever authors an agent's front matter is
as trusted as whoever writes the global `agents.config.yaml`. Concretely, a
per-agent `http_call` object may **widen** beyond the global config (add hosts,
relax `allow_http`, replace `auth`), because a secret-reference in front matter
already implies config-level access to the deployment. The global config is a
**default**, not a ceiling.

What is **not** overridable at any layer is the always-on SSRF floor (private/
internal-range blocking, IP-pinning, scheme/port validation, absolute operational
caps) — it is validation logic, not a config field.

A future **global-ceiling mode** (`allow_agent_override: false`) is noted as a v2
follow-up for deployments where agent authors are *less* trusted than operators;
it is intentionally out of v1 (see Non-goals).

### SSRF validator contract

A single URL parser runs before every request and every redirect hop:

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

The same normalized host feeds both allowlist and auth-profile matching:

| Aspect | Rule |
| --- | --- |
| Case | Case-insensitive (hosts lowercased). |
| Trailing dot | One trailing `.` stripped before comparison. |
| IDN / punycode | IDNA-encoded to ASCII before comparison. |
| Exact match | `api.example.com` matches only that host. |
| Wildcard `*.example.com` | Matches any sub-label depth (`a.example.com`, `a.b.example.com`) but **not** the apex `example.com`. |
| Port | Allowlist entries are host-only; port is validated separately (below). |

### Header policy (request & response)

- **Request denylist** (caller-supplied headers dropped so they can't break
  framing or hijack routing/auth): `Host`, `Content-Length`, `Transfer-Encoding`,
  `Connection`, `Upgrade`, `TE`, `Trailer`, `Proxy-Authorization`,
  `Proxy-Connection`. Per-host `auth` headers are applied by the runtime **after**
  validation and take precedence over any model-supplied header of the same name.
- **Response** headers are returned as the redaction-filtered subset described
  above (auth/cookie/hop-by-hop stripped, secret values redacted).
- **Port policy (v1):** any TCP port is allowed on an allow-listed public host;
  the scheme floor is `https` unless `allow_http: true`.

### Redirect handling

Redirects are followed manually (`allow_redirects=False`) so each hop is
re-validated:

- Validate the **next** hop's URL (scheme/host/port + full SSRF check + pin)
  **before** sending it; reject non-`http(s)` redirect targets.
- **Rebuild headers from scratch** per hop; attach per-host `auth` **only** if the
  new host matches a profile — auth is **never** carried across hosts.
- Method semantics: `301/302/303` → `GET` (drop the body); `307/308` → preserve
  method + body.
- Stop at `max_redirects`; report the number followed as `redirect_count`.

### Secret handling & redaction

Auth secret values use a **typed env secret-reference** — a mapping
`{ env: VAR_NAME }` — rather than an inline `${VAR}` string. This is deliberate:
the loader's eager env-substitution (`config/loader.py` → `resolve_env_vars_in_data`
in `config/env.py`) only substitutes **string** values, so a dict passes through
untouched and the secret value **never enters the eager-substitution path** and is
**never stored** on `GlobalConfig` / `AgentSpec` / `ResolvedAgent`. The reference
is dereferenced to its real value only at **tool-build time**, inside
`create_http_call_tools`, into a closure-local structure.

Redaction is defense-in-depth: known configured secret values are stripped from
result headers, body, `url`, and error strings, and from span attributes, before
anything is returned to the model or telemetry. The honest guarantee is: *the
runtime never returns the auth it injected and redacts known secret values, but a
cooperating upstream can still reflect other data — response bodies are untrusted.*

See **Residual threat: reflection / confused-deputy exfiltration** below for the
sharpest remaining risk and its mitigation ladder, and **Execution surfaces & the
in-worker-code boundary** for *why* an injected secret is safe from model-authored
code in the first place.

This shape also extends cleanly to #1037 (`{ key_vault: ... }` /
`{ managed_identity: ... }` references).

### Execution surfaces & the in-worker-code boundary

This subsection makes the runtime's code-execution model explicit, because it is
what makes an injected `http_call` secret safe from the model in the first place.

- **The model never executes code in the worker process.** The runtime's *only*
  code-execution tool is the ACA Dynamic Sessions sandbox (`system_tools/sandbox.py`),
  and it dispatches the model's code to a **separate ACA container over HTTPS** —
  there is no `exec` / `eval` / `compile` / `runpy` of model output anywhere in
  `src/`. Crucially, **there is no in-worker fallback**: if
  `system_tools.dynamic_sessions_code_interpreter` is absent, `sandbox_config` is
  `None` (`registration/_handlers.py`) and *no* code-execution tool is built. The
  model then simply **cannot run code anywhere** — any code it emits is inert text.
  "No sandbox configured" therefore *removes* execution; it does **not** downgrade
  to running code in the worker.
- **The ACA sandbox cannot read the worker's secrets either.** Session pools are
  independent Azure resources that do **not** inherit the Function app's App
  Settings, so an `http_call` auth secret living in a worker closure is not visible
  to sandbox code. (The sandbox's own managed-identity token is host-locked to
  `*.dynamicsessions.io`.)
- **The one in-worker author-code path is `tools/*.py`.** Discovery loads project
  tool modules via `spec.loader.exec_module` (`discovery/tools.py`) **once at
  startup**, cached thereafter. That code runs in the worker with full `os.environ`
  access — but it is **author / deploy-time** code, not model-authored runtime
  code. Whoever writes `tools/*.py` is whoever deploys the app and already holds
  every app secret; reading an `http_call` secret there is not a privilege
  escalation, it is the definition of owning the deployment (the **single
  trust-domain assumption**, above). The model **cannot add or modify `tools/` at
  runtime** (discovery is startup-only and cached; the sandbox filesystem is
  isolated from the worker's `tools/` folder), so it can only *call* human-vetted
  tools, never author new in-worker code.

**Standing invariant.** The runtime must **never `exec` model-supplied output in
the worker process.** Model code has exactly one home — the isolated ACA sandbox.

**Author guideline.** If you need to run LLM-generated code or shell commands,
enable the **ACA sandbox** (isolated; cannot see App Settings). **Do not** author
an in-worker `tools/*.py` that executes model-supplied code/commands: that
re-creates the sandbox *without* its isolation and hands the model an
`echo $SECRET` primitive that can exfiltrate **every** app secret (not just
`http_call`'s). This footgun is **outside `http_call`'s threat model** — no
in-worker storage trick can hide a secret from hostile code in the same process —
and the escalation path if that assumption ever weakens is out-of-process custody
(see *Alternatives considered*).

### Residual threat: reflection / confused-deputy exfiltration

Because the model never receives the secret, and the secret is attached only
**after** SSRF validation and never carried across a redirect to another host, the
runtime prevents the model from *reading* the credential directly or *redirecting*
it to an unscoped host. The **sharpest remaining residual** is a confused-deputy
**reflection**: the model controls the *destination within the allowlist* **and**
*reads the response*, so a credentialed request aimed at an echo/debug endpoint on
an allow-listed host (`/echo`, `/headers`, `/anything`, a logging sink) can bounce
the injected `Authorization` header back in the response body → into model context
→ out. **Untrusted response bodies amplify this**: an injected instruction inside a
third-party body can *drive* the offending call (prompt-injection → reflection →
exfiltration chain).

This residual is **inherent to any authenticated tool** whose
destination-within-allowlist and response the model controls; it is **not** closed
by *where* the secret is stored (see the egress-proxy analysis below — moving
custody out of process does not fix it, and in one respect makes it worse).
Mitigation ladder, in order of leverage:

1. **Least-privilege, short-lived credentials** (managed identity / Key Vault refs,
   #1037) so a leaked token is low-value and self-expiring. **Highest-value
   mitigation.** Static, long-lived bearer tokens are the real danger.
2. **Path/method-level allowlist** (v2, "Tier 1" below) so operators can block known
   reflection endpoints — finer-grained than v1's host-only allowlist.
3. **Response redaction** (v1, best-effort): known configured secret *values* are
   stripped from result headers/body/`url`/errors and span attributes. Catches
   **verbatim** reflection only; an adversarial upstream can base64/hex/split the
   value to defeat it.
4. **Response DLP** (future): scan responses for secret-shaped tokens before they
   reach the model.
5. **Governance:** do not grant credentialed `http_call` to less-trusted agent
   authors — the v2 `allow_agent_override: false` ceiling (**A6**) and the
   single-trust-domain assumption exist for exactly this.

### Alternatives considered: out-of-process secret custody

The v1 design holds the auth secret in the **worker process** (a closure-local
structure, dereferenced at tool-build). This is safe **under the single
trust-domain assumption** but, by construction, cannot defend against *malicious
in-worker code* (a hostile `tools/*.py`). Two out-of-process alternatives were
evaluated; both are **deferred or rejected** for v1.

**(a) ACA Dynamic Sessions egress proxy — deferred to a v2 opt-in tier.** ACA
sandboxes ship an [egress-policy engine](https://learn.microsoft.com/en-us/azure/container-apps/sandboxes-egress-policies)
(preview): a `default` Allow/Deny action, rules matching host/path/method, and a
`Transform` action that **injects credentials from a secret store / managed
identity at the network layer** — so sandbox code never holds the credential. This
is genuine out-of-process custody, and it destination-binds the credential in
infrastructure rather than in our hand-rolled validator.

| Property | v1 in-worker `FunctionTool` (this FRD) | ACA egress-proxy `Transform` |
| --- | --- | --- |
| **Secret custody** | In the worker process (readable by hostile in-worker `tools/*.py`) | **Out-of-process** — proxy / secret store; never in the process or model context |
| **Cross-host leak** | Blocked by our in-process SSRF validator | Blocked at the network layer; credential **destination-bound** by infra |
| **Reflection off an allow-listed host** | Open — **but** the runtime can redact the known secret value from the response | Open — **and** the proxy does not scrub response bodies, so a reflected secret returns **unredacted** (the runtime never holds it to redact) |
| **SSRF enforcement** | Hand-rolled validator (large surface to get right) | Managed, `default-deny`, auditable; host + path + method |
| **Sandbox dependency** | **None** — works with zero sandbox config | **Mandatory** — the proxy only governs traffic **originating inside a session** |
| **Latency / cost** | Direct worker `aiohttp` call | Sandbox round-trip per call |
| **Maturity** | Stable | **Preview** |

Net: the egress proxy is a strictly better *custody* model and *cross-host* guard,
but it (i) makes the sandbox **mandatory** for credentialed calls — directly
contradicting the "no sandbox required" motivation (§2); (ii) is **preview**;
(iii) adds latency/cost; and, decisively, (iv) **does not solve the reflection
residual** — and on *that one axis* is marginally **worse**, because moving custody
out of the worker removes the runtime's ability to redact a reflected secret it no
longer holds. It is therefore recorded as an **opt-in v2 "Tier 2" custody backend**,
not a v1 replacement. **`http_call`'s v1 design is deliberately independent of this
feature:** it is a plain Functions `FunctionTool` that needs no sandbox, so the
egress proxy neither blocks v1 nor is required by it.

**(b) Re-hosting the runtime's compute on ACA Sandboxes — rejected.** "Move our
compute onto Sandboxes so the egress proxy governs *all* traffic" is a **category
mismatch**: Dynamic Sessions are an ephemeral *code-execution service you call
into*, not an app-hosting compute — there is no ingress / trigger / long-lived
process model there to host a `FunctionApp`. More broadly, the compute host (Azure
Functions vs. ACA Container Apps vs. anything else) **defines what this project
is** and is a **product-level decision outside this FRD's scope**. The achievable
form of "brokered egress" *without leaving Functions* is an **out-of-process egress
broker** — Azure Firewall / APIM-as-egress / a forward-proxy sidecar in the
worker's outbound path — that injects credentials and enforces the allowlist. That
is the real Tier 2 vehicle and does **not** require re-platforming. (Note:
re-hosting still would **not** fix reflection.)

**Tiered custody roadmap** — the throughline of the above:

| Tier | Where the secret lives | Adds defense against | Cost |
| --- | --- | --- | --- |
| **0 (v1, this FRD)** | Worker closure | Model reading the secret; cross-host redirect; **verbatim** reflection (redaction) | None — no sandbox, direct calls |
| **1 (v2)** | Worker closure | + known reflection endpoints (path/method allowlist) | Small — still in-worker |
| **2 (future, opt-in)** | Out-of-process broker / egress proxy | + hostile **in-worker** code (secret never in the process) | Sandbox or egress-broker dependency; **still not reflection-proof** |

v1 deliberately targets **Tier 0** under the single trust-domain assumption; Tiers
1–2 are the escalation path if that assumption weakens. **The choice of custody tier
is orthogonal to the compute host** and to the ACA egress feature — `http_call`
remains a plain Functions `FunctionTool` regardless.

### Compatibility

- **Purely additive** and **default off.** No existing behavior changes; the
  sandbox is untouched. An app with no `system_tools.http_call` block behaves
  exactly as before.
- **No new hard dependency** — `aiohttp` is already a runtime dependency (used by
  the sandbox).
- The three per-agent shapes (`false` / inherit / object) match the established
  `bool | Filter | None` override idiom used for `mcp` / `skills` / `tools`.
- The auth-profile model is forward-compatible with #1037: today only `headers`
  is supported; `managed_identity` / `key_vault_ref` can be added per profile
  later without breaking the shape.

## 5. Decisions log

> Ported from the pre-plan design discussion (`files/1176-design-questions.md`).
> Append-only.

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

## 6. Test plan

- [ ] Unit: `tests/test_config_merge.py` — `_resolve_http_call`: `False` opt-out;
  `True`/absent inherits global; override object replaces specified fields and
  falls back to global for the rest; `auth` full-replace (A5); caps fallback.
- [ ] Fixture scenario: `tests/fixtures/config_scenarios/<nn_http_call>/` —
  global-only enablement, per-agent opt-out, per-agent object replacement,
  per-agent auth override.
- [ ] Unit: `tests/test_config_schema.py` (or loader test) — **config** models
  parse; `extra="forbid"` rejects unknown keys; the `{env: VAR}` secret-reference
  parses as a typed ref (not a plain string). Note: `method` Literal validation and
  `body`/`json` mutual exclusion are **tool-parameter** schema, so they live in
  `tests/test_http_call.py`, not the config-schema test.
- [ ] **Secret-reference / no-leak** (`tests/test_config_loader.py` +
  `test_http_call.py`): a `{env: VAR}` auth value **passes through** the loader's
  eager env-substitution untouched and is **never** present on the resolved
  `GlobalConfig` / `AgentSpec` / `ResolvedAgent`; it is dereferenced only at
  tool-build; the resolved secret value never appears in the tool result
  (`response_headers`/`body`/`url`/error), in span attributes, or in logs.
- [ ] Unit: `tests/test_http_call.py` (new) — tool-param schema validation
  (`method` Literal; `body`/`json` mutual exclusion); JSON-vs-text parse; **binary →
  `body: null` + `body_omitted_reason: "binary"` + `response_bytes`**; **HEAD →
  `body: null` + reason `"head"`**; truncation + `body_truncated` with **truncated
  JSON returned as raw text**; `response_headers` **subset** (auth/cookie/hop-by-hop
  stripped); returned `url` has query + userinfo stripped; `redirect_count` present;
  request-header denylist (expanded set); config-auth-wins precedence.
- [ ] **SSRF suite** (`tests/test_http_call.py`), using an **injectable async
  resolver** so no test touches real DNS/network:
  - IMDS / loopback / link-local / private (RFC1918) / ULA / CGNAT
    (`100.64.0.0/10`) / unspecified blocked, **with and without** an allowlist.
  - **Evasion vectors:** IPv4-mapped IPv6 (`::ffff:169.254.169.254`), non-canonical
    numeric IPs (decimal `2130706433`, octal `0177.0.0.1`, hex `0x7f000001`),
    embedded userinfo (`user@host`), trailing-dot host, IDN/punycode host.
  - DNS-rebind IP-pin: host resolves to a blocked IP → refused; multi-record host
    with one blocked IP → refused.
  - Allowlist exact + wildcard (subdomain matches at any depth, apex does **not**).
  - `https` floor + `allow_http`; non-`http(s)` scheme rejected.
- [ ] **Redirect suite:** redirect to a blocked host → refused (each hop
  re-validated); **auth is not carried across hosts** (redirect to a different host
  → no `Authorization`; redirect to a profile host → its auth attached);
  `301/302/303` → GET (body dropped), `307/308` preserve method + body;
  `max_redirects` enforced.
- [ ] Auth: per-host profile attached only **after** validation; wildcard host
  match; config auth header overrides a model-supplied header of the same name.
- [ ] Telemetry: `record_http_call` counter increments per outcome; span emitted
  with the F2 attributes; F3 + F4 redaction (no query/auth/body/secret values in
  spans or results).
- [ ] Regression: empty `GlobalConfig` dump includes the new `http_call` key path
  as expected (mirror the observability-key regression in `test_config_loader`).
- [ ] Regression: empty `GlobalConfig` dump includes the new `http_call` key path
  as expected (mirror the observability-key regression in `test_config_loader`).

## 7. Docs impact

- [ ] `docs/front-matter-spec.md` — document the `system_tools.http_call` global
  block and the per-agent `system_tools.http_call: bool | object` override
  (the spec already anticipates multiple system tools). Include the `{env: VAR}`
  secret-reference shape and a **security warning**: v1 is **public-only** (private
  ranges are always blocked), per-agent config may **widen** the global config
  (single trust domain), and **response bodies are untrusted** (a cooperating
  upstream can reflect data the runtime does not redact). Also state the
  **execution-surface** guidance from §4: the model can only run code in the ACA
  sandbox (never the worker), operators should prefer **short-lived /
  least-privilege** credentials (#1037), and authors must **never write an
  in-worker `tools/*.py` that executes model-supplied code/commands** (use the ACA
  sandbox instead).
- [ ] `docs/architecture.md` — add `system_tools/http_call.py` to the §3 module
  map, note the new `AgentCapabilities.http_call_tools` field + `http_call_tools=`
  runner channel, and describe the second system tool in the pipeline description.
- [ ] `README.md` — add an `http_call` example under the system-tools / quickstart
  section (enable + a minimal agent call), with the same public-only + untrusted-
  body caveats.
- [ ] `docs/triggers.md` — no change (not a trigger).

## 8. Status & sign-off

- **Status:** `In review`. **Ready for human sign-off.** Decisions **A6, B4, B5,
  C8–C12, D5, F4, G3–G5** were made by the Agent while the human reviewer was
  unavailable (recorded in §5 as *Agent (arch review)*) and are **pending human
  confirmation**; the FRD is **not** flipped to `Finalized` and **no product code
  is written** until sign-off (AGENTS.md §1, phase 2).

- **Architecture review (phase 2): complete — two independent reviews.**
  - **gpt-5.4** (rubber-duck): verdict *Has blockers* — 3 blockers, 3 refinements,
    2 nits.
  - **gpt-5.5** (rubber-duck, high effort): verdict *Has blockers* — 5 blockers,
    7 refinements, 4 nits; a strict superset of gpt-5.4 plus the internal-LOB/floor
    conflict (B4), the wiring channel (G3), deeper SSRF evasion enumeration (C9),
    and the eager-env-substitution root cause with `file:line` citations (D5).
  - **All findings incorporated** into this revision (§4 security subsections, §5
    rows A6/B4/B5/C8–C12/D5/F4/G3–G5, §6 expanded suite, §7 security warning).
    Both reviews split only on the trust model; resolved per **A6** (keep the A2/A4
    widening; document the boundary; defer a ceiling mode to v2).

- **Follow-up security discussion (phase 2, 2026-07-09): complete.** A human-led
  deep-dive on secret custody and code-execution surfaces, folded into §4 (new
  *Execution surfaces & the in-worker-code boundary*, *Residual threat: reflection /
  confused-deputy exfiltration*, and *Alternatives considered: out-of-process secret
  custody* subsections) and §5 rows **I1–I4**. Conclusions: (1) the model never runs
  code in the worker and has no in-worker fallback, so an injected secret is safe
  from model-authored code; (2) the **reflection** residual is named with a
  mitigation ladder; (3) the ACA **egress proxy** is real out-of-process custody but
  is **deferred to a v2 opt-in tier** (mandates the sandbox, preview, doesn't fix
  reflection, removes the redaction backstop); (4) **re-hosting compute on ACA
  Sandboxes is rejected** (category mismatch + product-level). **This does not change
  v1 scope** — `http_call` stays a plain Functions `FunctionTool`. Rows I1–I4 were
  decided in the human-led discussion; the sign-off gate is unchanged.

- **Prior open items — now resolved (were §"Open implementation details"):**
  - *`aiohttp` session lifecycle* → **G4**: build the closure synchronously at
    registration; create connector/session lazily on first call.
  - *IP-pinning mechanics* → **C3 + C9**: custom resolver/connector pins to the
    validated IP(s), preserving `Host` + SNI, DNS cache disabled.
  - *Where env-substitution runs* → **D5**: typed `{env: VAR}` ref, dereferenced
    only at tool-build, never on `ResolvedAgent`.
  - *Interaction with `tools: false`* → **G5**: `http_call` follows the same
    `tools_disabled` kill-switch as the sandbox.

- **Human sign-off:** pending → on sign-off, set `status: Finalized`, confirm (or
  revise) the Agent-decided rows, then proceed to phase 3 (implementation).

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
