// Credential-free mock model provider.
//
// Stands in for a real LLM so the Composer works end-to-end with no API keys.
// Given the skill's rendered prompt (system) and the user's plain-English
// request (user), it returns a JSON plan in the exact shape the generator
// expects. It is a deterministic, keyword-driven planner — good enough to demo
// the full describe -> compose -> edit -> run loop offline, and a useful golden
// baseline for tests.
//
// The heuristics live here (in the "model"), while the vocabulary of components
// and the output contract live in the skill (./skills/composer-plan). Replacing
// this with a real provider changes only the quality of the plan, not the flow.

function has(text, words) {
  return words.some((w) => text.includes(w))
}

// Title-case a verb phrase into an agent name.
function titleCase(s) {
  return s.replace(/\b\w/g, (c) => c.toUpperCase())
}

// Split a request into clauses so each agent step can be grounded in the exact
// phrase the user wrote (this is what makes the offline plan prompt-specific
// rather than a fixed template).
function clausesOf(prompt) {
  return String(prompt)
    .split(/\s*(?:,|;|\.|\band then\b|\bthen\b|\band\b)\s*/i)
    .map((s) => s.trim())
    .filter((s) => s.length > 2)
}

function instructionFor(verb, name, clauses) {
  const clause = clauses.find((c) => c.toLowerCase().includes(verb))
  const task = clause ? clause.charAt(0).toUpperCase() + clause.slice(1) : `${titleCase(verb)} the incoming payload`
  return `You are the ${name} step. ${task}. Read the upstream payload, ${verb} it, and return a concise, structured result the next step can use.`
}

// Parse a Python signature "def name(a: T, b: U) -> R" into parts.
function parseSignature(sig) {
  const m = /def\s+(\w+)\s*\(([^)]*)\)\s*->\s*(.+)$/.exec(String(sig || '').trim())
  if (!m) return { fn: 'run', args: 'payload: dict', ret: 'dict' }
  return { fn: m[1], args: m[2].trim(), ret: m[3].trim() }
}

// Stage 2 (code generation): produce implementation for a single component from
// its spec + the workflow's intent. Deterministic here; a real model writes real
// code from the same skill prompt.
function codegenFromSpec(spec) {
  const { type, name, signature, workflowName, workflowPrompt, upstream } = spec || {}
  const intent = String(workflowPrompt || '').replace(/\s+/g, ' ').slice(0, 140)
  if (type === 'tool') {
    const { fn, args, ret } = parseSignature(signature)
    const argNames = args
      .split(',')
      .map((a) => a.split(':')[0].trim())
      .filter(Boolean)
    const code = [
      '@tool',
      `def ${fn}(${args}) -> ${ret}:`,
      `    """${name} — part of the "${workflowName}" workflow.`,
      '',
      `    ${intent || 'Perform the tool action.'}`,
      '    """',
      `    # Implements this step using: ${argNames.join(', ') || 'the inputs'}`,
      '    # TODO: call your API / data source and shape the result.',
      `    result: ${ret} = ...`,
      '    return result',
    ].join('\n')
    return { code }
  }
  const inbound = Array.isArray(upstream) && upstream.length ? upstream.join(' and ') : 'the input payload'
  const instructions = [
    `You are the ${name} agent in the "${workflowName}" workflow.`,
    '',
    `Goal: ${intent || 'process the input and produce a structured result'}.`,
    '',
    `You receive ${inbound}. Do your part of the task, be concise, and return a structured result the next step can use. If you are unsure, ask for the specific missing field rather than guessing.`,
  ].join('\n')
  return { instructions }
}

function detectTrigger(text) {
  if (has(text, ['outlook', 'email', 'inbox', 'mailbox', 'gmail', 'mail arrives', 'incoming mail']))
    return { kind: 'connectorTrigger', name: 'New email', config: { connector: 'outlook', event: 'messageReceived' } }
  if (has(text, ['every day', 'daily', 'each morning', 'weekday', 'schedule', 'cron', 'am ', 'pm ', 'hourly', 'nightly', 'every hour']))
    return { kind: 'timerTrigger', name: 'Scheduled', config: { schedule: '0 0 8 * * 1-5' } }
  if (has(text, ['service bus', 'servicebus']))
    return { kind: 'serviceBusTrigger', name: 'Service Bus message', config: { queue: 'incoming' } }
  if (has(text, ['queue']))
    return { kind: 'queueTrigger', name: 'Queue message', config: { queue: 'jobs' } }
  if (has(text, ['blob', 'file uploaded', 'file is uploaded', 'document uploaded', 'upload']))
    return { kind: 'blobTrigger', name: 'Blob uploaded', config: { path: 'incoming/{name}' } }
  if (has(text, ['github', 'issue', 'pull request', 'webhook', 'ado', 'azure devops']))
    return { kind: 'connectorTrigger', name: 'Webhook event', config: { connector: 'github', event: 'issueOpened' } }
  return { kind: 'httpTrigger', name: 'HTTP request', config: { route: 'run', methods: ['POST'] } }
}

// Verbs that indicate a reasoning/agent step, in a rough pipeline order.
const AGENT_VERBS = [
  ['classify', 'Classifier'],
  ['categorize', 'Categorizer'],
  ['triage', 'Triager'],
  ['extract', 'Extractor'],
  ['analyze', 'Analyst'],
  ['summarize', 'Summarizer'],
  ['reconcile', 'Reconciler'],
  ['review', 'Reviewer'],
  ['draft', 'Drafter'],
  ['generate', 'Generator'],
  ['reply', 'Reply Drafter'],
  ['respond', 'Responder'],
  ['route', 'Router'],
]

function detectAgents(text) {
  const found = []
  for (const [verb, name] of AGENT_VERBS) {
    if (text.includes(verb)) found.push({ verb, name })
  }
  if (found.length === 0) found.push({ verb: 'handle', name: 'Assistant' })
  // De-dupe by name, cap at 3 to keep the graph legible.
  const seen = new Set()
  return found.filter((a) => (seen.has(a.name) ? false : seen.add(a.name))).slice(0, 3)
}

function detectTools(text) {
  const tools = []
  if (has(text, ['look up', 'lookup', 'find the customer', 'customer record', 'crm']))
    tools.push({ name: 'lookup_customer', signature: 'def lookup_customer(email: str) -> dict' })
  if (has(text, ['fetch', 'get the', 'pull ', 'query', 'retrieve', 'cost', 'spend']))
    tools.push({ name: 'fetch_data', signature: 'def fetch_data(query: str) -> list[dict]' })
  if (has(text, ['search', 'web', 'browse']))
    tools.push({ name: 'web_search', signature: 'def web_search(q: str) -> list[dict]' })
  return tools
}

function detectSkills(text) {
  const skills = []
  if (has(text, ['taxonomy', 'grounded', 'tone', 'guidelines', 'guideline', 'policy', 'policies', 'knowledge', 'brand', 'style guide']))
    skills.push({ name: 'domain-knowledge', summary: 'Domain vocabulary, tone, and canned patterns the agents ground their output in.' })
  return skills
}

function detectOutput(text) {
  if (has(text, ['ticket', 'servicenow', 'incident', 'jira']))
    return { kind: 'connector', name: 'Open ticket', config: { connector: 'servicenow', action: 'createIncident' } }
  if (has(text, ['teams', 'slack', 'channel', 'post ']))
    return { kind: 'connector', name: 'Post message', config: { connector: 'teams', action: 'postMessage', channel: '#general' } }
  if (has(text, ['reply', 'send an email', 'respond', 'email back']))
    return { kind: 'email', name: 'Send reply', config: { via: 'outlook' } }
  if (has(text, ['save', 'store', 'database', 'ynab', 'record', 'write to']))
    return { kind: 'blob', name: 'Save result', config: { container: 'results' } }
  return { kind: 'http_response', name: 'Return response', config: {} }
}

// Build the plan JSON. Mirrors store schema node/edge shapes.
function planFromText(prompt) {
  const text = ` ${prompt.toLowerCase()} `
  const clauses = clausesOf(prompt)
  const nodes = []
  const edges = []
  let col = 0
  const link = (from, to, label) => edges.push({ from, to, label })

  const trig = detectTrigger(text)
  const triggerId = 'n_trigger'
  nodes.push({ id: triggerId, type: 'trigger', kind: trig.kind, name: trig.name, source: 'generated', position: { x: col++, y: 1 }, config: trig.config })

  const skills = detectSkills(text).map((s, i) => ({
    id: `n_skill_${i}`, type: 'skill', kind: 'skill', name: s.name, source: 'generated',
    position: { x: 1, y: 2 + i }, config: { path: `skills/${s.name}/SKILL.md`, summary: s.summary },
  }))
  const tools = detectTools(text).map((t, i) => ({
    id: `n_tool_${i}`, type: 'tool', kind: 'tool', name: t.name, source: 'generated',
    position: { x: 2, y: 2 + i },
    config: { signature: t.signature, code: `@tool\n${t.signature}:\n    """TODO: implement ${t.name}."""\n    ...` },
  }))

  const agentDefs = detectAgents(text)
  let prevId = triggerId
  const agentIds = []
  agentDefs.forEach((a, i) => {
    const id = `n_agent_${i}`
    agentIds.push(id)
    nodes.push({
      id, type: 'agent', kind: 'agent', name: a.name, source: 'generated',
      position: { x: ++col === 1 ? 1 : col, y: 1 },
      config: {
        sourceFile: `${a.name.toLowerCase().replace(/[^a-z0-9]+/g, '_')}.agent.md`,
        instructions: instructionFor(a.verb, a.name, clauses),
        skills: skills.map((s) => s.name),
        tools: [],
        builtinEndpoints: i === agentDefs.length - 1,
      },
    })
    link(prevId, id, i === 0 ? 'payload' : 'intermediate result')
    prevId = id
  })

  // Attach skills + tools to the agents and wire their edges.
  nodes.push(...skills, ...tools)
  for (const s of skills) for (const aid of agentIds) link(s.id, aid, 'knowledge')
  if (tools.length && agentIds.length) {
    const lastAgent = nodes.find((n) => n.id === agentIds[agentIds.length - 1])
    for (const t of tools) {
      link(t.id, lastAgent.id, 'result')
      if (!lastAgent.config.tools.includes(t.name)) lastAgent.config.tools.push(t.name)
    }
  }

  const out = detectOutput(text)
  const outId = 'n_output'
  nodes.push({ id: outId, type: 'output', kind: out.kind, name: out.name, source: 'generated', position: { x: ++col, y: 1 }, config: out.config })
  link(prevId, outId, 'final result')

  // Give edges ids.
  edges.forEach((e, i) => (e.id = `e${i + 1}`))

  // Suggest run-surface inputs based on the trigger.
  const inputs =
    trig.kind === 'httpTrigger' || trig.kind === 'connectorTrigger'
      ? [{ id: 'payload', label: 'Test input', type: 'textarea', required: true, placeholder: 'Paste sample input to test the workflow…' }]
      : [{ id: 'date', label: 'Run parameter (optional)', type: 'text', required: false, placeholder: 'e.g. a date or id' }]

  return { nodes, edges, inputs }
}

export function createMockProvider() {
  return {
    describe() {
      return { id: 'mock', model: 'heuristic-planner', note: 'Offline keyword planner — no API key required. Set COMPOSER_MODEL_PROVIDER=openai for real generation.' }
    },
    async complete({ user, task }) {
      // Stage 2: per-component code generation.
      if (task === 'codegen') {
        let spec = {}
        try {
          spec = JSON.parse(String(user || '{}'))
        } catch {
          spec = {}
        }
        return JSON.stringify(codegenFromSpec(spec))
      }
      // Stage 1: plan a workflow graph from the plain-English request.
      const plan = planFromText(String(user || ''))
      return JSON.stringify(plan)
    },
  }
}
