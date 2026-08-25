---
frd: 0008
title: Harness-first agent configuration
status: Finalized
author: victoriahall
created: 2026-08-20
updated: 2026-08-25
issues: []
pull_requests: []
branch: hallvictoria/harness-agent
---

# FRD 0008 — Harness-first agent configuration

## 1. Summary

Every agent will execute through Microsoft Agent Framework's harness-agent mechanism. A new
`agent_configuration` object will be accepted globally and per agent, with per-agent values
recursively overriding inherited global values. Portable output limits live at
`agent_configuration.max_output_tokens`, while MAF-specific compaction settings live at
`agent_configuration.maf.compaction.max_context_window_tokens`.

## 2. Motivation / problem

Applications need an extensible configuration namespace that carries shared defaults and narrow
per-agent overrides. Portable controls should remain at the configuration root, while controls
whose semantics are specific to MAF should be namespaced under `maf`. The runtime should resolve
that authoring structure once during translation, pass a normalized contract through registration,
and execute every agent consistently through the harness-agent mechanism.

## 3. Goals / Non-goals

**Goals**

- Always construct direct, delegated, and Workflow Sub Agent roles with `create_harness_agent`.
- Add nullable `agent_configuration` fields to global configuration and agent front matter.
- Put `max_output_tokens` at the root of `agent_configuration`.
- Put `max_context_window_tokens` under `agent_configuration.maf.compaction`.
- Recursively merge per-agent authored fields over global fields.
- Distinguish omission from explicit `null`: omission and empty objects inherit; `null` clears.
- Validate token relationships after global and per-agent configuration are merged.
- Preserve role-specific history, tools, skills, sandbox, delegation, workflow, streaming, and
  observability behavior.

**Non-goals**

- Move the existing top-level `model` or `timeout` fields into `agent_configuration`.
- Expose additional harness controls such as todo, mode tools, file memory, web search, or tool
  auto-approval.
- Change inference-client selection, trigger behavior, endpoint authentication, or workflow policy.
- Add configuration for choosing an agent-construction mechanism.

## 4. Proposed design

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| discover | None | No discovery changes. |
| translate | `config/schema.py`, `config/merge.py` | Parse strict nested configuration at both scopes, recursively merge authored fields, validate the effective token limits, and emit a normalized resolved contract. |
| register | `registration/_handlers.py`, `registration/endpoints.py` | Forward the resolved agent configuration without interpreting authoring syntax. |
| execute | `runner.py` | Use harness construction unconditionally and map the resolved portable and MAF-specific limits to MAF parameters. |

### Authoring / API surface

Global defaults are authored in `agents.config.yaml`:

```yaml
agent_configuration:
  max_output_tokens: 4096
  maf:
    compaction:
      max_context_window_tokens: 8192
```

An agent may override individual inherited leaves in `.agent.md` front matter:

```yaml
---
name: Support agent
description: Answers support questions
agent_configuration:
  max_output_tokens: 2048
---
```

The typed public schema is equivalent to:

```python
class MafCompactionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_context_window_tokens: int | None = Field(default=None, gt=0)


class MafAgentConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    compaction: MafCompactionConfig | None = None


class AgentConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_output_tokens: int | None = Field(default=None, gt=0)
    maf: MafAgentConfiguration | None = None


class GlobalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_configuration: AgentConfiguration | None = None


class AgentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_configuration: AgentConfiguration | None = None
```

Strict extra-field handling rejects unsupported keys, including the out-of-scope
`agent_configuration.model` and `agent_configuration.timeout` shapes.

### Recursive precedence and null behavior

`config.merge` resolves each leaf by authored field presence, using Pydantic's `model_fields_set`
rather than truthiness or an untyped dictionary merge:

| Per-agent value | Effective result |
| --- | --- |
| `agent_configuration` omitted | Inherit the complete global configuration. |
| `agent_configuration: {}` | Inherit the complete global configuration because no fields were authored. |
| `agent_configuration: null` | Clear all inherited agent configuration. |
| Nested `maf` or `compaction` omitted / `{}` | Inherit that global subtree. |
| Nested `maf: null` or `compaction: null` | Clear that inherited subtree. |
| Leaf omitted | Inherit the corresponding global leaf. |
| Leaf `null` | Clear the corresponding global leaf. |
| Concrete leaf | Override the corresponding global leaf. |

The resolver returns a normalized `AgentConfiguration` even when every value is absent or cleared.
Harness construction does not depend on configuration presence.

Pydantic v2 preserves omission versus explicit `null` without loader side metadata. At the root,
`"agent_configuration" in spec.model_fields_set` distinguishes an omitted field from one authored
as `null`. At each object level, that nested model's own `model_fields_set` distinguishes an omitted
leaf/subtree from an explicitly authored value. The loader therefore remains responsible only for
parsing and environment substitution; it does not maintain a parallel authored-field structure.

The typed merge is equivalent to:

```python
def _resolve_agent_configuration(
  spec: AgentSpec, global_config: GlobalConfig
) -> AgentConfiguration:
  if "agent_configuration" not in spec.model_fields_set:
    return _copy_or_empty(global_config.agent_configuration)
  if spec.agent_configuration is None:
    return AgentConfiguration()
  return _merge_agent_configuration(
    spec.agent_configuration,
    global_config.agent_configuration,
  )


def _merge_optional_model(agent_value, global_value, field_name, merge_nested):
  if field_name not in agent_value.model_fields_set:
    return _deep_copy(global_value)
  authored = getattr(agent_value, field_name)
  if authored is None:
    return None
  return merge_nested(authored, global_value)
```

`_merge_agent_configuration`, `_merge_maf_configuration`, and `_merge_compaction` apply the same
rule recursively. Scalar leaves use the agent value, including `None`, only when their field name is
in the agent model's `model_fields_set`; otherwise they inherit the global leaf. An empty object has
an empty `model_fields_set`, so it inherits all leaves in its subtree. Copies are deep enough that
composition cannot mutate either parsed source model.

### Effective validation

Each authored numeric leaf must be a positive integer. Cross-field validation runs in
`config/merge.py` immediately after `_resolve_agent_configuration()` and before `compose()` returns
the `ResolvedAgent`. This is part of the side-effect-free translate stage, never registration or
execution. The authored schema models intentionally do not enforce a pair because a partial object
may become valid through inheritance. Effective validation enforces:

- `max_output_tokens` may be configured alone and limits model generation.
- `maf.compaction.max_context_window_tokens` requires an effective `max_output_tokens`.
- When both are effective, `max_output_tokens` must be less than
  `max_context_window_tokens`.

This permits an agent to inherit one half of the effective pair from global configuration while
overriding the other. Clearing an inherited output limit while retaining an inherited or overridden
context limit is invalid and fails composition before registration.

### Environment substitution

The existing recursive environment substitution remains unchanged for `agents.config.yaml` and
agent front matter. Environment variables may supply nested token values. Pydantic performs integer
and positivity validation before composition; the merge resolver validates the effective pair.
String substitution cannot synthesize a YAML mapping, so object structure remains authored in YAML.

### Resolved contract and registration

`ResolvedAgent.agent_configuration` is non-nullable and always contains a normalized
`AgentConfiguration`; an empty effective configuration is represented as
`AgentConfiguration(max_output_tokens=None, maf=None)`. Nested `maf` and `compaction` fields remain
optional because explicit clearing is meaningful. A single runner helper safely extracts the
context leaf through those optional subtrees, avoiding duplicated null checks. Registration
handlers and built-in endpoints forward the resolved object to `run_agent()` /
`run_agent_stream()` and do not inspect MAF-specific subtrees.

### Execution behavior

`runner.py` unconditionally calls `create_harness_agent` for every direct, delegated, and Workflow
Sub Agent role; there is no conditional based on configuration presence. The portable output limit maps to MAF's
`max_output_tokens`; the MAF compaction context limit maps to `max_context_window_tokens`. Missing
values are passed as `None`, preserving MAF defaults and disabling configured compaction when no
effective context limit exists.

The current runtime-owned harness controls remain unchanged: empty harness instructions, disabled
todo/mode/file-memory/web-search/tool-auto-approval, and `default_options={"store": False}`. Direct
agents retain the Blob/File history provider. Delegated and Workflow Sub Agent roles remain fresh,
stateless leaves without persistent history, sandbox tools, workflow-management tools, or nested
delegation. Their own filtered tools and skills remain available.

The pinned MAF dependency is required to expose `create_harness_agent`. The runtime imports it
directly at construction time; an incompatible installation raises a clear import/runtime error and
never silently changes execution semantics.

Configuration inheritance is authoring-scope inheritance, not invocation-tree inheritance. Each
delegated or Workflow Sub Agent executes with its own independently composed `ResolvedAgent`: its
agent-level values override only the global `agents.config.yaml` values. It never inherits
`agent_configuration` from the coordinator or workflow parent that invoked it.

### Compatibility

`agent_configuration` is a new optional authoring surface. Existing applications may omit it and
use MAF defaults. Existing top-level `model` and `timeout` fields remain valid and unchanged.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Execution implementation | Configurable construction / consistent harness construction | Always use MAF harness construction for every role | Human | 2026-08-25 |
| 2 | Configuration scope | Global only / agent only / global plus agent override | Accept `agent_configuration` globally and per agent | Human | 2026-08-25 |
| 3 | Portable output limit | MAF subtree / configuration root | `agent_configuration.max_output_tokens` | Human | 2026-08-25 |
| 4 | Context-limit namespace | Configuration root / MAF compaction subtree | `agent_configuration.maf.compaction.max_context_window_tokens` | Human | 2026-08-25 |
| 5 | Override behavior | Whole-object replacement / recursive authored-field merge | Recursively merge; omitted fields inherit | Human | 2026-08-25 |
| 6 | Explicit null | Reject / treat as omission / clear inheritance | `null` clears the selected inherited leaf or subtree | Human | 2026-08-25 |
| 7 | Effective token validation | Permit context alone / require output-context pair | Context requires effective output; output must be less than context | Human | 2026-08-25 |
| 8 | Model and timeout | Move under `agent_configuration` / retain top-level fields | Keep the existing top-level fields unchanged | Human | 2026-08-25 |
| 9 | Missing harness API | Continue without harness behavior / fail | Fail clearly; harness construction is required behavior | Agent | 2026-08-25 |

## 6. Test plan

- [x] Unit: global and per-agent schemas accept omitted, empty, partial, complete, and explicit-null
  `agent_configuration` shapes and reject unknown fields and invalid numeric leaves.
- [x] Unit: unsupported top-level and nested fields are rejected.
- [x] Unit: loader recursively substitutes global and per-agent token values and reports invalid or
  unresolved values through normal schema validation.
- [x] Unit: merge covers global inheritance, per-leaf overrides, mixed-scope effective pairs, empty
  object inheritance, null clearing at every level, output-only configuration, missing effective
  output, and invalid output/context ordering.
- [x] Unit: nested `maf: null` clears the full inherited MAF subtree while preserving the portable
  output leaf; omitted `maf` inherits the complete nested global subtree.
- [x] Unit: invalid environment substitutions fail authored schema validation before merge.
- [x] Fixture scenario: cover global defaults and per-agent inheritance, override, and opt-out
  cases.
- [x] Unit: trigger and built-in endpoint registration forward the normalized resolved configuration
  without parsing it.
- [x] Unit: direct, delegated, and Workflow Sub Agent roles always use harness construction and map
  each effective token limit to the correct MAF argument.
- [x] Unit: delegated and Workflow Sub Agent roles use their specialist's independently resolved
  global-plus-agent configuration and never inherit the invoking coordinator's configuration.
- [x] Unit: role-specific history, tools, skills, sandbox, workflow, delegation, streaming, usage,
  and observability behavior remains unchanged.
- [ ] Full CI-equivalent Ruff, mypy, and pytest-with-coverage gate.

## 7. Docs impact

- [x] `docs/architecture.md` — document recursive translation and unconditional harness execution.
- [x] `docs/front-matter-reference.md` — regenerate from `schema.py`; do not edit manually.
- [x] `docs/front-matter-spec.md` — document both scopes, merge/null semantics, and validation.
- [x] `README.md` — add the nested agent configuration example.
- [x] `docs/index.md` / `docs/getting-started.md` — reviewed; landing-page summary updated and optional configuration omitted from the quickstart.
- [x] `docs/triggers.md` — reviewed; no execution-config changes required.
- [x] Basic-chat sample — demonstrate global output limit inherited by per-agent MAF compaction.
- [x] Run the `update-schema-docs` skill after regenerating the reference.
- [x] `docs/frds/README.md` — add FRD 0008 to the index.

## 8. Status & sign-off

- **Architecture review (phase 2):** Independent review found the finalized design complete and
  aligned with the runtime pipeline, normalized resolved contract, harness construction behavior,
  and specialist configuration isolation. READY on 2026-08-25.
- **Human sign-off:** victoriahall, 2026-08-25. Decisions 1–9 approved for implementation.
- **Testing review (phase 4):** Independent review returned READY on 2026-08-25. Ruff, strict mypy,
  generated-reference checks, and strict MkDocs pass. The CI-equivalent test run completed with
  1,109 passing tests and one unrelated local failure caused by installed
  `agent-framework-foundry==1.10.4` differing from the branch's `1.10.3` pin; dependencies were not
  changed during this work.