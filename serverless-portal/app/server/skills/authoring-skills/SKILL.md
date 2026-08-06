---
name: authoring-skills
description: How to author a reusable SKILL.md knowledge file that the azure-functions-agents-runtime auto-discovers from skills/ and an agent pulls in on demand.
---

# Authoring skills

A **skill** is reusable, durable knowledge — not code. The runtime auto-discovers
`skills/<kebab-name>/SKILL.md` and makes it available to agents.

## File shape

`skills/<name>/SKILL.md`:

```markdown
---
name: <kebab-case-name>
description: <one sentence: what the skill provides and when to use it>
---

# Title

Durable, reusable guidance: domain knowledge, step-by-step how-to, key
references / URLs / commands, best practices, and pitfalls.
```

- `name` must be kebab-case (`^[a-z0-9]([a-z0-9]*-[a-z0-9])*[a-z0-9]*$`, max 64
  chars) and should match the folder name.
- `description` is what an agent uses to decide whether to load the skill — make it
  specific about the capability and when to use it.

## Layout

```
skills/
  my-skill/
    SKILL.md        # required
    references/     # optional supporting docs the agent can read
    assets/         # optional files (templates, images)
```

## Filtering per agent

An agent's `.agent.md` can control which skills it sees:

```yaml
skills: false            # disable all skills for this agent
# or
skills:
  exclude: [some-skill]  # load all except these
```

## What makes a good skill

- Focused on one capability or domain.
- Actionable: concrete steps, commands, and examples over prose.
- Includes authoritative references (URLs, API versions, exact identifiers).
- Calls out common mistakes and how to avoid them.
