# Feature Requirements Documents (FRDs)

FRDs are lightweight, committed design records for **medium+ features** in this
repo — think "ADR + requirements." They capture the problem, the proposed
design, and an append-only **Decisions log** that records who decided what.

The full lifecycle that produces an FRD lives in [`../../AGENTS.md`](../../AGENTS.md)
§1 and is automated by the `add-feature` skill
([`.github/skills/add-feature/SKILL.md`](../../.github/skills/add-feature/SKILL.md)).

## When do I need one?

| Scope | FRD? |
| --- | --- |
| nit, bug, small self-contained feature | No — a PR description is enough |
| medium+ feature (new public surface, cross-module, new authoring/discovery behavior) | **Yes** |

## How to create one

1. Find the highest existing `NNNN-*.md` in this folder; the next FRD is
   `NNNN + 1`, zero-padded to 4 digits.
2. Copy [`_template.md`](_template.md) to `docs/frds/<NNNN>-<slug>.md`
   (e.g. `0001-agents-folder-indexing.md`).
3. Fill every section; keep the Decisions log up to date as choices are made.
4. Run the **architecture review** (AGENTS.md phase 2). Record human sign-off and
   set `status: Finalized` before implementing.

## Index

| FRD | Title | Status |
| --- | ----- | ------ |
| [0001](0001-agents-folder-indexing.md) | agents/ folder indexing | Finalized |
| [0002](0002-skill-includes.md) | Skill file includes | Finalized |
| [0003](0003-runtime-observability.md) | Runtime-owned observability (OpenTelemetry) | Finalized |
| [0004](0004-dynamic-workflows.md) | Dynamic workflows | Finalized |
| [0005](0005-web-request-system-tool.md) | `web_request` system tool | In review |
| [0006](0006-endpoint-authentication.md) | Endpoint & HTTP trigger authentication (API key / Entra ID) | Finalized |

> `_template.md` is the template, not an FRD — the leading underscore keeps it
> sorted first and excludes it from numbering.
