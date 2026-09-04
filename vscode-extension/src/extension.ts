import * as vscode from "vscode";
import { AgentDiagnostics } from "./validation";
import { AgentCompletionProvider } from "./completion";
import { AgentHoverProvider } from "./hover";
import { AgentCodeLensProvider } from "./codelens";
import { AgentsTreeProvider } from "./explorer";
import { addAgentCommand } from "./commands/addAgent";
import { addToolCommand } from "./commands/addTool";
import { addSkillCommand } from "./commands/addSkill";
import { addSubagentCommand } from "./commands/addSubagent";
import { scaffoldAppCommand } from "./commands/scaffoldApp";
import { openChatUICommand, openChatUIForFileCommand } from "./commands/localRun";
import { isAgentDocument, isAgentsConfigDocument, isHostedSkillsAgentUri } from "./workspace";

const AGENT_DOC_SELECTOR: vscode.DocumentSelector = [
  { scheme: "file", language: "markdown" },
  // VS Code's built-in `prompt-basics` extension assigns `*.agent.md` (and
  // `.chatmode.md`, `.github/agents/*.md`, `.claude/agents/*.md`) the `chatagent`
  // language, not `markdown`. Register there too or our providers never attach.
  { scheme: "file", language: "chatagent" },
];

function isRelevant(document: vscode.TextDocument): boolean {
  return isAgentDocument(document) || isAgentsConfigDocument(document);
}

/**
 * Hosted Skills reuses the `.agent.md` extension that VS Code claims for its
 * built-in `chatagent` language — whose front-matter schema rejects valid
 * Hosted Skills fields (`trigger`, `builtin_endpoints`, …). When a `.agent.md`
 * belongs to a Hosted Skills app, re-map it to `markdown` so that incompatible
 * built-in schema stops flagging it. Our own providers cover both languages.
 */
async function ensureHostedSkillsLanguage(document: vscode.TextDocument): Promise<vscode.TextDocument> {
  if (document.languageId === "markdown" || document.uri.scheme !== "file") {
    return document;
  }
  const enabled = vscode.workspace
    .getConfiguration("agentsAuthoring")
    .get<boolean>("treatAgentFilesAsMarkdown", true);
  if (!enabled) {
    return document;
  }
  if (!(await isHostedSkillsAgentUri(document.uri))) {
    return document;
  }
  try {
    return await vscode.languages.setTextDocumentLanguage(document, "markdown");
  } catch {
    return document;
  }
}

export function activate(context: vscode.ExtensionContext): void {
  const diagnostics = new AgentDiagnostics(context.extensionPath);
  context.subscriptions.push(diagnostics);

  const debounce = new Map<string, NodeJS.Timeout>();
  const scheduleValidate = (document: vscode.TextDocument, delay = 300): void => {
    if (!isRelevant(document)) {
      return;
    }
    const key = document.uri.toString();
    const pending = debounce.get(key);
    if (pending) {
      clearTimeout(pending);
    }
    debounce.set(
      key,
      setTimeout(() => {
        debounce.delete(key);
        void diagnostics.validate(document);
      }, delay)
    );
  };

  // Re-map + validate everything already open.
  for (const document of vscode.workspace.textDocuments) {
    void ensureHostedSkillsLanguage(document).then((doc) => diagnostics.validate(doc));
  }

  context.subscriptions.push(
    vscode.workspace.onDidOpenTextDocument((doc) => {
      void ensureHostedSkillsLanguage(doc).then((d) => scheduleValidate(d, 0));
    }),
    vscode.workspace.onDidChangeTextDocument((e) => scheduleValidate(e.document)),
    vscode.workspace.onDidSaveTextDocument((doc) => {
      // Re-validate all open agent files so slug-collision diagnostics stay fresh.
      if (isAgentDocument(doc)) {
        for (const open of vscode.workspace.textDocuments) {
          if (isAgentDocument(open)) {
            void diagnostics.validate(open);
          }
        }
      } else {
        scheduleValidate(doc, 0);
      }
    }),
    vscode.workspace.onDidCloseTextDocument((doc) => diagnostics.delete(doc.uri))
  );

  // Keep collision diagnostics accurate when agent files are added/removed/renamed.
  const watcher = vscode.workspace.createFileSystemWatcher("**/*.{agent,claude}.md");
  const agentsTree = new AgentsTreeProvider();
  const codeLensProvider = new AgentCodeLensProvider();
  const revalidateOpenAgents = (): void => {
    for (const open of vscode.workspace.textDocuments) {
      if (isAgentDocument(open)) {
        void diagnostics.validate(open);
      }
    }
  };
  const onAgentFileChanged = (): void => {
    revalidateOpenAgents();
    agentsTree.refresh();
    codeLensProvider.refresh();
  };
  watcher.onDidCreate(onAgentFileChanged);
  watcher.onDidDelete(onAgentFileChanged);
  watcher.onDidChange(() => {
    agentsTree.refresh();
    codeLensProvider.refresh();
  });
  context.subscriptions.push(watcher);

  context.subscriptions.push(
    vscode.languages.registerCompletionItemProvider(AGENT_DOC_SELECTOR, new AgentCompletionProvider(), " ", ":"),
    vscode.languages.registerHoverProvider(AGENT_DOC_SELECTOR, new AgentHoverProvider()),
    vscode.languages.registerCodeLensProvider(AGENT_DOC_SELECTOR, codeLensProvider),
    vscode.window.registerTreeDataProvider("hostedSkillsAgents", agentsTree)
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("agentsAuthoring.addAgent", () => addAgentCommand()),
    vscode.commands.registerCommand("agentsAuthoring.addTool", () => addToolCommand()),
    vscode.commands.registerCommand("agentsAuthoring.addSkill", () => addSkillCommand()),
    vscode.commands.registerCommand("agentsAuthoring.addSubagent", () => addSubagentCommand()),
    vscode.commands.registerCommand("agentsAuthoring.scaffoldApp", () => scaffoldAppCommand()),
    vscode.commands.registerCommand("agentsAuthoring.openChatUI", () => openChatUICommand()),
    vscode.commands.registerCommand("agentsAuthoring.openChatUIForFile", (uri?: vscode.Uri) =>
      openChatUIForFileCommand(uri)
    ),
    vscode.commands.registerCommand("agentsAuthoring.refreshAgents", () => agentsTree.refresh())
  );
}

export function deactivate(): void {
  /* no-op */
}
