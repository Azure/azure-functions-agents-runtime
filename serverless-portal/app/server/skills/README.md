# Portal authoring skills

These `SKILL.md` files are the portal's **authoritative knowledge** for generating
agent capabilities. When you generate a trigger, tool, or skill in the portal, the
matching skill below is injected into the model prompt so the output follows the
azure-functions-agents-runtime conventions.

| Skill | Grounds generation of | Capability kind |
|---|---|---|
| [`authoring-triggers/`](./authoring-triggers/SKILL.md) | Triggers (HTTP, timer, queue, connector, …) — declarative `.agent.md` | `http_trigger`, `connector_trigger` |
| [`authoring-custom-tools/`](./authoring-custom-tools/SKILL.md) | Custom Python tools in `tools/` | `custom_tool` |
| [`authoring-skills/`](./authoring-skills/SKILL.md) | Reusable `SKILL.md` knowledge files | `skill` |

## How it works

The backend reads the skill mapped to each capability `kind` on every
`/api/generate-capability` request (see `KIND_TO_PORTAL_SKILL` /
`readPortalSkill` in `app/server/src/index.js`) and prepends it to the model's
system prompt as authoritative guidance.

## Updating

Edit these files to control and enhance how the portal generates capabilities —
changes take effect on the next generation (no restart needed; they are read per
request). They use the same `SKILL.md` format the runtime auto-discovers, so they
can also be copied into an app's own `skills/` folder.

To add a new authoring skill, create `skills/<kebab-name>/SKILL.md` and map a
capability `kind` to it in `KIND_TO_PORTAL_SKILL`.
