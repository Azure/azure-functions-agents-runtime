/**
 * Pure semantic checks for agent front matter that go beyond JSON-Schema shape:
 * trigger-vs-endpoints, trigger arg completeness, cron plausibility, and unsupported
 * trigger types. Slug-collision checks live in the workspace layer (needs the file set).
 */
import { getTriggerInfo, isPlausibleCron, TRIGGER_TYPE_NAMES, UNSUPPORTED_TRIGGER_TYPES } from "./triggers";

export type IssueSeverity = "error" | "warning";

export interface SemanticIssue {
  message: string;
  severity: IssueSeverity;
  /** Value path to anchor the diagnostic (best effort). */
  path: Array<string | number>;
}

function endpointsEnabled(builtin: unknown): boolean {
  if (builtin === true) {
    return true;
  }
  if (builtin && typeof builtin === "object") {
    const b = builtin as Record<string, unknown>;
    return b.debug_chat_ui === true || b.chat_api === true || b.mcp === true;
  }
  return false;
}

function isReferencedElsewhere(data: Record<string, unknown>): boolean {
  // An agent used only as a delegation/workflow specialist may omit trigger + endpoints.
  return Array.isArray(data.subagents) || (data.workflows != null && typeof data.workflows === "object");
}

export function checkAgentSemantics(data: unknown): SemanticIssue[] {
  const issues: SemanticIssue[] = [];
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    return issues;
  }
  const obj = data as Record<string, unknown>;
  const trigger = obj.trigger as { type?: unknown; args?: unknown } | undefined | null;
  const hasTrigger = !!trigger && typeof trigger === "object";

  // trigger required unless endpoints enabled (or referenced as an internal specialist).
  if (!hasTrigger && !endpointsEnabled(obj.builtin_endpoints) && !isReferencedElsewhere(obj)) {
    issues.push({
      severity: "warning",
      path: [],
      message:
        "Agent has no `trigger` and no enabled `builtin_endpoints`. Add a trigger, enable a built-in endpoint, or reference it as a subagent (internal specialist).",
    });
  }

  if (hasTrigger) {
    const type = typeof trigger!.type === "string" ? (trigger!.type as string) : "";
    if (type) {
      const unsupported = UNSUPPORTED_TRIGGER_TYPES[type];
      if (unsupported) {
        issues.push({ severity: "warning", path: ["trigger", "type"], message: unsupported });
      } else {
        const info = getTriggerInfo(type);
        if (!info) {
          issues.push({
            severity: "warning",
            path: ["trigger", "type"],
            message: `Unknown trigger type "${type}". Supported: ${TRIGGER_TYPE_NAMES.join(", ")}.`,
          });
        } else {
          const args = (trigger!.args && typeof trigger!.args === "object" ? trigger!.args : {}) as Record<
            string,
            unknown
          >;
          for (const field of info.fields) {
            if (field.required && (args[field.name] === undefined || args[field.name] === null || args[field.name] === "")) {
              issues.push({
                severity: "warning",
                path: ["trigger", "args"],
                message: `Trigger "${type}" requires arg "${field.name}" (${field.description}).`,
              });
            }
          }
          if (type === "timer_trigger" && typeof args.schedule === "string" && !isPlausibleCron(args.schedule)) {
            issues.push({
              severity: "warning",
              path: ["trigger", "args", "schedule"],
              message: `Schedule "${args.schedule}" does not look like a valid NCRONTAB expression (expected 5 or 6 whitespace-separated fields).`,
            });
          }
        }
      }
    }
  }

  return issues;
}
