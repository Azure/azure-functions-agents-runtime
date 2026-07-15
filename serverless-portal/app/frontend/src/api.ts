export interface AgentSummary {
  name: string
  displayName: string
  description: string
  trigger: string
  builtinEndpoints: boolean
  lastModified: string | null
  size: number
}

export interface AgentDetail {
  name: string
  content: string
  frontmatter: Record<string, unknown>
  body: string
}

export interface Health {
  status: string
  storage: string
  project: string
  environment: string
  container: string
}

export interface CreateAgentPayload {
  name: string
  description: string
  instructions: string
  builtin_endpoints: boolean
}

async function req<T>(method: string, url: string, body?: unknown): Promise<T> {
  const opts: RequestInit = { method }
  if (body !== undefined) {
    opts.headers = { 'Content-Type': 'application/json' }
    opts.body = JSON.stringify(body)
  }
  const res = await fetch(url, opts)
  const text = await res.text()
  let data: unknown = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = text
  }
  if (!res.ok) {
    const detail =
      data && typeof data === 'object' && 'detail' in data
        ? (data as { detail: unknown }).detail
        : `HTTP ${res.status}`
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  return data as T
}

export const api = {
  health: () => req<Health>('GET', '/api/health'),
  list: () => req<AgentSummary[]>('GET', '/api/agents'),
  get: (name: string) => req<AgentDetail>('GET', `/api/agents/${encodeURIComponent(name)}`),
  create: (payload: CreateAgentPayload) => req<AgentDetail>('POST', '/api/agents', payload),
  update: (name: string, content: string) =>
    req<AgentDetail>('PUT', `/api/agents/${encodeURIComponent(name)}`, { content }),
}
