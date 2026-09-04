/**
 * "Add Agent" wizard — a native multi-step form that generates a valid
 * `<slug>.agent.md`. Uses QuickPick/InputBox so it needs no webview bundle.
 */
import * as vscode from "vscode";
import * as path from "path";
import { buildAgentMarkdown, coerceScalar, type AgentDraft, type BuiltinEndpointsDraft } from "../core/agentTemplate";
import { TRIGGER_TYPES, getTriggerInfo } from "../core/triggers";
import { safeFunctionName, slugFromFilename } from "../core/slug";
import { findAppRoot, collectAgentFiles } from "../workspace";

async function pickTargetFolder(): Promise<vscode.Uri | undefined> {
  const active = vscode.window.activeTextEditor?.document.uri;
  if (active && active.scheme === "file") {
    return findAppRoot(active);
  }
  const folders = vscode.workspace.workspaceFolders;
  if (folders && folders.length === 1) {
    return folders[0].uri;
  }
  if (folders && folders.length > 1) {
    const pick = await vscode.window.showWorkspaceFolderPick({ placeHolder: "Select the agent app folder" });
    return pick?.uri;
  }
  const chosen = await vscode.window.showOpenDialog({
    canSelectFolders: true,
    canSelectFiles: false,
    openLabel: "Select agent app folder",
  });
  return chosen?.[0];
}

async function collectTriggerArgs(triggerType: string): Promise<Record<string, unknown> | undefined> {
  const info = getTriggerInfo(triggerType);
  if (!info) {
    return {};
  }
  const args: Record<string, unknown> = {};
  for (const field of info.fields) {
    const value = await vscode.window.showInputBox({
      title: `${info.label} trigger — ${field.name}`,
      prompt: `${field.description}${field.required ? " (required)" : " (optional — leave blank to skip)"}`,
      value: field.required ? field.placeholder : "",
      ignoreFocusOut: true,
      validateInput: (v) => (field.required && v.trim() === "" ? `${field.name} is required` : undefined),
    });
    if (value === undefined) {
      return undefined; // cancelled
    }
    if (value.trim() !== "") {
      args[field.name] = coerceScalar(value);
    }
  }
  return args;
}

interface EndpointItem extends vscode.QuickPickItem {
  value: string;
  info: string;
}

const ENDPOINT_OPTIONS: EndpointItem[] = [
  {
    label: "All (chat UI + chat API + MCP)",
    value: "all",
    info:
      "Enable every built-in surface at once: the debug chat UI, the REST/streaming chat API, and the MCP tool endpoint. Equivalent to 'builtin_endpoints: true'.",
  },
  {
    label: "Debug chat UI",
    value: "debug_chat_ui",
    picked: true,
    info:
      "A browser-based chat page served at /agents/<slug>/ for trying the agent interactively while you develop. Intended for local/debug use, not production traffic.",
  },
  {
    label: "Chat API (REST + streaming)",
    value: "chat_api",
    info:
      "HTTP endpoints for programmatic access — send messages and receive responses (including streaming) from your own client or app, without the chat UI.",
  },
  {
    label: "MCP tool",
    value: "mcp",
    info:
      "Expose this agent as a Model Context Protocol (MCP) tool so MCP-compatible clients (other agents, IDEs, etc.) can discover and call it.",
  },
];

function resolveEndpoints(picks: readonly EndpointItem[]): boolean | BuiltinEndpointsDraft | undefined {
  if (picks.length === 0) {
    return undefined;
  }
  if (picks.some((p) => p.value === "all")) {
    return true;
  }
  const be: BuiltinEndpointsDraft = {};
  for (const p of picks) {
    (be as Record<string, boolean>)[p.value] = true;
  }
  return be;
}

async function pickEndpoints(): Promise<boolean | BuiltinEndpointsDraft | undefined> {
  return new Promise((resolve) => {
    const qp = vscode.window.createQuickPick<EndpointItem>();
    qp.title = "Built-in endpoints";
    qp.placeholder = "Select the interactive surfaces to expose — hover the ⓘ for details";
    qp.canSelectMany = true;
    qp.ignoreFocusOut = true;
    // Attach an info button per item; its tooltip shows on hover, and clicking
    // it reveals the full description (tooltips can be truncated).
    qp.items = ENDPOINT_OPTIONS.map((item) => ({
      ...item,
      buttons: [{ iconPath: new vscode.ThemeIcon("info"), tooltip: item.info }],
    }));
    qp.selectedItems = qp.items.filter((i) => i.picked);

    let accepted = false;
    qp.onDidTriggerItemButton((event) => {
      void vscode.window.showInformationMessage(`${event.item.label}: ${event.item.info}`);
    });
    qp.onDidAccept(() => {
      accepted = true;
      const picks = [...qp.selectedItems];
      qp.hide();
      resolve(resolveEndpoints(picks));
    });
    qp.onDidHide(() => {
      qp.dispose();
      if (!accepted) {
        resolve(undefined);
      }
    });
    qp.show();
  });
}

export async function addAgentCommand(): Promise<void> {
  const name = await vscode.window.showInputBox({
    title: "Add Agent — name",
    prompt: "Display name for the agent",
    ignoreFocusOut: true,
    validateInput: (v) => (v.trim() === "" ? "Name is required" : undefined),
  });
  if (name === undefined) {
    return;
  }

  const description = await vscode.window.showInputBox({
    title: "Add Agent — description",
    prompt: "Brief description of what this agent does",
    ignoreFocusOut: true,
    validateInput: (v) => (v.trim() === "" ? "Description is required" : undefined),
  });
  if (description === undefined) {
    return;
  }

  const invocation = await vscode.window.showQuickPick(
    [
      { label: "$(zap) Trigger", detail: "Event/HTTP/schedule-driven (timer, queue, http, connector, …)", value: "trigger" },
      { label: "$(comment-discussion) Endpoints only", detail: "Interactive chat UI / API / MCP tool", value: "endpoints" },
      { label: "$(zap) Trigger + endpoints", detail: "Triggered agent that also exposes a chat surface", value: "both" },
    ],
    { title: "How is this agent invoked?", ignoreFocusOut: true }
  );
  if (!invocation) {
    return;
  }

  const draft: AgentDraft = { name, description };

  if (invocation.value === "trigger" || invocation.value === "both") {
    const triggerPick = await vscode.window.showQuickPick(
      TRIGGER_TYPES.map((t) => ({ label: t.label, detail: `${t.type} — ${t.description}`, value: t.type })),
      { title: "Trigger type", ignoreFocusOut: true }
    );
    if (!triggerPick) {
      return;
    }
    const args = await collectTriggerArgs(triggerPick.value);
    if (args === undefined) {
      return;
    }
    draft.triggerType = triggerPick.value;
    draft.triggerArgs = args;
  }

  if (invocation.value === "endpoints" || invocation.value === "both") {
    const endpoints = await pickEndpoints();
    if (endpoints === undefined) {
      return;
    }
    draft.builtinEndpoints = endpoints;
  }

  const targetFolder = await pickTargetFolder();
  if (!targetFolder) {
    return;
  }

  const existing = await collectAgentFiles(targetFolder);
  const usedSlugs = new Set(existing.map((e) => e.slug));

  const defaultStem = safeFunctionName(name);
  const stem = await vscode.window.showInputBox({
    title: "Add Agent — file name",
    prompt: "File stem (becomes <stem>.agent.md and the agent's unique slug)",
    value: defaultStem,
    ignoreFocusOut: true,
    validateInput: (v) => {
      const trimmed = v.trim();
      if (trimmed === "") {
        return "File stem is required";
      }
      const slug = slugFromFilename(`${trimmed}.agent.md`);
      if (usedSlugs.has(slug)) {
        return `Slug "${slug}" already exists in this app. Choose another name.`;
      }
      return undefined;
    },
  });
  if (stem === undefined) {
    return;
  }

  const fileName = `${stem.trim()}.agent.md`;
  const targetUri = vscode.Uri.joinPath(targetFolder, fileName);

  try {
    await vscode.workspace.fs.stat(targetUri);
    const overwrite = await vscode.window.showWarningMessage(
      `${fileName} already exists. Overwrite?`,
      { modal: true },
      "Overwrite"
    );
    if (overwrite !== "Overwrite") {
      return;
    }
  } catch {
    /* does not exist — good */
  }

  const content = buildAgentMarkdown(draft);
  await vscode.workspace.fs.writeFile(targetUri, Buffer.from(content, "utf-8"));
  const doc = await vscode.workspace.openTextDocument(targetUri);
  await vscode.window.showTextDocument(doc);
  vscode.window.showInformationMessage(`Created ${path.basename(targetUri.fsPath)}`);
}
