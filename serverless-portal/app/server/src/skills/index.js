// Skill loader for the Composer.
//
// A "skill" is a folder of PROMPT + KNOWLEDGE files — deliberately separate from
// the model (../llm). The generator loads a skill, renders its prompt template
// with runtime values (the component catalog + the user's request), and sends
// the result to whichever model provider is configured. Swapping the model does
// not touch skills; editing a skill does not touch the model.
//
// Skill folder layout:
//   skills/<name>/SKILL.md        human description + when-to-use (metadata)
//   skills/<name>/prompt.md       the system prompt template ({{catalog}} slot)
//   skills/<name>/components.json  optional domain knowledge (the catalog)

import { fileURLToPath } from 'node:url'
import path from 'node:path'
import fs from 'node:fs/promises'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

async function readIfExists(p) {
  try {
    return await fs.readFile(p, 'utf-8')
  } catch {
    return null
  }
}

/** List available skills (folder name + first paragraph of SKILL.md). */
export async function listSkills() {
  const entries = await fs.readdir(__dirname, { withFileTypes: true })
  const out = []
  for (const e of entries) {
    if (!e.isDirectory()) continue
    const md = await readIfExists(path.join(__dirname, e.name, 'SKILL.md'))
    const summary = md ? firstParagraph(md) : ''
    out.push({ name: e.name, summary })
  }
  return out
}

function firstParagraph(md) {
  const body = md.replace(/^---[\s\S]*?---\s*/m, '') // strip frontmatter
  const para = body.split(/\n\s*\n/).map((s) => s.trim()).find((s) => s && !s.startsWith('#'))
  return (para || '').replace(/\s+/g, ' ').slice(0, 200)
}

/** Load a skill's prompt template + knowledge. */
export async function loadSkill(name) {
  const dir = path.join(__dirname, name)
  const [promptTemplate, componentsRaw, skillMd] = await Promise.all([
    readIfExists(path.join(dir, 'prompt.md')),
    readIfExists(path.join(dir, 'components.json')),
    readIfExists(path.join(dir, 'SKILL.md')),
  ])
  if (!promptTemplate) throw new Error(`Skill '${name}' is missing prompt.md`)
  let components = null
  if (componentsRaw) {
    try {
      components = JSON.parse(componentsRaw)
    } catch {
      components = null
    }
  }
  return { name, promptTemplate, components, description: skillMd ? firstParagraph(skillMd) : '' }
}

/** Render a skill's prompt template, substituting {{catalog}} and {{...}} slots. */
export function renderSkillPrompt(skill, values = {}) {
  const catalog = skill.components ? JSON.stringify(skill.components, null, 2) : '(none)'
  const merged = { catalog, ...values }
  return skill.promptTemplate.replace(/{{\s*(\w+)\s*}}/g, (_, key) =>
    key in merged ? String(merged[key]) : '',
  )
}
