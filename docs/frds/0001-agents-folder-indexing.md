---
frd: 0001
title: agents/ folder indexing
status: Finalized
author: victoriahall
created: 2026-06-25
updated: 2026-06-25
issues: []
pull_requests: []
branch: victoriahall/agents-folder-indexing
---

# FRD 0001 — agents/ folder indexing

## 1. Summary

Introduce support for an optional `agents/` folder at the app root, allowing
customers to organize their `*.agent.md` files in a dedicated directory instead
of (or in addition to) placing them at the top level. This mirrors the existing
`tools/` and `skills/` directory conventions.

## 2. Motivation / problem

Currently, all `*.agent.md` files must reside at the app root level. As projects
grow beyond 3–5 agents, the top-level directory becomes cluttered with agent
markdown files mixed alongside `function_app.py`, `agents.config.yaml`,
`requirements.txt`, and other project files. This makes navigation harder and
doesn't match the organizational patterns already established for tools and
skills.

## 3. Goals / Non-goals

**Goals**
- Allow `*.agent.md` files to live in a top-level `agents/` folder
- Maintain backward compatibility — existing top-level agents continue to work
- Support a hybrid model where some agents are top-level and some are in `agents/`
- Mirror the discovery pattern used by `skills/` and `tools/`

**Non-goals**
- Recursive discovery (no nested `agents/subdir/*.agent.md`)
- New frontmatter fields or config keys
- Changes to how skills, tools, or MCP discovery works
- Support for `agents/` folders at arbitrary nested locations

## 4. Proposed design

The change is localized to the **translate** stage of the pipeline. The
`load_agent_specs()` function in `config/loader.py` currently only globs
`{app_root}/*.agent.md`. After this change it will also search
`{app_root}/agents/*.agent.md` (case-insensitive folder name, matching the
`skills/` discovery pattern).

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| discover | — | No change |
| translate | `config/loader.py` | `load_agent_specs()` searches both top-level and `agents/` folder |
| register | — | No change |
| execute | — | No change |

### Authoring / API surface

**New directory convention:**
```
my-agent-app/
├── host.json
├── function_app.py
├── agents.config.yaml
├── agents/                    # NEW: optional
│   ├── email-assistant.agent.md
│   └── calendar-bot.agent.md
├── main.agent.md              # Still supported at top-level
├── tools/
└── skills/
```

- The `agents/` folder must be at the same level as `host.json` (app root).
- Both `agents/` and `Agents/` are recognized (case-insensitive, matching skills).
- Only immediate children are discovered (no recursive descent).
- `main.agent.md` in either location is marked `is_main=True`.

### Compatibility

- **Fully backward-compatible**: projects with top-level agents only continue
  to work unchanged.
- **Additive**: projects can adopt the folder incrementally by moving agents
  one at a time.
- **No deprecation**: top-level agents are not deprecated.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Location of `agents/` folder | A) Same level as `host.json`, B) Allow nested | A — consistency with tools/skills | User | 2026-06-25 |
| 2 | Recursive search | A) Yes, B) No | B — keep simple, match tools/skills | User | 2026-06-25 |
| 3 | Case sensitivity | A) Exact `agents/`, B) Case-insensitive | B — match skills discovery | User | 2026-06-25 |
| 4 | Duplicate handling | A) Error, B) Load both with warning | Both loaded; if same source_file stem, last wins per existing behavior | User | 2026-06-25 |

## 6. Test plan

- [ ] Unit: extend `test_config_loader.py` with tests for:
  - Loading agents from `agents/` folder only
  - Loading agents from both top-level and `agents/` folder (hybrid)
  - Empty `agents/` folder with top-level agents
  - `main.agent.md` in `agents/` folder marked `is_main=True`
  - No agents in either location returns empty list
- [ ] Fixture scenario: `tests/fixtures/config_scenarios/13_agents_folder/`
  - Demonstrates agents/ folder with multiple agents

## 7. Docs impact

- Update `docs/architecture.md` §4.3 (Load all agent markdown files) to mention
  the `agents/` folder.
- Update `docs/front-matter-spec.md` if it mentions file locations.
- Update `README.md` quickstart structure diagram.

## 8. Status

Finalized — ready for implementation.
