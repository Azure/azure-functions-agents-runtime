/**
 * "Add Tool" command — scaffolds `tools/<name>.py` with a discoverable
 * plain-function tool.
 */
import * as vscode from "vscode";
import * as path from "path";
import { buildToolPython, toolFileName } from "../core/toolTemplate";
import { resolveAppRoot } from "../workspace";

export async function addToolCommand(): Promise<void> {
  const name = await vscode.window.showInputBox({
    title: "Add Tool — name",
    prompt: "Function name for the tool (e.g. get_weather)",
    ignoreFocusOut: true,
    validateInput: (v) => (v.trim() === "" ? "Name is required" : undefined),
  });
  if (name === undefined) {
    return;
  }

  const description = await vscode.window.showInputBox({
    title: "Add Tool — description",
    prompt: "One line describing what the tool does (becomes the tool description)",
    ignoreFocusOut: true,
    validateInput: (v) => (v.trim() === "" ? "Description is required" : undefined),
  });
  if (description === undefined) {
    return;
  }

  const style = await vscode.window.showQuickPick(
    [
      { label: "$(symbol-method) Synchronous", detail: "def — quick, CPU-bound logic", value: false },
      { label: "$(sync) Asynchronous", detail: "async def — network / I/O-bound work", value: true },
    ],
    { title: "Tool style", ignoreFocusOut: true }
  );
  if (!style) {
    return;
  }

  const appRoot = await resolveAppRoot();
  if (!appRoot) {
    vscode.window.showErrorMessage("Could not locate an agent app. Open the app folder first.");
    return;
  }

  const toolsDir = vscode.Uri.joinPath(appRoot, "tools");
  await vscode.workspace.fs.createDirectory(toolsDir);
  const fileName = toolFileName(name);
  const target = vscode.Uri.joinPath(toolsDir, fileName);

  try {
    await vscode.workspace.fs.stat(target);
    const overwrite = await vscode.window.showWarningMessage(
      `tools/${fileName} already exists. Overwrite?`,
      { modal: true },
      "Overwrite"
    );
    if (overwrite !== "Overwrite") {
      return;
    }
  } catch {
    /* does not exist — good */
  }

  const content = buildToolPython({ name, description, async: style.value });
  await vscode.workspace.fs.writeFile(target, Buffer.from(content, "utf-8"));
  const doc = await vscode.workspace.openTextDocument(target);
  await vscode.window.showTextDocument(doc);
  vscode.window.showInformationMessage(`Created tools/${path.basename(target.fsPath)}`);
}
