import assert from 'node:assert/strict'
import test from 'node:test'

import {
  callAgentChat,
  discoverFoundry,
  formatGeneratedAgentInstructions,
  openAgentChatStream,
  resolveSubscriptionId,
} from '../src/azure.js'
import { resolveConfiguredModelSettings } from '../src/custom-tools.js'
import { storageAccountName } from '../src/provision.js'

test('configured model follows runtime provider detection precedence', () => {
  assert.deepEqual(
    resolveConfiguredModelSettings({
      AZURE_OPENAI_ENDPOINT: 'https://example.openai.azure.com/',
      FOUNDRY_PROJECT_ENDPOINT: 'https://example.services.ai.azure.com/api/projects/demo',
      OPENAI_API_KEY: 'secret',
      AZURE_OPENAI_DEPLOYMENT: 'azure-deployment',
    }),
    {
      provider: 'azure_openai',
      model: 'azure-deployment',
      endpoint: 'https://example.openai.azure.com/',
      apiKey: '',
    },
  )
})

test('explicit provider and provider-specific model override runtime defaults', () => {
  const resolved = resolveConfiguredModelSettings({
    AZURE_FUNCTIONS_AGENTS_PROVIDER: 'foundry',
    FOUNDRY_PROJECT_ENDPOINT: 'https://example.services.ai.azure.com/api/projects/demo',
    FOUNDRY_MODEL: 'foundry-deployment',
    AZURE_FUNCTIONS_AGENTS_MODEL: 'runtime-default',
  })

  assert.equal(resolved.provider, 'foundry')
  assert.equal(resolved.model, 'foundry-deployment')
  assert.equal('OPENAI_API_KEY' in resolved, false)
})

test('configured model reports missing and incomplete configuration', () => {
  assert.throws(() => resolveConfiguredModelSettings({}), /no configured model provider/)
  assert.throws(
    () => resolveConfiguredModelSettings({ AZURE_FUNCTIONS_AGENTS_PROVIDER: 'openai' }),
    /configuration is incomplete/,
  )
})

test('generated instructions retain Markdown and format one-line prose as paragraphs', () => {
  assert.equal(
    formatGeneratedAgentInstructions('You are a reporter. Gather resources. Send a summary.'),
    'You are a reporter.\n\nGather resources.\n\nSend a summary.',
  )
  assert.equal(
    formatGeneratedAgentInstructions('## Role\r\n\r\n- Gather resources\r\n- Send a summary'),
    '## Role\n\n- Gather resources\n- Send a summary',
  )
})

test('prepared app storage naming is stable and Azure-safe', () => {
  const first = storageAccountName('11111111-1111-1111-1111-111111111111', 'rg-report', 'func-report')
  const retry = storageAccountName('11111111-1111-1111-1111-111111111111', 'rg-report', 'func-report')
  const other = storageAccountName('11111111-1111-1111-1111-111111111111', 'rg-report', 'func-other')

  assert.equal(first, retry)
  assert.notEqual(first, other)
  assert.match(first, /^[a-z0-9]{3,24}$/)
})

test('subscription GUID resolution does not require subscription enumeration', async () => {
  const subscription = '11111111-1111-4111-8111-111111111111'

  assert.equal(await resolveSubscriptionId('not-a-real-token', subscription), subscription)
})

test('Foundry discovery surfaces invalid ARM authentication as 401', async (t) => {
  const originalFetch = globalThis.fetch
  globalThis.fetch = async () => new Response(
    JSON.stringify({ error: { code: 'InvalidAuthenticationToken', message: 'Access token validation failure.' } }),
    { status: 401, headers: { 'Content-Type': 'application/json' } },
  )
  t.after(() => {
    globalThis.fetch = originalFetch
  })

  await assert.rejects(
    discoverFoundry('malformed-token', '11111111-1111-4111-8111-111111111111'),
    (error) => error.status === 401 && error.portalCode === 'invalid_arm_token',
  )
})

test('chatstream resolves a cached display name to the runtime agent slug', async () => {
  const requested = []
  const response = await openAgentChatStream(
    'stock-analysis-agent-im2x1.azurewebsites.net',
    'stock-analysis-agent-13',
    'Hello',
    {
      fetchImpl: async (url) => {
        requested.push(String(url))
        return new Response('data: {"type":"done"}\n\n', {
          status: String(url).includes('/agents/stock_analysis_agent_13/chatstream') ? 200 : 404,
          headers: { 'Content-Type': 'text/event-stream' },
        })
      },
    },
  )

  assert.equal(response.status, 200)
  assert.match(requested[0], /\/agents\/stock_analysis_agent_13\/chatstream$/)
})

test('non-streaming chat resolves a cached display name to the runtime agent slug', async () => {
  const requested = []
  const result = await callAgentChat(
    'stock-analysis-agent-im2x1.azurewebsites.net',
    'stock-analysis-agent-13',
    'Hello',
    {
      fetchImpl: async (url) => {
        requested.push(String(url))
        return new Response(JSON.stringify({ response: 'OK', tool_calls: [] }), {
          status: String(url).includes('/agents/stock_analysis_agent_13/chat') ? 200 : 404,
          headers: { 'Content-Type': 'application/json' },
        })
      },
    },
  )

  assert.equal(result.response, 'OK')
  assert.match(requested[0], /\/agents\/stock_analysis_agent_13\/chat$/)
})