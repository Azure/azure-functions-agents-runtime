/**
 * Scaffold for a skill discovered from an app's `skills/` folder.
 *
 * The runtime loads `skills/<name>/SKILL.md`. Its front matter carries `name`
 * (conventionally the folder name) and `description` (used by the model to
 * decide when to load the skill); the markdown body is the skill's instructions.
 */

/** Convert an arbitrary label into a kebab-case skill folder name. */
export function toSkillSlug(raw: string): string {
  let slug = raw
    .trim()
    .replace(/([A-Z]+)([A-Z][a-z])/g, "$1-$2")
    .replace(/([a-z0-9])([A-Z])/g, "$1-$2")
    .toLowerCase()
    .replace(/[^a-z0-9-]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-+|-+$/g, "");
  if (slug === "") {
    slug = "my-skill";
  }
  return slug;
}

export interface SkillTemplateOptions {
  /** Skill name (normalized to a kebab-case slug used for the folder + name). */
  name: string;
  /** Description that tells the model when to use this skill. */
  description: string;
}

function titleCase(slug: string): string {
  return slug
    .split("-")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

/** Build the contents of a `SKILL.md` file. */
export function buildSkillMarkdown(opts: SkillTemplateOptions): string {
  const slug = toSkillSlug(opts.name);
  const description = (opts.description || `Skill: ${slug}`).trim();
  const title = titleCase(slug);
  return `---
name: ${slug}
description: ${description}
---

# ${title}

Describe what this skill helps the agent do, and when it should be used.

## Instructions

1. Explain the first step the agent should take.
2. Add any tools, APIs, or data the agent needs.

## Tips

- Add guidance, examples, or constraints that improve results.
`;
}
