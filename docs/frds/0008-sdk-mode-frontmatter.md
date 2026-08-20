---
frd: 0008
title: SDK and execution mode front matter
status: Finalized
author: victoriahall
created: 2026-08-20
updated: 2026-08-20
issues: []
pull_requests: []
branch: hallvictoria/harness-agent
---

# FRD 0008 — SDK and execution mode front matter

## 1. Summary

Agent front matter will expose an optional `sdk` discriminator and an explicit execution `mode`
instead of the implementation-oriented top-level `harness` key. `sdk` currently accepts only `maf`
and defaults to `maf` when omitted. `mode: default` preserves plain MAF agent execution, while a
nested `mode.harness` object selects MAF harness execution and contains optional conversation-
compaction token limits. The selected mode follows the agent across direct, delegated, and workflow
sub-agent execution.

## 2. Motivation / problem

Harness agents without compaction can provide an experience equivalent to the current plain-agent
model, while harness agents with compaction offer significant customer value for long-running
conversations. The authoring model should make that execution choice explicit without presenting a
top-level `harness` implementation switch that cannot naturally accommodate other SDKs or execution
modes in the future.

An SDK discriminator gives the format a stable extension point, and grouping harness-only settings
under the selected mode keeps unrelated configuration out of the agent's top-level namespace. The
runtime can retain one typed resolution boundary and avoid duplicating mode interpretation in
registration or execution.

## 3. Goals / Non-goals

**Goals**

- Add optional agent front matter `sdk: maf`, with `maf` implied when omitted.
- Reject every explicit `sdk` value other than `maf` at load time.
- Support exactly two agent execution mode shapes: `mode: default` and a nested harness object.
- Keep harness compaction token limits under `mode.harness` while the runtime owns all other harness
  settings.
- Preserve the existing internal `HarnessAgentConfig | None` boundary consumed by registration and
  direct execution.
- Apply an agent's selected mode consistently in direct, delegated, and workflow sub-agent roles.
- Reject the unmerged top-level `harness` syntax so there is one public authoring shape.

**Non-goals**

- Support an SDK other than MAF.
- Select or load SDK packages dynamically.
- Add global `sdk` or `mode` defaults to `agents.config.yaml`.
- Infer token limits from the selected model.
- Change harness runtime behavior, history persistence, tools, skills, sessions, streaming, or
  observability.
- Make `sdk` mandatory for existing agent files.

## 4. Proposed design

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| discover | None | No discovery changes. |
| translate | `config/schema.py`, `config/merge.py` | Parse the SDK literal and mode union, reject unsupported shapes, and resolve harness configuration. Remove global and agent-level top-level `harness` fields. |
| register | None | Continue consuming `ResolvedAgent.harness_config`; no public-syntax interpretation. |
| execute | `runner.py` | Continue selecting direct execution from `harness_config`; extend the same selection to delegated and workflow sub-agents while preserving their role restrictions. |

### Authoring / API surface

The default execution path may be written explicitly:

```yaml
---
name: Support agent
description: Answers support questions
sdk: maf
mode: default
---
```

Because MAF is the only supported SDK, omitting `sdk` is equivalent to `sdk: maf`. Omitting `mode`
is equivalent to `mode: default`, preserving existing agent files.

Harness execution uses an object whose only key is `harness`:

```yaml
---
name: Support agent
description: Answers support questions across long conversations
sdk: maf
mode:
  harness:
    max_context_window_tokens: 8192
    max_output_tokens: 4096
---
```

An empty `harness: {}` object enables harness execution with the current harness defaults and no
  compaction token limits. The harness object exposes only:

- `max_context_window_tokens: int | None`
- `max_output_tokens: int | None`

  Both fields must be omitted or supplied together. Supplied values must be positive integers, and
  `max_output_tokens` must be less than `max_context_window_tokens`. The runtime keeps harness
  instructions empty and disables todo, mode, file-memory, web-search, and automatic tool approval;
  these implementation controls are not author-configurable.

  ### Schema definition

  The typed public schema is equivalent to:

  ```python
  class HarnessAgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_context_window_tokens: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)

    # An after-validator enforces all-or-none fields and output < context.


  class HarnessModeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    harness: HarnessAgentConfig


  class AgentSpec(BaseModel):
    sdk: Literal["maf"] = "maf"
    mode: Literal["default"] | HarnessModeConfig = "default"
  ```

  `AgentSpec.harness` and `GlobalConfig.harness` are removed. Both containing models already use
  `extra="forbid"`, so the old top-level key is rejected in agent front matter and
  `agents.config.yaml`. No compatibility alias or deprecation period is added for this unmerged
  syntax.

`HarnessModeConfig` and `HarnessAgentConfig` both forbid extra fields. Object forms without
`harness`, with additional sibling keys, or with a scalar `harness` value are invalid. Scalar
`mode: harness`, top-level `harness`, boolean modes, and unsupported SDK names are also invalid.

  ### Environment substitution

  The loader continues recursively substituting environment variables in front matter before
  Pydantic validation. A scalar `sdk: $AGENT_SDK` is valid only when substitution produces `maf`, and
  `mode: $AGENT_MODE` is valid only when substitution produces `default`. String substitution cannot
  synthesize a YAML mapping, so harness mode must remain a mapping in the source document. Environment
  variables may be used for the nested numeric limits; Pydantic performs the final integer and range
  validation. Unresolved placeholders and substituted unsupported values fail normal schema
  validation.

  ### Merge implementation

  `config.merge` replaces `_resolve_harness(spec, global_config)` with an agent-only translation:

  ```python
  def _resolve_mode(spec: AgentSpec) -> HarnessAgentConfig | None:
    if spec.mode == "default":
      return None
    return spec.mode.harness
  ```

  `compose()` assigns the result to the unchanged `ResolvedAgent.harness_config` field. Registration
  continues forwarding that internal field and does not inspect `sdk` or `mode`.

  ### Execution roles

  Direct trigger and endpoint execution continues selecting the plain or harness constructor from
  `ResolvedAgent.harness_config`. Delegated and workflow sub-agents currently always use plain
  construction; `runner.py` will extend their role-specific builder to make the same selection from
  the specialist's resolved configuration.

  Harness leaf agents retain fresh single-task state, their own model/instructions/tools/skills, no
  per-request sandbox, no workflow-management tools, and no nested delegation. They use the same
  runtime-owned conservative harness settings as direct agents, with no persistent history provider.
  The existing MAF compatibility fallback behavior remains unchanged. Registration and workflow
  dispatch require no authoring-syntax logic because both already supply each specialist's
  `ResolvedAgent` to the runner.

### Precedence

`sdk` and `mode` are agent-only fields. They are not accepted in `agents.config.yaml`, so there is
no global inheritance or per-agent opt-out behavior. An omitted mode and explicit `mode: default`
are equivalent. An agent selects harness execution only through its own nested harness object.

### Compatibility

Existing agent files without `sdk` or `mode` continue to use the current plain-agent path. Authors
may add `sdk: maf` for clarity without changing execution.

The top-level `harness: true | false | object` syntax exists only on the unmerged feature branch and
is replaced rather than deprecated. Pydantic's existing `extra="forbid"` behavior rejects it. The
migration is:

```yaml
# Before
harness:
  max_context_window_tokens: 8192
  max_output_tokens: 4096

# After
sdk: maf
mode:
  harness:
    max_context_window_tokens: 8192
    max_output_tokens: 4096
```

`harness: false` becomes `mode: default`; `harness: true` becomes `mode: { harness: {} }`.

Apps using a global `harness` value in `agents.config.yaml` must move the corresponding nested
`mode.harness` object into every agent that should use harness execution. Agents without that
front-matter object use `mode: default`. The authoring spec will carry this migration guidance; the
loader continues reporting its normal extra-field validation error rather than adding branch-only
special handling.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | SDK requirement | Required / optional with implicit MAF | Optional; default to `maf` | Human | 2026-08-20 |
| 2 | Configuration scope | Agent only / global and agent | Agent front matter only | Human | 2026-08-20 |
| 3 | Mode shapes | Scalar default plus nested harness / object for both | `mode: default` or `mode: { harness: {...} }` | Human | 2026-08-20 |
| 4 | Existing files | Require migration / preserve plain default | Omitted mode remains the plain-agent path | Agent | 2026-08-20 |
| 5 | Internal contract | Rename through all layers / preserve resolved harness config | Preserve `ResolvedAgent.harness_config` | Agent | 2026-08-20 |
| 6 | Old unmerged syntax | Compatibility alias / warning / reject | Reject top-level `harness` | Agent | 2026-08-20 |
| 7 | Execution roles | Direct only / every role | Apply each agent's mode in direct, delegated, and workflow sub-agent roles | Human | 2026-08-20 |
| 8 | Public harness settings | Token limits only / expose provider controls | Token limits only; runtime owns conservative provider settings | Human | 2026-08-20 |
| 9 | Compaction validation | Partial limits / required valid pair | Empty object disables compaction; otherwise require a positive pair with output less than context | Human | 2026-08-20 |
| 10 | Environment substitution | Disable for mode fields / preserve recursive substitution | Preserve string substitution, then validate the resulting scalar or nested values | Agent | 2026-08-20 |
| 11 | Migration errors | Custom loader hint / normal schema error plus docs | Use normal extra-field validation and document migration | Agent | 2026-08-20 |

## 6. Test plan

- [x] Unit: schema accepts omitted and explicit `sdk: maf`, and rejects unsupported SDK values and
  non-string forms.
- [x] Unit: schema accepts omitted and explicit default mode plus empty/configured harness objects.
- [x] Unit: schema rejects scalar `mode: harness`, booleans, malformed harness wrappers, extra mode
  keys, the removed top-level `harness` key, partial/non-positive token limits, and output limits
  greater than or equal to context limits.
- [x] Unit: loader substitutes `sdk`, scalar default mode, and nested token-limit environment values,
  then rejects unsupported or unresolved results.
- [x] Unit: merge resolves omitted/default mode to no harness config and preserves the nested
  harness configuration for harness mode.
- [x] Fixture scenario: add `tests/fixtures/config_scenarios/19_sdk_modes/` covering implicit and
  explicit MAF default agents plus empty and configured harness agents.
- [x] Unit: registration continues forwarding the resolved harness configuration without parsing
  authoring syntax.
- [x] Unit: direct, delegated, and workflow sub-agent roles select plain or harness construction from
  the executed agent's own mode while preserving role-specific tools, skills, state, and delegation
  restrictions.
- [x] Existing runner tests continue to cover direct plain and harness construction.
- [ ] Full CI-equivalent Ruff, mypy, and pytest-with-coverage gate. Ruff and mypy pass; pytest has one
  unchanged Foundry dependency assertion failure in
  `test_foundry_stateless_request_does_not_include_encrypted_content` (1,101 other tests pass).

## 7. Docs impact

- [x] `docs/architecture.md` — document SDK/mode resolution in the translation stage.
- [x] `docs/front-matter-reference.md` — regenerate from `schema.py`; do not edit manually.
- [x] `docs/front-matter-spec.md` — replace top-level harness syntax with SDK/mode examples and
  validation rules, including global-harness migration guidance.
- [x] `docs/index.md` / `docs/getting-started.md` — review onboarding examples; update only where the
  execution mode is relevant.
- [x] `README.md` — document the optional SDK discriminator and harness mode example.
- [x] Existing samples — demonstrate `sdk: maf` and nested harness mode where harness execution is
  useful; do not require SDK declarations in every sample.
- [x] Use the `update-schema-docs` skill after regenerating the reference to synchronize examples and
  review the docs-site onboarding pages.
- [x] `docs/frds/README.md` — add FRD 0008 to the index.
- [x] `docs/triggers.md` — no change required.

## 8. Status & sign-off

- **Architecture review (phase 2):** Independent review requested explicit schema models, field
  removal, environment-substitution semantics, merge pseudocode, global migration guidance, and
  validation of the no-runner-change assumption. The design now includes those details and scopes a
  runner change because delegated and workflow sub-agents currently bypass mode selection. An
  independent re-review found no blockers and approved the FRD for human sign-off on 2026-08-20.
- **Human sign-off:** victoriahall, 2026-08-20. Approved the reviewed design for implementation.