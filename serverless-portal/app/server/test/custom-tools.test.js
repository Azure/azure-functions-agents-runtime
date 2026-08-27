import assert from 'node:assert/strict'
import test from 'node:test'

import {
  azureRoleScope,
  customToolPath,
  mergeRequirements,
  normalizeDistributionName,
  renderAzureRestTool,
  validateAzureRestSource,
} from '../src/custom-tools.js'

test('normalizes distribution names using PEP 503 rules', () => {
  assert.equal(normalizeDistributionName('Azure.Identity'), 'azure-identity')
  assert.equal(normalizeDistributionName('azure__identity'), 'azure-identity')
})

test('merges missing dependencies without replacing existing constraints', () => {
  const input = '# packages\naiohttp[speedups]==3.12; python_version >= "3.13"\nazure_identity @ https://example.test/wheel.whl\n'
  const result = mergeRequirements(input)

  assert.deepEqual(result.added, ['jmespath'])
  assert.equal(result.content, `${input}jmespath\n`)
})

test('requirements merge preserves CRLF and is idempotent', () => {
  const first = mergeRequirements('aiohttp\r\n')
  const second = mergeRequirements(first.content)

  assert.equal(first.content, 'aiohttp\r\nazure-identity\r\njmespath\r\n')
  assert.deepEqual(second, { content: first.content, added: [] })
})

test('requirements merge uses LF for empty and mixed files', () => {
  assert.equal(mergeRequirements('').content, 'aiohttp\nazure-identity\njmespath\n')
  assert.equal(mergeRequirements('one\r\ntwo\n').content, 'one\r\ntwo\naiohttp\nazure-identity\njmespath\n')
})

test('renders a discoverable Azure REST tool with the approved schema', () => {
  const source = renderAzureRestTool('azure rest')

  assert.equal(customToolPath('azure rest'), 'tools/azure_rest.py')
  assert.match(source, /@tool\(schema=AzureRestParams\)\nasync def azure_rest\(params: AzureRestParams\) -> str:/)
  assert.match(source, /Literal\["GET", "POST", "PUT", "PATCH", "DELETE"\]/)
  assert.match(source, /management\.azure\.com\/\.default/)
  assert.match(source, /api-version/)
})

test('rejects tool names that cannot form Python identifiers', () => {
  assert.throws(() => customToolPath('123'), /valid Python identifier/)
})

test('constructs only validated Azure RBAC scopes', () => {
  const subscription = '11111111-1111-1111-1111-111111111111'
  assert.equal(azureRoleScope(subscription, 'subscription'), `/subscriptions/${subscription}`)
  assert.equal(
    azureRoleScope(subscription, 'resourceGroup', 'rg-custom-tools'),
    `/subscriptions/${subscription}/resourceGroups/rg-custom-tools`,
  )
  assert.throws(() => azureRoleScope(subscription, 'resourceGroup', '../bad'), /valid resource group/)
})

test('validates edited Azure REST source', () => {
  const source = renderAzureRestTool('azure_rest')
  assert.equal(validateAzureRestSource(source, 'azure_rest'), source)
  assert.throws(() => validateAzureRestSource(`${source}\nexec('bad')`, 'azure_rest'), /execution APIs/)
  assert.throws(() => validateAzureRestSource(source.replace('@tool', ''), 'azure_rest'), /missing required/)
})