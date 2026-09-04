/**
 * Front-matter completion for `.agent.md`: top-level keys, structured-block
 * snippets, and trigger-type values. Only fires inside the front-matter region
 * so it never interferes with normal markdown editing.
 */
import * as vscode from "vscode";
import { locateFrontMatter, topLevelKeys } from "./core/frontmatter";
import { AGENT_FIELD_DOCS } from "./core/fieldDocs";
import { TRIGGER_TYPES, TRIGGER_TYPE_NAMES } from "./core/triggers";
import { isAgentDocument } from "./workspace";

const TRIGGER_CHOICE = `\${1|${TRIGGER_TYPE_NAMES.join(",")}|}`;

const STRUCTURED_SNIPPETS: Record<string, string> = {
  trigger: `trigger:\n  type: ${TRIGGER_CHOICE}\n  args:\n    \${2}`,
  builtin_endpoints: "builtin_endpoints: ${1|true,false|}",
  subagents: "subagents:\n  - agent: ${1:slug}\n    when: ${2:When to delegate to this specialist}",
  agent_configuration: "agent_configuration:\n  max_output_tokens: ${1:4096}",
  system_tools: "system_tools:\n  web_request: ${1|true,false|}",
  mcp: "mcp:\n  exclude:\n    - ${1:server-name}",
  skills: "skills:\n  exclude:\n    - ${1:skill-name}",
  tools: "tools:\n  exclude:\n    - ${1:tool-name}",
  workflows: "workflows:\n  enabled: ${1|true,false|}",
};

const SCALAR_DEFAULT: Record<string, string> = {
  name: "${1:Agent Name}",
  description: "${1:What this agent does}",
  model: "${1:$FOUNDRY_MODEL}",
  timeout: "${1:900}",
  logger: "${1|true,false|}",
  substitute_variables: "${1|true,false|}",
  response_example: "|\n  ${1:{}}",
};

function inFrontMatter(document: vscode.TextDocument, position: vscode.Position): ReturnType<typeof locateFrontMatter> {
  const fm = locateFrontMatter(document.getText());
  if (!fm) {
    return undefined;
  }
  const offset = document.offsetAt(position);
  return offset >= fm.baseOffset && offset <= fm.closeFenceStart ? fm : undefined;
}

export class AgentCompletionProvider implements vscode.CompletionItemProvider {
  provideCompletionItems(
    document: vscode.TextDocument,
    position: vscode.Position
  ): vscode.CompletionItem[] | undefined {
    if (!isAgentDocument(document)) {
      return undefined;
    }
    const fm = inFrontMatter(document, position);
    if (!fm) {
      return undefined;
    }

    const linePrefix = document.lineAt(position.line).text.slice(0, position.character);

    // Trigger type value completion (the only `type:` key in agent front matter).
    if (/^\s*type\s*:\s*\S*$/.test(linePrefix)) {
      return TRIGGER_TYPES.map((t) => {
        const item = new vscode.CompletionItem(t.type, vscode.CompletionItemKind.EnumMember);
        item.detail = t.label;
        item.documentation = new vscode.MarkdownString(t.description);
        return item;
      });
    }

    // Top-level key completion: only when the line is a bare (indent-0) partial key.
    if (!/^[A-Za-z_]*$/.test(linePrefix)) {
      return undefined;
    }

    const present = new Set(topLevelKeys(fm));
    const items: vscode.CompletionItem[] = [];
    for (const field of AGENT_FIELD_DOCS) {
      if (present.has(field.name)) {
        continue;
      }
      const item = new vscode.CompletionItem(field.name, vscode.CompletionItemKind.Property);
      item.detail = field.detail;
      item.documentation = new vscode.MarkdownString(field.documentation);
      const snippetText = STRUCTURED_SNIPPETS[field.name] ?? `${field.name}: ${SCALAR_DEFAULT[field.name] ?? "${1}"}`;
      item.insertText = new vscode.SnippetString(snippetText);
      items.push(item);
    }
    return items;
  }
}
