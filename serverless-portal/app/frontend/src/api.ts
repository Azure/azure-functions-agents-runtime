// API client for the Serverless Agent Portal — live Azure discovery.
//
// Every request forwards the signed-in user's ARM access token (acquired via
// MSAL) as a Bearer token; the backend uses it to call ARM as that user.

import { acquireArmToken } from './auth'

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
  routes: string[]
  supportingFunctions: string[]
  defaultHostName: string
}

export interface LiveAgentApp {
  name: string
  resourceGroup: string
  location: string
  provider: string
  defaultHostName: string
  agents: {
    name: string
    trigger: string
    builtinEndpoints: boolean
    routes: string[]
    supportingFunctions: string[]
  }[]
  supportingFunctions: { name: string; trigger: string }[]
}

export interface LiveDiscovery {
  subscriptionId: string
  apps: LiveAgentApp[]
  agents: LiveAgent[]
}

export interface AgentDefinition {
  name: string
  app: string
  draftContent: string | null
  deployedContent: string | null
  content: string
  source: 'draft' | 'deployed' | 'none'
}

export interface SourceFile {
  path: string
  app: string
  draftContent: string | null
  deployedContent: string | null
  content: string
  source: 'draft' | 'deployed' | 'none'
}

export interface DeployResult {
  status: 'running' | 'deployed' | 'staged' | 'error'
  message: string
  files: string[]
  url?: string
  portalUrl?: string
  principalId?: string
  insightsUrl?: string
  grantOutcome?: 'granted' | 'partial' | 'failed'
}

export interface DeployStarted {
  jobId: string
  status: 'running'
  files: string[]
  portalUrl?: string
}

export interface AgentChatResult {
  sessionId: string
  response: string
  toolCalls: Record<string, unknown>[]
}

export interface FoundryModel {
  deployment: string
  model: string
}
export interface FoundryProject {
  name: string
  endpoint: string
}
export interface FoundryAccount {
  name: string
  resourceGroup: string
  location: string
  kind: string
  foundryEndpoint: string
  openaiEndpoint: string
  projects: FoundryProject[]
  models: FoundryModel[]
}
export interface FoundryDiscovery {
  subscriptionId: string
  accounts: FoundryAccount[]
}

export interface ResourceGroup {
  name: string
  location: string
}
export interface ResourceGroupList {
  subscriptionId: string
  resourceGroups: ResourceGroup[]
}

export interface GrantResult {
  granted: string[]
  failed: { role: string; error: string }[]
  scope: string
}

export type DeployTarget =
  | { kind: 'existing'; app: string; resourceGroup: string }
  | {
      kind: 'new'
      appName: string
      resourceGroup: string
      region: string
      foundryEndpoint: string
      foundryModel: string
      foundryAccount?: { subscription: string; resourceGroup: string; account: string }
    }

// Error carrying the HTTP status so React Query's retry guard can skip 4xx.
export class ApiError extends Error {
  readonly status: number
  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function req<T>(method: string, url: string, body?: unknown): Promise<T> {
  const token = await acquireArmToken()
  const res = await fetch(url, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
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
    throw new ApiError(typeof detail === 'string' ? detail : JSON.stringify(detail), res.status)
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

  // Agent definition (.agent.md) — read deployed source or portal draft, save draft.
  getAgentDefinition: (p: { subscription: string; app: string; resourceGroup: string; name: string }) =>
    req<AgentDefinition>(
      'GET',
      `/api/agents/definition?subscription=${enc(p.subscription)}&app=${enc(p.app)}&resourceGroup=${enc(p.resourceGroup)}&name=${enc(p.name)}`,
    ),
  saveAgentDefinition: (p: { subscription: string; app: string; name: string; content: string }) =>
    req<{ ok: boolean; source: string }>(
      'PUT',
      `/api/agents/definition?subscription=${enc(p.subscription)}&app=${enc(p.app)}&name=${enc(p.name)}`,
      { content: p.content },
    ),

  // Source files (e.g. function_app.py) — read deployed source or portal draft, save draft.
  getSource: (p: { subscription: string; app: string; resourceGroup: string; path: string }) =>
    req<SourceFile>(
      'GET',
      `/api/source?subscription=${enc(p.subscription)}&app=${enc(p.app)}&resourceGroup=${enc(p.resourceGroup)}&path=${enc(p.path)}`,
    ),
  saveSource: (p: { subscription: string; app: string; path: string; content: string }) =>
    req<{ ok: boolean; source: string }>(
      'PUT',
      `/api/source?subscription=${enc(p.subscription)}&app=${enc(p.app)}&path=${enc(p.path)}`,
      { content: p.content },
    ),

  // Start creating/deploying an agent app; returns a job id to poll. Provisioning
  // + remote build run in the background so the user can watch in the portal.
  startDeploy: (p: {
    subscription: string
    agent: { fileName: string; content: string }
    target: DeployTarget
  }) => req<DeployStarted>('POST', '/api/deploy', p),

  // Start redeploying an existing app from its own current source with the
  // portal's saved edits overlaid (safe for multi-agent apps).
  startRedeploy: (p: { subscription: string; resourceGroup: string; app: string }) =>
    req<DeployStarted>('POST', '/api/redeploy', p),

  // Poll a deploy/redeploy job's status.
  getDeployStatus: (jobId: string) => req<DeployResult>('GET', `/api/deploy/${enc(jobId)}`),

  // Chat with a deployed agent's built-in endpoint (proxied via the backend).
  agentChat: (p: {
    subscription: string
    resourceGroup: string
    app: string
    agent: string
    prompt: string
    sessionId?: string
  }) => req<AgentChatResult>('POST', '/api/agent/chat', p),

  // Open the agent's streaming chat (SSE) via the backend proxy. Returns the
  // raw Response; the caller reads response.body for `data: {json}` events.
  agentChatStream: async (p: {
    subscription: string
    resourceGroup: string
    app: string
    agent: string
    prompt: string
    sessionId?: string
  }): Promise<Response> => {
    const token = await acquireArmToken()
    const res = await fetch('/api/agent/chatstream', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
      },
      body: JSON.stringify(p),
    })
    if (!res.ok || !res.body) {
      const text = await res.text().catch(() => '')
      let detail = `HTTP ${res.status}`
      try {
        const j = JSON.parse(text)
        if (typeof j?.detail === 'string') detail = j.detail
      } catch {
        if (text) detail = text
      }
      throw new ApiError(detail, res.status)
    }
    return res
  },

  // Microsoft Foundry: list accounts/models for the create flow, and generate
  // an agent's instructions with the chosen model.
  listFoundry: (subscription?: string) =>
    req<FoundryDiscovery>(
      'GET',
      subscription ? `/api/foundry?subscription=${enc(subscription)}` : '/api/foundry',
    ),
  generateAgentMd: (p: {
    subscription: string
    name: string
    description: string
    foundry: { resourceGroup: string; account: string; openaiEndpoint: string; model: string }
  }) => req<{ content: string }>('POST', '/api/generate-agent-md', p),

  // Resource groups in a subscription (for the create flow's RG picker).
  listResourceGroups: (subscription?: string) =>
    req<ResourceGroupList>(
      'GET',
      subscription ? `/api/resource-groups?subscription=${enc(subscription)}` : '/api/resource-groups',
    ),

  // Grant a deployed app's identity access to a Foundry account (cross-sub OK).
  grantFoundryAccess: (p: {
    subscription: string
    resourceGroup: string
    account: string
    principalId: string
  }) => req<GrantResult>('POST', '/api/foundry/grant-access', p),
}
