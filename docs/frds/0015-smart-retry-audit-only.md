---
frd: 0015
title: Smart retry audit-only mode
status: Finalized
author: hallvictoria
created: 2026-10-22
updated: 2026-10-22
---

# FRD 0015 — Smart retry audit-only mode

## 1. Summary

Add `audit_only: bool = False` to `app.smart_retry`. After an original handler
failure, audit mode runs source-aware analysis and logs bounded remediation
advice, without granting the agent a retry tool or changing handler inputs.

## 2. Motivation / problem

Some applications want diagnostics without model-directed input correction.
Advisory instructions alone cannot enforce this boundary. Removing the replay
capability makes audit-only behavior independent of model obedience.

## 3. Goals / Non-goals

**Goals:** opt-in advice-only execution, strict boolean validation, unchanged
defaults and normal retry behavior, no agent on original success, and no hidden
acknowledgement of failed queue work.

**Non-goals:** new trigger types, auditing successful calls, accepting
`max_retries=0` as a separate public shorthand, a new log sink or return contract,
redacting arbitrary model text, or changing the deterministic sample handlers.

## 4. Proposed design

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| discover / translate | Existing configuration | No changes |
| register | `_hosted_skill_app.py` | Add keyword-only `audit_only=False` on the common app mixin and validate it |
| execute | `_hosted_skill_recovery.py` | Effective zero retries in audit mode, source-only tools, bounded advice logging |
| execute | `_hosted_recovery_input.py`, `hosted_skill_binding.py` | Select audit-specific runtime instructions without weakening normal recovery instructions |

### Authoring contract

Use `@app.smart_retry(agent_name="queue_advisor", audit_only=True)`. No injected
parameter is introduced. Both normal and Durable enhanced apps support the option.
Require `type(audit_only) is bool`; reject integer/string/None substitutes.
Keep existing positive-integer `max_retries` validation. With `audit_only=True`,
any otherwise valid `max_retries` is ignored for execution and the effective
retry budget is zero. The default remains one retry with `audit_only=False`.

### Execution and capability boundary

Preserve trigger selection, supported-input qualification, and the original
handler call. Success returns normally with no agent and no advice log.
After an uncaught exception, audit mode performs one isolated advisor run with
only `list_files`, `read_file`, and `search_source`. Do not expose `retry_function`
or prepare replacement inputs. Audit system instructions describe a failure
analysis task, forbid replay/input mutation, and request concise source-grounded
remediation. Diagnostics include effective `max_retries=0` and `audit_only=true`.
Keep an explicit effective-zero execution guard as defense in depth.

Log the bounded advice once through the shared logger at WARNING level after
analysis, then preserve the existing adapter failure outcome: HTTP 500
remediation (HEAD bodyless) or `HostedSkillRecoveryError` for queue processing.
Audit-only must not turn failed queue work into success. Host redelivery remains
separate and each delivery may invoke another audit. Missing advice, provider
errors and deadline expiry log the static fallback and retain failure semantics.
External cancellation propagates without a remediation log. Source tools expire
and bounded cooperative cleanup remains unchanged.

The model cannot replay the handler through the audit tools, even if it ignores
instructions. Model advice remains untrusted text; logging is not a new
confidentiality guarantee. Existing reviewed-source/non-sensitive-input warnings
apply. No authored tools, MCP servers, skills, sandbox or web tools are added.

### Compatibility

The new option is additive. Existing calls, normal recovery tools, instruction
policy and outcomes stay unchanged. Keep the user's current sample handlers
unchanged; document an opt-in example instead.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Advice-only API | Zero-budget shorthand / named boolean | `audit_only: bool = False` makes the agent advisory only | Human | 2026-10-22 |
| 2 | Retry capability | Prompt prohibition / no tool | Omit retry tool and enforce an effective zero budget | Agent | 2026-10-22 |
| 3 | Failed delivery outcome | Acknowledge with advice / retain failure | Log advice and retain existing HTTP/queue failure outcome | Agent | 2026-10-22 |

## 6. Test plan

- Exact API defaults, strict boolean validation, positive retry-count validation
  and audit overriding a configured positive retry budget on both app types.
- Successful HTTP/queue handlers run once without an agent or audit log.
- Failed audit handlers run once; fake advisor uses real source tools and cannot
  obtain a retry capability. Inputs stay unchanged. Verify effective zero budget.
- Actual source-only tool set and audit instructions in the real runner call;
  normal recovery instruction/tool isolation stays unchanged.
- HTTP failure, queue typed failure, bounded logged advice and fallback for
  empty/provider/timeout outcomes. Cancellation, tool expiry and concurrent
  audit/retry invocation isolation.
- Focused tests, full Ruff/mypy/pytest gate, editor diagnostics and independent
  review. Exercise the audit path with the configured live model without
  changing the user's current sample functions.

## 7. Docs impact

Update the API guide, architecture map, primary README and remediation sample
README with the additive option, capability boundary and failure/logging contract.
Update the FRD index. No schema changes or reference generation.

## 8. Status & sign-off

- Architecture review: independent review identified the required option plumbing,
  separate audit system instructions, omitted retry capability, effective-zero
  guard and post-analysis logging. These are included in the execution contract.
- Human sign-off: hallvictoria approved the audit-only failure/logging contract,
  including positive public retry-count validation, on 2026-10-22.