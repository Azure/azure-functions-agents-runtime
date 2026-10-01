---
frd: NNNN
title: <Feature title>
status: Draft            # Draft → In review → Finalized  (→ Implemented after merge)
author: <user>
created: YYYY-MM-DD
updated: YYYY-MM-DD
issues: []              # e.g. [#123]
pull_requests: []      # e.g. [#456]
branch: <user>/<slug>
---

# FRD NNNN — <Feature title>

> Copy this file to `docs/frds/<NNNN>-<slug>.md` (next zero-padded number) and
> fill every section. Delete these guidance blockquotes as you go. See
> `docs/frds/README.md` for the process and `AGENTS.md` §1 for the lifecycle.
> Describe the intended feature behavior, not PR sequencing, delivery scope,
> ownership, schedule, or rollout plans. Track those in PRs or a separate plan.

## 1. Summary

> One paragraph: what is changing and why, in plain language. Use short sentences, define terms on first use, and state conclusions as facts instead of narrating investigation history.

## 2. Motivation / problem

> The concrete pain today and who feels it. Include the triggering scenario
> (e.g. "AgentApps with many `.agent.md` files become unwieldy at 3+ agents").

## 3. Goals / Non-goals

**Goals**
- …

**Non-goals**
- …

## 4. Proposed design

> Map the change onto the runtime pipeline (`docs/architecture.md` §2):
> **discover → translate → register → execute**. Name the modules touched and
> the new/changed public surface (authoring format, config keys, endpoints).
> Define intended behavior and contracts, not delivery mechanics: omit PR
> numbers, commit SHAs, temporary slice/phasing labels, and package versions
> already owned by `pyproject.toml`.

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| discover | `discovery/…` | … |
| translate | `config/…` | … |
| register | `registration/…` | … |
| execute | `runner.py` / `client_manager.py` | … |

### Authoring / API surface

> New or changed `*.agent.md` frontmatter, `agents.config.yaml` keys,
> `mcp.json`, directory conventions, or HTTP/MCP endpoints. Keep aligned with
> `docs/front-matter-spec.md`.

### Compatibility

> Backward-compatibility notes, deprecations, and migration guidance.

## 5. Decisions log

> Append-only. Record consequential feature-contract choices and **who** made
> them, not implementation or delivery-planning decisions.
<!-- Never cite Decisions-log numbers in FRD prose or cross-references; describe the decision because numbers can shift. -->

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | …        | A / B / C          | B      | Human / Agent | YYYY-MM-DD |

## 6. Test plan

> New/changed tests, mirroring source modules under `tests/`. When config or
> authoring behavior changes, add a scenario folder under
> `tests/fixtures/config_scenarios/`. List the key cases (happy path, errors,
> edge cases) and any new fixtures.

- [ ] Unit: …
- [ ] Fixture scenario: `tests/fixtures/config_scenarios/<nn_name>/`
- [ ] Regression (if fixing a bug): …

## 7. Docs impact

> Which docs change and how.

- [ ] `docs/architecture.md` — module map / pipeline stages
- [ ] `docs/front-matter-spec.md` — authoring format
- [ ] `docs/triggers.md` — trigger types
- [ ] `README.md` — user-facing quickstart / examples

## 8. Status & sign-off

- **Architecture review (phase 2):** <summary of reviewer findings / link>
- **Human sign-off:** <name, date> → set `status: Finalized` before implementing.
