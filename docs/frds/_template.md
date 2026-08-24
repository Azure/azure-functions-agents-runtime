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

## 1. Summary

> One paragraph: what is changing and why, in plain language.

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

## 5. Sample / user journey

> Complete this section when the feature adds or modifies a runnable sample;
> otherwise state "Not applicable" and why. Keep each sample focused on one
> primary capability so a learner can understand what its result demonstrates.

- **Target learner:** …
- **Primary capability:** …
- **User-centered story / natural-language prompt:** …
- **Expected observable terminal result:** …
- **Dedicated sample vs existing sample:** …
- **Rationale and impact on existing samples:** …

### Model-backed E2E boundary

> If an LLM generates structured execution inputs such as tool arguments,
> dependency graphs, or configuration, require an opt-in real-model E2E that
> covers **natural-language prompt → model → tool call → parser/runtime →
> observable terminal result**. Unit or integration tests that bypass the model
> cannot substitute for this boundary.

- **Real-model E2E required:** Yes / No — …
- **Uniquely named Foundry environment variables:** …
- **Sample-owned setup/test script:** …
- **Exact setup, run, and test commands:** …
- **Concise evidence to capture in the PR:** …
- **Generated ignored files (for example, `local.settings.json`):** …
- **Cleanup:** … (remove only files created by the sample-owned script)
- **Missing-environment behavior:** Skip clearly rather than fail.

## 6. Decisions log

> Append-only. Record every non-trivial choice and **who** made it. This is the
> durable record that makes the FRD worth committing. When a runnable sample is
> in scope, include the decision to create a dedicated sample or extend an
> existing one.

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | …        | A / B / C          | B      | Human / Agent | YYYY-MM-DD |

## 7. Test plan

> New/changed tests, mirroring source modules under `tests/`. When config or
> authoring behavior changes, add a scenario folder under
> `tests/fixtures/config_scenarios/`. List the key cases (happy path, errors,
> edge cases) and any new fixtures. For runnable samples, include an independent
> sample review, exact validation commands and evidence, and tests for existing
> samples affected by extraction or shared-code changes.

- [ ] Unit: …
- [ ] Fixture scenario: `tests/fixtures/config_scenarios/<nn_name>/`
- [ ] Regression (if fixing a bug): …
- [ ] Independent sample review (if applicable): …
- [ ] Existing affected samples: …
- [ ] Opt-in real-model E2E (if required): prompt → model → tool call →
      parser/runtime → observable terminal result
- [ ] Missing E2E environment variables skip clearly rather than fail.
- [ ] Exact commands and concise evidence recorded in the FRD and PR.
- [ ] Sample-owned script cleans up only the files it created.

## 8. Docs impact

> Which docs change and how.

- [ ] `docs/architecture.md` — module map / pipeline stages
- [ ] `docs/front-matter-spec.md` — authoring format
- [ ] `docs/triggers.md` — trigger types
- [ ] `README.md` — user-facing quickstart / examples

## 9. Status & sign-off

- **Architecture review (phase 2):** <summary of reviewer findings / link>
- **Human sign-off:** <name, date> → set `status: Finalized` before implementing.
