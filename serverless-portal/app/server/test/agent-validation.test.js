import assert from 'node:assert/strict'
import test from 'node:test'

import {
  validateAgentFiles,
  validateAgentMarkdown,
} from '../src/agent-validation.js'
import { shouldExposeDiscoveredApp } from '../src/azure.js'

test('deployment validation rejects an agent missing its required description', () => {
  const result = validateAgentMarkdown(`---
name: stock-analysis-agent-11
builtin_endpoints: true
---

Analyze a stock.`)

  assert.equal(result.ok, false)
  assert.deepEqual(result.errors, [
    { path: '/description', message: 'description is required and must be a non-empty string.' },
  ])
})

test('deployment validation accepts a described agent with built-in endpoints', () => {
  const result = validateAgentMarkdown(`---
name: stock-analysis-agent-11
description: Analyze a public stock ticker.
builtin_endpoints: true
---

Analyze a stock.`)

  assert.equal(result.ok, true)
  assert.deepEqual(result.errors, [])
})

test('deployment bundle validation rejects invalid overlaid agent source', () => {
  const result = validateAgentFiles([
    { name: 'host.json', data: Buffer.from('{}') },
    {
      name: 'agents/invalid.agent.md',
      data: Buffer.from(`---
name: invalid
builtin_endpoints: true
---

Test.`),
    },
  ])

  assert.equal(result.ok, false)
  assert.equal(result.failures[0].file, 'agents/invalid.agent.md')
  assert.equal(result.failures[0].errors[0].path, '/description')
})

test('portal-managed app shells stay hidden until indexing or deployment completes', () => {
  assert.equal(shouldExposeDiscoveredApp({ portalManaged: true, preparationId: '', deploymentComplete: false, indexedAgentCount: 0 }), false)
  assert.equal(shouldExposeDiscoveredApp({ portalManaged: true, preparationId: '', deploymentComplete: false, indexedAgentCount: 1 }), true)
  assert.equal(shouldExposeDiscoveredApp({ portalManaged: true, preparationId: '', deploymentComplete: true, indexedAgentCount: 0 }), true)
  assert.equal(shouldExposeDiscoveredApp({ portalManaged: false, preparationId: 'draft-123', deploymentComplete: false, indexedAgentCount: 0 }), false)
  assert.equal(shouldExposeDiscoveredApp({ portalManaged: false, preparationId: '', deploymentComplete: false, indexedAgentCount: 0 }), true)
})