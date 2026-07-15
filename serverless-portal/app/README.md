# Serverless Agent Portal — app (v0.1)

A minimal, runnable slice of the [portal requirements](../requirements.md): the
**create-agent flow** with **Azure Blob Storage** persistence and an **editor**
for the generated `*.agent.md` file.

- Backend: FastAPI (`backend/`)
- Frontend: static HTML/CSS/JS (`static/`), served by the same app
- Persistence: agent files stored as blobs — the *working copy* from
  requirements §5.3

## Storage backends

The persistence layer ([backend/storage.py](backend/storage.py)) picks a backend
in this order:

1. `PORTAL_STORAGE_CONNECTION` — a storage connection string
2. `PORTAL_STORAGE_ACCOUNT_URL` — e.g. `https://stnldwouneaobbm.blob.core.windows.net`
   (uses `DefaultAzureCredential`; run `az login`, needs *Storage Blob Data
   Contributor*)
3. **Fallback:** Azurite (`UseDevelopmentStorage=true`) — zero-config local dev

Blob layout: `agent-projects/<project>/<environment>/agents/<name>.agent.md`.

## Run locally

```powershell
cd serverless-portal/app
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Option A — local Azurite (default). Start it in another terminal:
#   azurite --skipApiVersionCheck --location $env:TEMP\azurite-portal
# Option B — the deployed app's storage account:
#   $env:PORTAL_STORAGE_ACCOUNT_URL = "https://stnldwouneaobbm.blob.core.windows.net"

uvicorn backend.main:app --reload --port 8080
```

Open <http://localhost:8080/> → Agents list. Use **Create agent** to author a
new `*.agent.md`, then edit and save it.

## API

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/api/health` | Active storage backend + project/env |
| GET | `/api/agents` | List agents (working copy) |
| POST | `/api/agents` | Create an agent (`name`, `description`, `instructions`, `builtin_endpoints`) |
| GET | `/api/agents/{name}` | Raw `*.agent.md` + parsed front matter |
| PUT | `/api/agents/{name}` | Overwrite the raw `*.agent.md` |

## Not yet included (next slices)

Publish to the running app, versioning/history, Push to GitHub, providers,
connectors, monitoring — see [requirements.md](../requirements.md).
