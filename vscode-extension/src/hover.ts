/**
 * Hover documentation for front-matter keys and trigger-type values in `.agent.md`.
 */
import * as vscode from "vscode";
import { locateFrontMatter } from "./core/frontmatter";
import { getFieldDoc } from "./core/fieldDocs";
import { getTriggerInfo } from "./core/triggers";
import { isAgentDocument } from "./workspace";

export class AgentHoverProvider implements vscode.HoverProvider {
  provideHover(document: vscode.TextDocument, position: vscode.Position): vscode.Hover | undefined {
    if (!isAgentDocument(document)) {
      return undefined;
    }
    const fm = locateFrontMatter(document.getText());
    if (!fm) {
      return undefined;
    }
    const offset = document.offsetAt(position);
    if (offset < fm.baseOffset || offset > fm.closeFenceStart) {
      return undefined;
    }

    const wordRange = document.getWordRangeAtPosition(position, /[A-Za-z_][A-Za-z0-9_]*/);
    if (!wordRange) {
      return undefined;
    }
    const word = document.getText(wordRange);
    const lineText = document.lineAt(position.line).text;

    // Top-level key: indent 0 and the word is immediately followed by ':'.
    const isTopLevelKey = /^[A-Za-z_]/.test(lineText) && new RegExp(`^${word}\\s*:`).test(lineText.trimStart());
    if (isTopLevelKey) {
      const doc = getFieldDoc(word);
      if (doc) {
        const md = new vscode.MarkdownString();
        md.appendMarkdown(`**${doc.name}** — _${doc.detail}_\n\n${doc.documentation}`);
        return new vscode.Hover(md, wordRange);
      }
    }

    // Trigger type value (e.g. on a `type: timer_trigger` line).
    if (/^\s*type\s*:/.test(lineText)) {
      const info = getTriggerInfo(word);
      if (info) {
        const md = new vscode.MarkdownString();
        const args = info.fields.length
          ? info.fields.map((f) => `- \`${f.name}\`${f.required ? " (required)" : ""} — ${f.description}`).join("\n")
          : "_No args._";
        md.appendMarkdown(`**${info.label} trigger** (\`${info.type}\`)\n\n${info.description}\n\n${args}`);
        return new vscode.Hover(md, wordRange);
      }
    }

    return undefined;
  }
}
