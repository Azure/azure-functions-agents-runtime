import assert from 'node:assert/strict'
import test from 'node:test'

import { resolveConfiguredModelSettings } from '../src/custom-tools.js'

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