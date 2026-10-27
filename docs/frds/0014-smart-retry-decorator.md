---
frd: 0014
title: Standalone smart retry decorator
status: Finalized
author: hallvictoria
created: 2026-10-22
updated: 2026-10-22
---

# FRD 0014 — Standalone smart retry decorator

## 1. Summary

Expose source-aware whole-handler recovery as `app.smart_retry()`, independent
of `app.hosted_skill()` injection. Ordinary handlers keep only their real Function
arguments. The recovery engine and its HTTP and Storage Queue contracts remain
unchanged.

## 2. Motivation / problem

Recovery currently requires an unused `HostedSkill` parameter and a public
`HostedSkillRecovery` configuration object. These imply application-owned agent
execution even though the decorator owns recovery. A standalone decorator makes
that boundary explicit.

## 3. Goals / Non-goals

**Goals**

- Provide `app.smart_retry(*, agent_name: str, max_retries: int = 1,
  timeout: float = 60, request_arg: str | None = None)` on both enhanced normal
  and Durable Function Apps, including apps created by the default factory.
- Inject no argument and expose no `arg_name` option on this decorator.
- Remove `hosted_skill(recovery=...)` and the `HostedSkillRecovery` class/export.
- Preserve injection-only `hosted_skill(arg_name=..., agent_name=...)`.
- Preserve existing input qualification, agent selection, lazy execution,
  source tools, retry budget, deadline, cancellation, result and failure semantics.

**Non-goals**

New trigger kinds, deterministic correction, altered model policies, new
frontmatter/config fields, semantic-equivalence validation, side-effect rollback,
or composing injection and recovery decorators on the same handler.

## 4. Proposed design

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| discover / translate | Existing config and discovery | No changes |
| register | `_hosted_skill_app.py` | Separate injection and recovery wrappers; reuse catalog qualification and post-index trigger selection |
| execute | `_hosted_skill_recovery.py` | Accept validated plain retry options; remove the public configuration class |
| execute | `hosted_skill_binding.py`, recovery input/source adapters | Reuse existing private recovery execution and per-input contracts |
| public API | `__init__.py`, samples | Remove the configuration export; migrate recovery handlers to `smart_retry` |

### Decorator contract

`smart_retry` is immediately above an `async def` handler, below the outer SDK
trigger/binding decorators. It preserves the authored signature and annotations;
it neither inserts nor hides a parameter. Reject synchronous handlers, SDK
FunctionBuilder objects, repeated smart-retry wrapping and combinations with the
injection decorator with actionable errors, rather than losing trigger selection.

Validate options when defining the decorator: `max_retries` is a positive integer
(not boolean); `timeout` is finite and positive (not boolean); `request_arg` is
`None` or a non-keyword Python identifier naming a concrete handler parameter.
Keep validation in one private helper; do not replace the removed public object
with another public configuration type.
The owning helper is `_validate_retry_options()` in `_hosted_skill_recovery.py`;
both the decorator and recovery engine reuse its checks.

Select `agent_name` from the existing immutable catalog and apply the same
capability/provider qualification as before, without acquiring a model client.
Implement `smart_retry` as a method of the existing `_HostedSkillAppMixin`.
Register each returned wrapper in the app-owned recovery-binding map keyed by
that wrapper; both concrete app classes keep their post-index resolution hook.
An explicit `request_arg` takes precedence. Otherwise `get_functions()` selects
the SDK trigger's argument after outer decorators attach metadata. Direct calls
without indexing or an explicit override fail clearly, just as before.
This is an invocation-time `RuntimeError` before input qualification or original
handler execution, not a decorator-time failure. An explicit override supports
direct calls without indexing.

Bind arguments once per invocation and preserve all non-selected values across
replays. Qualify the selected input with the existing HTTP/queue adapter. Run the
original handler first. A successful original call does not run an agent. Only
an uncaught handler exception starts recovery, using an internal `HostedSkill`
facade that is never passed to application code. Its source hint identifies the
original handler, not the wrapper.

The agent calls the existing `retry_function` to execute that original handler
again inside the same host invocation. Invalid tool arguments do not consume an
attempt. Sequential failed attempts share one deadline and budget. No overlap
or execution after success is allowed. The actual first normal result, including
`None`, is retained. External cancellation wins; bounded cooperative cleanup,
provider failure and timeout result retention remain unchanged.

### Input and failure contracts

Keep bodyless GET/HEAD HTTP route/query correction and native HTTP 500 remediation.
Keep bounded UTF-8 queue body replacement, explicit delivery-metadata preservation
and no queue update/settlement tools. Unresolved queue recovery still raises
`HostedSkillRecoveryError`, leaving redelivery and poison handling to the host.
Source/body sharing and model-guided correction limits remain unchanged.

### Compatibility

This intentionally replaces the existing recovery authoring surface, not a
deprecation alias. Importing `HostedSkillRecovery`, passing `recovery` to
`hosted_skill`, or passing `arg_name` to `smart_retry` must fail. Ordinary
`HostedSkill.run()`/`stream()` injection remains unchanged. Keep historical FRDs
as design history; current examples and API documentation use `smart_retry`.

Both sample functions retain their existing trigger bindings and named advisors.
Their handlers remove the unused `skill` argument. Queue processing still delegates
all deterministic logic to its separate helper and sets `max_retries=3`; HTTP uses
the defaults. No hard-coded input repair rules are added.
The sample decorators are `@app.smart_retry(agent_name="error_advisor")` and
`@app.smart_retry(agent_name="queue_advisor", max_retries=3)`. Neither supplies
`request_arg`: indexing selects `req` and `message` from their trigger metadata.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Recovery authoring | Injected HostedSkill plus config / independent decorator | Independent `smart_retry` with plain options and no injected argument | Human | 2026-10-22 |
| 2 | Compatibility | Retain aliases / remove old recovery surface | Remove `HostedSkillRecovery` and `hosted_skill(recovery=...)`; preserve injection-only API | Human | 2026-10-22 |
| 3 | Defaults | Change policy / retain existing values | `max_retries=1`, `timeout=60`, automatic trigger argument unless overridden | Human | 2026-10-22 |

## 6. Test plan

- API: exact option defaults, validation, unknown agent/provider qualification,
  removed export/keywords, invalid handlers/decorator order and repeated wrapping.
- Indexing: both app classes, factory-created apps, genuine worker-visible
  signatures, trigger-based selection, explicit overrides and direct calls.
- Execution: valid input with no agent; positional/keyword/non-selected argument
  preservation; no injected facade; fresh invocation state and source identity.
- Migrate existing wrapper tests; preserve engine, adapter, cancellation,
  budget, `None` success, timeout, concurrency and isolation coverage.
- Sample tests index both real handlers with no synthetic `skill` binding;
  source-driven correction and unfamiliar-property remediation use real tools.
- No authoring-schema interpretation changes: existing config scenarios remain
  valid; use the real sample indexing fixtures for this Python decorator change.
- Full Ruff, strict mypy, pytest with branch coverage, editor diagnostics and
  whitespace checks.
- Core Tools with Azurite and the real configured model: HTTP normal, quoted
  numeric correction and unknown-value advice; queue valid, extra brace,
  trailing comma and unfamiliar-property advice/failure. Correlate tool activity,
  message identity and host invocation IDs. Verify no model run on valid inputs.
- Keep recovery attempts distinct from host redelivery; use process-local test
  settings to bound deliveries. Clean only synthetic test-owned storage/processes.
- Independent testing review. Report observed behavior and any model-dependent
  deviations rather than claiming guaranteed inference or semantic preservation.

## 7. Docs impact

Update `docs/architecture.md`, `docs/front-matter-spec.md`, `docs/triggers.md`,
`README.md`, the shared sample index and remediation sample README. Update the
FRD index. No schema/reference regeneration is required.

## 8. Status & sign-off

- Architecture review: independent review found the API separation and engine
  reuse sound. Wrapper-map registration, unindexed-call failure timing, private
  validation ownership and automatic sample trigger selection have been clarified.
- Human sign-off: approved by hallvictoria on 2026-10-22, including the API
  replacement and separate injection/recovery decorator contracts.