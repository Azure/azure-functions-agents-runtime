// Component code generation: node -> runtime code (stage 2 of generation).
//
// Where generator.js turns a prompt into a graph, this turns a single node in
// that graph into the code the runtime runs — a tool's Python body or an agent's
// instructions — using the `component-codegen` SKILL (prompt + contract) and the
// configured MODEL. Skill and model stay separate, exactly as in stage 1.

import { getModelProvider } from '../llm/provider.js'
import { loadSkill, renderSkillPrompt } from '../skills/index.js'

const SKILL_NAME = 'component-codegen'

function stripFences(text) {
  const t = String(text || '').trim()
  const fenced = /^```(?:json)?\s*([\s\S]*?)\s*```$/m.exec(t)
  return (fenced ? fenced[1] : t).trim()
}

function neighborNames(workflow, nodeId, dir) {
  const edges = workflow.edges || []
  const nodes = workflow.nodes || []
  const matches = edges.filter((e) => (dir === 'up' ? e.to === nodeId : e.from === nodeId))
  return matches
    .map((e) => {
      const other = nodes.find((n) => n.id === (dir === 'up' ? e.from : e.to))
      return other ? other.name : null
    })
    .filter(Boolean)
}

// The JSON spec the codegen skill consumes for one node.
function nodeSpec(node, workflow) {
  return JSON.stringify({
    type: node.type,
    kind: node.kind,
    name: node.name,
    signature: node.config?.signature,
    workflowName: workflow.name || '',
    workflowPrompt: workflow.prompt || '',
    upstream: neighborNames(workflow, node.id, 'up'),
    downstream: neighborNames(workflow, node.id, 'down'),
  })
}

/**
 * Generate implementation for one node. Returns { code } for tools or
 * { instructions } for agents. Not persisted — the caller merges it into the
 * (possibly unsaved) draft so the user can review/edit before saving.
 */
export async function generateComponentCode({ node, workflow }) {
  if (!node || !node.type) {
    const e = new Error('A node is required.')
    e.code = 'bad_request'
    throw e
  }
  const model = getModelProvider()
  const skill = await loadSkill(SKILL_NAME)
  const system = renderSkillPrompt(skill, {
    workflowName: workflow?.name || '',
    workflowPrompt: workflow?.prompt || '',
  })

  let raw
  try {
    raw = await model.complete({ system, user: nodeSpec(node, workflow || {}), json: true, task: 'codegen' })
  } catch (err) {
    const e = new Error(`Code generation failed: ${err.message}`)
    e.code = 'model_error'
    throw e
  }

  let parsed
  try {
    parsed = JSON.parse(stripFences(raw))
  } catch {
    const e = new Error('Code generation returned invalid output. Try again.')
    e.code = 'bad_codegen'
    throw e
  }

  return {
    code: typeof parsed.code === 'string' ? parsed.code : undefined,
    instructions: typeof parsed.instructions === 'string' ? parsed.instructions : undefined,
  }
}
