/**
 * "Scaffold New Agent App" — writes a deterministic baseline Azure Functions
 * agents-runtime project (offline, always valid).
 */
import * as vscode from "vscode";
import { buildScaffold } from "../core/appTemplates";

async function pickBaseFolder(): Promise<vscode.Uri | undefined> {
  const folders = vscode.workspace.workspaceFolders;
  if (folders && folders.length === 1) {
    return folders[0].uri;
  }
  if (folders && folders.length > 1) {
    const pick = await vscode.window.showWorkspaceFolderPick({ placeHolder: "Select where to create the app" });
    return pick?.uri;
  }
  const chosen = await vscode.window.showOpenDialog({
    canSelectFolders: true,
    canSelectFiles: false,
    openLabel: "Select parent folder for the new app",
  });
  return chosen?.[0];
}

async function exists(uri: vscode.Uri): Promise<boolean> {
  try {
    await vscode.workspace.fs.stat(uri);
    return true;
  } catch {
    return false;
  }
}

export async function scaffoldAppCommand(): Promise<void> {
  const base = await pickBaseFolder();
  if (!base) {
    return;
  }

  const appName = await vscode.window.showInputBox({
    title: "Scaffold agent app — folder name",
    prompt: 'Subfolder to create for the app. Use "." to scaffold directly into the selected folder.',
    value: "my-agent-app",
    ignoreFocusOut: true,
    validateInput: (v) => (v.trim() === "" ? "Enter a folder name (or '.')" : undefined),
  });
  if (appName === undefined) {
    return;
  }

  const target = appName.trim() === "." ? base : vscode.Uri.joinPath(base, appName.trim());

  if (await exists(vscode.Uri.joinPath(target, "function_app.py"))) {
    vscode.window.showErrorMessage(`An agent app already exists at ${target.fsPath} (function_app.py present). Aborting.`);
    return;
  }

  const includePick = await vscode.window.showQuickPick(
    [
      { label: "Yes — add an interactive main agent (chat UI + API + MCP)", value: true },
      { label: "No — start empty (add agents later)", value: false },
    ],
    { title: "Include a main chat agent?", ignoreFocusOut: true }
  );
  if (includePick === undefined) {
    return;
  }

  const model =
    (await vscode.window.showInputBox({
      title: "Scaffold agent app — default model",
      prompt: "FOUNDRY_MODEL placeholder for local.settings.json",
      value: "gpt-5.4",
      ignoreFocusOut: true,
    })) ?? "gpt-5.4";

  const files = buildScaffold({ includeMainChatAgent: includePick.value, model });

  await vscode.workspace.fs.createDirectory(target);
  for (const [rel, content] of Object.entries(files)) {
    const fileUri = vscode.Uri.joinPath(target, rel);
    await vscode.workspace.fs.writeFile(fileUri, Buffer.from(content, "utf-8"));
  }

  const openTarget = includePick.value
    ? vscode.Uri.joinPath(target, "main.agent.md")
    : vscode.Uri.joinPath(target, "function_app.py");
  const doc = await vscode.workspace.openTextDocument(openTarget);
  await vscode.window.showTextDocument(doc);

  const openFolder = await vscode.window.showInformationMessage(
    `Scaffolded agent app at ${target.fsPath}.`,
    "Open Folder"
  );
  if (openFolder === "Open Folder") {
    await vscode.commands.executeCommand("vscode.openFolder", target, { forceNewWindow: false });
  }
}
