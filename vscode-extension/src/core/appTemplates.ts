/**
 * Bundled, deterministic scaffold for a new agent app. Produces the baseline
 * Azure Functions + agents-runtime project files (offline, always valid).
 * Mirrors docs/getting-started.md and the azure-functions-agents skill's
 * references/project-files.md.
 */
import { buildAgentMarkdown } from "./agentTemplate";

export interface ScaffoldOptions {
  /** Add an interactive main.agent.md chat agent (chat UI + API + MCP). */
  includeMainChatAgent: boolean;
  /** Default model placeholder written into local.settings.json. */
  model: string;
  /** Optional additional agent file to write, e.g. from the Add Agent wizard. */
  extraAgent?: { filename: string; content: string };
}

const FUNCTION_APP_PY = `from azure_functions_agents import create_function_app

app = create_function_app()
`;

const HOST_JSON = `{
  "version": "2.0",
  "functionTimeout": "00:30:00",
  "extensions": {
    "http": {
      "routePrefix": ""
    }
  },
  "extensionBundle": {
    "id": "Microsoft.Azure.Functions.ExtensionBundle",
    "version": "[4.*, 5.0.0)"
  }
}
`;

const REQUIREMENTS_TXT = `azurefunctions-agents-runtime
`;

const AGENTS_CONFIG_YAML = `# App-wide runtime defaults inherited by every agent.
model: $FOUNDRY_MODEL
timeout: 900
`;

const FUNCIGNORE = `.git*
.vscode
__azurite_db*__.json
__blobstorage__
__queuestorage__
local.settings.json
test
.venv
__pycache__
*.pyc
*.pyo
.python_packages
.env
`;

function localSettings(model: string): string {
  return `{
  "IsEncrypted": false,
  "Values": {
    "FUNCTIONS_WORKER_RUNTIME": "python",
    "AzureWebJobsStorage": "UseDevelopmentStorage=true",
    "AZURE_FUNCTIONS_AGENTS_PROVIDER": "foundry",
    "FOUNDRY_PROJECT_ENDPOINT": "",
    "FOUNDRY_MODEL": "${model}"
  }
}
`;
}

function mainAgentMarkdown(): string {
  return buildAgentMarkdown({
    name: "Main Agent",
    description: "An interactive assistant.",
    builtinEndpoints: true,
    instructions: "You are a helpful assistant. Answer questions concisely.",
  });
}

/**
 * Return a map of relative file path -> file content for the new app.
 * All paths are relative to the chosen target (function app) folder.
 */
export function buildScaffold(opts: ScaffoldOptions): Record<string, string> {
  const files: Record<string, string> = {
    "function_app.py": FUNCTION_APP_PY,
    "host.json": HOST_JSON,
    "requirements.txt": REQUIREMENTS_TXT,
    "agents.config.yaml": AGENTS_CONFIG_YAML,
    "local.settings.json": localSettings(opts.model),
    ".funcignore": FUNCIGNORE,
  };

  if (opts.includeMainChatAgent) {
    files["main.agent.md"] = mainAgentMarkdown();
  }
  if (opts.extraAgent) {
    files[opts.extraAgent.filename] = opts.extraAgent.content;
  }
  return files;
}
