---
name: add-feature
description: "Use when adding a medium or larger feature (or a non-trivial change) to the azure-functions-agents-runtime repository — i.e. anything that introduces new public surface, spans multiple modules, or changes authoring/discovery/registration behavior. Drives the full feature lifecycle defined in AGENTS.md: triage + worktree, writing a Feature Requirements Document (FRD) with a Decisions log, an architecture-review checkpoint, surgical implementation, a testing checkpoint, and documentation updates. Trigger on requests like 'add a feature', 'I want to implement X', 'let's design X', or when a change is clearly larger than a nit/bug/small fix. Do NOT use for typos, formatting, comments, or self-contained one-module fixes — those follow the lightweight lane in AGENTS.md."
---

# add-feature — feature lifecycle for azure-functions-agents-runtime

This skill operationalizes the lifecycle in [`AGENTS.md`](../../../AGENTS.md) §1
for **medium+ features**. Run the phases in order; each has an exit gate — do not
advance until it is met. Record every non-trivial choice in the FRD Decisions log.

## When to use

Use for medium+ features: new public surface (frontmatter keys, config, endpoints),
cross-module changes, or new discovery/registration/authoring behavior.
Skip (use the lightweight lane) for nits, typos, and self-contained one-module
bug fixes.

## Prerequisites

- Read `AGENTS.md` (process) and `docs/architecture.md` §2–§3 (design + module map).
- Know the canonical gate commands (`AGENTS.md` §3).

## Phase 0 — Triage + worktree  *(gate: scope + lane agreed)*

1. Confirm the change is genuinely medium+. If not, drop to the lightweight lane.
2. Agree a short slug and create a worktree off `main`:
   ```bash
   git worktree add \
     ../copilot-worktrees/azure-functions-agents-runtime/<user>-<slug> \
     -b <user>/<slug> main
   ```
3. **Multi-phase features:** if the FRD is delivered in phases across several
   PRs, branch off the feature integration branch (e.g. `feature/<slug>`), not
   `main`, and target your PR at it. Rebase onto its current tip before opening
   the PR — parallel phases land between your branch point and your review.

## Phase 1 — FRD  *(gate: FRD drafted)*

1. Determine the next FRD number: highest `docs/frds/NNNN-*.md` + 1, zero-padded.
2. Copy `docs/frds/_template.md` → `docs/frds/<NNNN>-<slug>.md`.
3. Fill every section. Map the **Proposed design** onto the pipeline stages
   (discover → translate → register → execute) and name the modules touched.
4. Seed the **Decisions log** with the initial choices and who made them
   (Human vs Agent). Follow the discipline below — it is what keeps a
   long-lived FRD readable.

### Writing the Decisions log

The log is an **index of choices, not an essay**. On a long-running FRD it is
the section that rots first, because every phase appends and nobody prunes.

- **One row per decision.** Target ≤ ~350 characters; treat ~500 as the hard
  ceiling. `Decision` is a short noun phrase, `Options considered` is
  `A / B / C`, and `Choice` is the decision plus the one fact that justifies it.
- **Record *what* was chosen and *why*, never *how* it was implemented.** Do not
  restate what the code, `AGENTS.md`, or `docs/architecture.md` already says.
  If a decision truly needs more, the detail belongs in the design section it
  governs — not in a table cell.
- **Append-only.** To revise, add a new row and annotate the row it narrows
  (e.g. "narrows #100"). Never rewrite or delete an existing row.
- **Numbers are positional, not stable.** Parallel phases and rebases renumber
  them. So:
  - never cite a decision number from code (see Phase 3);
  - expect a decision-table conflict when rebasing a long-lived feature
    branch. Resolve it by keeping **both** sets and renumbering yours to
    follow, then fix any internal cross-references you shifted.
- **Sanity-check size before you commit.** If your rows are among the longest
  in the table, compact them — a log nobody reads defeats the point.


## Phase 2 — Architecture review (planning mode)  *(gate: human sign-off → `status: Finalized`)*

1. Run a dedicated review pass — prefer a review sub-agent (e.g. `rubber-duck`)
   so the author's context does not bias it. Ask it to judge the FRD for:
   - completeness (all sections answered, edge cases, compatibility);
   - alignment with the `docs/architecture.md` module map and pipeline boundaries
     (discovery read-only; registration is the only Azure-aware stage; lazy runner);
   - whether the public surface stays consistent with `docs/front-matter-spec.md`.
2. Iterate on the FRD until findings are resolved. Append decisions to the log.
3. **Verify a review finding before acting on it.** Review sub-agents are
   confidently wrong often enough to matter, especially about third-party
   behavior. If a finding claims "the SDK/service does X", check it against the
   actually-installed package or a live call — not a hand-constructed payload.
   A reviewer once "proved" a field was a `str` by building a fake response;
   the live service sends an `int`, and acting on that review would have
   introduced the bug it claimed to fix.
4. Get explicit human sign-off, record it in §8, and set `status: Finalized`.
   **Do not implement before the FRD is Finalized.**

## Phase 3 — Implementation  *(gate: `ruff` + `mypy` clean)*

1. Implement **product changes only**, per the finalized FRD. Keep diffs surgical;
   no unrelated refactors.
2. Follow [`.github/instructions/python.instructions.md`](../../instructions/python.instructions.md)
   for source semantics, `pyproject.toml` for lint/type rules, and
   `tests/test_convention_guards.py` for CI-enforced structural rules.
   - **Don't reach into another phase's modules.** If a change you want (a
     rename, a shared helper) lives in code another in-flight phase owns, ask
     that phase to make it rather than doing it as a drive-by — it will collide
     on merge. Record the hand-off as a decision.
3. Run and pass:
   ```bash
   python -m ruff check src tests
   python -m mypy src
   ```

## Phase 4 — Testing  *(gate: full gate green)*

1. Design coverage for the new behavior — prefer a separate testing review pass
   (sub-agent or fresh checklist) so gaps are caught independently.
2. Add tests under `tests/`, mirroring source modules. For config/authoring
   changes, add a scenario folder under `tests/fixtures/config_scenarios/`.
3. For bug-adjacent work, add a failing regression test first. Prove a
   regression test actually catches its bug by reverting the fix and watching
   it fail.
4. **Make test doubles mirror the real dependency's shape.** A fake built from
   your own runtime types pins *your assumption* instead of the contract, and
   will happily agree with a wrong belief forever.
5. Run the full CI-equivalent gate:
   ```bash
   python -m pytest --cache-clear --cov=./src/azure_functions_agents --cov-report=xml --cov-branch tests
   ```
   Do not add `-m` marker filters — the default `addopts` already deselects the
   suites that need extra infrastructure, and overriding it silently pulls them
   in and produces spurious failures.

## Phase 5 — Docs  *(gate: DoD met)*

1. Update `docs/architecture.md` (module map / pipeline) — it is the design source
   of truth and must stay accurate.
2. **If schema.py changed:**
   - Run `python eng/scripts/generate_config_reference.py` to regenerate the reference
   - Use the **`update-schema-docs` skill** to add examples to `docs/front-matter-spec.md`
     and review `docs/architecture.md` for consistency
   - Review the skill's PR checklist and address architectural concerns
3. Update relevant docs under `docs/` (commonly `front-matter-spec.md`, `triggers.md`, 
   or other files as needed) if the authoring surface changed (for non-schema changes, 
   or to refine schema-generated examples).
4. Update `README.md` if user-facing behavior changed.
5. Update the FRD index in `docs/frds/README.md`.
6. Verify the `AGENTS.md` §8 Definition of Done, then open the PR.
7. After merge, remove the worktree (`git worktree remove <path>`) and set the FRD
   `status: Implemented`.

## Guardrails

- Never skip a gate. If a gate fails, fix before advancing.
- Keep the Decisions log current **and short** — it is the durable record that
  justifies the FRD, and it only stays useful if it stays readable.
- Keep implementation diffs surgical and scoped to the FRD.
- **Prefer mechanized enforcement over written convention.** If review feedback
  is a recurring style rule, try to encode it as a lint rule so future changes
  are checked automatically. Only enable a rule that passes cleanly repo-wide —
  if it surfaces many pre-existing violations, scope it or write it into
  `AGENTS.md` instead of triggering a refactor this feature does not own.
  Record which rules were enabled vs. rejected as a decision, and keep the
  rationale out of the config file itself.
- This skill is repo dev-tooling under `.github/skills/`; it is unrelated to the
  runtime's user-authored agent skills discovered from an app's `skills/` folder.
