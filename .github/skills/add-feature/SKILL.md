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
5. If the feature adds or modifies a runnable sample, complete the
   **Sample / user journey** section before architecture review:
   - identify the target learner and the one primary capability the sample teaches;
   - write a user-centered story, including the natural-language prompt when the
     sample starts from one, and the observable terminal result;
   - record an explicit FRD decision to create a dedicated sample or extend an
     existing one, with the rationale and migration/testing impact;
   - define the exact setup, run, test, and cleanup commands, the concise evidence
     the PR will include, and any existing samples affected when code is extracted.
6. Treat the completed sample design as a gate. Do not advance a sample-bearing
   feature to architecture review while the learner, capability, journey,
   dedicated-vs-existing decision, or validation boundary remains ambiguous.

## Phase 2 — Architecture review (planning mode)  *(gate: human sign-off → `status: Finalized`)*

1. Run a dedicated review pass — prefer a review sub-agent (e.g. `rubber-duck`)
   so the author's context does not bias it. Ask it to judge the FRD for:
   - completeness (all sections answered, edge cases, compatibility);
   - alignment with the `docs/architecture.md` module map and pipeline boundaries
     (discovery read-only; registration is the only Azure-aware stage; lazy runner);
   - whether the public surface stays consistent with `docs/front-matter-spec.md`.
2. Iterate on the FRD until findings are resolved. Append decisions to the log.
3. Get explicit human sign-off, record it in §9, and set `status: Finalized`.
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
4. If the feature adds or modifies a runnable sample, run an independent sample
   review (a reviewer other than the sample author, preferably a review sub-agent)
   and resolve findings about:
   - whether the sample tells the FRD's user-centered story and keeps one primary
     capability;
   - whether its documented commands produce the expected observable terminal
     result from a clean setup;
   - whether shared-code extraction or other changes preserve coverage for every
     affected existing sample.
5. For behavior where an LLM generates structured execution inputs such as tool
   arguments, dependency graphs, or configuration, include an opt-in real-model
   E2E that crosses the complete boundary:
   **natural-language prompt → model → tool call → parser/runtime → observable
   terminal result**. Unit or integration tests that inject model output or call
   the parser/runtime directly cannot substitute for this E2E.
6. Give each live E2E dependency a uniquely named Foundry environment variable
   documented in the sample and FRD. When the opt-in variables are absent, the
   live sample E2E must skip with a clear reason rather than fail; partial
   configuration should fail with an actionable message. When they are present,
   a sample-owned setup/test script may:
   - generate an ignored `local.settings.json`, but must refuse to overwrite an
     existing or concurrently created user file;
   - run the real-model E2E and capture exact evidence of the authored tool
     arguments, dependency graph, or configuration plus the observable result;
   - track file ownership and clean up only the files that script created.
   Record the exact commands, prompt, relevant model-authored structured input,
   and resulting terminal evidence in both the FRD and PR without exposing secrets.
   The `samples/workflow-retry-policy/scripts/run-e2e.py` proposed in PR #170 is
   a concrete example of this pattern when available, but these gates are generic
   and do not depend on that unmerged implementation.
7. Treat independent sample review and the required live boundary as a sample
   validation gate. Do not accept bypass-model tests as evidence that it passed.
8. Run the full CI-equivalent gate:
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
- Keep the Decisions log current — it is the durable record that justifies the FRD.
- Keep implementation diffs surgical and scoped to the FRD.
- This skill is repo dev-tooling under `.github/skills/`; it is unrelated to the
  runtime's user-authored agent skills discovered from an app's `skills/` folder.
