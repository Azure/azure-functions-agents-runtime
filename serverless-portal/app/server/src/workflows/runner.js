// Simulate a run of a workflow for the run surface.
//
// Walks the graph from the trigger along the edges and produces a per-node trace
// the UI renders as a live timeline. Deterministic and offline — it does not
// invoke the deployed app. When the app is deployed for real, this is where a
// live invocation + trace fetch would plug in.

function orderNodes(doc) {
  const byId = new Map(doc.nodes.map((n) => [n.id, n]))
  const trigger = doc.nodes.find((n) => n.type === 'trigger')
  const ordered = []
  const seen = new Set()
  // Breadth-first from the trigger following edges; skills/tools are attached to
  // the agent they feed, so surface them inline before that agent.
  const queue = trigger ? [trigger.id] : doc.nodes.map((n) => n.id)
  while (queue.length) {
    const id = queue.shift()
    if (seen.has(id)) continue
    seen.add(id)
    const node = byId.get(id)
    if (!node) continue
    // Emit upstream tool/skill dependencies first.
    for (const e of doc.edges.filter((x) => x.to === id)) {
      const dep = byId.get(e.from)
      if (dep && (dep.type === 'tool' || dep.type === 'skill') && !seen.has(dep.id)) {
        seen.add(dep.id)
        ordered.push(dep)
      }
    }
    ordered.push(node)
    for (const e of doc.edges.filter((x) => x.from === id)) queue.push(e.to)
  }
  // Include any nodes not reached (defensive).
  for (const n of doc.nodes) if (!seen.has(n.id)) ordered.push(n)
  return ordered
}

function sampleOutput(node, inputs) {
  switch (node.type) {
    case 'trigger':
      return `Received payload: ${JSON.stringify(inputs).slice(0, 120)}`
    case 'skill':
      return `Loaded ${node.name} knowledge`
    case 'tool':
      return `${node.name}() → { ok: true }`
    case 'agent':
      return `${node.name} produced a structured result`
    case 'output':
      return `${node.name}: side effect performed`
    default:
      return 'ok'
  }
}

export function simulateRun(doc, inputs = {}) {
  const ordered = orderNodes(doc)
  const startedAt = Date.now()
  let t = 0
  const steps = ordered.map((n) => {
    const ms = n.type === 'agent' ? 800 : n.type === 'tool' ? 300 : 120
    t += ms
    return {
      nodeId: n.id,
      type: n.type,
      kind: n.kind,
      name: n.name,
      status: 'done',
      elapsedMs: t,
      output: sampleOutput(n, inputs),
    }
  })
  return {
    status: 'completed',
    startedAt: new Date(startedAt).toISOString(),
    durationMs: t,
    inputs,
    steps,
  }
}
