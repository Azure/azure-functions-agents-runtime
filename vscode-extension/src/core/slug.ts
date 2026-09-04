/**
 * Slug / function-name derivation, ported from
 * src/azure_functions_agents/_slug.py so the extension computes the exact same
 * identity slug the runtime uses (function name + endpoint route + delegate tool
 * name). Keep in sync with _slug.py.
 */

export function safeFunctionName(raw: string): string {
  const name = raw.replace(/[^a-zA-Z0-9_]/g, "_").replace(/^_+|_+$/g, "");
  if (!name) {
    return "agent_function";
  }
  if (/^[0-9]/.test(name)) {
    return `fn_${name}`;
  }
  return name;
}

/** Return the identity slug for an agent file name (not a full path). */
export function slugFromFilename(filename: string): string {
  const lower = filename.toLowerCase();
  if (lower === "agent.md" || lower === "claude.md") {
    return "main";
  }
  if (lower.endsWith(".claude.md")) {
    return safeFunctionName(filename.slice(0, -".claude.md".length));
  }
  if (lower.endsWith(".agent.md")) {
    return safeFunctionName(filename.slice(0, -".agent.md".length));
  }
  const stem = filename.replace(/\.[^.]*$/, "");
  return safeFunctionName(stem);
}

/** True for agent.md, CLAUDE.md, *.agent.md and *.claude.md (case-insensitive). */
export function isAgentFilename(filename: string): boolean {
  const lower = filename.toLowerCase();
  return (
    lower === "agent.md" ||
    lower === "claude.md" ||
    lower.endsWith(".agent.md") ||
    lower.endsWith(".claude.md")
  );
}

/** True for the single-agent aliases that all resolve to slug "main". */
export function isMainAlias(filename: string): boolean {
  const lower = filename.toLowerCase();
  return lower === "agent.md" || lower === "claude.md" || lower === "main.agent.md";
}
