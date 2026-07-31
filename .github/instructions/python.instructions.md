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

- Keep docstrings and comments terse. Explain a durable contract or reason, not
  the next line of code or feature/PR history. Do not cite phase labels, PR
  numbers, or mutable FRD decision numbers in source comments, docstrings, or
  assertion messages.
- Use the shared `azure_functions_agents._logger.logger`.
