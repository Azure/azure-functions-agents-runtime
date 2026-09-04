/**
 * CodeLens for agent files — shows the identity slug, an "Open Chat UI" action
 * (when the debug chat UI is enabled), and an "Add Subagent" shortcut above the
 * front matter.
 */
import * as vscode from "vscode";
import * as path from "path";
import { isAgentDocument } from "./workspace";
import { locateFrontMatter } from "./core/frontmatter";
import { summarizeAgent } from "./core/agentSummary";
import { slugFromFilename } from "./core/slug";

export class AgentCodeLensProvider implements vscode.CodeLensProvider {
  private readonly _onDidChange = new vscode.EventEmitter<void>();
  readonly onDidChangeCodeLenses = this._onDidChange.event;

  refresh(): void {
    this._onDidChange.fire();
  }

  provideCodeLenses(document: vscode.TextDocument): vscode.CodeLens[] {
    if (!isAgentDocument(document)) {
      return [];
    }
    const text = document.getText();
    const fm = locateFrontMatter(text);
    if (!fm) {
      return [];
    }

    const range = new vscode.Range(0, 0, 0, 0);
    const slug = slugFromFilename(path.basename(document.uri.fsPath));
    const summary = summarizeAgent(text);
    const lenses: vscode.CodeLens[] = [
      new vscode.CodeLens(range, { title: `$(hubot) ${slug}`, command: "" }),
    ];

    if (summary.hasChatUI) {
      lenses.push(
        new vscode.CodeLens(range, {
          title: "$(link-external) Open Chat UI",
          command: "agentsAuthoring.openChatUIForFile",
          arguments: [document.uri],
        })
      );
    }

    lenses.push(
      new vscode.CodeLens(range, {
        title: "$(person-add) Add Subagent",
        command: "agentsAuthoring.addSubagent",
      })
    );

    return lenses;
  }
}
