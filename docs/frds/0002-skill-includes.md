---
frd: 0002
title: Skill file includes
status: Finalized
author: victoriahall
created: 2026-06-29
updated: 2026-06-29
issues: []
pull_requests: []
branch: victoriahall/skill-includes
---

# FRD 0002 — Skill file includes

## 1. Summary

Add an `{{include:path}}` directive to SKILL.md files that inlines content from
referenced files (relative to the skill directory) during discovery. This
enables skills to reference external assets and documentation without manually
copying content, keeping SKILL.md files maintainable while allowing rich,
modular instruction sets.

## 2. Motivation / problem

Today, a skill's instructions must be entirely self-contained within the
SKILL.md file. For skills with extensive reference material (API specs, code
examples, diagrams descriptions), this leads to either:

1. **Bloated SKILL.md files** — thousands of lines mixing core instructions with
   reference material, making maintenance difficult.
2. **Incomplete skills** — authors omit useful reference material to keep files
   manageable, reducing skill effectiveness.

Authors want to organize skills with supporting files:

```
my-skill/
├── SKILL.md              # Core instructions
├── assets/               # Images, diagrams, data files
│   └── workflow.png
└── references/           # Extended documentation
    ├── api-spec.md
    └── examples.md
```

MAF's `read_skill_resource` tool already allows agents to read files within the
skill directory at runtime. However, authors cannot pre-populate instructions
with reference content — they must either inline everything or rely on the agent
to discover and read resources dynamically.

The `{{include:path}}` directive bridges this gap by resolving includes at
discovery time, so the skill's full instructions (including referenced content)
are available when the skill loads.

## 3. Goals / Non-goals

**Goals**
- Support `{{include:relative/path}}` syntax in SKILL.md body content
- Resolve includes relative to the skill directory (not app root)
- Process includes recursively (included files can themselves include)
- Detect and fail on circular includes
- Preserve existing `${ENV_VAR}` substitution (includes processed first)
- Keep discovery read-only (no side effects beyond reading files)

**Non-goals**
- Front matter includes (only body content is processed)
- Glob patterns or wildcards (`{{include:references/*.md}}`)
- Conditional includes or templating logic
- Binary file embedding (text files only)
- Cross-skill includes (`{{include:../other-skill/file.md}}` rejected)

## 4. Proposed design

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| discover | `discovery/skills.py` | Add `_resolve_includes()` to process `{{include:path}}` in skill content |
| discover | `discovery/skills.py` | Add `prepare_resolved_skills()` to write resolved skills to temp directory |
| translate | — | No change |
| register | `registration/capabilities.py` | Call `prepare_resolved_skills()` to get temp paths for skills with includes |
| execute | — | No change (MAF reads resolved content from temp paths) |

### Detailed design

**Challenge:** MAF's `SkillsProvider.from_paths()` reads `SKILL.md` files from
disk. We cannot inject resolved content in memory — MAF will re-read the
original file. The solution is to write resolved skills to a temp directory.

**Resolution flow:**

1. `discover_skills()` — unchanged, returns `dict[str, Path]` of skill
   directories (original paths).

2. `_resolve_includes(content, skill_dir)` — new helper that:
   - Regex matches `{{include:path}}` patterns in skill body content
   - Resolves each path relative to the skill directory
   - Rejects paths that escape the skill directory (security)
   - Reads referenced files as UTF-8 text
   - Recursively resolves nested includes (tracking visited paths for cycles)
   - Returns the fully resolved content

3. `prepare_resolved_skills(skills)` — new function that:
   - For each skill, checks if `SKILL.md` contains `{{include:}}` directives
   - If yes: creates a temp directory, copies the skill directory, writes
     resolved `SKILL.md`, returns the temp path
   - If no: returns the original path (no copy overhead)
   - Registers cleanup via `atexit` for the temp directory
   - Returns `dict[str, Path]` with temp paths where needed

4. Registration calls `prepare_resolved_skills()` before passing paths to MAF.

### Include syntax

```
{{include:relative/path/to/file.md}}
```

- Path is relative to the skill directory (where SKILL.md lives)
- No leading `/` (rejected as absolute path)
- Whitespace around path is trimmed: `{{include: path }}` works
- File extension is preserved (not assumed to be `.md`)

### Error handling

| Condition | Behavior |
| --- | --- |
| File not found | `ValueError` at startup (fail fast) |
| Path escapes skill directory | `ValueError` at startup |
| Circular include | `ValueError` at startup |
| Non-UTF-8 file | `ValueError` at startup |
| Empty path | `ValueError` at startup |

### Authoring / API surface

**New SKILL.md syntax (body only):**
```markdown
---
name: my-api-skill
description: Skill for interacting with the Foo API
---

# Foo API Skill

Use this skill when working with Foo API endpoints.

## API Reference

{{include:references/api-spec.md}}

## Examples

{{include:references/examples.md}}
```

**Supported directory structure:**
```
skills/
└── my-api-skill/
    ├── SKILL.md
    ├── assets/
    │   └── diagram.png       # Accessible via read_skill_resource
    └── references/
        ├── api-spec.md       # Can be included
        └── examples.md       # Can be included
```

### Compatibility

- **Backward compatible:** Skills without `{{include:}}` directives work unchanged.
- **No breaking changes:** Existing discovery API returns the same types, just
  with includes resolved in the content passed to MAF.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Include syntax | `{{include:path}}`, `{!path!}`, `{% include %}` | `{{include:path}}` | Human | 2026-06-29 |
| 2 | Processing stage | Discovery (eager), runtime (lazy via MAF) | Discovery (eager) | Agent | 2026-06-29 |
| 3 | Path resolution base | App root, skill directory | Skill directory | Agent | 2026-06-29 |
| 4 | Circular include handling | Warn and skip, error and fail | Error and fail | Agent | 2026-06-29 |
| 5 | MAF integration | In-memory injection, temp directory with resolved files | Temp directory — MAF's SkillsProvider reads from disk | Agent | 2026-06-29 |
| 6 | Temp directory cleanup | Manual, atexit, context manager | atexit — process-lifetime management | Agent | 2026-06-29 |

## 6. Test plan

- [ ] Unit: `test_discovery_skills.py` — `test_resolve_includes_*` (basic, nested, circular, path escape, file not found)
- [ ] Unit: `test_discovery_skills.py` — `test_prepare_resolved_skills_*` (with/without includes, temp paths)
- [ ] Fixture scenario: `tests/fixtures/config_scenarios/14_skill_includes/`

## 7. Docs impact

- [ ] `docs/architecture.md` — Update skills.py description to mention include resolution
- [ ] `docs/front-matter-spec.md` — Document `{{include:path}}` syntax in skills section
- [ ] `README.md` — Add example of skill with includes in the skills section

## 8. Status & sign-off

- **Architecture review (phase 2):** Design updated to use temp directory approach for MAF integration (Agent, 2026-06-29)
- **Human sign-off:** User requested implementation (2026-06-29) → `status: Finalized`
