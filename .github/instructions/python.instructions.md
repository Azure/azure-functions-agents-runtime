---
applyTo: "src/**/*.py"
---

# Python source conventions

`AGENTS.md` owns the development lifecycle. This instruction owns source-level
Python semantics; `pyproject.toml` owns ruff and mypy enforcement.

## Types and trust boundaries

- Use Pydantic v2 models for configuration and other external documents. Shared
  fields and validators belong on the common model base.
- Parse untrusted documents with `ConfigDict(extra=...)`, reject duplicate
  JSON keys before `model_validate`, and translate `ValidationError` at the
  trust boundary. Only add `strict=True` where the authoring format does not
  rely on non-strict coercion (e.g. `resolve_env_vars_in_data` substitutes
  environment variables as strings, so numeric front-matter fields such as
  `$OUTPUT_LIMIT` depend on non-strict parsing today).
- Do not defensively revalidate already typed SDK results or local dataclasses
  with `getattr`, `isinstance`, casts, or `Any`; import the boundary type and
  use its declared fields directly. This does not apply to declared,
  intentionally dynamic extension points (e.g. `ClientManager.build_chat_client`,
  which returns `Any` because the underlying SDK is pluggable) — narrow
  runtime inspection is expected there.
- Validate external or SDK-owned inputs once at the trust boundary, then pass
  a normalized internal shape downstream so ordinary code can use non-optional
  fields directly. Keep `None` / optional handling where the upstream contract
  genuinely permits absence.
- For fixed-shape data, reuse an SDK-provided schema when available; otherwise
  define a `TypedDict` or dataclass for trusted internal payloads and use
  Pydantic for untrusted documents. Avoid ad hoc string-keyed dictionaries and
  parallel schemas for values that already have a declared type.
- Prefer SDK-exported enums and constants for SDK-owned names and values when
  available. Do not invent SDK exports or depend on private symbols merely to
  avoid a literal.
- Frozen dataclasses validate through a `create()` factory and module-level
  normalization helpers, not `__post_init__` mutation.

## Structure and naming

- Prefer guard clauses, early returns, and helpers over deeply nested control
  flow, but keep justified nesting when it preserves cleanup, cancellation, or
  error-precedence invariants such as `try`/`finally`.
- Before adding another nested `try` / `except`, prefer a small helper that
  isolates the inner failure policy. Keep the nesting only when it is the
  clearest way to preserve cleanup, cancellation, or exception precedence.
- When callers share validation, normalization, or identity rules, extend one
  existing helper and use it at every relevant surface. Do not duplicate its
  logic or force distinct contracts through a helper that does not fit them.
- Give every source module (other than package initializers such as
  `__init__.py`) a globally unique, intent-revealing basename. Source tests
  mirror the module name as `tests/test_<module>.py`.
- Use a module constant rather than repeating a named URL, API version, package
  distribution, environment variable, or path.
- New runtime code reads environment variables through
  `config.env.runtime_env_value()` with a named `EnvVar`, not string literals;
  existing modules migrate opportunistically. Use `raw_env_value()` when unset
  and blank values have different behavior.
- Avoid duplicated logic: when two code paths share the same validation or parsing
  shape, extract a shared helper and keep only caller-specific policy separate.
- Reuse an existing import in the same module rather than re-importing the same
  symbol later. Keep a local import when it avoids a cycle or intentionally
  defers an optional or heavyweight dependency.
- When behavior varies by a provider/backend/kind enum, prefer an interface with
  one implementation per kind plus a registry over repeated `if`/`elif` chains
  across modules.
- Declare each finite domain vocabulary once in its owning module. Use a
  `StrEnum` when the vocabulary is a runtime concept or crosses a persistence,
  serialization, logging, or API boundary; consumers should use named enum
  members and `.value` at string boundaries. Use `Literal[...]` when the
  vocabulary is type-only, local to a signature or model, and does not need
  runtime identity or member access. Use frozensets or mappings when membership
  or value lookup is the runtime operation. Consumers reuse these symbols rather
  than redeclaring raw strings.

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
- When a change needs an FRD Decisions-log update, record only consequential
  feature-contract choices, with the fewest durable rows that cover them.
  Do not record PR sequencing, slice scope, or implementation corrections in
  the FRD; put delivery details in PR descriptions or a separate plan.
  See the add-feature skill for the full logging discipline.
- Use the shared `azure_functions_agents._logger.logger`.
