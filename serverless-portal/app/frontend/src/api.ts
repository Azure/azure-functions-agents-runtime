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
  defaultHostName: string
}

export interface LiveAgentApp {
  name: string
  resourceGroup: string
  location: string
  provider: string
  defaultHostName: string
  agents: { name: string; trigger: string; builtinEndpoints: boolean; routes: string[] }[]
}

export interface LiveDiscovery {
  subscriptionId: string
  apps: LiveAgentApp[]
  agents: LiveAgent[]
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

async function req<T>(method: string, url: string): Promise<T> {
  const token = await acquireArmToken()
  const res = await fetch(url, {
    method,
    headers: { Authorization: `Bearer ${token}` },
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

// Composer requests are backed by the portal-owned store and need no ARM token.
async function reqJson<T>(method: string, url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
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
}

// ---------------------------------------------------------------------------
// Workflow Composer API (portal-owned store; no ARM token required).
// ---------------------------------------------------------------------------

import type {
  Workflow,
  WorkflowSummary,
  WorkflowVersion,
  ModelInfo,
  SkillInfo,
  RunResult,
} from './workflow/types'

export type ComponentCatalog = Record<string, unknown>

export const composerApi = {
  model: () => reqJson<ModelInfo>('GET', '/api/composer/model'),
  skills: () => reqJson<SkillInfo[]>('GET', '/api/composer/skills'),
  catalog: () => reqJson<ComponentCatalog>('GET', '/api/composer/catalog'),

  list: () => reqJson<WorkflowSummary[]>('GET', '/api/workflows'),
  get: (id: string) => reqJson<Workflow>('GET', `/api/workflows/${enc(id)}`),
  create: (partial: Partial<Workflow>) => reqJson<Workflow>('POST', '/api/workflows', partial),
  generate: (prompt: string, target?: Workflow['target']) =>
    reqJson<Workflow>('POST', '/api/workflows/generate', { prompt, target }),
  regenerate: (id: string, prompt: string) =>
    reqJson<Workflow>('POST', `/api/workflows/${enc(id)}/regenerate`, { prompt }),
  // Stage-2 codegen for a single (possibly unsaved) node.
  generateCode: (node: unknown, workflow: unknown) =>
    reqJson<{ code?: string; instructions?: string }>('POST', '/api/composer/generate-code', { node, workflow }),
  save: (id: string, doc: Partial<Workflow>) => reqJson<Workflow>('PUT', `/api/workflows/${enc(id)}`, doc),
  remove: (id: string) => reqJson<void>('DELETE', `/api/workflows/${enc(id)}`),
  compile: (id: string) => reqJson<{ files: Record<string, string> }>('GET', `/api/workflows/${enc(id)}/compile`),
  deploy: (id: string) => reqJson<Workflow>('POST', `/api/workflows/${enc(id)}/deploy`, {}),
  run: (id: string, inputs: Record<string, unknown>) =>
    reqJson<RunResult>('POST', `/api/workflows/${enc(id)}/run`, { inputs }),
  // Version history + restore (working-copy model).
  versions: (id: string) => reqJson<WorkflowVersion[]>('GET', `/api/workflows/${enc(id)}/versions`),
  restore: (id: string, version: number) =>
    reqJson<Workflow>('POST', `/api/workflows/${enc(id)}/restore/${version}`, {}),
}
