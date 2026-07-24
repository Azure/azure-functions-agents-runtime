// Deploy a compiled workflow to Azure Functions — on behalf of the customer.
//
// In production this runs under the PORTAL's own identity (a managed identity or
// service principal with rights on the customer's target resource group), builds
// a package from `compileWorkflow(doc)`, and ships it via `azd`/`func` /
// zip-deploy. The customer never handles credentials — the portal controls the
// deployment.
//
// This implementation SIMULATES the deploy so the full loop works locally with
// no Azure calls. It records a deployment envelope (steps + status + resulting
// endpoints) on the workflow document. The real driver slots in behind
// `runDeploySteps` without changing callers.

import { compileWorkflow } from './compiler.js'

function endpointsFor(doc) {
  const host = doc.target?.functionApp
    ? `${doc.target.functionApp}.azurewebsites.net`
    : 'app.azurewebsites.net'
  const eps = []
  for (const n of doc.nodes) {
    if (n.type === 'trigger' && n.kind === 'httpTrigger') {
      eps.push({ label: n.name, url: `https://${host}/${(n.config?.route || 'run').replace(/^\//, '')}`, method: (n.config?.methods?.[0] || 'POST') })
    }
    if (n.type === 'agent' && n.config?.builtinEndpoints) {
      const slug = (n.config?.sourceFile || n.name).replace(/\.agent\.md$/, '').toLowerCase().replace(/[^a-z0-9]+/g, '_')
      eps.push({ label: `${n.name} chat UI`, url: `https://${host}/agents/${slug}/`, method: 'GET' })
    }
  }
  return eps
}

// The ordered steps a real deploy would perform; each returns a short log line.
function planSteps(doc) {
  const files = compileWorkflow(doc)
  return [
    { key: 'compile', title: 'Compile graph to project', detail: `${Object.keys(files).length} files generated` },
    { key: 'package', title: 'Build deployment package', detail: 'zip artifact assembled' },
    { key: 'provision', title: 'Ensure Function App + storage', detail: doc.target?.functionApp || 'func-agents-new (created on your behalf)' },
    { key: 'settings', title: 'Apply app settings', detail: `AZURE_FUNCTIONS_AGENTS_PROVIDER=${doc.target?.provider || 'foundry'}` },
    { key: 'connectors', title: 'Wire connectors', detail: connectorSummary(doc) },
    { key: 'deploy', title: 'Zip-deploy to Azure Functions', detail: 'uploaded and restarted' },
    { key: 'verify', title: 'Verify endpoints', detail: `${endpointsFor(doc).length} endpoint(s) live` },
  ]
}

function connectorSummary(doc) {
  const connectors = new Set()
  for (const n of doc.nodes) {
    if (n.config?.connector) connectors.add(n.config.connector)
  }
  return connectors.size ? [...connectors].join(', ') : 'none'
}

/** Produce a completed deployment envelope (simulated). */
export function deployWorkflow(doc) {
  const startedAt = new Date().toISOString()
  const steps = planSteps(doc).map((s) => ({ ...s, status: 'done' }))
  return {
    status: 'succeeded',
    onBehalfOfCustomer: true,
    startedAt,
    finishedAt: new Date().toISOString(),
    target: {
      functionApp: doc.target?.functionApp || 'func-agents-new',
      resourceGroup: doc.target?.resourceGroup || 'rg-serverless-agents',
      provider: doc.target?.provider || 'foundry',
    },
    steps,
    endpoints: endpointsFor(doc),
  }
}
