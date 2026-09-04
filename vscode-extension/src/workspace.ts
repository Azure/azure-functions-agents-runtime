/**
 * Workspace helpers: locate the agent app root, enumerate agent files, and
 * detect identity-slug collisions (which the runtime treats as a fatal error).
 */
import * as vscode from "vscode";
import * as path from "path";
import { isAgentFilename, slugFromFilename } from "./core/slug";

const APP_ROOT_MARKERS = ["function_app.py", "agents.config.yaml", "host.json"];
const IGNORE_GLOB = "**/{node_modules,.venv,.git,__pycache__,.vscode-test,dist,out}/**";

export function isAgentDocument(document: vscode.TextDocument): boolean {
  return document.uri.scheme === "file" && isAgentFilename(path.basename(document.uri.fsPath));
}

export function isAgentsConfigDocument(document: vscode.TextDocument): boolean {
  return document.uri.scheme === "file" && path.basename(document.uri.fsPath).toLowerCase() === "agents.config.yaml";
}

/**
 * True when `uri` is an agent file (`*.agent.md` / `*.claude.md` / `agent.md` /
 * `CLAUDE.md`) that lives inside a Hosted Skills app — i.e. an ancestor directory
 * (up to the workspace folder) contains one of the app-root markers. Used to
 * decide whether to treat the file as Markdown instead of VS Code's built-in
 * `chatagent` language.
 */
export async function isHostedSkillsAgentUri(uri: vscode.Uri): Promise<boolean> {
  if (uri.scheme !== "file" || !isAgentFilename(path.basename(uri.fsPath))) {
    return false;
  }
  const workspaceFolder = vscode.workspace.getWorkspaceFolder(uri);
  const stopAt = workspaceFolder ? workspaceFolder.uri.fsPath : path.parse(uri.fsPath).root;
  let dir = path.dirname(uri.fsPath);
  for (;;) {
    for (const marker of APP_ROOT_MARKERS) {
      try {
        await vscode.workspace.fs.stat(vscode.Uri.file(path.join(dir, marker)));
        return true;
      } catch {
        /* marker not here */
      }
    }
    if (dir === stopAt || path.dirname(dir) === dir) {
      break;
    }
    dir = path.dirname(dir);
  }
  return false;
}

/**
 * Walk up from a file to the nearest directory containing an app-root marker.
 * Falls back to the containing workspace folder, then the file's directory.
 */
export async function findAppRoot(fileUri: vscode.Uri): Promise<vscode.Uri> {
  let dir = vscode.Uri.file(path.dirname(fileUri.fsPath));
  const workspaceFolder = vscode.workspace.getWorkspaceFolder(fileUri);
  const stopAt = workspaceFolder ? workspaceFolder.uri.fsPath : path.parse(fileUri.fsPath).root;

  // Agent files may live in an `agents/` subfolder; start the search one level
  // up in that case so markers in the app root are found.
  for (;;) {
    for (const marker of APP_ROOT_MARKERS) {
      try {
        await vscode.workspace.fs.stat(vscode.Uri.joinPath(dir, marker));
        return dir;
      } catch {
        /* marker not here */
      }
    }
    if (dir.fsPath === stopAt || path.dirname(dir.fsPath) === dir.fsPath) {
      break;
    }
    dir = vscode.Uri.file(path.dirname(dir.fsPath));
  }
  return workspaceFolder ? workspaceFolder.uri : vscode.Uri.file(path.dirname(fileUri.fsPath));
}

export interface AgentFileEntry {
  uri: vscode.Uri;
  slug: string;
}

/**
 * Resolve the app root to act on: the active editor's app, or the (single or
 * user-picked) workspace folder. Returns undefined when no folder is open.
 */
export async function resolveAppRoot(): Promise<vscode.Uri | undefined> {
  const active = vscode.window.activeTextEditor?.document.uri;
  if (active && active.scheme === "file") {
    return findAppRoot(active);
  }
  const folders = vscode.workspace.workspaceFolders;
  if (!folders || folders.length === 0) {
    return undefined;
  }
  if (folders.length === 1) {
    return folders[0].uri;
  }
  const pick = await vscode.window.showWorkspaceFolderPick({ placeHolder: "Select the agent app folder" });
  return pick?.uri;
}

/** All agent files under an app root, with their computed identity slugs. */
export async function collectAgentFiles(appRoot: vscode.Uri): Promise<AgentFileEntry[]> {
  const pattern = new vscode.RelativePattern(appRoot, "**/*.md");
  const uris = await vscode.workspace.findFiles(pattern, IGNORE_GLOB);
  const entries: AgentFileEntry[] = [];
  for (const uri of uris) {
    const base = path.basename(uri.fsPath);
    if (isAgentFilename(base)) {
      entries.push({ uri, slug: slugFromFilename(base) });
    }
  }
  return entries;
}

/** Return other agent files that share this file's slug within the same app. */
export async function findSlugCollisions(fileUri: vscode.Uri): Promise<string[]> {
  const base = path.basename(fileUri.fsPath);
  const slug = slugFromFilename(base);
  const appRoot = await findAppRoot(fileUri);
  const entries = await collectAgentFiles(appRoot);
  return entries
    .filter((e) => e.slug === slug && e.uri.fsPath !== fileUri.fsPath)
    .map((e) => path.relative(appRoot.fsPath, e.uri.fsPath) || path.basename(e.uri.fsPath));
}
