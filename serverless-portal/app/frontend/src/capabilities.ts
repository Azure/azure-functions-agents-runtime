// Helpers to add capabilities — triggers, connector triggers, and MCP tool
// servers — to an agent by editing its `.agent.md` YAML frontmatter or the app's
// `mcp.json`. The formats mirror the azure-functions-agents-runtime config
// (config/schema.py `TRIGGER_TYPES` and discovery/mcp.py).

export interface TriggerField {
  name: string
  label: string
  required?: boolean
  placeholder?: string
  default?: string
  kind?: 'string' | 'methods'
  help?: string
}

export interface TriggerSpec {
  label: string
  type: string
  fields: TriggerField[]
  note?: string
}

// Subset of the runtime's supported trigger types with their args, keyed by a
// short id used in the UI.
export const TRIGGER_SPECS: Record<string, TriggerSpec> = {
  http: {
    label: 'HTTP request',
    type: 'http_trigger',
    fields: [
      { name: 'route', label: 'Route', required: true, placeholder: 'my-agent' },
      {
        name: 'methods',
        label: 'Methods',
        kind: 'methods',
        default: 'POST',
        help: 'Comma-separated (GET, POST, PUT, DELETE, PATCH)',
      },
      { name: 'auth_level', label: 'Auth level', placeholder: 'function', help: 'anonymous · function · admin' },
    ],
  },
  timer: {
    label: 'Timer schedule',
    type: 'timer_trigger',
    fields: [
      {
        name: 'schedule',
        label: 'Schedule (NCRONTAB)',
        required: true,
        placeholder: '0 0 */6 * * *',
        help: '6-field cron. e.g. "0 0 15 * * *" = 3pm daily',
      },
    ],
  },
  queue: {
    label: 'Storage queue',
    type: 'queue_trigger',
    fields: [
      { name: 'queue_name', label: 'Queue name', required: true },
      { name: 'connection', label: 'Connection (app setting)', required: true, placeholder: 'AzureWebJobsStorage' },
    ],
  },
  service_bus_queue: {
    label: 'Service Bus queue',
    type: 'service_bus_queue_trigger',
    fields: [
      { name: 'queue_name', label: 'Queue name', required: true },
      { name: 'connection', label: 'Connection (app setting)', required: true },
    ],
  },
  service_bus_topic: {
    label: 'Service Bus topic',
    type: 'service_bus_topic_trigger',
    fields: [
      { name: 'topic_name', label: 'Topic name', required: true },
      { name: 'subscription_name', label: 'Subscription name', required: true },
      { name: 'connection', label: 'Connection (app setting)', required: true },
    ],
  },
  blob: {
    label: 'Blob storage',
    type: 'blob_trigger',
    fields: [
      { name: 'path', label: 'Path pattern', required: true, placeholder: 'uploads/{name}.txt' },
      { name: 'connection', label: 'Connection (app setting)', placeholder: 'AzureWebJobsStorage' },
    ],
  },
  event_grid: {
    label: 'Event Grid',
    type: 'event_grid_trigger',
    fields: [],
    note: 'No properties — the Event Grid subscription is configured in Azure.',
  },
}

export interface SchedulePreset {
  label: string
  cron: string
}

// Friendly recurring-schedule choices mapped to 6-field NCRONTAB, so the UI can
// offer plain-language options instead of asking users to hand-write cron. The
// values mirror the timer schedules used in the runtime samples.
export const SCHEDULE_PRESETS: SchedulePreset[] = [
  { label: 'Every hour', cron: '0 0 * * * *' },
  { label: 'Every 6 hours', cron: '0 0 */6 * * *' },
  { label: 'Every day at 9:00 AM', cron: '0 0 9 * * *' },
  { label: 'Every day at 3:00 PM', cron: '0 0 15 * * *' },
  { label: 'Weekdays at 8:00 AM', cron: '0 0 8 * * 1-5' },
  { label: 'Every Monday at 9:00 AM', cron: '0 0 9 * * 1' },
]

function yamlScalar(v: string): string {
  return '"' + v.replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"'
}

// Build the `trigger:` YAML block for a trigger id + field values. The special
// `connector` id emits the runtime's connector trigger shape.
export function buildTriggerYaml(
  specKey: string,
  values: Record<string, string>,
  preservedArgs: Record<string, string> = {},
): string {
  if (specKey === 'connector') {
    return ['trigger:', '  type: generic_trigger', '  args:', '    type: connectorTrigger'].join('\n')
  }
  const spec = TRIGGER_SPECS[specKey]
  if (!spec) throw new Error(`Unknown trigger type: ${specKey}`)
  const argLines: string[] = []
  const declaredFields = new Set(spec.fields.map((field) => field.name))
  for (const f of spec.fields) {
    const raw = (values[f.name] ?? f.default ?? '').trim()
    if (!raw) continue
    if (f.kind === 'methods') {
      const arr = raw
        .split(',')
        .map((m) => m.trim().toUpperCase())
        .filter(Boolean)
      if (arr.length) argLines.push(`    methods: [${arr.map((m) => `"${m}"`).join(', ')}]`)
    } else {
      argLines.push(`    ${f.name}: ${yamlScalar(raw)}`)
    }
  }
  for (const [name, raw] of Object.entries(preservedArgs)) {
    if (declaredFields.has(name) || !raw.trim()) continue
    const value = /^(?:true|false|null|-?\d+(?:\.\d+)?)$/i.test(raw.trim()) ? raw.trim() : yamlScalar(raw.trim())
    argLines.push(`    ${name}: ${value}`)
  }
  const lines = ['trigger:', `  type: ${spec.type}`]
  if (argLines.length) {
    lines.push('  args:', ...argLines)
  } else {
    lines.push('  args: {}')
  }
  return lines.join('\n')
}

// Replace (or insert) the `trigger:` block inside an `.agent.md`'s YAML
// frontmatter, leaving the body and other keys untouched.
export function applyTriggerToMarkdown(md: string, triggerBlock: string): string {
  const nl = '\n'
  const lines = (md ?? '').split(/\r?\n/)
  if (lines[0]?.trim() !== '---') {
    return `---${nl}${triggerBlock}${nl}---${nl}${nl}${md ?? ''}`
  }
  let end = -1
  for (let i = 1; i < lines.length; i++) {
    if (lines[i].trim() === '---') {
      end = i
      break
    }
  }
  if (end === -1) return `---${nl}${triggerBlock}${nl}---${nl}${nl}${md}`

  const fm = lines.slice(1, end)
  const cleaned: string[] = []
  let i = 0
  while (i < fm.length) {
    if (/^trigger:/.test(fm[i])) {
      // Drop the `trigger:` line and its indented child lines.
      i++
      while (i < fm.length && /^[ \t]/.test(fm[i])) i++
      continue
    }
    cleaned.push(fm[i])
    i++
  }
  // Collapse blank-line runs left behind (e.g. where an old trigger block was)
  // and trim trailing blanks, then append the new trigger block.
  const collapsed: string[] = []
  for (const l of cleaned) {
    if (l.trim() === '' && collapsed.length && collapsed[collapsed.length - 1].trim() === '') continue
    collapsed.push(l)
  }
  while (collapsed.length && collapsed[collapsed.length - 1].trim() === '') collapsed.pop()
  const newFm = [...collapsed, ...triggerBlock.split(/\r?\n/)]
  const body = lines.slice(end + 1).join(nl)
  return `---${nl}${newFm.join(nl)}${nl}---${nl}${body}`
}

export interface AgentTriggerSettings {
  type: string
  args: Record<string, string>
  instructions: string
}

function unquoteYamlScalar(value: string): string {
  const trimmed = value.trim()
  if (trimmed.startsWith('[') && trimmed.endsWith(']')) {
    return trimmed
      .slice(1, -1)
      .split(',')
      .map((item) => item.trim().replace(/^['"]|['"]$/g, ''))
      .filter(Boolean)
      .join(', ')
  }
  return trimmed.replace(/^(['"])(.*)\1$/, '$2').replace(/\\"/g, '"').replace(/\\\\/g, '\\')
}

export function readAgentTriggerSettings(md: string): AgentTriggerSettings {
  const match = md.match(/^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)([\s\S]*)$/)
  if (!match) return { type: '', args: {}, instructions: md.trim() }

  const lines = match[1].split(/\r?\n/)
  const triggerStart = lines.findIndex((line) => /^trigger:\s*$/.test(line))
  if (triggerStart < 0) return { type: '', args: {}, instructions: match[2].replace(/^\r?\n/, '').trimEnd() }

  const args: Record<string, string> = {}
  let type = ''
  for (let index = triggerStart + 1; index < lines.length; index++) {
    const line = lines[index]
    if (line.trim() && !/^[ \t]/.test(line)) break
    const typeMatch = line.match(/^\s{2}type:\s*(.+?)\s*$/)
    if (typeMatch) type = unquoteYamlScalar(typeMatch[1])
    const argMatch = line.match(/^\s{4}([A-Za-z_][\w-]*):\s*(.*?)\s*$/)
    if (argMatch) args[argMatch[1]] = unquoteYamlScalar(argMatch[2])
  }
  return { type, args, instructions: match[2].replace(/^\r?\n/, '').trimEnd() }
}

export function applyInstructionsToMarkdown(md: string, instructions: string): string {
  const match = md.match(/^(---\r?\n[\s\S]*?\r?\n---)(?:\r?\n[\s\S]*)?$/)
  if (!match) return instructions.trimEnd()
  return `${match[1]}\n\n${instructions.trim()}\n`
}

export interface McpServer {
  type: string
  url: string
  tools?: string[]
  auth?: { scope: string; client_id?: string }
  headers?: Record<string, string>
}

// Add (or replace) a named server in an `mcp.json` document, creating the file
// shape if it's empty/absent. Returns pretty-printed JSON.
export function addMcpServer(jsonText: string, name: string, server: McpServer): string {
  let doc: { servers?: Record<string, unknown> } = {}
  const trimmed = (jsonText || '').trim()
  if (trimmed) {
    try {
      doc = JSON.parse(trimmed)
    } catch {
      doc = {}
    }
  }
  if (!doc || typeof doc !== 'object') doc = {}
  if (!doc.servers || typeof doc.servers !== 'object') doc.servers = {}
  doc.servers[name] = server
  return JSON.stringify(doc, null, 2) + '\n'
}

export interface McpPreset {
  label: string
  name: string
  description?: string
  server: McpServer
}

// Common connector-backed / remote MCP servers, taken from the runtime samples.
export const MCP_PRESETS: McpPreset[] = [
  {
    label: 'Microsoft Learn',
    name: 'microsoft-learn',
    description: 'Search official Microsoft/Azure docs. No auth required.',
    server: { type: 'http', url: 'https://learn.microsoft.com/api/mcp' },
  },
  {
    label: 'Office 365 Outlook',
    name: 'office365-outlook',
    description: 'Send email via Outlook. Needs a connection + O365 env vars.',
    server: {
      type: 'http',
      url: '$O365_MCP_SERVER_URL',
      tools: ['office365_SendEmailV2'],
      auth: { scope: 'https://apihub.azure.com/.default', client_id: '$O365_MCP_CLIENT_ID' },
    },
  },
]

// Slugify a skill name to the runtime's required kebab-case (a-z0-9 + single
// hyphens), max 64 chars.
export function skillSlug(name: string): string {
  const s = (name || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 64)
    .replace(/-+$/g, '')
  return s || 'skill'
}

// Build a SKILL.md: kebab-case `name` + `description` frontmatter + Markdown body.
export function buildSkillMd(name: string, description: string, body: string): string {
  const nm = skillSlug(name)
  const desc = '"' + (description || '').replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"'
  const content =
    (body || '').trim() ||
    `# ${name || nm}\n\nDescribe what this skill provides and how the agent should use it.`
  return ['---', `name: ${nm}`, `description: ${desc}`, '---', '', content, ''].join('\n')
}
