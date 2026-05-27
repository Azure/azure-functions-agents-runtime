# Samples

Each subdirectory is a standalone Azure Functions app deployable with [`azd up`](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd).

| Sample | Trigger | Custom Tools | Connectors | MCP Servers | Skills | Sandbox | Chat UI |
|---|---|---|---|---|---|---|---|
| [basic-chat](basic-chat/) | HTTP | | | | | ✅ | ✅ |
| [outlook-reply-agent](outlook-reply-agent/) | Office 365 Outlook | | ✅ Office 365 Outlook | ✅ Office 365 Outlook | | ✅ | |
| [daily-tech-news-email](daily-tech-news-email/) | Timer | | ✅ Office 365 Outlook | ✅ Office 365 Outlook | | ✅ | |
| [daily-azure-report](daily-azure-report/) | Timer + HTTP | ✅ azure_rest | ✅ Office 365 Outlook | ✅ MS Learn + Office 365 Outlook | ✅ azure-resources | | ✅ |

## Run Locally (optional)

Each sample is set up to be deployed and run easily in Azure. Running in Azure is the most friction-free option to try out these samples.

If you would instead prefer to run locally (for local development, testing, etc.), you can do so using the instructions below.

### Prerequisites

- [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local)
- Python 3.13+
- credentials and settings for the model provider referenced by the sample's checked-in `agent_configuration` (the samples default to Microsoft Foundry, so `az login` is the common path)
- (Optional) [Azurite](https://learn.microsoft.com/azure/storage/common/storage-use-azurite) for local storage emulation

### 1. Install dependencies

**Bash (macOS/Linux):**

```bash
cd samples/<sample-name>/src
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.dev.txt
```

**PowerShell (Windows):**

```powershell
cd samples/<sample-name>/src
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.dev.txt
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

**Model provider (required for all samples):**

Samples select their model provider through the checked-in `agent_configuration` block in each sample's config. The samples currently default to Microsoft Foundry.

| Provider | Typical local settings |
| --- | --- |
| Microsoft Foundry | `FOUNDRY_PROJECT_ENDPOINT`, `FOUNDRY_MODEL` |
| Azure OpenAI | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`, optional `AZURE_OPENAI_API_KEY` |
| OpenAI | `OPENAI_API_KEY` plus a matching `agent_configuration.model` |

For Foundry, set `FOUNDRY_PROJECT_ENDPOINT` to your project endpoint and `FOUNDRY_MODEL` to your model deployment name (for example, `gpt-5.4`). Authentication uses `DefaultAzureCredential`, so run `az login` locally first.

Azure OpenAI and OpenAI remain supported alternatives. If you switch providers, update the sample's `agent_configuration` and the corresponding environment variables in `local.settings.json`. For Azure OpenAI, that usually means setting your resource endpoint (for example `https://<name>.openai.azure.com/`) plus the deployment and API-version values referenced by the config. If the sample omits `api_key`, authentication uses `DefaultAzureCredential`.

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

1. Verify Python 3.13+ is available.

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

Sample `requirements.txt` files are generated for Azure deployment and install a wheel bundled under `src/wheels/`. For local development in this repo, install `requirements.dev.txt` so the sample uses your editable local source.

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

1. Install local-development dependencies.

**Bash:**

```bash
pip install -r requirements.dev.txt
```

**PowerShell:**

```powershell
pip install -r requirements.dev.txt
```

1. Restart the Functions host (`func start`).

See each sample's README for prerequisites and deployment instructions.
