---
frd: 0016
title: Default smart retry advisor
status: Finalized
author: hallvictoria
created: 2026-10-22
updated: 2026-10-22
---

# FRD 0016 — Default smart retry advisor

## 1. Summary

Make `smart_retry(agent_name=None)` use a packaged runtime advisor. Applications
need no advisor markdown file. An explicit agent name selects the customer's
catalog entry and authored guidance instead of the packaged guidance.

## 2. Motivation / problem

Requiring an otherwise unused advisor file adds setup to the default recovery
experience. Default behavior should work with ordinary provider/model settings,
while customers can still supply domain-specific advisor instructions.

## 3. Goals / Non-goals

**Goals:** optional advisor name and file; shipped markdown instructions;
inherited application model settings; actual use of custom advisor instructions;
normal/Durable app support; unchanged retry and audit capability boundaries.

**Non-goals:** new public configuration fields, an environment variable for advisor
selection, per-handler harness selection, automatic fallback for misspelled named
agents, agent registration endpoints, or broader execution capabilities.

## 4. Proposed design

| Stage | Modules | Change |
| --- | --- | --- |
| translate / compose | Packaged advisor helper, `app.py` | Parse trusted bundled markdown and use existing config composition with app global defaults |
| register | `_hosted_skill_app.py` | Optional keyword-only `agent_name: str | None = None`; select default or explicit entry |
| execute | `hosted_skill_binding.py` | Include selected advisor guidance alongside runtime recovery/audit instructions |
| packaging | `pyproject.toml`, bundled markdown | Ship the default advisor in wheels and source distributions |

### Default advisor

Package a copy of the current queue advisor markdown body unchanged. Rename its
frontmatter to `name: Smart Retry Advisor` and
`description: Uses project source to recover a failed function handler or explain human remediation`.
The packaged markdown is the default guidance source of truth, not an app-root
file or a generated customer file. No default advisor file is required in the
customer's project and no files are written at startup.

Compose its private resolved entry with the existing global model/config merge:
app global model and agent configuration inherit normally, then existing
environment/provider defaults apply. Keep `timeout=60` on the decorator as the
recovery deadline. Bind the same app harness. Do not infer defaults from an
arbitrary discovered agent.

Keep the private default entry outside the authored catalog and trigger/endpoint
registration. It must not make an app Durable, appear as a discovered agent,
become a delegation target or grant discovered tools. Use stable internal identity
`_smart_retry_advisor` for history. Reject an authored agent with that exact slug
at composition to prevent history identity collisions. Both app classes hold
their own immutable default configuration snapshot. Provider qualification occurs
only when the default is selected by a decorator; no model/client acquisition
is added at startup.

### Optional name and custom behavior

`@app.smart_retry()` and `@app.smart_retry(agent_name=None)` select the built-in
advisor. Explicit nonempty string names keep existing catalog lookup and
validation. Empty strings, wrong types and unknown names remain errors, with no
silent default fallback. `hosted_skill` still requires `agent_name`.

Explicit custom advisors contribute their own resolved markdown instructions,
model and agent configuration; do not append packaged guidance to custom
guidance. The selected guidance supplements runtime-owned recovery or audit
policy; it cannot enable tools, alter budgets or change failure semantics.
Audit-only still exposes source tools only, never retry execution, even when
selected markdown describes a retry workflow. No authored tools, MCP servers,
skills, sandbox, web access or response schema are inherited.

The current recovery runner discards authored instructions. Add a regression
test first and correct this so an explicit name actually customizes guidance.
Do not mutate authored or default resolved entries while selecting a mode.

### Sample and compatibility

Preserve the user's current two queue handlers, including their audit-only
import-failure example, logging and bindings. Remove `agent_name` from both
smart-retry usages and remove the now-redundant sample queue advisor file; its
body is shipped in the runtime. Keep the separate error-advisor example as an
optional authored agent. Update sample tests to the current queue handlers rather
than restoring the previously removed HTTP sample.

All existing retry defaults, audit logging/failure behavior, supported trigger
inputs, cancellation and data-sharing limits remain unchanged.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Default authoring | Required advisor file / packaged advisor | Optional `agent_name` and no customer advisor file required | Human | 2026-10-22 |
| 2 | Default guidance | New instructions / reuse current queue advisor | Preserve body; rename name and description for a generic advisor | Human | 2026-10-22 |
| 3 | Custom override | Model-only selection / selected authored guidance | Explicit name uses the customer's guidance and configuration within runtime limits | Human | 2026-10-22 |

## 6. Test plan

- Regression: explicit custom markdown reaches recovery runner; no accidental
  default guidance appended; audit mode remains source-only.
- Default API and strict invalid names; both app classes and factory entry points
  with zero authored agents; original success starts no model.
- App global model/config inheritance, environment fallback, per-app isolation,
  reserved identity and no catalog/endpoint/discovery pollution.
- Retry correction, actual `None` retention, budgets and audit advice with no
  custom advisor. Preserve existing custom and ordinary injection coverage.
- Default instructions exist in an installed wheel; load from outside checkout
  with no app-root markdown dependency. Source distribution includes resource.
- Sample tests reflect current queue handlers, including import-failure audit;
  no hard-coded input corrections.
- Ruff, strict mypy, full pytest gate, diagnostics, whitespace and independent
  test review. Real-model default-advisor retry and audit smoke tests in isolated
  storage; no need to delete customer files/settings for validation.

## 7. Docs impact

Update architecture, API guide, landing/readme/onboarding examples and sample
instructions to show `smart_retry()` by default and custom `agent_name` opt-in.
No schema changes or generated reference edits.

## 8. Status & sign-off

- Architecture review: independent review confirmed composition, resource loading,
  constructor threading and identity isolation are feasible. Custom instruction
  forwarding requires the regression test and runner change described above;
  mandatory runtime/audit policy remains in force alongside selected guidance.
- Human sign-off: hallvictoria approved the default advisor, custom guidance,
  and current queue sample migration on 2026-10-22.