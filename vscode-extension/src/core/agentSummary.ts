/**
 * Pure (vscode-free) summary of an agent file's front matter, used by the
 * Agents explorer tree and the CodeLens provider.
 */
import { locateFrontMatter } from "./frontmatter";

export interface AgentSummary {
  name?: string;
  description?: string;
  /** Trigger type (e.g. "http", "timer", "queue") if a trigger is declared. */
  triggerType?: string;
  /** True if any built-in endpoints are enabled. */
  endpointsEnabled: boolean;
  /** True if the debug chat UI endpoint is available. */
  hasChatUI: boolean;
  /** Slugs of referenced subagents. */
  subagents: string[];
}

const EMPTY: AgentSummary = {
  endpointsEnabled: false,
  hasChatUI: false,
  subagents: [],
};

/** Parse an agent markdown file into a lightweight summary. */
export function summarizeAgent(text: string): AgentSummary {
  const fm = locateFrontMatter(text);
  if (!fm) {
    return { ...EMPTY };
  }

  let data: unknown;
  try {
    data = fm.doc.toJS();
  } catch {
    return { ...EMPTY };
  }
  if (!data || typeof data !== "object") {
    return { ...EMPTY };
  }
  const obj = data as Record<string, unknown>;

  const name = typeof obj.name === "string" ? obj.name : undefined;
  const description = typeof obj.description === "string" ? obj.description : undefined;

  let triggerType: string | undefined;
  const trigger = obj.trigger;
  if (trigger && typeof trigger === "object") {
    const t = (trigger as Record<string, unknown>).type;
    if (typeof t === "string") {
      triggerType = t;
    }
  }

  let endpointsEnabled = false;
  let hasChatUI = false;
  const be = obj.builtin_endpoints;
  if (be === true) {
    endpointsEnabled = true;
    hasChatUI = true;
  } else if (be && typeof be === "object") {
    endpointsEnabled = true;
    hasChatUI = (be as Record<string, unknown>).debug_chat_ui === true;
  }

  const subagents: string[] = [];
  const subs = obj.subagents;
  if (Array.isArray(subs)) {
    for (const s of subs) {
      if (s && typeof s === "object") {
        const a = (s as Record<string, unknown>).agent;
        if (typeof a === "string" && a.trim()) {
          subagents.push(a.trim());
        }
      }
    }
  }

  return { name, description, triggerType, endpointsEnabled, hasChatUI, subagents };
}
