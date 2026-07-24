// Workflow Composer — shared types (mirror the backend store schema).

export type NodeType = 'trigger' | 'agent' | 'tool' | 'skill' | 'mcp' | 'router' | 'output'
export type NodeSource = 'generated' | 'reused' | 'manual'
export type WorkflowStatus = 'draft' | 'published' | 'published_with_changes'

export interface WorkflowVersion {
  version: number
  updatedAt: string
  message: string
}

export interface WorkflowNode {
  id: string
  type: NodeType
  kind: string
  name: string
  source?: NodeSource
  position?: { x: number; y: number }
  // Kind-specific; editors read/write known fields.
  config: Record<string, unknown>
}

export interface WorkflowEdge {
  id: string
  from: string
  to: string
  label?: string
  condition?: string
}

export interface WorkflowInput {
  id: string
  label: string
  type: 'text' | 'textarea' | 'file' | 'choice' | 'number' | 'boolean'
  required?: boolean
  options?: string[]
  placeholder?: string
}

export interface WorkflowTarget {
  functionApp?: string
  resourceGroup?: string
  subscriptionId?: string
  provider?: string
  model?: string
}

export interface DeploymentStep {
  key: string
  title: string
  detail: string
  status: string
}

export interface Deployment {
  status: string
  onBehalfOfCustomer: boolean
  startedAt: string
  finishedAt: string
  target: { functionApp: string; resourceGroup: string; provider: string }
  steps: DeploymentStep[]
  endpoints: { label: string; url: string; method: string }[]
}

export interface Generation {
  model: string
  provider: string
  skill: string
  generatedAt: string
  generatedNodeIds: string[]
  reusedNodeIds: string[]
}

export interface Workflow {
  id: string
  name: string
  description: string
  version: number
  status: WorkflowStatus
  createdBy?: string
  createdAt?: string
  updatedAt?: string
  etag?: string
  prompt?: string
  target: WorkflowTarget
  inputs: WorkflowInput[]
  nodes: WorkflowNode[]
  edges: WorkflowEdge[]
  generation?: Generation | null
  deployment?: Deployment | null
  publishedVersion?: number | null
  publishedSnapshot?: {
    nodes: WorkflowNode[]
    edges: WorkflowEdge[]
    inputs: WorkflowInput[]
    target: WorkflowTarget
  } | null
  history?: WorkflowVersion[]
}

export interface WorkflowSummary {
  id: string
  name: string
  description: string
  status: WorkflowStatus
  version: number
  publishedVersion?: number | null
  updatedAt: string
  target: WorkflowTarget
  nodeCount: number
  triggerKinds: string[]
  componentCounts: Record<string, number>
}

export interface ModelInfo {
  id: string
  model: string
  note: string
}

export interface SkillInfo {
  name: string
  summary: string
}

export interface RunStep {
  nodeId: string
  type: NodeType
  kind: string
  name: string
  status: string
  elapsedMs: number
  output: string
}

export interface RunResult {
  status: string
  startedAt: string
  durationMs: number
  inputs: Record<string, unknown>
  steps: RunStep[]
}

// Presentation metadata per node type (color, icon, label).
export const NODE_META: Record<NodeType, { label: string; icon: string; cls: string }> = {
  trigger: { label: 'Trigger', icon: '⚡', cls: 'trigger' },
  agent: { label: 'Agent', icon: '🤖', cls: 'agent' },
  tool: { label: 'Tool', icon: '🔧', cls: 'tool' },
  skill: { label: 'Skill', icon: '📚', cls: 'skill' },
  mcp: { label: 'MCP', icon: '🔌', cls: 'mcp' },
  router: { label: 'Router', icon: '🔀', cls: 'router' },
  output: { label: 'Output', icon: '📤', cls: 'output' },
}
