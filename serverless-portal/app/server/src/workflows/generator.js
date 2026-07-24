// Workflow generation: plain English -> component graph.
//
// This is where a SKILL (prompt + knowledge, from ../skills) meets a MODEL
// (provider, from ../llm) — kept as separate inputs on purpose:
//
//   skill  = loadSkill('composer-plan')      // prompt template + component catalog
//   model  = getModelProvider()              // mock | openai | azure-openai
//   plan   = model.complete(renderedPrompt)  // model fills the skill's contract
//
// The model returns a JSON plan; we normalize it into a persistable workflow
// document (ids, positions, config defaults, provenance). Swapping the model
// changes plan quality; editing the skill changes the planner's knowledge —
// neither touches this wiring.

import { getModelProvider } from '../llm/provider.js'
import { loadSkill, renderSkillPrompt } from '../skills/index.js'
import { newNodeId, newEdgeId } from './store.js'

const SKILL_NAME = 'composer-plan'

function stripFences(text) {
  const t = String(text || '').trim()
  const fenced = /^```(?:json)?\s*([\s\S]*?)\s*```$/m.exec(t)
  return (fenced ? fenced[1] : t).trim()
}

function coerceConfigForType(type, kind, config) {
  const c = { ...(config || {}) }
  if (type === 'agent') {
    c.sourceFile = c.sourceFile || 'agent.agent.md'
    c.instructions = c.instructions || ''
    c.skills = Array.isArray(c.skills) ? c.skills : []
    c.tools = Array.isArray(c.tools) ? c.tools : []
    c.builtinEndpoints = !!c.builtinEndpoints
  }
  if (type === 'tool') {
    c.signature = c.signature || 'def tool(x: str) -> str'
    c.code = c.code || `@tool\n${c.signature}:\n    """TODO."""\n    ...`
  }
  if (type === 'skill') {
    c.path = c.path || `skills/${(config?.name || 'skill')}/SKILL.md`
    c.summary = c.summary || ''
  }
  return c
}

// Turn a raw model plan into normalized nodes/edges/inputs with stable ids.
function normalizePlan(plan) {
  const rawNodes = Array.isArray(plan?.nodes) ? plan.nodes : []
  const rawEdges = Array.isArray(plan?.edges) ? plan.edges : []
  const rawInputs = Array.isArray(plan?.inputs) ? plan.inputs : []

  // Remap any duplicate/missing ids to fresh ones while preserving edge links.
  const idMap = new Map()
  const nodes = rawNodes.map((n, i) => {
    const type = n.type || 'agent'
    const oldId = n.id || `__${i}`
    const id = n.id && !idMap.has(n.id) ? n.id : newNodeId(type)
    idMap.set(oldId, id)
    return {
      id,
      type,
      kind: n.kind || type,
      name: n.name || `${type} ${i + 1}`,
      source: n.source || 'generated',
      position: n.position && typeof n.position.x === 'number' ? n.position : { x: i, y: 1 },
      config: coerceConfigForType(type, n.kind, n.config),
    }
  })

  const edges = rawEdges
    .map((e) => {
      const from = idMap.get(e.from) || e.from
      const to = idMap.get(e.to) || e.to
      if (!nodes.find((n) => n.id === from) || !nodes.find((n) => n.id === to)) return null
      return { id: e.id || newEdgeId(), from, to, label: e.label || '', condition: e.condition }
    })
    .filter(Boolean)

  const inputs = rawInputs.map((f, i) => ({
    id: f.id || `input_${i}`,
    label: f.label || `Input ${i + 1}`,
    type: ['text', 'textarea', 'file', 'choice', 'number', 'boolean'].includes(f.type) ? f.type : 'text',
    required: !!f.required,
    options: Array.isArray(f.options) ? f.options : undefined,
    placeholder: f.placeholder,
  }))

  return { nodes, edges, inputs }
}

function deriveName(prompt) {
  const words = String(prompt || '').replace(/\s+/g, ' ').trim().split(' ').slice(0, 6).join(' ')
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : 'New workflow'
}

/**
 * Generate a draft workflow document body from a plain-English prompt.
 * Returns the doc fields (not persisted) plus provenance; the caller stores it.
 */
export async function generateWorkflow({ prompt, name, target }) {
  const model = getModelProvider()
  const skill = await loadSkill(SKILL_NAME)
  const system = renderSkillPrompt(skill)

  let planText
  try {
    planText = await model.complete({ system, user: prompt, json: true })
  } catch (err) {
    const e = new Error(`Model generation failed: ${err.message}`)
    e.code = 'model_error'
    throw e
  }

  let plan
  try {
    plan = JSON.parse(stripFences(planText))
  } catch {
    const e = new Error('The model did not return a valid workflow plan. Try rephrasing the description.')
    e.code = 'bad_plan'
    throw e
  }

  const { nodes, edges, inputs } = normalizePlan(plan)
  const generatedIds = nodes.filter((n) => n.source !== 'reused').map((n) => n.id)
  const reusedIds = nodes.filter((n) => n.source === 'reused').map((n) => n.id)

  return {
    name: name || deriveName(prompt),
    description: String(prompt || '').slice(0, 240),
    prompt,
    target: target || {},
    inputs,
    nodes,
    edges,
    generation: {
      model: model.describe().model,
      provider: model.describe().id,
      skill: SKILL_NAME,
      generatedAt: new Date().toISOString(),
      generatedNodeIds: generatedIds,
      reusedNodeIds: reusedIds,
    },
  }
}
