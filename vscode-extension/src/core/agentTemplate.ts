/**
 * Build a valid `.agent.md` file from wizard answers. Front matter is produced
 * with the `yaml` serializer to guarantee well-formed, correctly-typed YAML.
 */
import { stringify } from "yaml";

export interface BuiltinEndpointsDraft {
  debug_chat_ui?: boolean;
  chat_api?: boolean;
  mcp?: boolean;
}

export interface AgentDraft {
  name: string;
  description: string;
  /** Trigger type, or null/undefined for an endpoints-only agent. */
  triggerType?: string | null;
  triggerArgs?: Record<string, unknown>;
  /** `true` = enable all; object = specific toggles; undefined/false = none. */
  builtinEndpoints?: boolean | BuiltinEndpointsDraft;
  model?: string;
  timeout?: number;
  instructions?: string;
}

/** Best-effort convert a string entered in the wizard into a typed YAML scalar. */
export function coerceScalar(value: string): unknown {
  const trimmed = value.trim();
  if (trimmed === "") {
    return "";
  }
  if (trimmed === "true") {
    return true;
  }
  if (trimmed === "false") {
    return false;
  }
  if (/^-?\d+$/.test(trimmed)) {
    return Number.parseInt(trimmed, 10);
  }
  if ((trimmed.startsWith("[") && trimmed.endsWith("]")) || (trimmed.startsWith("{") && trimmed.endsWith("}"))) {
    try {
      return JSON.parse(trimmed);
    } catch {
      /* fall through to string */
    }
  }
  return value;
}

function endpointsActive(be: AgentDraft["builtinEndpoints"]): boolean {
  if (be === true) {
    return true;
  }
  if (be && typeof be === "object") {
    return !!(be.debug_chat_ui || be.chat_api || be.mcp);
  }
  return false;
}

function defaultBody(d: AgentDraft): string {
  return `You are ${d.name}. ${d.description}\n\nDescribe the task, the steps to take, and the expected output here.`;
}

export function buildAgentMarkdown(d: AgentDraft): string {
  const fm: Record<string, unknown> = { name: d.name, description: d.description };

  if (endpointsActive(d.builtinEndpoints)) {
    fm.builtin_endpoints = d.builtinEndpoints;
  }
  if (d.model) {
    fm.model = d.model;
  }
  if (typeof d.timeout === "number") {
    fm.timeout = d.timeout;
  }
  if (d.triggerType) {
    const trigger: Record<string, unknown> = { type: d.triggerType };
    if (d.triggerArgs && Object.keys(d.triggerArgs).length > 0) {
      trigger.args = d.triggerArgs;
    }
    fm.trigger = trigger;
  }

  const yaml = stringify(fm, { lineWidth: 0 });
  const body = (d.instructions ?? "").trim() || defaultBody(d);
  return `---\n${yaml}---\n\n${body}\n`;
}
