/**
 * "Add Subagent" command — inserts a `subagents:` entry into the active agent
 * file's front matter, delegating to another agent in the same app.
 */
import * as vscode from "vscode";
import * as path from "path";
import { addSubagent } from "../core/subagent";
import { slugFromFilename } from "../core/slug";
import { isAgentDocument, findAppRoot, collectAgentFiles } from "../workspace";

export async function addSubagentCommand(): Promise<void> {
  const editor = vscode.window.activeTextEditor;
  if (!editor || !isAgentDocument(editor.document)) {
    vscode.window.showErrorMessage("Open the coordinator agent file (*.agent.md) first.");
    return;
  }
  const doc = editor.document;

  const appRoot = await findAppRoot(doc.uri);
  const entries = await collectAgentFiles(appRoot);
  const selfSlug = slugFromFilename(path.basename(doc.uri.fsPath));
  const candidates = entries.filter((e) => e.slug !== selfSlug);
  if (candidates.length === 0) {
    vscode.window.showWarningMessage(
      "No other agents found in this app to delegate to. Add another agent first."
    );
    return;
  }

  const pick = await vscode.window.showQuickPick(
    candidates.map((e) => ({
      label: e.slug,
      detail: path.relative(appRoot.fsPath, e.uri.fsPath),
      value: e.slug,
    })),
    { title: "Delegate to which agent?", ignoreFocusOut: true, placeHolder: "Pick the specialist agent" }
  );
  if (!pick) {
    return;
  }

  const when = await vscode.window.showInputBox({
    title: "Subagent — routing hint (optional)",
    prompt: "When should the coordinator delegate to this agent? Blank = use the agent's own description.",
    ignoreFocusOut: true,
  });
  if (when === undefined) {
    return;
  }

  const result = addSubagent(doc.getText(), { agent: pick.value, when });
  if (!result.ok) {
    vscode.window.showErrorMessage(result.error);
    return;
  }

  const fullRange = new vscode.Range(doc.positionAt(0), doc.positionAt(doc.getText().length));
  await editor.edit((builder) => builder.replace(fullRange, result.text));
  vscode.window.showInformationMessage(`Added subagent '${pick.value}'.`);
}
