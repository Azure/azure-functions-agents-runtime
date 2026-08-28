// API client for the Serverless Agent Portal — live Azure discovery.
//
// Every request forwards the signed-in user's ARM access token (acquired via
// MSAL) as a Bearer token; the backend uses it to call ARM as that user.

import { acquireArmToken, acquireStorageToken, getManualToken, clearManualToken } from './auth'

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

export interface SourceListEntry {
  path: string
  size: number
  source: 'draft' | 'deployed' | 'both'
}
export interface SourceListing {
  app: string
  files: SourceListEntry[]
}

export interface CustomToolPreview {
  toolPath: string
  python: string
  requirements: string
  addedDependencies: string[]
  existingToolSource: SourceFile['source']
  requiresOverwrite: boolean
}

export interface RuntimeIdentity {
  type: 'system' | 'user'
  name: string
  clientId: string
  principalId: string
  resourceId?: string
}

export interface AzureRole {
  id: string
  name: string
  description: string
  assignableScopes: string[]
  isDefault: boolean
}

export interface SessionSummary {
  sessionId: string
  size: number
  lastModified: string | null
}
export interface SessionListing {
  app: string
  sessions: SessionSummary[]
  readable: boolean
}
export interface SessionMessage {
  role?: string
  content?: unknown
  [key: string]: unknown
}
export interface SessionRead {
  app: string
  sessionId: string
  messages: SessionMessage[]
}

export interface AppInsightsColumn {
  name: string
  type: string
}
export interface AppInsightsTable {
  name: string
  columns: AppInsightsColumn[]
  rows: unknown[][]
}
export interface AppInsightsResult {
  componentId: string
  tables?: AppInsightsTable[]
  error?: string
}

export interface SampleAgentSummary {
  file: string
  name: string
  description: string
  triggerType: string
  builtinEndpoints: boolean
}
export interface SampleFile {
  path: string
  content: string
}
export interface SampleSummary {
  slug: string
  title: string
  blurb: string
  agents: SampleAgentSummary[]
  triggerTypes: string[]
  hasMcp: boolean
  hasSkills: boolean
  hasWorkflow: boolean
  files?: SampleFile[]
}

export interface ValidationIssue {
  path: string
  message: string
}
export interface AgentMdValidation {
  ok: boolean
  errors: ValidationIssue[]
  warnings: ValidationIssue[]
  front?: unknown
}

export interface DeployHistoryEntry {
  jobId: string
  kind: 'create' | 'deploy' | 'redeploy' | string
  status: 'deployed' | 'error' | string
  finishedAt: string
  files?: string[]
  message?: string
  resourceGroup?: string
  url?: string
  fileName?: string
  grantOutcome?: string
}
export interface DeployHistory {
  app: string
  deploys: DeployHistoryEntry[]
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

export interface NameAvailability {
  available: boolean
  reason?: string
  message?: string
}

export interface CapabilitySuggestion {
  kind: string
  name: string
  description: string
}

export interface GrantResult {
  granted: string[]
  failed: { role: string; error: string }[]
  scope: string
}

export interface GitHubStatus {
  configured: boolean
  connected: boolean
  login?: string
  avatarUrl?: string
}
export interface GitHubRepo {
  fullName: string
  name: string
  owner: string
  private: boolean
  defaultBranch: string
  htmlUrl: string
}
export type GitHubPublishMode = 'pr' | 'direct'
export interface GitHubConnectResult {
  htmlUrl: string
  repoUrl: string
  owner: string
  name: string
  publishMode: GitHubPublishMode
  branch: string
  base?: string
  prUrl?: string
  prNumber?: number
  commitSha?: string
  stored: boolean
  deploymentCenter?: boolean
  pushed: string[]
}
export interface GitHubAppConnection {
  connected: boolean
  repoUrl?: string
  branch?: string
  connectedBy?: string
  source?: 'deploymentCenter' | 'appSettings'
}

export type ConnectionStatus = 'Connected' | 'Expired' | 'Action required'

export interface OutlookConnection {
  id: string
  displayName: string
  service: 'Office 365 Outlook'
  allowedOperations: ['SendEmailV2']
  status: ConnectionStatus
  providerStatus: string
  provisioningState: string
  authenticatedUser: string
  authorizationRequired: boolean
  providerErrorCode: string
  providerErrorMessage: string
  source: 'Created' | 'Existing'
  subscriptionId: string
  gatewayName: string
  resourceGroup: string
  infrastructureReady: boolean
  authenticationReady: boolean
  detail: string
  mcpEndpointUrl: string
  portalUrl: string
}

export interface OutlookConnectionCandidate {
  id: string
  subscriptionId: string
  displayName: string
  connectionName: string
  gatewayName: string
  resourceGroup: string
  status: ConnectionStatus
  providerStatus: string
  authenticatedUser: string
}

export interface ConnectionTest {
  ok: boolean
  checks: { name: string; ok: boolean; detail?: string }[]
  connection: OutlookConnection
}

export interface ConnectionSetupSource {
  path: 'mcp.json'
  changed: boolean
  deploymentRequired: boolean
  state: 'draft' | 'deployed'
}

export interface ConnectionSetup {
  connection: OutlookConnection
  source: ConnectionSetupSource
}

export interface ConnectionRemoval {
  removed: true
  source: 'Created' | 'Existing'
  sourceDraftChanged: boolean
  cleanup: {
    sourceDraft: string
    appSetting: string
    azure: string
  }
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
  readonly data: Record<string, unknown>
  constructor(message: string, status: number, data: Record<string, unknown> = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.data = data
  }
}

async function req<T>(
  method: string,
  url: string,
  body?: unknown,
  extraHeaders?: Record<string, string>,
): Promise<T> {
  const token = await acquireArmToken()
  const res = await fetch(url, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      ...(extraHeaders ?? {}),
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
    // A pasted ARM token (Option C) that expired/became invalid — drop it so the
    // app returns to the sign-in gate where a fresh one can be pasted.
    if (res.status === 401 && getManualToken()) clearManualToken()
    const detail =
      data && typeof data === 'object' && 'detail' in data
        ? (data as { detail: unknown }).detail
        : `HTTP ${res.status}`
    throw new ApiError(
      typeof detail === 'string' ? detail : JSON.stringify(detail),
      res.status,
      data && typeof data === 'object' ? (data as Record<string, unknown>) : {},
    )
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

  listConnections: (p: { subscription: string; resourceGroup: string; app: string }) =>
    req<{ connections: OutlookConnection[] }>(
      'GET',
      `/api/connections?subscription=${enc(p.subscription)}&resourceGroup=${enc(p.resourceGroup)}&app=${enc(p.app)}`,
    ),
  createOutlookConnection: (p: {
    subscription: string
    resourceGroup: string
    app: string
    displayName: string
  }) => req<ConnectionSetup>(
    'POST',
    `/api/connections?subscription=${enc(p.subscription)}&resourceGroup=${enc(p.resourceGroup)}&app=${enc(p.app)}`,
    p,
  ),
  listOutlookConnectionCandidates: (p: {
    subscription: string
    resourceGroup: string
    app: string
    connectorSubscription: string
  }) =>
    req<{ connections: OutlookConnectionCandidate[]; partial: boolean }>(
      'GET',
      `/api/connections/candidates?subscription=${enc(p.subscription)}&resourceGroup=${enc(p.resourceGroup)}&app=${enc(p.app)}&connectorSubscription=${enc(p.connectorSubscription)}`,
    ),
  attachOutlookConnection: (p: {
    subscription: string
    resourceGroup: string
    app: string
    connectionId: string
    connectorSubscription: string
  }) => req<ConnectionSetup>(
    'POST',
    `/api/connections/attach?subscription=${enc(p.subscription)}&resourceGroup=${enc(p.resourceGroup)}&app=${enc(p.app)}`,
    p,
  ),
  getConnectionStatus: (p: { subscription: string; resourceGroup: string; app: string; id: string }) =>
    req<{ connection: OutlookConnection }>(
      'GET',
      `/api/connections/${enc(p.id)}/status?subscription=${enc(p.subscription)}&resourceGroup=${enc(p.resourceGroup)}&app=${enc(p.app)}`,
    ),
  getConnectionAuthLink: (p: { subscription: string; resourceGroup: string; app: string; id: string }) =>
    req<{ url: string }>(
      'GET',
      `/api/connections/${enc(p.id)}/auth-link?subscription=${enc(p.subscription)}&resourceGroup=${enc(p.resourceGroup)}&app=${enc(p.app)}`,
    ),
  testConnection: (p: { subscription: string; resourceGroup: string; app: string; id: string }) =>
    req<ConnectionTest>(`POST`, `/api/connections/${enc(p.id)}/test`, p),
  deleteOutlookConnection: async (p: {
    subscription: string
    resourceGroup: string
    app: string
    id: string
  }) => {
    const storageToken = await acquireStorageToken()
    return req<ConnectionRemoval>(
      'DELETE',
      `/api/connections/${enc(p.id)}?subscription=${enc(p.subscription)}&resourceGroup=${enc(p.resourceGroup)}&app=${enc(p.app)}`,
      undefined,
      storageToken ? { 'X-Storage-Token': storageToken } : undefined,
    )
  },

  // Agent definition (.agent.md) — read deployed source or portal draft, save draft.
  getAgentDefinition: async (p: { subscription: string; app: string; resourceGroup: string; name: string }) => {
    const storageToken = await acquireStorageToken()
    return req<AgentDefinition>(
      'GET',
      `/api/agents/definition?subscription=${enc(p.subscription)}&app=${enc(p.app)}&resourceGroup=${enc(p.resourceGroup)}&name=${enc(p.name)}`,
      undefined,
      storageToken ? { 'X-Storage-Token': storageToken } : undefined,
    )
  },
  saveAgentDefinition: (p: { subscription: string; app: string; name: string; content: string }) =>
    req<{ ok: boolean; source: string }>(
      'PUT',
      `/api/agents/definition?subscription=${enc(p.subscription)}&app=${enc(p.app)}&name=${enc(p.name)}`,
      { content: p.content },
    ),

  // Source files (e.g. function_app.py) — read deployed source or portal draft, save draft.
  getSource: async (p: { subscription: string; app: string; resourceGroup: string; path: string }) => {
    const storageToken = await acquireStorageToken()
    return req<SourceFile>(
      'GET',
      `/api/source?subscription=${enc(p.subscription)}&app=${enc(p.app)}&resourceGroup=${enc(p.resourceGroup)}&path=${enc(p.path)}`,
      undefined,
      storageToken ? { 'X-Storage-Token': storageToken } : undefined,
    )
  },
  saveSource: (p: { subscription: string; app: string; path: string; content: string }) =>
    req<{ ok: boolean; source: string }>(
      'PUT',
      `/api/source?subscription=${enc(p.subscription)}&app=${enc(p.app)}&path=${enc(p.path)}`,
      { content: p.content },
    ),

  // Remove a source-file draft (portal-side working copy). Does not touch the
  // deployed file — that only changes on the next "Deploy edits".
  deleteSourceDraft: (p: { subscription: string; app: string; path: string }) =>
    req<{ ok: boolean; removed: boolean }>(
      'DELETE',
      `/api/source?subscription=${enc(p.subscription)}&app=${enc(p.app)}&path=${enc(p.path)}`,
    ),

  // Enumerate every source file for an app — deployed files merged with local
  // drafts, tagged so the UI can show which paths carry unpublished edits.
  listSources: async (p: { subscription: string; app: string; resourceGroup: string }) => {
    const storageToken = await acquireStorageToken()
    return req<SourceListing>(
      'GET',
      `/api/source/list?subscription=${enc(p.subscription)}&app=${enc(p.app)}&resourceGroup=${enc(p.resourceGroup)}`,
      undefined,
      storageToken ? { 'X-Storage-Token': storageToken } : undefined,
    )
  },

  previewAzureRestTool: (p: { subscription: string; resourceGroup: string; app: string; toolName: string }) =>
    req<CustomToolPreview>('POST', '/api/custom-tools/azure-rest/preview', p),
  saveAzureRestTool: (p: {
    subscription: string
    resourceGroup: string
    app: string
    toolName: string
    python: string
    overwrite: boolean
  }) => req<{ ok: boolean; source: string; toolPath: string; requirementsPath: string; addedDependencies: string[] }>(
    'POST',
    '/api/custom-tools/azure-rest/save',
    p,
  ),
  getCustomToolIdentity: (p: { subscription: string; resourceGroup: string; app: string }) =>
    req<{ identity: RuntimeIdentity }>(
      'GET',
      `/api/custom-tools/identity?subscription=${enc(p.subscription)}&resourceGroup=${enc(p.resourceGroup)}&app=${enc(p.app)}`,
    ),
  listCustomToolRoles: (p: {
    subscription: string
    scopeType: 'subscription' | 'resourceGroup'
    resourceGroup?: string
  }) => req<{ scope: string; roles: AzureRole[] }>(
    'GET',
    `/api/custom-tools/roles?subscription=${enc(p.subscription)}&scopeType=${enc(p.scopeType)}&resourceGroup=${enc(p.resourceGroup ?? '')}`,
  ),
  grantCustomToolAccess: (p: {
    subscription: string
    resourceGroup: string
    app: string
    scopeType: 'subscription' | 'resourceGroup'
    scopeResourceGroup?: string
    identityClientId?: string
    roleDefinitionId: string
  }) => req<{ identity: RuntimeIdentity; outcome: 'granted' | 'existing'; role: AzureRole; scope: string }>(
    'POST',
    '/api/custom-tools/access',
    p,
  ),
  getConfiguredModel: (p: { subscription: string; resourceGroup: string; app: string }) =>
    req<{ provider: string; model: string; available: boolean }>(
      'GET',
      `/api/custom-tools/configured-model?subscription=${enc(p.subscription)}&resourceGroup=${enc(p.resourceGroup)}&app=${enc(p.app)}`,
    ),

  // Enumerate the runtime's blob-backed sessions for an app so the Playground
  // can offer a history browser. `readable: false` means storage was reached
  // but the caller lacks permission (or storage isn't yet configured).
  listSessions: async (p: { subscription: string; app: string; resourceGroup: string }) => {
    const storageToken = await acquireStorageToken()
    return req<SessionListing>(
      'GET',
      `/api/sessions?subscription=${enc(p.subscription)}&app=${enc(p.app)}&resourceGroup=${enc(p.resourceGroup)}`,
      undefined,
      storageToken ? { 'X-Storage-Token': storageToken } : undefined,
    )
  },

  // Read one session's persisted messages (parsed from the JSONL blob).
  getSession: async (p: { subscription: string; app: string; resourceGroup: string; sessionId: string }) => {
    const storageToken = await acquireStorageToken()
    return req<SessionRead>(
      'GET',
      `/api/sessions/${enc(p.sessionId)}?subscription=${enc(p.subscription)}&app=${enc(p.app)}&resourceGroup=${enc(p.resourceGroup)}`,
      undefined,
      storageToken ? { 'X-Storage-Token': storageToken } : undefined,
    )
  },

  // Run a curated Application Insights KQL preset for an app (summary /
  // timeline / agents / recentFailures / invocations). `timeRange` is a Kusto-shortform
  // window like "24h" / "7d" / "15m".
  appInsightsQuery: (p: {
    subscription: string
    resourceGroup: string
    app: string
    agent?: string
    traceId?: string
    startTime?: string
    endTime?: string
    page?: number
    pageSize?: number
    preset?: 'summary' | 'timeline' | 'agents' | 'recentFailures' | 'invocations' | 'trace'
    query?: string
    timeRange?: string
  }) => req<AppInsightsResult>('POST', '/api/app-insights/query', p),

  // Enumerate the runtime's bundled samples so the landing page + Create
  // wizard can offer a "Start from a sample" gallery. Pass `includeFiles=true`
  // to get every source file (used by the wizard to pre-fill the draft).
  listSamples: (includeFiles?: boolean) =>
    req<{ samples: SampleSummary[] }>(
      'GET',
      `/api/samples${includeFiles ? '?includeFiles=1' : ''}`,
    ),

  // Validate a `.agent.md` file's frontmatter against the runtime's schema —
  // catches missing fields, unsupported trigger types, and missing required
  // trigger args *before* the user hits deploy.
  validateAgentMd: (content: string) =>
    req<AgentMdValidation>('POST', '/api/validate/agent-md', { content }),

  // Fetch persisted deploy history for an app (create + redeploy runs). The
  // portal snapshots each completed job to disk under `.data/deploy-history/`.
  listDeployHistory: (p: { subscription: string; app: string }) =>
    req<DeployHistory>(
      'GET',
      `/api/deploy-history?subscription=${enc(p.subscription)}&app=${enc(p.app)}`,
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
  generateCapability: (p: {
    subscription: string
    app?: string
    resourceGroup?: string
    kind: 'http_trigger' | 'connector_trigger' | 'timer_trigger' | 'custom_tool' | 'skill'
    triggerType?: string
    name: string
    description: string
    groundInSkills?: boolean
    foundry?: { resourceGroup: string; account: string; openaiEndpoint: string; model: string }
  }) => req<{ content: string; kind: string }>('POST', '/api/generate-capability', p),

  // Resource groups in a subscription (for the create flow's RG picker).
  listResourceGroups: (subscription?: string) =>
    req<ResourceGroupList>(
      'GET',
      subscription ? `/api/resource-groups?subscription=${enc(subscription)}` : '/api/resource-groups',
    ),

  // Check if a Function App name is globally available (for the deploy flow).
  checkName: (p: { subscription: string; name: string }) =>
    req<NameAvailability>(
      'GET',
      `/api/check-name?subscription=${enc(p.subscription)}&name=${enc(p.name)}`,
    ),

  // Infer the capabilities an agent needs from its description (skill-grounded).
  planCapabilities: (p: {
    subscription: string
    description: string
    foundry: { resourceGroup: string; account: string; openaiEndpoint: string; model: string }
  }) => req<{ capabilities: CapabilitySuggestion[] }>('POST', '/api/plan-capabilities', p),

  // Grant a deployed app's identity access to a Foundry account (cross-sub OK).
  grantFoundryAccess: (p: {
    subscription: string
    resourceGroup: string
    account: string
    principalId: string
  }) => req<GrantResult>('POST', '/api/foundry/grant-access', p),

  // Self-heal Foundry access for a running app — resolves the principalId and
  // Foundry account from its app settings, then grants both roles in one shot.
  healFoundryAccess: (p: { subscription: string; resourceGroup: string; app: string }) =>
    req<GrantResult & { principalId: string; account: string; accountResourceGroup: string }>(
      'POST',
      '/api/foundry/heal-access',
      p,
    ),

  // GitHub connection (Phase 1): OAuth status, sign-in URL, repo list, connect.
  githubStatus: () => req<GitHubStatus>('GET', '/api/github/status'),
  githubAppConnection: (p: { subscription: string; resourceGroup: string; app: string }) =>
    req<GitHubAppConnection>(
      'GET',
      `/api/github/app-connection?subscription=${enc(p.subscription)}&resourceGroup=${enc(p.resourceGroup)}&app=${enc(p.app)}`,
    ),
  githubLoginUrl: (callbackUrl: string) =>
    req<{ authorizeUrl: string }>('POST', '/api/github/login-url', { callbackUrl }),
  githubLocalSession: () => req<GitHubStatus>('POST', '/api/github/local-session'),
  githubDisconnect: () => req<GitHubStatus>('POST', '/api/github/disconnect'),
  githubUnlink: (p: { subscription: string; resourceGroup: string; app: string; deploymentCenter?: boolean }) =>
    req<{ ok: boolean; cleared: boolean; deploymentCenter: boolean; deploymentCenterCleared: boolean }>(
      'POST',
      '/api/github/unlink',
      p,
    ),
  githubRepos: () => req<{ repos: GitHubRepo[] }>('GET', '/api/github/repos'),
  githubConnect: (p: {
    subscription: string
    resourceGroup: string
    app: string
    mode: 'new' | 'existing'
    publishMode: GitHubPublishMode
    repoName?: string
    private?: boolean
    org?: string
    repo?: string
    branch?: string
  }) => req<GitHubConnectResult>('POST', '/api/github/connect', p),
  githubProvisionDeployment: (p: {
    subscription: string
    resourceGroup: string
    app: string
    repo: string
    branch?: string
  }) =>
    req<{
      ok: boolean
      steps: Record<string, unknown>
      clientId: string
      workflowUrl: string
      runsUrl: string
    }>('POST', '/api/github/provision-deployment', p),
}
