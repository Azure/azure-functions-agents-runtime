---
frd: 0012
title: Copilot SDK default harness
status: In review
author: larohra
created: 2026-10-29
updated: 2026-10-29
issues:
  - https://github.com/Azure/azure-functions-bucees-planning/issues/1384
pull_requests: []
branch: copilot/fhs-make-github-copilot-default-harness
---

# FRD 0012 — Copilot SDK default harness

## 1. Summary

Select the GitHub Copilot SDK harness for every app when no harness setting is
provided. Keep MAF available through the existing app-wide setting, with no
per-agent selection or fallback after startup.

## 2. Motivation / problem

The Copilot SDK harness is currently opt-in even though the requested default
experience is the Copilot SDK harness. Every app with an unset selector should
use it; apps that need MAF can opt out explicitly.

## 3. Goals / Non-goals

**Goals**

- Select Copilot for every app when `AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT` is
  unset.
- Select MAF when the setting is explicitly `false` or `0`; preserve `true` or
  `1` as Copilot selections.
- Ensure a standard runtime installation includes the SDK required by the
  default harness.
- Keep harness selection immutable per app, fail unsupported Copilot
  configuration explicitly, and never fall back to MAF.

**Non-goals**

- Add per-agent harness selection or a public harness plug-in API.
- Change either harness's execution, session, capability, or authoring contracts.
- Remove MAF or its explicit opt-out.
- Expand the Copilot harness's existing qualification or support claims.

## 4. Proposed design

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| discover | `discovery/*` | No change; discovery remains harness-neutral. |
| translate | `config/*` | No change to authored configuration. |
| register | `app.py`, `harness/_harness_binding.py` | An unset selector binds Copilot once per app; explicit false values bind MAF. Existing qualification rejects unsupported Copilot configurations before app registration. |
| execute | `harness/copilot_sdk/*`, `harness/agent_framework/*` | No adapter changes. The selected runner continues to own execution and persistence; selection failures do not fall back. |

### Authoring / API surface

No agent front matter or config fields change. The existing
`AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT` selector remains supported: unset, true,
or 1 selects Copilot; false or 0 selects MAF; invalid values fail app
construction.

### Compatibility

Apps that previously relied on an unset selector to choose MAF must set the
selector to `false` or `0` to keep that behavior. Copilot capability and
provider requirements remain unchanged and continue to fail explicitly during
qualification. App construction without Copilot-compatible provider and model
settings can therefore fail where it previously selected MAF. MAF history is
not imported into Copilot sessions; switching harnesses does not migrate
conversation state. Previously documented Copilot qualification gaps remain
gaps; selecting Copilot by default does not establish support for unqualified
host scenarios. The Copilot SDK becomes part of the standard runtime
installation. The `copilot` extra remains as a compatibility alias.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Default and MAF opt-out | Keep MAF default / make Copilot default and retain opt-out / remove MAF | Unset selects Copilot; explicit false or 0 selects MAF; no fallback | Human (issue request) | 2026-10-29 |
| 2 | SDK availability | Keep Copilot SDK optional / include it in the standard installation | Move `github-copilot-sdk` to core dependencies and keep `copilot = []` as an install-syntax alias | Agent | 2026-10-29 |
| 3 | Qualification and session compatibility | Expand qualification and migrate MAF history / preserve existing boundaries and document migration impact | Keep Copilot qualification unchanged; make no claim of MAF-history migration or support for unqualified host scenarios | Agent | 2026-10-29 |

## 6. Test plan

- [ ] Unit: an unset selector chooses Copilot.
- [ ] Unit: false and 0 choose MAF; true and 1 choose Copilot; invalid values still fail.
- [ ] Regression: app construction and standalone binding retain once-per-app behavior and never fall back.
- [ ] Packaging: the Copilot SDK is available in the standard dependency set.
- [ ] Integration: default selection is exercised without the selector, and MAF-specific integration tests explicitly opt out.

## 7. Docs impact

- [ ] `AGENTS.md` — replace the MAF-default convention with Copilot default and explicit MAF opt-out.
- [ ] `docs/architecture.md` — update default runner and selection descriptions.
- [ ] `docs/copilot-preview-operations.md`, `docs/front-matter-spec.md`, `docs/triggers.md`, `docs/workflows.md` — document the selector and keep preview limitations explicit.
- [ ] `README.md`, `docs/index.md`, `docs/getting-started.md`, `samples/README.md`, and the Copilot sample README — describe default harness and MAF opt-out where applicable.
- [ ] `pyproject.toml` — make the default harness dependency available by default.

## 8. Status & sign-off

- **Architecture review (phase 2):** Initial review identified default-selection
  compatibility and qualification risks. The revised contract preserves those
  limits and documents the required MAF opt-out and session migration impact;
  final review pending.
- **Human sign-off:** The issue request explicitly asks to make Copilot the
  default harness; explicit acceptance of the qualification and migration risks
  remains pending.
