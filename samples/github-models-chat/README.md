# GitHub Models Chat

A minimal Azure Functions chat app backed by GitHub Models. It includes the runtime's built-in browser UI, JSON chat API, and streaming API without requiring Azure AI resources.

| Trigger | Built-in Endpoints | Custom Tools | Connectors | MCP Servers | Skills | Sandbox | Chat UI |
|---|---|---|---|---|---|---|---|
| HTTP | HTTP + MCP | | | | | | Yes |

## Prerequisites

- Python 3.13+
- [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local)
- A GitHub token with access to [GitHub Models](https://github.com/marketplace/models)

## Run Locally

1. Create and activate a virtual environment:

   ```powershell
   cd samples/github-models-chat/src
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   ```

2. Create local settings:

   ```powershell
   Copy-Item local.settings.template.json local.settings.json
   ```

3. In `local.settings.json`, replace `<github-token>` with your token. To use another GitHub Models model, change `GITHUB_MODELS_MODEL` to its catalog identifier.

4. Start the app:

   ```powershell
   func start
   ```

5. Open `http://localhost:7071/agents/main/` or send the requests in [`test.http`](test.http).

## Configuration

The sample selects GitHub Models explicitly:

| Setting | Value |
|---|---|
| `AZURE_FUNCTIONS_AGENTS_PROVIDER` | `github` |
| `GITHUB_MODELS_TOKEN` | GitHub token with Models access |
| `GITHUB_MODELS_MODEL` | `openai/gpt-4.1-mini` by default |

`GITHUB_TOKEN` can replace `GITHUB_MODELS_TOKEN` because this sample explicitly selects the `github` provider. The dedicated variable is preferred because it also supports runtime auto-detection and is less likely to conflict with unrelated GitHub credentials.

Never commit `local.settings.json`; it is excluded by `.funcignore` and the repository ignore rules.