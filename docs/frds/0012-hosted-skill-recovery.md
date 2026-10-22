---
frd: 0012
title: Agent-driven Hosted Skill handler recovery
status: Finalized
author: hallvictoria
created: 2026-10-20
updated: 2026-10-21
---

# FRD 0012 — Agent-driven Hosted Skill handler recovery

## 1. Summary

An opt-in Hosted Skill decorator catches an HTTP handler exception and gives its
agent read-only project source tools and an invocation-local retry tool. The
agent can correct request inputs and invoke the entire original handler up to a
configured retry limit, defaulting to one.
The wrapper returns the actual handler response within the same host invocation.

## 2. Motivation / problem

Application-level operation callbacks retry only part of a Function. Customers
should write ordinary handlers without recovery plumbing or explicit skill calls.
Agents need source access to infer corrections without manually authored input
contracts or typo tables.

## 3. Goals / Non-goals

Goals: unchanged injection-only behavior; whole-handler retries chosen by the
agent within a configurable retry limit; isolated per-invocation tools; source-aware inference; actionable failure
guidance; numeric in-process route corrections and textual query corrections.

Non-goals: HTTP self-calls, host redelivery, arbitrary code execution, file writes,
body replay, streaming, mutation of authentication or routing, automatic rollback,
or guaranteed model inference. Initial recovery supports GET/HEAD HTTP requests.

## 4. Proposed design

| Pipeline stage | Modules | Change |
| --- | --- | --- |
| discover / translate | Existing modules | No authoring/schema changes |
| register | `_hosted_skill_app.py` | Validate optional recovery configuration and bind original handler |
| execute | `hosted_skill_binding.py` | Private invocation-bound additional-tool execution; public run/stream unchanged |
| execute | New recovery, HTTP-adapter, source-reader modules | Bound retry capability, request cloning, constrained read-only source access |

### Authoring / API surface

Export `HostedSkillRecovery(request_arg=None, timeout=60.0, max_retries=1)` and accept it as
`hosted_skill(..., recovery=...)`. Supplying it explicitly asserts the handler is
safe to replay. No manual operation context or input contract is required.
The injected HostedSkill argument remains required and may be unused.

When `request_arg` is omitted, resolve the argument from the actual trigger's
binding name during function indexing, after all outer decorators have applied.
Validate it against the worker-visible handler signature. An explicit override
retains its existing name and takes precedence. Automatic selection requires
indexing; direct calls without indexing require an explicit override. Selection
does not infer from annotations, parameter order or the name `req`, and is
independent of trigger kind. Input reconstruction remains HTTP-only; unsupported
inputs fail qualification before executing the handler or recovery agent.

On an uncaught handler Exception, run the selected agent once with error type,
project-relative stack locations, handler identity and non-secret request inputs.
Do not send headers, body, environment, raw error messages or absolute paths.
Use project-root-bound list/read/search tools for Python source only, with size,
count and result limits; reject symlinks, hidden/build/environment directories,
non-source paths and traversal. Source may contain secrets: author opt-in is a
data-sharing boundary, not automatic redaction. Do not grant write/exec access.

The agent calls `retry_function` with optional route and query corrections. Route
values may be finite JSON scalars, including numbers, because this is an in-process
overlay; HTTP URLs do not preserve JSON numeric types. Route keys must already
exist. Query corrections are string pairs to preserve duplicates. Preserve all
unmodified request attributes and other handler arguments. No headers, method,
path, identity or binding changes are permitted. Starlette query corrections
change the query component of the cloned URL; Azure HttpRequest query corrections
are limited to unique keys because its SDK exposes a mapping. Query patches merge
by key and preserve unrelated values; duplicate patch keys are rejected on the
Azure adapter. No deletion is supported. Validate tool data once at its
trust boundary; do not hard-code domain correction policy.

`max_retries` must be a positive integer (not a boolean) and counts handler
replays, excluding the initial failed invocation. One agent run may make multiple
sequential correction attempts within that budget and the existing shared
deadline; there is no automatic retry loop or deadline reset. Each attempt clones
the original request, so the agent must include all corrections it still needs.
Invalid arguments and concurrent-call rejection do not consume attempts; a
handler failure does. Tool results expose only safe status/error types.

The retry tool claims an attempt before awaiting the original undecorated
handler. Concurrent/late calls, exhausted budgets and calls after the first
successful handler result are rejected. Retain the actual result
out of band; the model gets only a safe success/failure summary, not the response
body. Return the actual successful response unchanged, even if subsequent model
formatting fails. A successful handler means it returned normally, not an inferred
HTTP status. Failed retry, provider failure or no retry yields HTTP 500 JSON with
requires_user_action and remediation (static guidance on infrastructure failure).
Adapters construct Azure Functions or Starlette responses without altering the
successful customer's response. Cancellation always propagates and invalidates
the tools. Recovery deadline includes source tools and retry execution. After a
successful retry, later provider errors or timeout cannot discard the retained
result; external cancellation still propagates. Invalid tool arguments do not
consume the execution attempt. Invalidate tools before cancelling owned tool tasks;
allow up to one additional second for shutdown, without awaiting cancellation
cleanup indefinitely. Re-cancel remaining tasks, consume their eventual exceptions
and log a warning without request, source or exception data. External cancellation
during shutdown still propagates. Python coroutine cancellation is cooperative:
uncooperative provider/handler code, including cancellation cleanup, cannot be
forcibly terminated, so the deadline is not a hard execution bound and zero work
after recovery returns is not guaranteed. Invalidated tools prevent new attempts,
not continuation of an already-running handler.

### Compatibility and safety

Default decorator behavior is unchanged. Existing public run/stream APIs keep
their signatures and authored response contracts. Recovery uses its own internal
instructions and plain-text remediation, not authored chat response schemas.
Recovery transcripts retain the selected harness's normal history persistence;
fresh session identity prevents reuse but does not make source/tool data ephemeral.
Recovery runs with only the runtime source/retry tools, empty MCP/skills/sandbox/web
inventories and internal instructions, preserving model/harness selection but not
authored response schemas. Runtime tools do not mutate the catalog. They are
unavailable outside the recovery turn. Request type and GET/HEAD method are checked
before execution. Requests with bodies are rejected; author opt-in asserts no
output bindings or other non-replayable effects, which the inner decorator cannot
inspect. Source root is the invocation harness app root, never cwd. Junctions and
symlinks are rejected. Known credential-like input keys are not exposed or editable;
opt-in still requires synthetic/non-sensitive inputs and reviewed source. Guidance
is model-generated and must not be treated as a confidentiality guarantee; bound
its length and use static guidance when unavailable. HEAD failure responses omit
the body. HTTP metadata/bindings are not reauthorized or recomputed after patches.
No side effects are rolled back. Output bindings, streaming, Durable orchestrators,
and handlers with non-idempotent effects are unsupported for recovery.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Recovery owner | Application callback / decorator | Decorator catches errors; agent calls an invocation-local whole-handler retry tool | Human | 2026-10-20 |
| 2 | Intent discovery | Mandatory manual contracts / source inspection | Read-only source inspection; no required operation contract | Human | 2026-10-20 |
| 3 | Replay boundary | Unbounded retries / bounded replay | One retry, original callable, same invocation, real response retained | Human | 2026-10-20 |
| 4 | Initial HTTP scope | All bodies/methods / route and query only | GET/HEAD route/query adapters, explicit opt-in, failure JSON response | Agent | 2026-10-20 |
| 5 | Cancellation shutdown | Unbounded drain / bounded cooperative shutdown | Invalidate tools first; wait up to one additional second, re-cancel and warn for remaining tasks; external cancellation wins, but uncooperative work may outlive recovery | Human | 2026-10-21 |
| 6 | Default recovery input | Fixed request name / annotation inference / trigger metadata | Resolve the actual trigger binding name at indexing; retain explicit request_arg override and HTTP-only reconstruction | Human | 2026-10-21 |
| 7 | Correction budget | Fixed single retry / configurable bounded retries | Positive integer max_retries, default one; sequential agent-driven retries under one deadline, stopping at first success, with each patch applied to the original request | Human | 2026-10-21 |

## 6. Test plan

- Injection-only regression; opt-in config qualification and signature hiding.
- Metadata-based selection for renamed HTTP, queue and timer arguments; explicit
  override, missing visible parameter, unindexed direct calls and Durable apps.
- Both HTTP adapters: source request unchanged, route numeric overlay, query
  duplicate preservation, unrelated metadata/arguments and real response identity.
- Fake runner invokes real retry tool; count two handler executions in one wrapper
  invocation, one model turn; prevent recursive/repeated/concurrent/late calls.
- Failed retry, no retry, malformed arguments, model failure after successful
  retry, timeouts, cancellation and concurrent request isolation.
- Configurable retry budget, failure followed by corrected success, exact
  exhaustion, no execution after success, overlapping calls without budget loss,
  and shared deadline/cancellation across later attempts.
- Blocked tool cancellation cleanup: bounded shutdown, safe warning, inactive tools,
  eventual task release and external cancellation precedence over baseline counts.
- Source traversal, symlink, hidden files, non-source, oversize and bounded search.
- Sample arithmetic succeeds after correction; unfixable error gives remediation.
- Independent coverage review, ruff, mypy, full pytest; live Core Tools smoke when
  available. Model inference remains an evaluation, not a mocked-test guarantee.

## 7. Docs impact

Update architecture module map, Hosted Skill guide and sample quickstart. Update
FRD index and sample catalog. No configuration schema change is intended.

## 8. Status & sign-off

- Architecture review: independent review conditionally approved the design.
  Resolved findings by isolating the recovery tool inventory, documenting query
  adapter differences and patch semantics, retaining successful results over later
  provider failure, binding source access to app root and defining replay limits.
- Human sign-off: user approved the source-aware, tool-driven design and requested
  implementation on 2026-10-20. Detailed compatibility restrictions are agent
  design choices subject to architecture review.