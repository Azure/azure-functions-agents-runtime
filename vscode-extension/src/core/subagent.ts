/**
 * Pure (vscode-free) helper to insert a `subagents:` entry into an agent
 * file's YAML front matter, preserving existing keys, comments, and body.
 *
 * A subagent reference is object form only: `{ agent: <slug>, when?: <hint> }`
 * where `agent` is the specialist agent's file-stem slug (resolved app-wide).
 */
import { parseDocument, YAMLSeq, isMap } from "yaml";
import { locateFrontMatter } from "./frontmatter";

export interface SubagentEntry {
  /** The specialist agent's slug (its file stem). */
  agent: string;
  /** Optional routing hint surfaced to the coordinator model. */
  when?: string;
}

export type AddSubagentResult =
  | { ok: true; text: string }
  | { ok: false; error: string };

/** Insert a subagent reference into the front matter of `fullText`. */
export function addSubagent(fullText: string, entry: SubagentEntry): AddSubagentResult {
  const agent = entry.agent.trim();
  if (!agent) {
    return { ok: false, error: "Subagent slug must not be empty." };
  }

  const fm = locateFrontMatter(fullText);
  if (!fm) {
    return { ok: false, error: "No front matter (--- ... ---) found in this file." };
  }

  const doc = parseDocument(fm.yamlText);
  if (doc.errors.length > 0) {
    return { ok: false, error: `Front matter is not valid YAML: ${doc.errors[0].message}` };
  }

  let seq = doc.get("subagents") as unknown;
  if (seq == null) {
    seq = new YAMLSeq();
    doc.set("subagents", seq);
  }
  if (!(seq instanceof YAMLSeq)) {
    return { ok: false, error: "`subagents` already exists but is not a list." };
  }

  for (const item of seq.items) {
    if (isMap(item)) {
      const existing = item.get("agent");
      if (typeof existing === "string" && existing.trim() === agent) {
        return { ok: false, error: `Subagent '${agent}' is already referenced.` };
      }
    }
  }

  const when = entry.when?.trim();
  const node = doc.createNode(when ? { agent, when } : { agent });
  seq.add(node);

  let newYaml = doc.toString();
  if (!newYaml.endsWith("\n")) {
    newYaml += "\n";
  }
  const before = fullText.slice(0, fm.baseOffset);
  const after = fullText.slice(fm.closeFenceStart);
  return { ok: true, text: before + newYaml + after };
}
