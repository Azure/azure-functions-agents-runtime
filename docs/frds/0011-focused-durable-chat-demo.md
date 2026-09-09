---
frd: 0011
title: Focused durable Chat UI demo
status: Finalized
author: larohra
created: 2026-09-08
updated: 2026-09-09
issues: []
pull_requests: []
branch: larohra/durable-loop-leadership-demo
---

# FRD 0011 — Focused durable Chat UI demo

## 1. Summary

Replace the broad Durable Agent Loop leadership demo with a focused private
experience that makes two claims visually undeniable: local tools execute in a
customer-owned ACA Sandbox, and a retained session resumes the same stopped
sandbox and workspace. A demo-specific Chat UI drives the real durable HTTP
surface while the authenticated DTS dashboard runs beside it as the
authoritative orchestration timeline. ACA portal supplies the lifecycle,
terminal/files, and Network Audit evidence DTS cannot show. A private
qualification path also makes the first model attempt receive a real APIM 429
before succeeding on retry.

## 2. Motivation / problem

The first leadership video demonstrated many real Azure surfaces but made the
main execution story difficult to follow. A viewer could not clearly see a
chat turn become explicit DTS model/tool activities, could not directly prove
that a tool ran inside ACA, and could not visually correlate a resumed session
with the same sandbox and workspace.

The current runtime has the required functional pieces, but its private
durable endpoints do not provide a chat experience and DTS cannot be embedded:
`dashboard.durabletask.io` returns `X-Frame-Options: DENY` and CSP
`frame-ancestors 'none'`. ACA Log Stream is also insufficient as the primary
proof because the runtime intentionally captures or redirects tool and
executor stdout/stderr. The portal's lifecycle, terminal/files, and Network
Audit surfaces remain useful independent evidence.

## 3. Goals / Non-goals

**Goals**

- Provide a private demo-specific Chat UI for durable start, status polling,
  result retrieval, session switching, and inline human input.
- Keep the Function key outside browser source and storage through a
  loopback-only proxy.
- Show the Chat UI and real DTS Timeline side-by-side, synchronized to the
  same run.
- Make local sandbox tool and remote MCP activity boundaries legible in DTS
  without opening activity payloads.
- Prove a retained session with a deterministic workspace file, a new session
  with a different sandbox, and a later resume of the original stopped sandbox.
- Use ACA portal terminal/files and Network Audit to prove sandbox-local
  execution and an allowed outbound request.
- Prove the complementary boundary: a Microsoft Learn remote MCP call traverses
  the dedicated APIM MCP API and does not create or resume an ACA sandbox.
- Make the fixed model-429 scenario traverse APIM and return a real first
  429, then succeed on a bounded retry.
- Produce a focused narrated demo and concise leadership closing slides.

**Non-goals**

- A public Durable Chat UI or new front-matter authoring contract.
- Embedding or cloning the DTS dashboard.
- Exposing prompts, tool arguments/results, content references, credentials,
  tokens, connection strings, provider response IDs, or full Durable payloads.
- Treating ACA Log Stream or the sampled Processes panel as an attestation
  mechanism.
- Exactly-once side effects, production support, an SLA, or broad runtime
  hardening.
- Replacing the existing built-in Chat UI or ordinary chat/streaming paths.

## 4. Proposed design

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| discover / project inputs | `samples/durable-agent-loop-spike/src/sandbox_bundle/tools/`, `durable-loop-tools.json` | Add `prepare_demo_workspace` as local `idempotent_write` and `read_demo_workspace` as local `read_only`; neither is parallel-safe. |
| translate | None | Preserve existing private environment gates; no public schema or front-matter change. |
| register | `experimental/durable_loop_registration.py`, `experimental/durable_loop_http.py` | Keep v1 unchanged; add v2 orchestration plus sandbox/MCP/generic activity registrations and an owner-authorized safe sandbox projection route. |
| execute | `experimental/durable_loop_activities.py`, `experimental/durable_loop_apim.py`, `experimental/hybrid_apim.py`, `experimental/hybrid_tools.py`, `experimental/durable_loop_sandbox.py`, `experimental/durable_loop_tools.py` | Add per-call fault headers/error adaptation and a typed read-only retained-sandbox inspection port. |
| infrastructure | sample `apim.bicep`, `function-app.bicep`, `main.bicep` | Add an APIM-specific private fault gate and one exact 429 policy path; retain zero body capture. |
| demo harness | sample `demo/` | Add the hardened loopback proxy, Durable Chat UI, split-window automation, portal masking, and focused recording assets. |

### 4.1 Demo-specific Chat UI

The sample adds a private UI derived from the visual and accessibility
patterns in `public/index.html`. It uses the durable endpoints rather than
`chatstream`:

- start a retained or per-call run;
- poll content-free status;
- retrieve the final result;
- fetch and answer pending human input;
- create a new session or resume a recent session.

A loopback-only Python proxy holds the Function key in process environment.
The page shows a compact run strip with safe aliases and counters, not a second
orchestration timeline. The strip includes an **Open/focus DTS** action and a
clearly labeled private **AI Gateway 429 once** control.

### 4.2 DTS companion

The Azure-hosted dashboard remains a separate authenticated browser window.
Recording automation places Chat UI and DTS side-by-side and navigates DTS to
the returned run ID with auto-refresh enabled.

The runtime preserves `durable_agent_turn_orchestrator_v1` and
`durable_agent_turn_orchestrator_v2` for every existing history. Ordinary new
runs use v2, which selects an activity name only from the already-persisted
`ToolDispatchRefV1.provenance`. Only new fixed `model_apim_429_once` demo runs
use `durable_agent_turn_orchestrator_v3`; it preserves v2 tool decisions and
schedules the failed first model activity, a one-second durable timer, and a
second model activity as three explicit orchestration decisions.
The runtime registers:

- `durable_agent_sandbox_tool_v1` for local tools; and
- `durable_agent_mcp_tool_v1` for remote MCP tools.

Both and the existing generic activity reuse one typed implementation. Existing
activity payloads remain refs-only. DTS therefore shows the trust boundary
without opening prompt or tool content. The recording helper opens the task-hub
dashboard, finds the exact returned instance ID, opens it, selects Timeline,
and enables auto-refresh; it does not depend on an unverified run deep link.

### 4.3 Deterministic sandbox proof

The sample adds private local tools:

- `prepare_demo_workspace`: idempotently creates `demo-context.txt`, performs
  one allowed `GET` to `www.example.com`, and keeps a named child process alive
  for a bounded observation window.
- `read_demo_workspace`: reads the exact prior file and returns deterministic
  bounded evidence.

During the tool step, ACA portal shows the sandbox Running. Its terminal runs a
pre-authored read-only process inspection command; Files shows the workspace
artifact; Network Audit shows the allowed host request. Log Stream is
secondary because runtime output is intentionally captured.

The demo explicitly stops the original sandbox to compress the idle
transition, labels that action as demo acceleration, creates a second session
and sandbox, then resumes the original session. The same sandbox-instance
alias and restored file prove continuity.

### 4.4 Remote MCP contrast

A separate Chat UI turn explicitly requests Microsoft Learn documentation.
The frozen catalog classifies the selected tool as `remote`; v2 schedules
`durable_agent_mcp_tool_v1`; the worker calls the dedicated APIM MCP API; and
ACA inventory remains unchanged. The recording correlates the Chat UI turn,
DTS MCP activity, APIM dependency, and unchanged sandbox count.

### 4.5 Safe session/sandbox correlation

The demo uses two different non-secret aliases:

- **session alias:** a proxy-owned random label mapped to the raw durable
  session ID only in process memory; even a caller-selected low-entropy ID is
  not dictionary-reversible;
- **sandbox-instance alias:** a deterministic short hash of the provider UUID,
  which is high entropy and stable across Function workers only while that
  physical sandbox exists.

The browser receives only aliases and opaque local handles. Raw run/session IDs
stay in proxy memory; provider sandbox IDs stay in the Function execution
layer. `durable_loop_tools.py` owns a typed read-only retained-sandbox
inspection port. `durable_loop_sandbox.py` implements it by validating the
session/run fence and expected manifest before reading the exact provider
summary. `durable_loop_http.py` owns
`GET /api/experimental/durable-agent-runs/{run_id}/sandbox`, authorizes the
owner through the same run-status boundary, obtains the session only from the
persisted durable input, and projects only sandbox-instance alias, generation,
state, and workspace-checkpoint presence.

The route fails closed for non-retained runs, missing/stale receipts, run or
session fence mismatch, corrupt manifests, deleted sandboxes, and provider
inspection errors. It never accepts a caller-supplied session ID.

Because the recording is internal, portal footage may show the operator's
name, tenant/subscription context, resource names, and the provider sandbox ID
where it directly proves instance reuse. The edited narrative also uses a
short sandbox-instance alias for readability. Owner/app hashes, operation
labels, credentials, tokens, connection strings, raw prompts/tool content,
content refs, and provider response IDs remain excluded.

The demo claims **same sandbox resumed** only when the sandbox-instance alias
is unchanged. If the instance alias changes while the file survives, the UI
labels the event **workspace restored into replacement sandbox**.

### 4.6 Real APIM 429 recovery

For this private scenario,
`MafOneStepModelProvider` accepts per-call client kwargs and attempt one adds an
exact bounded fault header through the real model request. It never mutates
shared manager/client default headers. V3 selects the foreground model path and
makes the exact injected 429 escape before the provider retry loop. The
recovery activity and all V1/V2 calls retain their existing bounded provider
retry behavior.

The APIM model policy accepts the header only when an APIM-specific Boolean
named value is enabled, only on the `responses-create` operation, and only when
the exact constant value is present. It returns an OpenAI-compatible 429 with a
bounded `Retry-After`, removes the fault header, and never forwards that
attempt. Attempt two omits the header and reaches the model backend.

Both attempts emit a bounded `x-af-operation-id` derived from run plus model
step, not a raw identifier. APIM validates its shape before diagnostics. The
fault header is not in the diagnostic allowlist. The pinned MAF/OpenAI
exception chain is normalized to `ApimResponsesError` with only the HTTP status
and bounded `Retry-After`; no provider body is surfaced. The first V3 model
activity therefore fails visibly in DTS. After the fixed one-second durable
timer, the orchestrator schedules a second model activity; the one-shot receipt
has already been consumed, so that activity reaches the backend. APIM and
Application Insights show exactly one 429 then one success with the same
operation identifier. Arbitrary fault values remain rejected.

### 4.7 Loopback proxy security

The proxy is a security boundary, not a default:

- it refuses every non-loopback bind address;
- validates exact loopback `Host`;
- requires same-origin `Origin` plus a per-process CSRF token for POSTs;
- exposes only fixed methods, route templates, scenarios, field allowlists,
  and request/response size limits;
- never follows an upstream redirect while carrying the Function key;
- returns `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, and CSP
  including `connect-src 'self'` and `frame-ancestors 'none'`;
- keeps raw run/session/sandbox mappings in process memory and gives the
  browser only opaque local handles and safe aliases.

### Authoring / API surface

No public authoring surface changes. All additions are private sample files,
private experimental activity names, and the existing fixed fault profile.
The demo proxy is machine-local and not deployed as a customer endpoint.

### Compatibility

Ordinary agents, the shared built-in Chat UI, existing durable HTTP contracts,
v1/v2 durable histories, and Azure Storage rollback settings remain unchanged.
The old orchestrators and generic/provenance-specific tool activities remain
registered and deterministic. Ordinary new private runs use v2; only new fixed
model-429 demo runs use v3. The real APIM fault behavior is active only when
both the Function and APIM private gates are enabled.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Primary demo surface | Broad control room / Shared Chat UI / Demo-specific Chat UI | Demo-specific Chat UI; it preserves the familiar interaction without adding a public runtime surface. | Human | 2026-09-08 |
| 2 | Orchestration visualization | Custom activity rail / Embedded DTS / Separate DTS window | Separate live DTS window; the dashboard is authoritative and blocks iframe embedding. | Human | 2026-09-08 |
| 3 | Sandbox evidence | Live Logs only / Custom inspector / ACA lifecycle, terminal, files, Network Audit | ACA native surfaces; Log Stream is intentionally quiet and Network Audit was proven live. | Human | 2026-09-08 |
| 4 | Recovery scenario | Sandbox loss / Tool acknowledgement loss / Model 429 | Real AI Gateway 429 followed by bounded retry; it is simplest for leadership to interpret. | Human | 2026-09-08 |
| 5 | DTS tool naming | Generic tool activity / Per-tool activity / Provenance-specific activities | Provenance-specific sandbox/MCP names; they expose the trust boundary without payload content or function explosion. | Agent | 2026-09-08 |
| 6 | Stopped-state timing | Wait for auto-suspend / Lower runtime policy / Explicit portal stop | Explicit portal stop with a visible demo-acceleration caption; predictable and does not change product lifecycle defaults. | Agent | 2026-09-08 |
| 7 | Secret handling | Key in browser / Deployed anonymous UI / Loopback proxy | Loopback proxy; the browser never receives or persists the Function key. | Agent | 2026-09-08 |
| 8 | Orchestration migration | Change v1 / Version orchestrator / Defer activity names | Add v2 for new runs and preserve v1 histories; changing a replay decision in place is unsafe. | Agent | 2026-09-08 |
| 9 | Correlation proof | Session alias only / Sandbox alias only / Separate session and instance aliases | Separate aliases; only an unchanged instance alias proves the same physical sandbox resumed. | Agent | 2026-09-08 |
| 10 | Gateway fault transport | Shared header mutation / Synthetic pre-transport error / Attempt-local header through APIM | Attempt-local header through APIM; it proves a real gateway 429 without contaminating concurrent calls. | Agent | 2026-09-08 |
| 11 | Browser identifier storage | Raw IDs in localStorage / Raw IDs in memory / Proxy-owned opaque handles | Proxy-owned mappings and opaque browser handles; raw durable/provider IDs stay server-side. | Agent | 2026-09-08 |
| 12 | Safe session alias | Plain hash / Secret-keyed alias / Random proxy mapping | Random process-lifetime alias; caller-selected low-entropy session IDs cannot be guessed from UI output. | Agent | 2026-09-08 |
| 13 | Sandbox inspection boundary | HTTP reads receipts / Proxy infers inventory / Typed execution port | Owner-authorized HTTP route calls a typed inspection port; private receipt/provider identities never cross layers. | Agent | 2026-09-08 |
| 14 | Internal portal context | Blanket masking / Show normal context / Show credentials | Show operator, tenant/subscription, resource names, and useful sandbox ID; continue excluding secrets and sensitive payloads. | Human | 2026-09-08 |
| 15 | Architecture approval | Continue design / Implement revised design | Implement; replay, alias, APIM, and proxy refinements preserve the approved Chat UI + DTS experience while closing correctness gaps. | Human | 2026-09-08 |
| 16 | Remote MCP proof | Mention in slides / Separate live turn / Combine with local tool | Separate live turn; DTS and APIM show MCP while ACA inventory remains unchanged. | Human | 2026-09-08 |
| 17 | Recovery activity boundary | Provider retry / Durable retry action / Explicit V3 activities | Explicit V3 activities with a durable timer; DTS must show a real failed model activity followed by a successful model activity while V1/V2 replay stays unchanged. | Human | 2026-09-09 |

## 6. Test plan

- [ ] Unit: demo proxy validates bounded scenarios, never returns secrets, and
  supports start/status/result/HITL/session resume.
- [ ] Unit: new sandbox tools produce deterministic workspace and egress
  evidence and reject unbounded input.
- [ ] Unit: local and remote calls schedule the correct DTS activity names.
- [ ] Replay: v1 history continues to schedule the generic activity after the
  v2 deployment.
- [ ] Unit: stable session aliases do not disclose raw session or sandbox IDs.
- [ ] Unit: same-session/same-instance, same-session/recreated-instance, and
  different-session/different-instance are projected accurately.
- [ ] Unit: sandbox inspection rejects cross-owner access, forged run/session
  relationships, non-retained runs, stale/missing/corrupt manifests, deleted
  sandboxes, and provider failures.
- [ ] Unit: low-entropy session IDs receive unrelated random aliases; raw IDs
  never enter browser storage; sandbox-instance aliases remain stable across
  Function worker reconstruction.
- [ ] Unit: malicious Host/Origin, missing CSRF, non-loopback bind, arbitrary
  routes, redirects, and oversized bodies fail closed.
- [ ] Unit: the model-429 fault header appears only on attempt one, concurrent
  ordinary calls receive no header, a wrapped real 429 preserves bounded
  `Retry-After`, attempt two succeeds, and arbitrary values fail closed.
- [ ] Unit: only fixed model-429 admissions select v3; v3 schedules two explicit
  model activities around a durable timer and retains one operation identifier.
- [ ] APIM: disabled gate, wrong operation, wrong value, correlation regex,
  OpenAI-compatible 429 body, no forwarding, and diagnostic header allowlist.
- [ ] Regression: ordinary durable runs and existing generic activity
  registration remain compatible.
- [ ] Live: Chat UI + DTS retained create, HITL, new session, stopped original
  sandbox, original resume, exact file read, remote MCP with unchanged ACA
  inventory, and APIM 429 recovery.
- [ ] Privacy: no keys, tokens, connection strings, raw prompts/tool payloads,
  content refs, provider response IDs, or owner/app hashes in artifacts;
  ordinary internal portal identity/subscription context is permitted.
- [ ] Full `ruff`, `mypy`, and `pytest --cov` gate.

## 7. Docs impact

- [ ] `docs/architecture.md` — private durable-loop registration/activity map
  and demo evidence boundary.
- [ ] `samples/durable-agent-loop-spike/README.md` — focused demo workflow,
  loopback proxy, DTS companion, ACA evidence, and fault behavior.
- [ ] `docs/front-matter-spec.md` — no change.
- [ ] `docs/triggers.md` — no change.
- [ ] Root `README.md` — no change; feature remains a private spike.

## 8. Status & sign-off

- **Architecture review (phase 2):** Two independent passes found and this
  revision resolves replay versioning, request/error seams for real 429, APIM
  gating/correlation, separate session/instance proof, typed inspection
  ownership, low-entropy aliases, and loopback proxy security.
- **Human sign-off:** larohra, 2026-09-08 — approved the focused Chat UI + live
  DTS split-screen plan, selected real AI Gateway 429 recovery, and instructed
  autonomous implementation of the reviewed design.
