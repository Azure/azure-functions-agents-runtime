---
frd: 0012
title: Runtime workflow and sandbox tool namespace
status: Finalized
author: larohra
created: 2026-10-15
updated: 2026-10-15
issues: []
pull_requests: []
branch: larohra/runtime-tool-namespace
---

# FRD 0012 - Runtime workflow and sandbox tool namespace

## 1. Summary

Give the five runtime workflow-management tools and the sandbox execution tool
an explicit `fhs_` prefix. Both harnesses expose the same names. Authored tools
retain their names, including names previously used by these internal tools.

## 2. Motivation / problem

The runtime injects workflow-management and sandbox tools under ordinary names.
An author can independently choose `start_workflow` or `execute_python`. These
names then collide with runtime injection. Renaming the internal tools removes
that collision without restricting ordinary authored names.

## 3. Goals / Non-goals

**Goals**
- Expose the six canonical names below in both MAF and Copilot.
- Allow authored unprefixed names alongside enabled internal tools.
- Reject exact collisions with an internal model-visible name only when that
  internal tool is enabled for the agent and role.
- Keep prompts, UI notifications, tool evidence, and documentation consistent.
- Preserve workflow task authorization and existing capability filters.

**Non-goals**
- Rename `web_request`, `delegate_<slug>`, MCP functions, or arbitrary tools.
- Rename Python functions, exported workflow helper APIs, configuration keys,
  HTTP routes, workflow operation IDs, or persisted session identifiers.
- Introduce name aliases, SDK-specific naming, a registry framework, or a
  general tool-authoring normalization layer.
- Rewrite user-authored prompts or historical session records.

## 4. Proposed design

### Canonical model-visible names

| Previous internal name | Canonical internal name |
| --- | --- |
| `start_workflow` | `fhs_start_workflow` |
| `get_workflow_status` | `fhs_get_workflow_status` |
| `list_workflows` | `fhs_list_workflows` |
| `cancel_workflow` | `fhs_cancel_workflow` |
| `terminate_workflow` | `fhs_terminate_workflow` |
| `execute_python` | `fhs_execute_python` |

The prefix distinguishes runtime ownership; it is not an authorization boundary.
It meets the current Copilot adapter's tool-name constraint
`[A-Za-z_][A-Za-z0-9_-]{0,63}`. No colon-containing alias is exposed.

| Pipeline stage | Modules | Contract |
| --- | --- | --- |
| discover | `discovery/tools.py` | Preserve authored names; do not rewrite or classify them by prefix. |
| translate | `config/merge.py`, `registration/capabilities.py`, `workflows/integration.py` | Preserve exact authored names in filters and workflow policies. Compute collisions from effective capabilities. |
| register | `registration/capabilities.py`, `workflows/registry.py`, `harness/copilot_sdk/_copilot_preview.py` | Reject exact active model-tool collisions with source/name context before invocation; reserve canonical workflow control-plane names as workflow node targets. |
| execute | `workflows/tools.py`, `system_tools/sandbox.py`, both harness adapters | Inject canonical descriptor names while retaining callable APIs, arguments, results, session binding, and lazy resource acquisition. |
| cross-cutting | `workflows/integration.py`, `public/index.html`, harness evidence/telemetry | Emit canonical internal names in runtime-authored instructions and newly recorded tool-call names. |

Use the existing SDK-neutral `ToolDescriptor` and descriptor builders. Keep name
definitions shared by injection and validation without importing sandbox Azure
dependencies during startup. Small constants are sufficient; no new naming
service or adapter is needed.

`_runtime_tool_names.py` is the dependency-free owner of the six constants and
the five-name workflow-management set. Injection, prompt generation, and
workflow node restrictions import these constants. Do not put naming behavior
in `_tool_descriptor.py` or `_function_tool.py`.

One small shared validation function in `registration/capabilities.py` compares
effective authored model-tool names against the internal names enabled for the
current role. Both harnesses call it before adapting or invoking tools. Startup
qualification may use the same function for a known direct-role configuration,
but must not globally reserve names on catalog entries used only in leaf roles.
Direct chat/trigger requests include their enabled workflow-management and
sandbox names. Delegated, workflow-subagent, and HostedSkill execution use their
existing role-specific tool selection: validate only internal tools actually
injected there, not every capability on the source agent. Do not change which
tools a role receives as part of this rename. Generic authored duplicates and
unrelated collisions keep their existing checks; MCP names only known after
remote expansion remain the adapter's responsibility.

### Authoring / API surface

The six model-visible names change. Config schemas, enable/disable behavior,
tool argument schemas, and result envelopes do not change.

`tools.exclude` continues to match authored chat tools by their exact names.
`workflows.exclude` continues to match authored workflow task targets, not
management tools. Neither filter translates legacy names into `fhs_` names.
An authored workflow task called `start_workflow` becomes an ordinary allowable
task target, subject to the existing agent policy.

The five canonical workflow-management names remain prohibited as workflow node
targets. This prevents plans from invoking the workflow control plane and does
not reserve old names or the entire `fhs_` prefix. Sandbox execution is not a
workflow-management operation; do not add its canonical name to that node-target
restriction merely because it shares a prefix.

For model-visible chat tools, an authored name equal to a canonical internal
name is rejected when that internal tool is enabled. The same authored name
remains eligible when that internal capability is disabled. Existing duplicate
authored-tool checks and unrelated collision protections remain intact.
Remote MCP function collisions cannot be fully known before expansion; retain
existing SDK/adapter validation rather than inventing remote discovery.

### Compatibility

This is an intentional rename with no unprefixed internal aliases. Existing
agent instructions, authored skills, or external tool-name selectors that intend
to invoke internal tools must migrate to the canonical names. The runtime does
not rewrite author content: an old name may now intentionally identify a user
tool. Copilot custom-tool selectors use `custom:fhs_<operation>`.

Runtime-generated workflow guidance, chat completion notifications, parameter
descriptions referring to other tools, and examples use canonical names.
Packaged skills are reviewed and updated only where they name these tools;
workflow plan grammar and authored task descriptions remain unchanged.

New tool-call evidence and tool-name telemetry expose canonical names naturally
from the descriptors. Preserve existing result/error interpretation and
operational metric/span identities that are not model-tool-name fields.
Historical evidence is not rewritten and old conversations are not promised
transparent resumption across this rename.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Internal naming and collision compatibility | `fhs::`; `fhs_`; aliases; reserve old names | `fhs_` for the five workflow-management tools and sandbox tool; exact enabled internal-name collisions only; no old-name aliases; unprefixed authored names stay free | Human (larohra) | 2026-10-15 |
| 2 | Authoring filters | Translate legacy names; retain exact authored identities | Retain exact authored tool/task identities and filters; do not translate excludes | Human (larohra) | 2026-10-15 |
| 3 | Evidence compatibility | Legacy labels; canonical labels; rewrite history | Canonical names in new tool evidence; retain non-tool operational identities and historical evidence | Human (larohra) | 2026-10-15 |
| 4 | Workflow node safety | Reserve all prefixed names; reserve canonical control-plane names only | Reserve the five canonical workflow-management names as task targets; release old control-plane names | Human (larohra) | 2026-10-15 |

## 6. Test plan

- Descriptor tests assert the exact six names, unchanged schemas and invocation,
  and absence of old internal aliases.
- Shared capability tests cover each active prefixed collision, an unrelated
  `fhs_` authored name, disabled internal capabilities, tools-disabled behavior,
  excluded authored collisions, and unaffected delegate/web-request checks.
- For both harnesses, explicitly contrast direct-role active collisions with
  delegated/workflow-subagent/HostedSkill role selection: a prefixed authored
  name is allowed whenever its matching internal tool is not injected.
- MAF and Copilot assembly tests expose authored `start_workflow`,
  `execute_python`, and other old management names beside canonical internals.
  Copilot validates canonical names and `custom:` selectors.
- Workflow registry tests accept old management names as authored task targets,
  reject the five canonical control-plane targets, and preserve ordinary task
  exclusions and per-agent allowlists.
- Add a config scenario under `tests/fixtures/config_scenarios/` proving literal
  authored names and filters remain unchanged with workflows/sandbox configured.
- Prompt and Debug UI notification tests assert canonical status-tool guidance,
  including batched completion notifications; never rewrite authored text.
- Result/telemetry regression tests assert canonical tool names in new evidence
  and unchanged tool-error/result interpretation, including sandbox stderr.
- Preserve sandbox session binding, workflow fire-and-forget semantics, and
  workflow Python helper API tests. Update only actual model-tool references.
- Audit old-name matches across runtime prompts, UI, current docs, examples,
  and packaged skills. Remaining matches must be intentional Python APIs,
  authored tools/task targets, migration notes, or historical material.
- Run `python -m ruff check src tests`, `python -m mypy src`, and
  `python -m pytest --cache-clear --cov=./src/azure_functions_agents --cov-report=xml --cov-branch tests`.
  No cloud calls or dependency installation are required by this design.

## 7. Docs impact

- `docs/architecture.md`: tool names, shared descriptor/collision ownership.
- `docs/front-matter-spec.md`: internal names, exact filters, migration examples.
- `docs/workflows.md`: management names versus authored task targets.
- `docs/observability.md`: model tool-name examples, not unrelated metric renames.
- Relevant samples, README/onboarding pages, evaluation examples, and packaged
  skills: change only references that actually mean runtime model tools.
- `docs/frds/README.md`: catalog this FRD. Historical FRDs stay historical.
- No schema change is intended. If implementation requires one, regenerate the
  schema reference and run `update-schema-docs` before completion.

## 8. Status & sign-off

- **Architecture review:** Dedicated review identified missing shared naming
  ownership and role-sensitive collision enforcement. The design now specifies
  dependency-free constants and one shared validation function driven by each
  role's actual injected tools. Role-specific tests and reference auditing added.
- **Human sign-off:** Full FRD approved by larohra on 2026-10-15.
