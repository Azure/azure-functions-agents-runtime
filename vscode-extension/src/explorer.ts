/**
 * Agents explorer — a tree view listing every agent in the workspace grouped
 * by app, with trigger / endpoint badges and click-to-open.
 */
import * as vscode from "vscode";
import * as path from "path";
import { isAgentFilename, slugFromFilename } from "./core/slug";
import { summarizeAgent, type AgentSummary } from "./core/agentSummary";
import { findAppRoot } from "./workspace";

const IGNORE_GLOB = "**/{node_modules,.venv,.git,__pycache__,.vscode-test,dist,out}/**";

class AppNode {
  readonly kind = "app" as const;
  constructor(public readonly root: vscode.Uri) {}
}

class AgentNode {
  readonly kind = "agent" as const;
  constructor(
    public readonly uri: vscode.Uri,
    public readonly slug: string,
    public readonly summary: AgentSummary
  ) {}
}

type TreeNode = AppNode | AgentNode;

interface AppIndexEntry {
  root: vscode.Uri;
  files: vscode.Uri[];
}

export class AgentsTreeProvider implements vscode.TreeDataProvider<TreeNode> {
  private readonly _onDidChange = new vscode.EventEmitter<TreeNode | undefined | void>();
  readonly onDidChangeTreeData = this._onDidChange.event;
  private indexPromise: Promise<Map<string, AppIndexEntry>> | undefined;

  refresh(): void {
    this.indexPromise = undefined;
    this._onDidChange.fire();
  }

  getTreeItem(node: TreeNode): vscode.TreeItem {
    if (node.kind === "app") {
      const item = new vscode.TreeItem(
        path.basename(node.root.fsPath) || node.root.fsPath,
        vscode.TreeItemCollapsibleState.Expanded
      );
      item.iconPath = new vscode.ThemeIcon("folder");
      item.resourceUri = node.root;
      item.contextValue = "hostedSkillsApp";
      item.tooltip = node.root.fsPath;
      return item;
    }

    const item = new vscode.TreeItem(
      node.summary.name || node.slug,
      vscode.TreeItemCollapsibleState.None
    );
    item.description = describe(node);
    item.tooltip = tooltip(node);
    item.iconPath = new vscode.ThemeIcon(node.summary.hasChatUI ? "comment-discussion" : "hubot");
    item.resourceUri = node.uri;
    item.contextValue = "hostedSkillsAgent";
    item.command = {
      command: "vscode.open",
      title: "Open Agent",
      arguments: [node.uri],
    };
    return item;
  }

  async getChildren(node?: TreeNode): Promise<TreeNode[]> {
    const index = await this.getIndex();
    if (!node) {
      const apps = [...index.values()];
      if (apps.length === 0) {
        return [];
      }
      if (apps.length === 1) {
        return this.agentsFor(apps[0]);
      }
      return apps
        .map((a) => new AppNode(a.root))
        .sort((a, b) => a.root.fsPath.localeCompare(b.root.fsPath));
    }
    if (node.kind === "app") {
      const entry = index.get(node.root.fsPath);
      return entry ? this.agentsFor(entry) : [];
    }
    return [];
  }

  private async agentsFor(entry: AppIndexEntry): Promise<AgentNode[]> {
    const nodes: AgentNode[] = [];
    for (const uri of entry.files) {
      let summary: AgentSummary;
      try {
        const bytes = await vscode.workspace.fs.readFile(uri);
        summary = summarizeAgent(Buffer.from(bytes).toString("utf-8"));
      } catch {
        summary = { endpointsEnabled: false, hasChatUI: false, subagents: [] };
      }
      nodes.push(new AgentNode(uri, slugFromFilename(path.basename(uri.fsPath)), summary));
    }
    return nodes.sort((a, b) => {
      if (a.slug === "main") {
        return -1;
      }
      if (b.slug === "main") {
        return 1;
      }
      return a.slug.localeCompare(b.slug);
    });
  }

  private getIndex(): Promise<Map<string, AppIndexEntry>> {
    if (!this.indexPromise) {
      this.indexPromise = this.buildIndex();
    }
    return this.indexPromise;
  }

  private async buildIndex(): Promise<Map<string, AppIndexEntry>> {
    const index = new Map<string, AppIndexEntry>();
    const folders = vscode.workspace.workspaceFolders ?? [];
    for (const folder of folders) {
      const uris = await vscode.workspace.findFiles(
        new vscode.RelativePattern(folder, "**/*.md"),
        IGNORE_GLOB
      );
      for (const uri of uris) {
        if (!isAgentFilename(path.basename(uri.fsPath))) {
          continue;
        }
        const root = await findAppRoot(uri);
        const key = root.fsPath;
        const existing = index.get(key);
        if (existing) {
          existing.files.push(uri);
        } else {
          index.set(key, { root, files: [uri] });
        }
      }
    }
    return index;
  }
}

function describe(node: AgentNode): string {
  const parts: string[] = [node.slug];
  if (node.summary.triggerType) {
    parts.push(node.summary.triggerType);
  }
  if (node.summary.hasChatUI) {
    parts.push("chat UI");
  } else if (node.summary.endpointsEnabled) {
    parts.push("endpoints");
  }
  if (node.summary.subagents.length > 0) {
    parts.push(`${node.summary.subagents.length} subagent${node.summary.subagents.length === 1 ? "" : "s"}`);
  }
  return parts.join(" · ");
}

function tooltip(node: AgentNode): string {
  const lines = [node.summary.name || node.slug];
  if (node.summary.description) {
    lines.push(node.summary.description);
  }
  lines.push("", `slug: ${node.slug}`);
  if (node.summary.triggerType) {
    lines.push(`trigger: ${node.summary.triggerType}`);
  }
  if (node.summary.subagents.length > 0) {
    lines.push(`subagents: ${node.summary.subagents.join(", ")}`);
  }
  return lines.join("\n");
}
