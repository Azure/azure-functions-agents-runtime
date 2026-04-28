# Samples

Each subdirectory is a standalone Azure Functions app deployable with [`azd up`](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd).

| Sample | Trigger | Custom Tools | Connectors | MCP Servers | Skills | Sandbox | Chat UI |
|---|---|---|---|---|---|---|---|
| [basic-chat](basic-chat/) | HTTP | | | | | ✅ | ✅ |
| [daily-tech-news-email](daily-tech-news-email/) | Timer | | ✅ Office 365 | | | ✅ | |
| [daily-azure-report](daily-azure-report/) | Timer + HTTP | ✅ azure_rest | ✅ Office 365 | ✅ MS Learn | ✅ azure-resources | | ✅ |

## Run Locally (optional)

Each sample is set up to be deployed and run easily in Azure. Running in Azure is the most friction-free option to try out these samples.

If you would instead prefer to run locally (for local development, testing, etc.), you can do so using the instructions below.

### Prerequisites

- [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local)
- Python 3.12+
- A [GitHub Personal Access Token](../README.md#github-token) with **Copilot** scope
- (Optional) [Azurite](https://learn.microsoft.com/azure/storage/common/storage-use-azurite) for local storage emulation

### 1. Install dependencies

**Bash (macOS/Linux):**

```bash
cd samples/<sample-name>/src
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**PowerShell (Windows):**

```powershell
cd samples/<sample-name>/src
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Create local settings

Copy `local.settings.template.json` to `local.settings.json`:

**Bash:**

```bash
cp local.settings.template.json local.settings.json
```

**PowerShell:**

```powershell
Copy-Item local.settings.template.json local.settings.json
```

Edit `local.settings.json` and set the required values. See each sample's README for specific requirements.

### 3. Set required environment variables

**GitHub Token (required for all samples):**

You need a GitHub Personal Access Token with **Copilot** scope:

**Option 1 - Using GitHub CLI**:

```bash
gh auth token
```

Then paste the token into `local.settings.json` as `GITHUB_TOKEN`.

**Option 2 - Create PAT manually**:

See [GitHub's Personal Access Token documentation](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#creating-a-fine-grained-personal-access-token) for instructions. Choose "Fine-grained personal access tokens" and enable the **Copilot** scope.

**Sample-specific variables:**

Edit `local.settings.json` and set any additional required variables for your sample. See each sample's README for details.

### 4. Start Azurite (required)

Samples use `AzureWebJobsStorage=UseDevelopmentStorage=true`, which requires Azurite. **Start Azurite in a separate terminal before running the Functions host.**

**Install Azurite:**

```bash
npm install -g azurite
```

**Start Azurite (in a separate terminal):**

```bash
azurite
```

Azurite will start on `http://127.0.0.1:10000`. Keep this terminal running.

**Alternative - Use a real Azure Storage account**:

If you prefer not to use Azurite, edit `local.settings.json` and replace `UseDevelopmentStorage=true` with:

```text
DefaultEndpointsProtocol=https;AccountName=<your-account>;AccountKey=<your-key>;EndpointSuffix=core.windows.net
```

### 5. Start the Functions host

```bash
cd samples/<sample-name>/src
func start
```

The host will connect to Azurite (or your Azure Storage account) and register all agent functions.

### 6. Test the app

Each sample exposes different endpoints. See the sample's README for testing details.

## Troubleshooting

### `func start` crashes with `Destination is too short`

If you see an exception like `System.ArgumentException: Destination is too short` from `Azure.Functions.Cli.Helpers.PythonHelpers`, check Python first, then update Azure Functions Core Tools.

1. Verify Python 3.12+ is available.

**Bash:**

```bash
python --version
```

**PowerShell:**

```powershell
python --version
```

1. Activate the sample virtual environment and verify the version again.

**Bash:**

```bash
cd samples/<sample-name>/src
source .venv/bin/activate
python --version
```

**PowerShell:**

```powershell
cd samples/<sample-name>/src
.venv\Scripts\Activate.ps1
python --version
```

1. Update Azure Functions Core Tools:

```bash
npm install -g azure-functions-core-tools@4 --force
```

1. Rerun the host:

```bash
func start
```

### Local source changes are not reflected at runtime

By default, sample `requirements.txt` files install a released wheel. If you are developing this repo and editing files under `src/azure_functions_agents`, install the package in editable mode so the sample uses your local source.

1. Activate the sample virtual environment.

**Bash:**

```bash
cd samples/<sample-name>/src
source .venv/bin/activate
```

**PowerShell:**

```powershell
cd samples/<sample-name>/src
.venv\Scripts\Activate.ps1
```

1. Replace the wheel install with an editable local install.

**Bash:**

```bash
pip uninstall -y azure-functions-agents
pip install -e ../../..
```

**PowerShell:**

```powershell
pip uninstall -y azure-functions-agents
pip install -e ..\..\..
```

1. Restart the Functions host (`func start`).

See each sample's README for prerequisites and deployment instructions.
