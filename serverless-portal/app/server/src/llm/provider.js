// Model-agnostic completion layer.
//
// The Composer's generation logic depends only on this small interface, never on
// a specific vendor. The active provider is chosen by environment
// (`COMPOSER_MODEL_PROVIDER`), defaulting to a credential-free `mock` provider so
// the portal generates workflows locally with no API keys. Swap in `openai` /
// `azure-openai` by setting the provider + credentials — no other code changes.
//
// A provider implements:
//   complete({ system, user, json }) -> Promise<string>
//   describe() -> { id, model, note }   (surfaced in the UI so users can see —
//                                        and confirm — which model is in use)
//
// Skills (prompts + domain knowledge) live entirely outside this layer, in
// ./skills. The generator loads a skill, renders its prompt, and hands the text
// to whichever provider is configured. Model and skill are intentionally
// decoupled.

import { createMockProvider } from './providers/mock.js'
import { createOpenAiProvider } from './providers/openai.js'
import { createAzureResponsesProvider } from './providers/azureResponses.js'

let cached = null

export function getModelProvider() {
  if (cached) return cached
  const kind = String(process.env.COMPOSER_MODEL_PROVIDER || 'mock').toLowerCase()
  switch (kind) {
    case 'openai':
      cached = createOpenAiProvider({ flavor: 'openai' })
      break
    case 'azure-openai':
    case 'azure_openai':
      cached = createOpenAiProvider({ flavor: 'azure' })
      break
    case 'azure-responses':
    case 'azure_responses':
      cached = createAzureResponsesProvider()
      break
    case 'mock':
    default:
      cached = createMockProvider()
      break
  }
  return cached
}

// Test/hook seam.
export function _setModelProvider(p) {
  cached = p
}
