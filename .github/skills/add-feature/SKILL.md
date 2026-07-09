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

## Phase 1 — FRD  *(gate: FRD drafted)*

1. Determine the next FRD number: highest `docs/frds/NNNN-*.md` + 1, zero-padded.
2. Copy `docs/frds/_template.md` → `docs/frds/<NNNN>-<slug>.md`.
3. Fill every section. Map the **Proposed design** onto the pipeline stages
   (discover → translate → register → execute) and name the modules touched.
4. Seed the **Decisions log** with the initial choices and who made them
   (Human vs Agent).

## Phase 2 — Architecture review (planning mode)  *(gate: human sign-off → `status: Finalized`)*

1. Run a dedicated review pass — prefer a review sub-agent (e.g. `rubber-duck`)
   so the author's context does not bias it. Ask it to judge the FRD for:
   - completeness (all sections answered, edge cases, compatibility);
   - alignment with the `docs/architecture.md` module map and pipeline boundaries
     (discovery read-only; registration is the only Azure-aware stage; lazy runner);
   - whether the public surface stays consistent with `docs/front-matter-spec.md`.
2. Iterate on the FRD until findings are resolved. Append decisions to the log.
3. Get explicit human sign-off, record it in §8, and set `status: Finalized`.
   **Do not implement before the FRD is Finalized.**

## Phase 3 — Implementation  *(gate: `ruff` + `mypy` clean)*

1. Implement **product changes only**, per the finalized FRD. Keep diffs surgical;
   no unrelated refactors.
2. Follow `AGENTS.md` §5 conventions (PEP 695 type aliases, strict typing,
   Pydantic v2 base-class fields, MAF-only, shared `_logger`).
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
3. For bug-adjacent work, add a failing regression test first.
4. Run the full CI-equivalent gate:
   ```bash
   python -m pytest --cache-clear --cov=./src/azure_functions_agents --cov-report=xml --cov-branch tests
   ```

## Phase 5 — Docs  *(gate: DoD met)*

1. Update `docs/architecture.md` (module map / pipeline) — it is the design source
   of truth and must stay accurate.
2. **If schema.py changed:**
   - Run `python eng/scripts/generate_config_reference.py` to regenerate the reference
   - Use the **`update-schema-docs` skill** to add examples to `docs/front-matter-spec.md`
     and review `docs/architecture.md` for consistency
   - Review the skill's PR checklist and address architectural concerns
3. Update `docs/front-matter-spec.md` and/or `docs/triggers.md` if the authoring
   surface changed (for non-schema changes, or to refine schema-generated examples).
4. Update `README.md` if user-facing behavior changed.
5. Update the FRD index in `docs/frds/README.md`.
6. Verify the `AGENTS.md` §8 Definition of Done, then open the PR.
7. After merge, remove the worktree (`git worktree remove <path>`) and set the FRD
   `status: Implemented`.

## Guardrails

- Never skip a gate. If a gate fails, fix before advancing.
- Keep the Decisions log current — it is the durable record that justifies the FRD.
- Keep implementation diffs surgical and scoped to the FRD.
- This skill is repo dev-tooling under `.github/skills/`; it is unrelated to the
  runtime's user-authored agent skills discovered from an app's `skills/` folder.
