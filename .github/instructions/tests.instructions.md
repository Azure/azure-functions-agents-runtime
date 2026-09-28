---
applyTo: "tests/**/*.py"
---

# Test conventions

`python.instructions.md` governs `src/**/*.py` semantics. Tests are not held
to the same strict-typing or trust-boundary rules (`mypy` runs with
`strict = false, ignore_errors = true` on `tests.*`, per `pyproject.toml`), but
the structural and naming conventions below still apply.

## Layout and naming

- Tests live in `tests/` and mirror the source module under test as
  `tests/test_<module>.py`. `tests/conftest.py` puts `src/` on `sys.path`.
- Config/authoring behavior is covered by scenario fixtures under
  `tests/fixtures/config_scenarios/`. Add a new scenario folder when you
  change how `*.agent.md`, `agents.config.yaml`, `mcp.json`, `skills/`, or
  `tools/` are interpreted.

## Writing tests

- For bug fixes, add a **failing regression test first**, then fix.
- Prefer guard clauses, early returns, and helpers over deeply nested control
  flow, the same as source code.
- Assert on the declared fields of typed SDK results or local dataclasses
  directly; don't defensively re-validate them with `getattr`, `isinstance`,
  or casts just because the code under test avoids that pattern.
- Keep docstrings and comments terse; explain a non-obvious fixture/scenario
  setup, not the next line of code.
