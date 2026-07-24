// Azure OpenAI **Responses API** provider.
//
// Targets the `/openai/responses` endpoint (distinct request/response shape from
// chat-completions). Activated by COMPOSER_MODEL_PROVIDER=azure-responses.
// Credentials come from the environment (loaded from a gitignored .env) — never
// hard-coded:
//   AZURE_OPENAI_RESPONSES_URL  full endpoint incl. ?api-version=…
//   AZURE_OPENAI_API_KEY        resource key (sent as the `api-key` header)
//   AZURE_OPENAI_DEPLOYMENT     the model deployment name (Responses `model`)
//
// Knows nothing about workflows — it relays the skill's system prompt + the
// user's request and returns raw text; the generator parses/validates JSON.

// Pull the assistant text out of a Responses API payload. The `output` array may
// contain reasoning items before the message; we take the first message's
// concatenated output_text parts. `output_text` is used when present.
function extractResponsesText(data) {
  if (data && typeof data.output_text === 'string' && data.output_text) return data.output_text
  const output = Array.isArray(data?.output) ? data.output : []
  for (const item of output) {
    if (item?.type === 'message' && Array.isArray(item.content)) {
      const text = item.content
        .filter((c) => c && (c.type === 'output_text' || typeof c.text === 'string'))
        .map((c) => c.text)
        .join('')
      if (text) return text
    }
  }
  return ''
}

export function createAzureResponsesProvider() {
  function config() {
    const url = process.env.AZURE_OPENAI_RESPONSES_URL
    const apiKey = process.env.AZURE_OPENAI_API_KEY
    const model = process.env.AZURE_OPENAI_DEPLOYMENT
    if (!url || !apiKey) {
      throw new Error('Azure Responses provider requires AZURE_OPENAI_RESPONSES_URL and AZURE_OPENAI_API_KEY.')
    }
    if (!model) {
      throw new Error('Azure Responses provider requires AZURE_OPENAI_DEPLOYMENT (the model deployment name).')
    }
    return { url, apiKey, model }
  }

  return {
    describe() {
      return {
        id: 'azure-responses',
        model: process.env.AZURE_OPENAI_DEPLOYMENT || '(unset)',
        note: 'Azure OpenAI Responses API.',
      }
    },
    async complete({ system, user }) {
      const cfg = config()
      // The skills already instruct "return only JSON"; the generator strips
      // fences and parses, so we don't rely on a provider-specific format flag.
      const body = {
        model: cfg.model,
        input: [
          { role: 'system', content: system },
          { role: 'user', content: user },
        ],
      }
      const res = await fetch(cfg.url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'api-key': cfg.apiKey },
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const detail = await res.text().catch(() => '')
        throw new Error(`Azure Responses call failed (${res.status}): ${detail.slice(0, 400)}`)
      }
      const data = await res.json()
      const text = extractResponsesText(data)
      if (!text) throw new Error('Azure Responses returned an empty completion.')
      return text
    },
  }
}
