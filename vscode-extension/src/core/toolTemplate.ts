/**
 * Scaffold for a user tool discovered from an app's `tools/` folder.
 *
 * The runtime (src/azure_functions_agents/discovery/tools.py) loads each
 * `tools/*.py` file whose name does not start with `_`, and registers the first
 * public function (or `@tool`-decorated value) it finds. For a plain function
 * the docstring becomes the tool description and the signature becomes the
 * argument schema. We therefore emit a single, well-documented plain function.
 */

const PY_KEYWORDS = new Set([
  "False", "None", "True", "and", "as", "assert", "async", "await", "break",
  "class", "continue", "def", "del", "elif", "else", "except", "finally", "for",
  "from", "global", "if", "import", "in", "is", "lambda", "nonlocal", "not",
  "or", "pass", "raise", "return", "try", "while", "with", "yield",
]);

/** Convert an arbitrary label into a snake_case Python identifier. */
export function toPythonIdentifier(raw: string): string {
  let name = raw
    .trim()
    .replace(/([A-Z]+)([A-Z][a-z])/g, "$1_$2")
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .toLowerCase()
    .replace(/[^a-z0-9_]+/g, "_")
    .replace(/_+/g, "_")
    .replace(/^_+|_+$/g, "");
  if (name === "") {
    name = "my_tool";
  }
  if (/^[0-9]/.test(name)) {
    name = `tool_${name}`;
  }
  if (PY_KEYWORDS.has(name)) {
    name = `${name}_tool`;
  }
  return name;
}

/** File name (`<name>.py`) for a tool module. Never starts with `_`. */
export function toolFileName(funcName: string): string {
  return `${toPythonIdentifier(funcName)}.py`;
}

export interface ToolTemplateOptions {
  /** Function/tool name (will be normalized to a Python identifier). */
  name: string;
  /** One-line description used as the tool's docstring/description. */
  description: string;
  /** Emit an `async def` (useful for I/O-bound tools). Defaults to false. */
  async?: boolean;
}

/** Build the Python source for a single-function tool module. */
export function buildToolPython(opts: ToolTemplateOptions): string {
  const func = toPythonIdentifier(opts.name);
  const description = (opts.description || `Tool: ${func}`).trim();
  const def = opts.async ? "async def" : "def";
  return `"""${description}"""

from __future__ import annotations


${def} ${func}(query: str) -> dict:
    """${description}

    Args:
        query: Describe the input this tool expects.

    Returns:
        A JSON-serializable result the agent can use.
    """
    # TODO: implement the tool. The return value is passed back to the model.
    return {"query": query}
`;
}
