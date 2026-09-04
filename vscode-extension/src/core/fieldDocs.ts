/**
 * Human-friendly documentation for top-level agent front-matter fields, used for
 * completion detail and hover. Sourced from docs/front-matter-reference.md.
 */

export interface FieldDoc {
  name: string;
  detail: string;
  documentation: string;
}

export const AGENT_FIELD_DOCS: FieldDoc[] = [
  { name: "name", detail: "string (required)", documentation: "Display name for the agent. Does not control the function name or route." },
  { name: "description", detail: "string (required)", documentation: "Brief description of the agent's purpose." },
  { name: "trigger", detail: "object", documentation: "Event/HTTP trigger. Required unless a built-in endpoint is enabled. One trigger per file." },
  { name: "builtin_endpoints", detail: "boolean | object", documentation: "Enable built-in chat UI, chat API, and/or MCP tool endpoints. `true` enables all." },
  { name: "model", detail: "string", documentation: "Override the LLM model for this agent (else inherits agents.config.yaml / env)." },
  { name: "timeout", detail: "number", documentation: "Execution timeout in seconds for this agent." },
  { name: "logger", detail: "boolean (default true)", documentation: "Enable/disable response logging for triggered agents." },
  { name: "substitute_variables", detail: "boolean (default true)", documentation: "Enable/disable $VAR / %VAR% environment substitution in front matter and body." },
  { name: "agent_configuration", detail: "object", documentation: "Portable output limits and Microsoft Agent Framework compaction settings. Inherits global values." },
  { name: "system_tools", detail: "object", documentation: "Opt out of system tools (dynamic_sessions_code_interpreter, web_request) for this agent." },
  { name: "mcp", detail: "boolean | object", documentation: "Filter MCP servers from mcp.json. `false` disables all; object with `exclude` list to filter." },
  { name: "skills", detail: "boolean | object", documentation: "Filter skills discovered under skills/. `false` disables all; object with `exclude` to filter." },
  { name: "tools", detail: "boolean | object", documentation: "Filter custom tools discovered under tools/. `false` disables all; object with `exclude` to filter." },
  { name: "workflows", detail: "object", documentation: "Enable Dynamic Workflows and grant leaf specialists. `enabled: true` plus optional `exclude`/`subagents`." },
  { name: "subagents", detail: "list", documentation: "Specialist agents this agent can delegate to as delegate_<slug> tools. Each item: { agent, when }." },
  { name: "input_schema", detail: "object", documentation: "JSON Schema used to validate the HTTP request body." },
  { name: "response_schema", detail: "object", documentation: "JSON Schema used to validate the structured JSON response." },
  { name: "response_example", detail: "string", documentation: "Example JSON response; makes an HTTP agent return structured JSON matching it." },
  { name: "metadata", detail: "object", documentation: "Free-form metadata for organization." },
];

export function getFieldDoc(name: string): FieldDoc | undefined {
  return AGENT_FIELD_DOCS.find((f) => f.name === name);
}
