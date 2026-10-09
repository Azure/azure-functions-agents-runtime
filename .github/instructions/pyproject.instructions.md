---
applyTo: "pyproject.toml"
---

# Dependency bump conventions

When changing the pinned `agent-framework-*` versions:

- Re-run the MAF-owned import/construction paths under `harness/agent_framework/` with
  warnings set to `always` in a fresh interpreter, and check whether any `ExperimentalWarning`
  (`agent_framework._feature_stage`) is emitted by code outside the sites already wrapped with
  `suppress_experimental_warnings()`.
- Wrap only the sites that emit it; remove the wrapper where a feature is no longer experimental.
  Keep `tests/test_maf_warning_suppression.py` and the helper docstring in sync.
