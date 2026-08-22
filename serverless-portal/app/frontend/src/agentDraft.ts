// Shared in-progress Hosted Skills app draft used by the two-step create flow:
//   CreateAgentPage  — pick a Foundry model, describe the agent, ✨ Generate.
//   DraftAppPage     — review the generated .agent.md, then Deploy / connect GitHub.
// The draft lives in sessionStorage so it survives the navigation between the two
// pages (and reloads) within the tab, and is discarded when the browser closes.

export type Trigger = 'http' | 'timer' | 'connector'

export interface NewApp {
  rgMode: 'existing' | 'new'
  resourceGroup: string
  region: string
  appName: string
}

export interface Draft {
  name: string
  description: string
  template: string
  provider: string
  // Foundry model (required): reuse an existing deployment or create one in AI Foundry.
  foundrySubscription: string
  foundryMode: 'pick' | 'manual'
  foundryAccount: string
  foundryResourceGroup: string
  foundryOpenaiEndpoint: string
  foundryEndpoint: string
  foundryModel: string
  builtinEndpoints: boolean
  sandbox: boolean
  trigger: Trigger
  instructions: string
  generatedFor: string
  mdOverride: string | null
  targetSubscription: string
  target: 'existing' | 'new'
  existingApp: string
  newApp: NewApp
}

// Regions that support Azure Functions Flex Consumption (+ the default Foundry
// gpt-5.4 Global Standard deployment) — matches the repo's infra allow-list.
export const FLEX_REGIONS = [
  'brazilsouth',
  'canadacentral',
  'canadaeast',
  'centralus',
  'eastus',
  'eastus2',
  'northcentralus',
  'southcentralus',
  'westus',
  'westus3',
]

export const DEFAULT_DRAFT: Draft = {
  name: '',
  description: '',
  template: 'chat',
  provider: 'foundry',
  foundrySubscription: '',
  foundryMode: 'pick',
  foundryAccount: '',
  foundryResourceGroup: '',
  foundryOpenaiEndpoint: '',
  foundryEndpoint: '',
  foundryModel: '',
  builtinEndpoints: true,
  sandbox: false,
  trigger: 'http',
  instructions: 'You are a helpful assistant. Answer the user clearly and concisely.',
  generatedFor: '',
  mdOverride: null,
  targetSubscription: '',
  target: 'new',
  existingApp: '',
  newApp: { rgMode: 'new', resourceGroup: '', region: 'westus3', appName: '' },
}

export const DRAFT_KEY = 'create-agent-draft'

// Ephemeral: the in-progress agent lives in sessionStorage, so it survives
// reloads/navigation within the tab but is discarded when the browser closes.
export function loadDraft(): Draft {
  try {
    const raw = sessionStorage.getItem(DRAFT_KEY)
    if (raw) return { ...DEFAULT_DRAFT, ...JSON.parse(raw) }
  } catch {
    /* ignore malformed draft */
  }
  return DEFAULT_DRAFT
}

export function saveDraft(d: Draft): void {
  try {
    sessionStorage.setItem(DRAFT_KEY, JSON.stringify(d))
  } catch {
    /* storage full/unavailable — non-fatal */
  }
}

export function clearDraft(): void {
  try {
    sessionStorage.removeItem(DRAFT_KEY)
  } catch {
    /* ignore */
  }
}

export function slugify(name: string): string {
  const s = name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
  return s || 'agent'
}

// Derive a reasonable agent name from a free-text description (first few words).
export function deriveName(description: string): string {
  const words = description.trim().split(/\s+/).slice(0, 5).join(' ')
  return slugify(words)
}

// A short random suffix (lowercase alnum) for globally-unique resource names.
export function randomSuffix(len = 5): string {
  const chars = 'abcdefghijklmnopqrstuvwxyz0123456789'
  const arr = new Uint32Array(len)
  crypto.getRandomValues(arr)
  let s = ''
  for (let i = 0; i < len; i++) s += chars[arr[i] % chars.length]
  return s
}

// Hyphen-cased base (Function App / resource-group names use hyphens, not the
// underscores slugify emits).
function hyphenBase(name: string, max: number): string {
  const b = slugify(name)
    .replace(/_/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, max)
  return b.replace(/-+$/g, '') || 'agents'
}

// Suggested globally-unique Function App name from the agent name.
export function defaultAppName(agentName: string): string {
  return `func-${hyphenBase(agentName, 20)}-${randomSuffix(5)}`
}

// Suggested resource-group name from the agent name.
export function defaultResourceGroup(agentName: string): string {
  return `rg-${hyphenBase(agentName, 24)}`
}

// Normalize a Function App name to the allowed charset (lowercase alphanumerics
// and hyphens). A trailing hyphen is left in place so it can be typed; callers
// trim it at submit time.
export function sanitizeAppName(name: string): string {
  return String(name)
    .toLowerCase()
    .replace(/[^a-z0-9-]+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-+/, '')
    .slice(0, 60)
}

export function composeAgentMd(d: Draft): string {
  const slug = slugify(d.name)
  const lines: string[] = ['---', `name: ${d.name || slug}`]
  if (d.description) lines.push(`description: ${d.description}`)
  if (d.foundryModel) lines.push(`model: ${d.foundryModel}`)
  if (d.trigger === 'http') {
    lines.push('trigger:', '  type: http_trigger', '  args:', `    route: ${slug}`, '    methods: ["POST"]')
  } else if (d.trigger === 'timer') {
    lines.push('trigger:', '  type: timer_trigger', '  args:', '    schedule: "0 0 */6 * * *"')
  } else if (d.trigger === 'connector') {
    lines.push('trigger:', '  type: connector_trigger', '  args: {}')
  }
  lines.push(`builtin_endpoints: ${d.builtinEndpoints ? 'true' : 'false'}`)
  if (d.sandbox) lines.push('system_tools:', '  dynamic_sessions_code_interpreter: true')
  lines.push('---', '', d.instructions || '')
  return lines.join('\n')
}
