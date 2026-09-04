/**
 * Local run helper: open an agent's built-in chat UI in the browser.
 */
import * as vscode from "vscode";
import * as path from "path";
import { findAppRoot, collectAgentFiles } from "../workspace";
import { locateFrontMatter } from "../core/frontmatter";
import { slugFromFilename } from "../core/slug";

async function resolveAppRoot(): Promise<vscode.Uri | undefined> {
  const active = vscode.window.activeTextEditor?.document.uri;
  if (active && active.scheme === "file") {
    return findAppRoot(active);
  }
  const folders = vscode.workspace.workspaceFolders;
  if (!folders || folders.length === 0) {
    return undefined;
  }
  const base = folders.length === 1 ? folders[0].uri : (await vscode.window.showWorkspaceFolderPick())?.uri;
  if (!base) {
    return undefined;
  }
  const found = await vscode.workspace.findFiles(
    new vscode.RelativePattern(base, "**/function_app.py"),
    "**/{node_modules,.venv,.git}/**",
    1
  );
  return found.length ? vscode.Uri.file(path.dirname(found[0].fsPath)) : base;
}

function endpointsEnabled(builtin: unknown): boolean {
  if (builtin === true) {
    return true;
  }
  if (builtin && typeof builtin === "object") {
    const b = builtin as Record<string, unknown>;
    return b.debug_chat_ui === true || b.chat_api === true || b.mcp === true;
  }
  return false;
}

export async function openChatUICommand(): Promise<void> {
  const appRoot = await resolveAppRoot();
  if (!appRoot) {
    vscode.window.showErrorMessage("Could not locate an agent app. Open the app folder first.");
    return;
  }

  const entries = await collectAgentFiles(appRoot);
  const eligible: string[] = [];
  for (const entry of entries) {
    try {
      const bytes = await vscode.workspace.fs.readFile(entry.uri);
      const fm = locateFrontMatter(Buffer.from(bytes).toString("utf-8"));
      if (fm) {
        const data = fm.doc.toJS() as { builtin_endpoints?: unknown } | null;
        if (data && endpointsEnabled(data.builtin_endpoints)) {
          eligible.push(entry.slug);
        }
      }
    } catch {
      /* ignore unreadable files */
    }
  }

  const choices = eligible.length > 0 ? eligible : entries.map((e) => e.slug);
  if (choices.length === 0) {
    vscode.window.showWarningMessage("No agents found in this app.");
    return;
  }

  const slug =
    choices.length === 1
      ? choices[0]
      : await vscode.window.showQuickPick([...new Set(choices)], {
          title: "Open chat UI for agent",
          placeHolder: eligible.length === 0 ? "No agent has built-in endpoints enabled; picking anyway" : undefined,
        });
  if (!slug) {
    return;
  }

  const baseUrl = vscode.workspace
    .getConfiguration("agentsAuthoring")
    .get<string>("localRun.baseUrl", "http://localhost:7071")
    .replace(/\/+$/, "");
  const url = `${baseUrl}/agents/${slug}/`;
  await vscode.env.openExternal(vscode.Uri.parse(url));
}

/** Build the local chat UI URL for an agent slug. */
export function chatUiUrl(slug: string): string {
  const baseUrl = vscode.workspace
    .getConfiguration("agentsAuthoring")
    .get<string>("localRun.baseUrl", "http://localhost:7071")
    .replace(/\/+$/, "");
  return `${baseUrl}/agents/${slug}/`;
}

/** Open the chat UI for a specific agent file (used by CodeLens / explorer). */
export async function openChatUIForFileCommand(uri?: vscode.Uri): Promise<void> {
  const target = uri ?? vscode.window.activeTextEditor?.document.uri;
  if (!target || target.scheme !== "file") {
    vscode.window.showErrorMessage("Open an agent file first.");
    return;
  }
  const slug = slugFromFilename(path.basename(target.fsPath));
  await vscode.env.openExternal(vscode.Uri.parse(chatUiUrl(slug)));
}
