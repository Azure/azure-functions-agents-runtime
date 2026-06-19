# AGENTS.md

Operating guide for AI coding agents (and humans) working in
`azure-functions-agents-runtime`. Read this first. It defines **how** we make
changes so features stay aligned with the architecture and each other.

- **What this project is:** a markdown-first programming model that turns an
  agent project (`*.agent.md` + config) into an `azure.functions.FunctionApp`,
  powered by the Microsoft Agent Framework (MAF).
- **Design source of truth:** [`docs/architecture.md`](docs/architecture.md).
  This file governs *process*; that file governs *design*. Keep both in sync.

---

## 0. Golden rule

**Every change starts with triage + a dedicated worktree.** Never commit feature
work directly on `main`. Classify the change, size it, then follow the matching
lane below. When in doubt about scope or design, stop and ask.

---

## 1. Feature development lifecycle

Classify the change first, then pick a lane:

| Scope | Examples | Lane |
| --- | --- | --- |
| **nit** | typo, comment, formatting, trivial rename | Worktree → implement → gate → PR |
| **bug** | incorrect behavior, regression | Worktree → (repro test first) → fix → gate → PR |
| **small feature** | self-contained, single module, no new public surface | Worktree → short design note in PR → implement → test → docs → gate → PR |
| **medium+ feature** | new public surface, cross-module, new authoring format or discovery behavior | **Full FRD pipeline** (below) |

### The medium+ pipeline (phases)

Run these phases in order. Each has an exit gate; do not advance until it is met.

| Phase | What happens | Exit gate |
| --- | --- | --- |
| **0 · Triage + Worktree** | Agree scope; create the worktree (§2). | Scope + lane agreed |
| **1 · FRD** | Write a Feature Requirements Document (§4): problem, goals/non-goals, proposed design, **Decisions log**, test plan, docs impact. | FRD drafted |
| **2 · Architecture review** *(planning mode)* | Review the FRD for **completeness** and alignment with the module map in `docs/architecture.md`. Iterate. | Human sign-off recorded in the Decisions log → status `Finalized` |
| **3 · Implementation** | Implement **product changes only**, per the finalized FRD. Keep diffs surgical. | `ruff` + `mypy` clean |
| **4 · Testing** | Design/extend coverage for the new behavior; add tests under `tests/`. | Full gate green (§3) |
| **5 · Docs** | Update `docs/architecture.md` (module map / pipeline), `docs/front-matter-spec.md`, `docs/triggers.md`, and `README.md` as relevant. | Docs reflect reality; DoD met (§6) |

> Phases 2 and 4 are explicit *review* checkpoints. Treat them as separate
> passes (ideally a dedicated review sub-agent): an **architecture review** that
> judges the FRD, and a **testing review** that judges coverage. Do not let the
> author's implementation context bias the review.

> **Tooling:** the `add-feature` workflow skill
> ([`.github/skills/add-feature/SKILL.md`](.github/skills/add-feature/SKILL.md))
> automates phases 1–5, and FRDs use the template at
> [`docs/frds/_template.md`](docs/frds/_template.md). When told to "use the
> add-feature skill," follow that playbook; §4 below is the FRD outline.

---

## 2. Worktree convention

One worktree per change, branched off `main`:

```bash
git worktree add \
  ../copilot-worktrees/azure-functions-agents-runtime/<user>-<slug> \
  -b <user>/<slug> main
```

- **Branch:** `<user>/<slug>` (e.g. `vrdmr/agents-folder-indexing`).
- **Directory:** mirrors the branch under
  `copilot-worktrees/azure-functions-agents-runtime/`.
- Remove the worktree after the PR merges: `git worktree remove <path>`.

---

## 3. Canonical commands (the gate)

These mirror CI exactly (`eng/templates/jobs/ci-tests.yml`; Python **3.13** and
**3.14**). All three must pass before a PR is ready. The `--cov-report=xml` flag
is what CI runs; drop it (or use the fast loop below) for everyday local runs.

```bash
# One-time setup (editable install with dev extras)
python -m pip install -U -e .[dev]

# Lint
python -m ruff check src tests

# Type-check (strict)
python -m mypy src

# Test (with coverage, matching CI exactly)
python -m pytest --cache-clear --cov=./src/azure_functions_agents --cov-report=xml --cov-branch tests

# Fast local test loop
python -m pytest tests -q
```

> `samples/` is intentionally excluded from `ruff` and `mypy`. `tests/` is
> linted but excluded from strict `mypy`.

---

## 4. FRD: Feature Requirements Document

Required for medium+ features. **Location:** committed to the repo at
`docs/frds/<NNNN>-<slug>.md` (zero-padded sequence, e.g.
`docs/frds/0001-agents-folder-indexing.md`) — treated like a lightweight ADR so
the Decisions log is durable history. Start from
[`docs/frds/_template.md`](docs/frds/_template.md); see
[`docs/frds/README.md`](docs/frds/README.md) for numbering. Recommended sections:

1. **Summary** — one paragraph: what and why.
2. **Motivation / problem** — the pain today (e.g. AgentApps with many agents).
3. **Goals / Non-goals** — explicit scope boundaries.
4. **Proposed design** — modules touched, mapped to the `docs/architecture.md`
   stages (discover → translate → register → execute).
5. **Decisions log** — append-only table; record *who* decided:

   | # | Decision | Options considered | Choice | Decided by | Date |
   | - | -------- | ------------------ | ------ | ---------- | ---- |
   | 1 | …        | A / B / C          | B      | Human / Agent | … |

6. **Test plan** — new/changed tests; fixtures under
   `tests/fixtures/config_scenarios/` when config behavior changes.
7. **Docs impact** — which `docs/*` and `README.md` sections change.
8. **Status** — `Draft` → `In review` → `Finalized`.

---

## 5. Code conventions

Grounded in `pyproject.toml` and current code:

- **Python ≥ 3.13.** Strict typing everywhere in `src/` (`mypy --strict`,
  `pydantic.mypy` plugin).
- **Type aliases use PEP 695:** `type Foo = Bar` (not `Foo: TypeAlias = Bar`);
  ruff `UP040` enforces this. See `src/azure_functions_agents/discovery/mcp.py`.
- **Ruff** rules: `E,F,I,B,UP,SIM,RUF,N`; line-length 100 (`E501` ignored).
  Imports sorted via ruff isort; first-party = `azure_functions_agents`.
- **Pydantic v2** for all config models (`config/schema.py`). When a field +
  validator is shared across provider/sub-models, declare it once on the common
  base class rather than duplicating per subclass.
- **MAF is the only runtime.** The legacy `runtime:` frontmatter field is ignored
  (one-time warning). Do not reintroduce runtime branching.
- **Logging** goes through the shared `azure_functions_agents._logger.logger`.
- **Respect the pipeline boundaries** (architecture.md §2): discovery is
  read-only; registration is the only Azure-aware stage; the runner executes
  lazily. Don't re-parse YAML/front matter in registration — trust
  `ResolvedAgent` / `AgentCapabilities`.

---

## 6. Testing conventions

- Tests live in `tests/` and **mirror source modules**
  (`tests/test_<module>.py`). `tests/conftest.py` puts `src/` on `sys.path`.
- Config/authoring behavior is covered by scenario fixtures under
  `tests/fixtures/config_scenarios/`. Add a new scenario folder when you change
  how `*.agent.md`, `agents.config.yaml`, `mcp.json`, `skills/`, or `tools/`
  are interpreted.
- For bug fixes, add a **failing regression test first**, then fix.

---

## 7. Documentation conventions

- `docs/architecture.md` is the design source of truth — its **module map** and
  **pipeline stages** must stay accurate. Update it in the same PR as behavior
  changes (lifecycle phase 5).
- `docs/front-matter-spec.md` documents the `.agent.md` authoring format;
  `docs/triggers.md` documents trigger types. Update when the authoring surface
  changes.
- `README.md` is the user-facing quickstart — update examples when public
  behavior changes.

---

## 8. Definition of Done

- [ ] Change made in a dedicated worktree off `main`.
- [ ] (medium+) FRD finalized with a completed Decisions log.
- [ ] `ruff`, `mypy`, and `pytest` all green locally (§3).
- [ ] New behavior is tested (regression test for bugs).
- [ ] `docs/architecture.md` + relevant `docs/*` / `README.md` updated.
- [ ] Diff is surgical — no unrelated changes.
- [ ] Worktree removed after merge.
