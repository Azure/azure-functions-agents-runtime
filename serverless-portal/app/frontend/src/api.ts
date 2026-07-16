// API client for the Serverless Agent Portal — live Azure discovery.

export interface Health {
  status: string
}

export interface Identity {
  user: { name: string; username: string; oid: string; tenantId: string }
  subscription: { id: string; name: string }
}

export interface Subscription {
  id: string
  name: string
  state: string
}

export interface LiveAgent {
  name: string
  app: string
  resourceGroup: string
  region: string
  provider: string
  trigger: string
  builtinEndpoints: boolean
  defaultHostName: string
}

export interface LiveAgentApp {
  name: string
  resourceGroup: string
  location: string
  provider: string
  defaultHostName: string
  agents: { name: string; trigger: string; builtinEndpoints: boolean }[]
}

export interface LiveDiscovery {
  subscriptionId: string
  apps: LiveAgentApp[]
  agents: LiveAgent[]
}

async function req<T>(method: string, url: string): Promise<T> {
  const res = await fetch(url, { method })
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

const enc = encodeURIComponent

export const api = {
  health: () => req<Health>('GET', '/api/health'),

  // Azure (live discovery)
  identity: () => req<Identity>('GET', '/api/identity'),
  listSubscriptions: () => req<Subscription[]>('GET', '/api/subscriptions'),
  liveAgents: (subscription?: string) =>
    req<LiveDiscovery>(
      'GET',
      subscription ? `/api/live/agents?subscription=${enc(subscription)}` : '/api/live/agents',
    ),
}
