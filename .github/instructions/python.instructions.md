---
applyTo: "src/**/*.py"
---

# Python source conventions

`AGENTS.md` owns the development lifecycle. This instruction owns source-level
Python semantics; `pyproject.toml` owns ruff and mypy enforcement.

## Types and trust boundaries

- Use Pydantic v2 models for configuration and other external documents. Shared
  fields and validators belong on the common model base.
- Parse untrusted documents with `ConfigDict(strict=True, extra=...)`, reject
  duplicate JSON keys before `model_validate`, and translate `ValidationError`
  at the trust boundary without exposing rejected values.
- Do not defensively revalidate already typed SDK results or local dataclasses
  with `getattr`, `isinstance`, casts, or `Any`; import the boundary type and
  use its declared fields directly.
- Frozen dataclasses validate through a `create()` factory and module-level
  normalization helpers, not `__post_init__` mutation.

## Structure and naming

- Prefer guard clauses, early returns, and helpers over deeply nested control
  flow.
- Give every source module a globally unique, intent-revealing basename.
  Source tests mirror the module name as `tests/test_<module>.py`.
- Use a module constant rather than repeating a named URL, API version, or path.

## Documentation and logging

- Default to a one-line docstring; the name and signature usually say enough.
  Use a multi-line docstring only for a non-obvious durable contract or
  invariant, and keep it to a summary line plus at most four short lines.
- Never use a docstring (or comment) for a step-by-step algorithm walkthrough,
  platform-specific mechanics, retry/error choreography, or design history —
  that belongs in `docs/architecture.md` or the owning FRD's design section,
  or, for a narrowly-scoped gotcha, a short comment placed at the exact line
  it explains.
- Keep docstrings and comments terse otherwise too. Explain a durable contract
  or reason, not the next line of code or feature/PR history. Do not cite
  phase labels, PR numbers, or mutable FRD decision numbers in source
  comments, docstrings, or assertion messages.
- When a change needs an FRD Decisions-log update, add the fewest durable
  rows that cover it — group related choices into one row rather than one row
  per test, review finding, or implementation correction, and keep mechanics
  out of the row (they belong in the design section the decision governs).
  See the add-feature skill for the full logging discipline.
- Use the shared `azure_functions_agents._logger.logger`.
