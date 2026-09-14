---
frd: 0009
title: Customer-facing release summaries
status: Finalized
author: hallvictoria
created: 2026-09-11
updated: 2026-09-11
issues: []
pull_requests: []
branch: hallvictoria/release-docs
---

# FRD 0009 — Customer-facing release summaries

## 1. Summary

Integrate customer-facing release summaries into every repository change lane. Authors will use a
new `feature-summary` skill to create or update a grounded entry in an **Unreleased** section of
`docs/releases.md`, or record a reviewed N/A rationale for changes without meaningful customer
impact. An offline validator will enforce the release-page structure and prevent duplicate,
premature, or historical edits while leaving prose quality to human review.

## 2. Motivation / problem

GitHub's generated release notes enumerate merged pull requests but do not explain what customers
can now do, what behavior changed, or whether an upgrade requires action. The new
`docs/releases.md` page provides that explanation for existing versions, but its current process
requires maintainers to reconstruct customer impact manually after publication.

Feature authors and reviewers understand a change best while its implementation, tests, and design
context are current. The feature lifecycle should capture that knowledge before merge without
requiring authors to guess a future package version, release date, Git tag, PyPI URL, or comparison
range. The process must also cover customer-visible bugs and small features, not only medium+
features with FRDs.

## 3. Goals / Non-goals

**Goals**

- Require every nit, bug, small feature, and medium+ feature to assess release impact and either
  update Unreleased content or record a reviewed N/A rationale.
- Provide a reusable `feature-summary` skill that grounds customer prose in implemented behavior,
  tests, finalized design decisions, and current documentation.
- Support multi-PR features without duplicate entries or claims about behavior that has not shipped.
- Capture compatibility, changed defaults, deprecations, and required customer action explicitly.
- Enforce deterministic release-page structure in offline CI while grandfathering historical entries.
- Preserve human review for customer value, prose quality, and security-disclosure judgment.

**Non-goals**

- Automatically assign versions, dates, tags, PyPI URLs, or comparison ranges during feature work.
- Automatically promote Unreleased entries into a numbered release.
- Modify the official version-bump or release-publishing pipelines in this delivery.
- Generate GitHub release bodies in this delivery.
- Query GitHub or PyPI from ordinary PR CI.
- Retrofit metadata into historical release sections or historical FRDs.
- Automatically decide whether prose is useful, accurate, or safe to publish.
- Authorize edits to published history through CI; repository governance and maintainer review own
  factual corrections.

## 4. Proposed design

This is a repository process and tooling feature. It does not change the runtime's discover →
translate → register → execute pipeline or its public authoring/API surface.

| Area | Module(s) | Change |
| --- | --- | --- |
| Process contract | `AGENTS.md`, `.github/PULL_REQUEST_TEMPLATE.md` | Require release-impact update or reviewed N/A across every change lane. |
| Feature design | `docs/frds/_template.md`, `docs/frds/README.md` | Capture customer outcome, category, defaults, compatibility, upgrade action, and delivery slice. |
| Workflow automation | `.github/skills/feature-summary/`, `.github/skills/add-feature/`, `.github/skills/update-schema-docs/` | Add the summary workflow and integrate it after product documentation is stable. |
| Customer documentation | `docs/releases.md`, `CONTRIBUTING.md` | Add Unreleased staging and define feature-author and release-owner responsibilities. |
| Validation | `eng/scripts/validate_release_notes.py`, `tests/test_validate_release_notes.py`, `eng/templates/jobs/ci-tests.yml` | Enforce an offline, deterministic metadata and structure contract. |

### Unreleased staging model

`docs/releases.md` will contain exactly one **Unreleased** section above published release history.
It will warn readers that staged changes are not yet available in a published package. Entries use
only the applicable canonical categories:

- Features
- Improvements
- Bug fixes
- Security
- Compatibility and deprecations
- Maintenance and documentation

Each entry combines ordinary customer-facing Markdown with an immediately preceding HTML comment
containing machine-readable metadata.

#### Release-entry wire format

The normative format is UTF-8 Markdown containing a single-line, strict JSON metadata comment
immediately followed by exactly one top-level Markdown bullet, with no intervening blank line or
content:

```markdown
<!-- release-note: {"id":"frd-0009-release-summaries","category":"maintenance-documentation","prs":[234],"frd":"0009"} -->
- **Customer-facing release summaries.** Feature work now records an Unreleased customer outcome or a reviewed N/A rationale.
```

The bullet may wrap across continuation lines but must remain one paragraph and must not contain a
nested list. Its entry boundary ends at the next top-level block, heading, or metadata comment.
Metadata has this contract:

- `id` is required and matches `^[a-z0-9]+(?:-[a-z0-9]+)*$`;
- `category` is required and is one of `feature`, `improvement`, `bug-fix`, `security`,
  `compatibility-deprecation`, or `maintenance-documentation`;
- `prs` is required and contains unique positive integer PR numbers from this repository;
- `frd` is optional and is a four-digit string;
- unknown keys, duplicate JSON keys, duplicate PR numbers, wrong types, orphan comments, entry
  bullets without metadata, and category/heading mismatches are errors.

Metadata moves with its entry during release promotion and remains attached in new published
sections. Historical entries that existed before this contract are exempt from metadata
requirements. HTML comments are parser metadata, not a confidentiality boundary.

A semantic ID represents a customer outcome rather than a commit or PR. Examples include
`frd-0004-dts-display-names` and `pr-184-trigger-serialization`. Multiple PRs may update one entry
before release when they jointly complete the same outcome. Once promoted into a published release,
a later evolution receives a new ID.

Feature work must not add version, release date, tag, PyPI URL, release URL, or comparison range to
Unreleased content. Release preparation remains responsible for moving selected entries—not copying
them—into a versioned section and adding exact release metadata.

### Impact assessment across change lanes

Every PR records one of two outcomes:

1. **Release entry required:** the change creates externally meaningful behavior for customers,
   operators, or maintainers and creates or updates an Unreleased entry.
2. **N/A:** the change has no meaningful external impact and records a concise rationale in the PR.

Routine internal maintenance, formatting, and inert scaffolding normally use N/A. Customer-visible
bugs, security changes, deprecations, operational behavior, and useful documentation changes require
assessment even when they do not use the medium+ FRD pipeline.

For medium+ features, the FRD captures a draft assessment during Phase 1. Architecture review checks
public behavior, defaults, compatibility, migration guidance, security wording, and delivery-slice
boundaries. The final summary is written during Phase 5 after implementation, tests, and product
documentation establish the actual behavior.

The skill has two lifecycle modes because a PR number does not exist during the initial Phase 5
pass:

1. **Assessment mode:** after implementation, tests, and product docs are complete, return a
   proposed entry or N/A rationale without modifying `docs/releases.md`.
2. **Write mode:** after the branch is pushed and a draft PR assigns a number, create or update the
   Unreleased entry with that PR number, then run validation and commit the result.

Every committed Unreleased entry has a non-empty `prs` list. The validator does not infer a
"merge-ready" state and accepts no PR placeholders.

For multi-PR delivery:

- prose describes only behavior present after the current PR merges;
- an independently valuable slice creates or updates wording that remains true at that intermediate
  state;
- inert scaffolding records N/A in its PR and creates no speculative entry;
- a completing PR may create one aggregate entry referencing all contributing PRs;
- a later slice may amend an existing Unreleased entry only while the prior wording remains an
  accurate, independently releasable description;
- a later evolution receives a new semantic ID when the earlier entry has already been promoted.

### `feature-summary` skill

The workspace skill will be located at `.github/skills/feature-summary/` with:

- `SKILL.md` — discovery metadata, workflow, inputs/outputs, and guardrails;
- `assets/release-entry-template.md` — the constrained Unreleased entry skeleton;
- `references/writing-guide.md` — categories, examples, multi-PR cases, compatibility language,
  N/A guidance, factual corrections, and security handling.

The skill will:

1. Read the applicable FRD or PR context, implementation diff, tests, product documentation, and
   existing release page.
2. Classify impact or identify N/A.
3. Stop for clarification when implementation, tests, design, and docs disagree.
4. Search by summary ID, PR, and semantic outcome before writing.
5. Create or update only Unreleased content.
6. Describe customer outcomes rather than copying PR titles or implementation details.
7. State changed defaults, compatibility, deprecations, and required action explicitly.
8. Flag breaking or security-sensitive wording for human review.
9. Return the impact decision, page result, duplicate check, validation result, and PR checklist.

The skill must not invent release metadata, rewrite published history, claim deferred work, or expose
embargoed vulnerability details. Published history may change only through an explicit factual
correction workflow.

The `add-feature` skill will seed and review release impact in Phases 1–2 and invoke
`feature-summary` after all other Phase 5 documentation. The `update-schema-docs` skill will hand off
customer-facing schema facts, defaults, and migration concerns rather than writing duplicate release
prose.

The skill frontmatter name is `feature-summary`. Its discovery description includes release-impact
assessment, Unreleased customer summary, customer outcome, release notes, compatibility, and upgrade
notes. It excludes published-release promotion, history correction, generic documentation editing,
and runtime-discovered application skills.

Required inputs are the mode, PR number in write mode, optional FRD, repository base/current diff,
tests, product documentation, and `docs/releases.md`. Implemented behavior and tests take precedence
over draft FRD prose; any material disagreement produces a no-write result and a clarification
request. Rerunning with the same evidence and semantic ID updates the same entry without duplication.

The skill output contains the decision and rationale, ID/category, affected entry, evidence read,
duplicate-search result, validation result, and human-review flags. Lightweight lanes invoke the
skill directly through the `AGENTS.md` and PR-template instructions rather than running
`add-feature`. `update-schema-docs` hands off structured release facts—customer outcome, changed
defaults, compatibility, required action, and evidence paths—which `feature-summary` independently
verifies.

Potentially embargoed security work is never written to public metadata, an Unreleased entry, or a
detailed public N/A rationale. The skill stops and directs the author to the private Microsoft
security process documented by `SECURITY.md`. A Security entry may be written only after the
designated security owner approves public disclosure and wording.

### Offline validation

`eng/scripts/validate_release_notes.py` will validate the constrained metadata and release structure,
not prose quality. Its CLI is `python eng/scripts/validate_release_notes.py [--path PATH]`; success
returns 0, validation findings return 1, usage errors return 2, and unexpected exceptions are
reported without a traceback by default and return 3.

The validator will enforce:

- exactly one Unreleased section;
- unique semantic IDs and allowed metadata fields/categories;
- at least one source PR for every committed Unreleased entry;
- no release metadata or unresolved placeholders in Unreleased content;
- compatibility entries include upgrade guidance or explicit no-action language;
- published versions and at-a-glance rows are unique, paired, and newest-first;
- released section versions agree with their PyPI URLs;
- comparison links contain explicit tags while accepting historical tag formats;
- historical entries without metadata remain valid.

Ordinary CI remains offline. A human reviewer decides whether an entry is warranted and whether its
prose accurately communicates value. The validator does not authorize or prohibit factual
corrections to published history; the prospective process and maintainer review govern those edits.
This avoids treating a contributor-selectable validation flag as authorization.

Ruff does not currently include `eng/scripts/`, so CI and the canonical local checks invoke Ruff
explicitly on `eng/scripts/validate_release_notes.py` in addition to `src tests`.

### Manual release promotion

Release-pipeline automation is deferred, but ownership is explicit. After the package and GitHub
release are published, the release owner opens a documentation PR against `main`. The owner:

1. reconciles the exact released tag and commit range against Unreleased `prs`;
2. moves included entries into one new, newest-first version section while preserving metadata;
3. leaves excluded entries under Unreleased;
4. adds the release date, exact GitHub release URL, PyPI URL, at-a-glance row, and exact comparison
   URL;
5. verifies existing published sections remain unchanged.

Promotion is move-not-copy. Partial promotion and preservation of remaining Unreleased entries are
covered by validator tests. Online GitHub/PyPI reconciliation remains a documented human step in
this delivery.

### Compatibility

The runtime, package, authoring format, and existing published release notes are unchanged. Existing
published sections are grandfathered. The process change applies prospectively to new PRs and new or
materially amended FRDs.

Contributors gain a required release-impact decision in the PR template and, for medium+ work, the
FRD. Existing automation gains one deterministic offline CI check.

### Delivery plan

The feature is split into two coherent PRs so the machine-readable contract and validator can be
reviewed independently from workflow adoption. Each slice uses its own branch/worktree and passes
the repository gate.

| Slice | Purpose | Scope | Dependencies | Compatibility | Review focus |
| --- | --- | --- | --- | --- | --- |
| 1 | Establish the release-note contract and integrity checks | Unreleased format, normative metadata, validator, tests, CI, script documentation, FRD | None | Historical content grandfathered; empty Unreleased is valid | Parser boundaries, category/metadata contract, promotion invariants, offline CI |
| 2 | Adopt the author workflow | `feature-summary`, existing-skill handoffs, `AGENTS.md`, FRD/PR templates, split guidance, contributor workflow | Slice 1 merged | Prospective update-or-N/A requirement | Lifecycle timing, lightweight lanes, grounding, idempotence, human review boundaries |

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | First delivery scope | Process + skill only / process + skill + validation / full release integration | Process + skill + offline validation; defer release-pipeline automation | Human | 2026-09-11 |
| 2 | Pre-release staging location | FRD/PR only / separate fragments / Unreleased in `docs/releases.md` | Unreleased in `docs/releases.md` | Human | 2026-09-11 |
| 3 | Applicability | Medium+ only / all features / every change update-or-N/A | Every change lane assesses impact and updates or records reviewed N/A | Human | 2026-09-11 |
| 4 | Summary identity | PR number / FRD number / semantic delivery-slice ID | Semantic ID with PR and optional FRD/slice metadata | Agent | 2026-09-11 |
| 5 | Published history | Retrofit metadata / rewrite into canonical format / grandfather | Grandfather; only explicit factual corrections may edit history | Agent | 2026-09-11 |
| 6 | Validation boundary | Fully manual / structural offline validation / automatic prose judgment | Structural offline validation; customer value and prose remain human-reviewed | Agent | 2026-09-11 |
| 7 | GitHub/PyPI checks | Ordinary CI / trusted release pipeline later / none | Keep ordinary CI offline; defer online reconciliation automation | Agent | 2026-09-11 |
| 8 | Metadata serialization | YAML / custom comments / strict JSON comment | Single-line strict JSON immediately preceding one top-level bullet | Agent | 2026-09-11 |
| 9 | PR-number timing | Placeholder PR / infer readiness / assessment then write | No-write assessment before PR; write mode after draft PR assigns a number | Agent | 2026-09-11 |
| 10 | Multi-PR truth | Speculative aggregate / entry per commit / outcome-based intermediate truth | Describe only merged behavior; aggregate only when the completing PR makes the outcome true | Agent | 2026-09-11 |
| 11 | Release promotion | Pipeline automation / pre-publication manual / post-publication docs PR | Release owner manually promotes by docs PR after publication; automation deferred | Agent | 2026-09-11 |
| 12 | Published-history corrections | Contributor flag / CI authorization / governance | Validator does not authorize corrections; maintainer governance owns factual edits | Agent | 2026-09-11 |
| 13 | Embargo handling | Public marker / vague placeholder / no public write | No public metadata or prose before approved disclosure; use private security process | Agent | 2026-09-11 |
| 14 | Delivery shape | One combined PR / two coherent slices | Contract + validator first; workflow adoption second | Agent | 2026-09-11 |
| 15 | Architecture approval | Revise / approve finalized design | Approve FRD 0009 and begin slice 1 | Human | 2026-09-11 |

## 6. Test plan

- [x] Unit: valid Unreleased metadata and customer entry pass.
- [x] Unit: duplicate semantic IDs fail.
- [x] Unit: updating one outcome across multiple PRs does not require duplicate entries.
- [x] Unit: distinct summary IDs may intentionally reference the same PR.
- [x] Unit: versions, dates, release/PyPI/comparison URLs, and placeholders fail under Unreleased.
- [x] Unit: missing upgrade/no-action guidance fails for compatibility entries.
- [x] Unit: historical tag forms (`0.1.0b1`, `release0.1.0b3`, `v0.1.0b4`) pass.
- [x] Unit: at-a-glance/version-section mismatch and invalid ordering fail.
- [x] Unit: malformed comparison metadata fails.
- [x] Unit: malformed or duplicate-key JSON, unknown fields, wrong types, invalid IDs/categories,
  duplicate PRs, orphan metadata, missing metadata, wrong adjacency, nested lists, CRLF, Unicode, and
  empty Unreleased behavior.
- [x] Unit: partial promotion moves selected entries and preserves entries left Unreleased.
- [x] CLI: success, validation failure, usage error, and unexpected-error exit codes.
- [ ] Skill dry-run: single feature, multi-PR feature, compatibility-sensitive feature, schema feature,
  customer bug, and maintainer-only N/A cases.
- [ ] Skill discovery: representative feature-completion and release-summary prompts load the skill
  without intercepting unrelated documentation work.
- [ ] Docs: `uv run --no-sync mkdocs build --strict` passes.
- [ ] Full gate: Ruff, mypy, and pytest pass.

## 7. Docs impact

- [ ] `AGENTS.md` — cross-lane requirement, FRD/review/Phase 5 integration, documentation convention,
  schema handoff, and Definition of Done.
- [x] `docs/releases.md` — Unreleased staging format and warning.
- [x] `CONTRIBUTING.md` — release-owner promotion, correction, and reconciliation guidance; feature
  authoring is deferred to slice 2.
- [ ] `docs/frds/_template.md` — customer-facing release-impact section.
- [x] `docs/frds/README.md` — index FRD 0009; prospective guidance is deferred to slice 2.
- [ ] `.github/PULL_REQUEST_TEMPLATE.md` — update-or-N/A review gate.
- [ ] `.github/skills/add-feature/SKILL.md` — Phase 1, Phase 2, and Phase 5 integration.
- [ ] `.github/skills/add-feature/references/split-rules.md` — independently shippable summary rules.
- [ ] `.github/skills/update-schema-docs/SKILL.md` — release-facts handoff.
- [ ] `.github/skills/feature-summary/` — new skill, entry template, and writing guide.
- [x] `eng/scripts/README.md` — validator usage and responsibility boundary.
- [ ] `docs/architecture.md` — not applicable; runtime architecture is unchanged.
- [ ] `docs/front-matter-spec.md` — not applicable; authoring format is unchanged.
- [ ] `docs/triggers.md` — not applicable; trigger behavior is unchanged.
- [ ] `README.md` — not applicable; runtime quickstart and capabilities are unchanged.

## 8. Status & sign-off

- **Architecture review (phase 2):** First independent review required changes to the metadata wire
  format, PR timing, multi-PR truth rules, promotion ownership, correction governance, embargo
  handling, skill contract, validator CLI, and delivery slicing. The FRD was revised to resolve each
  blocking finding. A second independent review passed with no implementation blockers.
- **Human sign-off:** hallvictoria, 2026-09-11 — approved; status set to `Finalized`.
