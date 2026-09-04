/**
 * Trigger metadata mirrored from TRIGGER_TYPES /_UNSUPPORTED_TRIGGER_TYPES in
 * src/azure_functions_agents/config/schema.py and config/validation.py.
 * Used by the Add Agent wizard, completions, and semantic validation.
 */

export interface TriggerArgField {
  name: string;
  required: boolean;
  description: string;
  /** Placeholder value used when building a starter snippet/template. */
  placeholder: string;
}

export interface TriggerTypeInfo {
  type: string;
  label: string;
  description: string;
  fields: TriggerArgField[];
}

export const TRIGGER_TYPES: TriggerTypeInfo[] = [
  {
    type: "http_trigger",
    label: "HTTP",
    description: "Expose the agent as a REST endpoint.",
    fields: [
      { name: "route", required: true, description: "URL path for the HTTP endpoint", placeholder: "my-route" },
      { name: "methods", required: false, description: 'HTTP methods, e.g. ["POST"]', placeholder: '["POST"]' },
      { name: "auth_level", required: false, description: "anonymous | function | admin", placeholder: "function" },
    ],
  },
  {
    type: "timer_trigger",
    label: "Timer (schedule)",
    description: "Run the agent on an NCRONTAB schedule.",
    fields: [
      { name: "schedule", required: true, description: "NCRONTAB expression (6 fields, or 5 with seconds prepended)", placeholder: "0 0 9 * * *" },
    ],
  },
  {
    type: "queue_trigger",
    label: "Azure Storage Queue",
    description: "Run when a message lands on a storage queue.",
    fields: [
      { name: "queue_name", required: true, description: "Azure Queue Storage queue name", placeholder: "my-queue" },
      { name: "connection", required: true, description: "App setting for the storage connection", placeholder: "AzureWebJobsStorage" },
    ],
  },
  {
    type: "blob_trigger",
    label: "Blob",
    description: "Run when a blob is created/updated.",
    fields: [
      { name: "path", required: true, description: 'Blob path pattern, e.g. "uploads/{name}.txt"', placeholder: "uploads/{name}" },
      { name: "connection", required: false, description: "App setting for the connection string", placeholder: "AzureWebJobsStorage" },
    ],
  },
  {
    type: "event_grid_trigger",
    label: "Event Grid",
    description: "Receive Event Grid events (no args).",
    fields: [],
  },
  {
    type: "service_bus_queue_trigger",
    label: "Service Bus Queue",
    description: "Run on a Service Bus queue message.",
    fields: [
      { name: "queue_name", required: true, description: "Service Bus queue name", placeholder: "my-queue" },
      { name: "connection", required: true, description: "App setting for the connection", placeholder: "ServiceBusConnection" },
    ],
  },
  {
    type: "service_bus_topic_trigger",
    label: "Service Bus Topic",
    description: "Run on a Service Bus topic subscription.",
    fields: [
      { name: "topic_name", required: true, description: "Service Bus topic name", placeholder: "my-topic" },
      { name: "subscription_name", required: true, description: "Service Bus subscription name", placeholder: "my-subscription" },
      { name: "connection", required: true, description: "App setting for the connection", placeholder: "ServiceBusConnection" },
    ],
  },
  {
    type: "connector_trigger",
    label: "Connector",
    description: "Receive Connector Namespace events (no args).",
    fields: [],
  },
];

export const TRIGGER_TYPE_NAMES: string[] = TRIGGER_TYPES.map((t) => t.type);

/** Known-but-unsupported trigger types → explanatory message (from validation.py). */
export const UNSUPPORTED_TRIGGER_TYPES: Record<string, string> = {
  activity_trigger: "Durable Functions triggers are not supported as agent triggers.",
  assistant_skill_trigger:
    "Assistant skill triggers are not supported as agent triggers; use agent tools or MCP surfaces instead.",
  entity_trigger: "Durable Functions triggers are not supported as agent triggers.",
  mcp_prompt_trigger: "MCP prompt triggers are registered by built-in endpoints, not agent trigger front matter.",
  mcp_resource_trigger: "MCP resource triggers are registered by built-in endpoints, not agent trigger front matter.",
  mcp_tool_trigger: "MCP tool triggers are registered by built-in endpoints, not agent trigger front matter.",
  orchestration_trigger: "Durable Functions triggers are not supported as agent triggers.",
  route: "Use `http_trigger` instead of the Azure Functions `route` decorator name.",
  schedule: "Use `timer_trigger` instead of the Azure Functions `schedule` decorator alias.",
  warm_up_trigger: "Warm-up triggers are host lifecycle hooks and are not supported as agent triggers.",
};

export function getTriggerInfo(type: string): TriggerTypeInfo | undefined {
  return TRIGGER_TYPES.find((t) => t.type === type);
}

/**
 * Validate an NCRONTAB-ish schedule string field count. The runtime accepts a
 * 6-field expression, or a 5-field expression (seconds prepended internally).
 */
export function isPlausibleCron(schedule: string): boolean {
  const parts = schedule.trim().split(/\s+/).filter(Boolean);
  return parts.length === 5 || parts.length === 6;
}
