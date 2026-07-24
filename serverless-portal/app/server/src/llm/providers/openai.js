// OpenAI / Azure OpenAI completion provider (optional).
//
// Activated by COMPOSER_MODEL_PROVIDER=openai|azure-openai. Uses the global
// fetch (Node 18+) so no SDK dependency is added. Credentials come from the
// environment; if they're missing the provider throws a clear error at call
// time (the generator falls back to the mock provider before that happens when
// no provider is configured).
//
// This provider knows nothing about workflows — it only relays the skill's
// rendered system prompt and the user's request, and returns the raw text. The
// generator parses/validates the JSON. Model and skill stay decoupled.

export function createOpenAiProvider({ flavor }) {
  const isAzure = flavor === 'azure'

  function config() {
    if (isAzure) {
      const endpoint = process.env.AZURE_OPENAI_ENDPOINT
      const apiKey = process.env.AZURE_OPENAI_API_KEY
      const deployment = process.env.AZURE_OPENAI_DEPLOYMENT || 'gpt-4o'
      const apiVersion = process.env.AZURE_OPENAI_API_VERSION || '2024-08-01-preview'
      if (!endpoint || !apiKey) {
        throw new Error('Azure OpenAI requires AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY.')
      }
      return {
        url: `${endpoint.replace(/\/$/, '')}/openai/deployments/${deployment}/chat/completions?api-version=${apiVersion}`,
        headers: { 'Content-Type': 'application/json', 'api-key': apiKey },
        model: deployment,
      }
    }
    const apiKey = process.env.OPENAI_API_KEY
    const model = process.env.OPENAI_MODEL || 'gpt-4o'
    if (!apiKey) throw new Error('OpenAI requires OPENAI_API_KEY.')
    return {
      url: 'https://api.openai.com/v1/chat/completions',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${apiKey}` },
      model,
    }
  }

  return {
    describe() {
      const model = isAzure
        ? process.env.AZURE_OPENAI_DEPLOYMENT || 'gpt-4o'
        : process.env.OPENAI_MODEL || 'gpt-4o'
      return { id: isAzure ? 'azure-openai' : 'openai', model, note: 'Live model generation.' }
    },
    async complete({ system, user, json }) {
      const cfg = config()
      const body = {
        model: cfg.model,
        messages: [
          { role: 'system', content: system },
          { role: 'user', content: user },
        ],
        temperature: 0.2,
      }
      if (json) body.response_format = { type: 'json_object' }
      const res = await fetch(cfg.url, { method: 'POST', headers: cfg.headers, body: JSON.stringify(body) })
      if (!res.ok) {
        const detail = await res.text().catch(() => '')
        throw new Error(`Model call failed (${res.status}): ${detail.slice(0, 300)}`)
      }
      const data = await res.json()
      return data?.choices?.[0]?.message?.content ?? ''
    },
  }
}
