---
frd: 0008
title: Universal harness runtime with capability-based compaction
status: Finalized
author: victoriahall
created: 2026-08-19
updated: 2026-08-19
issues: []
pull_requests: []
branch: hallvictoria/harness-agent
---

# FRD 0008 — Universal harness runtime with capability-based compaction

## 1. Summary

The runtime will construct every direct, delegated, and workflow-subagent execution through MAF's
`create_harness_agent`. Agent authors will no longer select the internal MAF constructor with a
`harness` setting. Instead, they may configure the user-meaningful `compaction` capability with
model-appropriate context and output token limits. The runtime will keep conservative parity
settings for all other harness features.

## 2. Motivation / problem

Conservatively configured harness agents without compaction provide an experience equivalent to the
current plain-agent model, while harness agents with compaction provide significant customer value
by bounding model-facing conversation context for long-running sessions. Maintaining both plain and
harness construction would create separate execution paths in the runner, duplicate construction and
dispatch behavior, expand the test/support matrix, and allow direct agents to differ from delegated
and workflow-subagent executions without a corresponding customer benefit.

MAF harness construction is now a pinned runtime dependency and can provide plain-agent parity when
its optional providers and instructions are disabled. The runtime can therefore own one construction
contract while exposing compaction independently.

## 3. Goals / Non-goals

**Goals**

- Use `create_harness_agent` for all direct, delegated, and workflow-subagent model execution.
- Replace public `harness` configuration with optional `compaction` configuration.
- Keep empty harness instructions and disable todo, mode, file-memory, web-search, and tool-approval
  features so universal harness adoption does not introduce unevaluated behavior.
- Preserve tool, skill, history, session, agent-name, streaming, and observability behavior.
- Fail with an actionable compatibility error if the pinned MAF package does not expose
  `create_harness_agent`; never silently fall back to plain `Agent`.
- Keep global compaction inheritance and a per-agent capability opt-out.

**Non-goals**

- Enable additional MAF harness providers or default instructions.
- Infer model token limits or provide universal token defaults.
- Persist compacted projections in place of the authoritative full Blob/File history.
- Add a hidden plain-agent rollback path; deployment/package rollback remains the operational escape
  hatch.
- Change public session IDs, history concurrency guarantees, tool filtering, or workflow policy.

## 4. Proposed design

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| discover | None | No discovery changes. |
| translate | `config/schema.py`, `config/merge.py` | Replace `HarnessAgentConfig` and `harness` fields with `CompactionConfig` and `compaction`; resolve global inheritance and per-agent `false` opt-out. |
| register | `registration/_handlers.py`, `registration/endpoints.py` | Forward resolved compaction capability rather than constructor selection. |
| execute | `runner.py` | Delete plain-agent construction/fallback and mode dispatch; use one conservative harness constructor for direct and leaf agents. |
| dependency | `pyproject.toml` | Verify the exact MAF core pin includes `create_harness_agent`; no change is required while `agent-framework-core==1.13.0` remains pinned. |

### Authoring / API surface

The exact schema is:

- `GlobalConfig.compaction: CompactionConfig | None`
- `AgentSpec.compaction: CompactionConfig | Literal[False] | None`
- `ResolvedAgent.compaction_config: CompactionConfig | None`

An agent-level object fully replaces the global object, `false` disables inherited global
compaction, and omission inherits the global value. Global `false`, all `true` values, and partial
objects are rejected. Both `CompactionConfig` fields are required positive integers, with a model
validator requiring `max_output_tokens < max_context_window_tokens`.

Global configuration may enable compaction for all agents:

```yaml
# agents.config.yaml
compaction:
  max_context_window_tokens: 128000
  max_output_tokens: 16000
```

An agent may provide its own compaction limits or opt out of a global compaction policy:

```yaml
---
name: Short-lived agent
description: Uses universal harness execution without conversation compaction
compaction: false
---
```

When neither global nor agent configuration supplies compaction, the runtime still uses the harness
constructor but passes no token limits, so MAF's default compaction strategy remains disabled.
`CompactionConfig` requires both positive limits and requires `max_output_tokens` to be less than
`max_context_window_tokens`. `max_output_tokens` also becomes the model's output-token limit, as it
does in MAF's default harness compaction configuration.

The removed `harness` key is rejected by Pydantic's existing `extra="forbid"` configuration. Removed
harness implementation controls (`disable_mode`, `disable_file_memory`, and previously removed
`harness_instructions`/`disable_todo`) are not moved under `compaction`.

### Execution contract

One internal helper constructs MAF harness agents with these runtime-owned settings:

- `harness_instructions=""`
- `disable_todo=True`
- `disable_mode=True`
- `disable_file_memory=True`
- `disable_web_search=True`
- `disable_tool_auto_approval=True`
- `default_options={"store": False}`

The helper always forwards `name=agent_name` and the assembled tool list, including an explicit empty
list. Role-specific inputs remain explicit:

| Concern | Direct trigger/endpoint | Delegated specialist | Workflow sub-agent |
| --- | --- | --- | --- |
| Constructor | Conservative harness helper | Same helper | Same helper |
| History | Runtime Blob/File provider | Fresh in-memory provider | Fresh in-memory provider |
| Session | Explicit public request session | No caller session; single task | No caller session; single Activity task |
| `store` | `false` | `false` | `false` |
| Tools | User, MCP, sandbox, web request, workflow, declared delegates as authorized | Specialist user/MCP/web-request tools only | Specialist user/MCP/web-request tools only |
| Skills | Agent-filtered skill paths | Specialist-filtered paths | Specialist-filtered paths |
| Delegation | Declared direct `subagents` only | None (single-level invariant) | None (workflow policy owns dispatch) |
| Compaction | Resolved direct-agent config | Specialist's resolved config; normally inert for one task | Specialist's resolved config; normally inert for one task |

Universal harness construction necessarily adds MAF's message-injection middleware,
per-service-call persistence behavior, and harness telemetry identity relative to the old plain
constructor. The conservative settings suppress the optional providers evaluated by this feature;
the unavoidable harness semantics are accepted as the new single runtime contract.

### Compatibility validation

`create_harness_agent` is imported during package/runner initialization. If the installed MAF package
does not expose it, initialization raises an actionable compatibility error naming the required
pinned version. The incompatibility cannot be converted into a request-time fallback or streaming
SSE error. The existing exact `agent-framework-core==1.13.0` pin satisfies this requirement.

### Compatibility

This is an intentional authoring break:

- Delete `harness: true`; harness construction is universal.
- Delete `harness: false`; plain-agent execution is no longer supported.
- Rename a `harness` object containing token limits to `compaction` and remove all non-compaction
  fields.
- Use `compaction: false` on an agent only when opting out of inherited global compaction.

The exported Python APIs `run_agent()` and `run_agent_stream()` replace the implementation-oriented
`harness_config=` keyword with `compaction_config=`. Direct callers omit it for universal harness
execution without compaction or pass `CompactionConfig` to enable compaction. No compatibility alias
is retained.

Applications without either key retain no-compaction behavior, but their internal constructor changes
from plain `Agent` to conservatively configured `create_harness_agent`.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Public abstraction | Expose constructor / expose capability | Remove `harness`; expose `compaction` | Human | 2026-08-19 |
| 2 | Runtime constructor | Plain by default / dual mode / universal harness | Universal harness | Human | 2026-08-19 |
| 3 | Harness optional features | Upstream defaults / conservative parity | Empty instructions; todo, mode, file-memory, web-search, and approval disabled | Human | 2026-08-19 |
| 4 | MAF compatibility | Silent plain fallback / fail fast | Fail fast with actionable pinned-version error | Human | 2026-08-19 |
| 5 | Internal execution roles | Direct only / all roles | Harness for direct, delegated, and workflow-subagent execution | Agent | 2026-08-19 |
| 6 | Global compaction opt-out | No opt-out / per-agent `false` | Support per-agent `compaction: false` as a capability-level opt-out | Agent | 2026-08-19 |
| 7 | Operational rollback | Hidden plain path / package rollback | No second execution path; roll back the deployment/package | Agent | 2026-08-19 |
| 8 | Token limits | Optional fields / required pair | Require both positive limits and output less than context | Agent | 2026-08-19 |
| 9 | Public Python API | Retain `harness_config` alias / rename cleanly | Replace with `compaction_config` and reject the old keyword | Agent | 2026-08-19 |
| 10 | Leaf state | Reuse direct history / fresh in-memory state | Preserve fresh, single-task leaf state while using the same harness constructor | Agent | 2026-08-19 |
| 11 | Unavoidable harness semantics | Hide / document and accept | Accept message injection, per-call persistence, and harness telemetry as the universal contract | Agent | 2026-08-19 |

## 6. Test plan

- [ ] Unit: schema accepts valid `compaction` objects and rejects removed `harness` keys, missing or
  invalid limits, and unsupported compaction values.
- [ ] Unit: merge resolves global compaction, per-agent override, per-agent opt-out, and no-compaction
  defaults.
- [ ] Fixture scenario: add a config scenario covering global compaction inheritance, per-agent
  override, and opt-out.
- [ ] Unit: registration forwards resolved compaction configuration for trigger and endpoint paths.
- [ ] Unit: direct streaming and non-streaming calls always use harness construction, with and without
  compaction limits.
- [ ] Unit: package/runner initialization without `create_harness_agent` raises an actionable
  compatibility error before request or SSE handling, with no plain fallback.
- [ ] Regression: all roles forward their agent name; empty tool lists remain empty at the factory
  boundary and on the resulting MAF agent, including clients that store history by default.
- [ ] Unit/integration: delegated and workflow-subagent roles use conservative harness construction,
  preserve role-specific tools and skills, use fresh isolated history, and do not gain nested
  delegation.
- [ ] Unit: public `run_agent()` / `run_agent_stream()` accept `compaction_config` and no longer expose
  `harness_config`.
- [ ] Existing history/compaction tests continue to prove full provider storage and compacted model
  context.
- [ ] Full CI-equivalent Ruff, mypy, and pytest-with-coverage gate.

## 7. Docs impact

- [ ] `docs/architecture.md` — describe universal harness execution and capability-based compaction.
- [ ] `docs/front-matter-reference.md` — regenerate from the new schema.
- [ ] `docs/front-matter-spec.md` — replace `harness` examples and migration guidance with
  `compaction`.
- [ ] `docs/index.md` / `docs/getting-started.md` — review; update only if their current onboarding
  mentions constructor selection or compaction.
- [ ] `README.md` — document the public configuration migration if harness configuration is present.
- [ ] `samples/harness-chat` and `samples/README.md` — replace the plain-versus-harness comparison
  with a universal-harness compaction demonstration and update sample indexes.
- [ ] `docs/frds/README.md` — add FRD 0008 to the index.
- [ ] `docs/triggers.md` — no change expected.

## 8. Status & sign-off

- **Architecture review (phase 2):** Independent review requested changes for Python API migration,
  exact schema precedence, role-specific history/session semantics, startup failure timing, sample
  migration, and parity coverage. All findings were incorporated; independent re-review approved
  the revised design with no blockers on 2026-08-19.
- **Human sign-off:** victoriahall, 2026-08-19. Approved the reviewed design for implementation.