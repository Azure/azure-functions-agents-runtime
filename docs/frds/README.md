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
| [0007](0007-multi-agent-delegation.md) | Multi-agent delegation (agent-as-tool) | In review |
| [0008](0008-aca-sandbox-session-runtime.md) | ACA Sandbox session runtime — overview & index | Finalized |
| [0008.1](0008.1-execution-backend-and-controller.md) | ↳ Execution-backend abstraction & "Functions as controller" | Finalized |
| [0008.2](0008.2-session-identity-ownership-concurrency.md) | ↳ Session identity, ownership & one-active-run concurrency | Finalized |
| [0008.3](0008.3-state-store-and-tamper-evident-trust.md) | ↳ Customer-owned state store & tamper-evident trust model | Finalized |
| [0008.4](0008.4-resource-residency-and-provisioning.md) | ↳ Resource residency & provisioning boundary | Finalized |
| [0008.5](0008.5-controller-sandbox-transport-and-protocol.md) | ↳ Controller↔sandbox transport & sandbox runtime protocol | Finalized |
| [0008.6](0008.6-sandbox-packaging-image-and-content.md) | ↳ Sandbox packaging: stdlib bootstrap image + captured content | Finalized |
| [0008.7](0008.7-harness-compatibility-and-conformance.md) | ↳ Harness compatibility & conformance | Finalized |
| [0008.8](0008.8-snapshot-suspend-and-durability.md) | ↳ Snapshot, auto-suspend & state durability | Finalized |
| [0008.9](0008.9-network-egress-and-obo.md) | ↳ Network, egress & credentials/OBO | Finalized |
| [0008.10](0008.10-authoring-surface-and-config.md) | ↳ Public authoring surface & config | Finalized |
| [0008.11](0008.11-http-api-sync-async-streaming.md) | ↳ HTTP API: sync default, explicit async, chunked streaming | Finalized |
| [0008.12](0008.12-lifecycle-failure-and-reconciler.md) | ↳ Session/run lifecycle, failure behavior & reconciler/reaper | Finalized |
| [0008.13](0008.13-subagent-delegation-compat.md) | ↳ Subagent (multi-agent delegation) compatibility | Finalized |
| [0008.14](0008.14-dynamic-workflows-aca-compat.md) | ↳ Dynamic Workflows on ACA — compatibility analysis | Finalized |

> `_template.md` is the template, not an FRD — the leading underscore keeps it
> sorted first and excludes it from numbering.

> **FRD 0008** was decomposed into a parent overview + 14 sub-FRDs
> (`0008.1`–`0008.14`), each an independently iterable decision area; `0008` itself is
> the overview/index and the 70-row master Decisions log. All 15 docs are **Finalized**
> (whole-FRD human sign-off, 2026-07-27, after a 5-round rubber-duck validation).
>
> **Visual explainers.** A [Simple Guide hub](https://gistpreview.github.io/?97bbda5c5663c182c82431588fd0a539)
> links to a rendered deep-dive for every sub-FRD (via gistpreview). The source HTML lives at
> [`docs/aca-sandbox-session-runtime-flow.html`](../aca-sandbox-session-runtime-flow.html)
> and under [`docs/frds/explainers/`](explainers/).
