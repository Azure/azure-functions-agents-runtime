import * as YAML from 'js-yaml'

const SUPPORTED_TRIGGER_TYPES = new Set([
  'http_trigger',
  'timer_trigger',
  'queue_trigger',
  'blob_trigger',
  'event_grid_trigger',
  'event_hub_message_trigger',
  'service_bus_queue_trigger',
  'service_bus_topic_trigger',
  'cosmos_db_trigger',
  'cosmos_db_trigger_v3',
  'sql_trigger',
  'mysql_trigger',
  'kafka_trigger',
  'dapr_binding_trigger',
  'dapr_service_invocation_trigger',
  'dapr_topic_trigger',
  'generic_trigger',
  'connector_trigger',
])

const REJECTED_TRIGGER_TYPES = new Set([
  'route',
  'schedule',
  'activity_trigger',
  'orchestration_trigger',
  'entity_trigger',
  'warm_up_trigger',
  'assistant_skill_trigger',
  'mcp_tool_trigger',
  'mcp_resource_trigger',
  'mcp_prompt_trigger',
])

const TRIGGER_REQUIRED_ARGS = {
  http_trigger: ['route'],
  timer_trigger: ['schedule'],
  queue_trigger: ['queue_name', 'connection'],
  blob_trigger: ['path'],
  event_hub_message_trigger: ['event_hub_name', 'connection'],
  service_bus_queue_trigger: ['queue_name', 'connection'],
  service_bus_topic_trigger: ['topic_name', 'subscription_name', 'connection'],
}

export function validateAgentFrontmatter(front) {
  const errors = []
  const warnings = []
  if (!front || typeof front !== 'object' || Array.isArray(front)) {
    errors.push({ path: '/', message: 'Missing or invalid YAML frontmatter block.' })
    return { errors, warnings }
  }
  if (!front.name || typeof front.name !== 'string' || !front.name.trim()) {
    errors.push({ path: '/name', message: 'name is required and must be a non-empty string.' })
  }
  if (!front.description || typeof front.description !== 'string' || !front.description.trim()) {
    errors.push({ path: '/description', message: 'description is required and must be a non-empty string.' })
  }
  const hasBuiltin =
    front.builtin_endpoints === true ||
    (front.builtin_endpoints && typeof front.builtin_endpoints === 'object')
  const trigger = front.trigger
  if (!hasBuiltin && !trigger) {
    errors.push({
      path: '/trigger',
      message:
        'Either a trigger: block or builtin_endpoints: true is required (agents that only expose MCP still need a trigger).',
    })
  }
  if (trigger) {
    if (typeof trigger !== 'object' || Array.isArray(trigger)) {
      errors.push({ path: '/trigger', message: 'trigger must be an object with type and args.' })
    } else {
      const type = String(trigger.type ?? '').trim()
      if (!type) {
        errors.push({ path: '/trigger/type', message: 'trigger.type is required.' })
      } else if (REJECTED_TRIGGER_TYPES.has(type)) {
        errors.push({
          path: '/trigger/type',
          message: `trigger.type "${type}" is not a runtime-supported trigger — see docs/triggers.md.`,
        })
      } else if (type.includes('.')) {
        errors.push({
          path: '/trigger/type',
          message: 'Dotted connector types (e.g. teams.new_channel_message_trigger) are not supported — use generic_trigger with args.type.',
        })
      } else if (!SUPPORTED_TRIGGER_TYPES.has(type)) {
        warnings.push({
          path: '/trigger/type',
          message: `Unknown trigger type "${type}". Continuing but expect a runtime error.`,
        })
      } else {
        const required = TRIGGER_REQUIRED_ARGS[type] ?? []
        const args =
          trigger.args && typeof trigger.args === 'object' && !Array.isArray(trigger.args) ? trigger.args : {}
        for (const key of required) {
          const value = args[key]
          if (value == null || (typeof value === 'string' && !value.trim())) {
            errors.push({ path: `/trigger/args/${key}`, message: `${key} is required for ${type}.` })
          }
        }
      }
    }
  }
  return { errors, warnings }
}

export function validateAgentMarkdown(content) {
  const text = String(content ?? '')
  const match = /^---\s*\r?\n([\s\S]*?)\r?\n---(?:\s*\r?\n|$)/.exec(text)
  let front = null
  if (match) {
    try {
      const parsed = YAML.load(match[1])
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) front = parsed
    } catch {
      front = null
    }
  }
  const result = validateAgentFrontmatter(front)
  return { ok: result.errors.length === 0, ...result, front }
}

export function validateAgentFiles(files) {
  const failures = []
  for (const file of files) {
    if (!String(file?.name ?? '').toLowerCase().endsWith('.agent.md')) continue
    const content = Buffer.isBuffer(file.data) ? file.data.toString('utf-8') : String(file.data ?? '')
    const result = validateAgentMarkdown(content)
    if (!result.ok) failures.push({ file: file.name, errors: result.errors })
  }
  return { ok: failures.length === 0, failures }
}