# Serverless Agent Portal — app

A runnable slice of the [portal requirements](../requirements.md): a read-only
control plane that scans Azure for **serverless agents** built on
`azurefunctions-agents-runtime` and lists them per subscription.

- **Backend:** Node.js + Express (`server/`)
- **Frontend:** a single **React + TypeScript** app (`frontend/`, Vite). The Node
  server serves the built app in production; Vite serves it in dev.
- **Auth:** browser **MSAL** sign-in (redirect flow, same first-party app as
  Polaris). The SPA acquires an **ARM** access token and forwards it as a Bearer
  token; the backend calls ARM as the signed-in user. No `az login` required —
  no persistence, all data is discovered live from Azure.

## How it works

The user signs in through MSAL (redirect). The SPA acquires an ARM token for the
signed-in user and sends it on every `/api/*` call; the backend uses that token
to call ARM — see [server/src/azure.js](server/src/azure.js). The portal lists
the user's subscriptions (top-bar picker) and scans the selected one for agents.
It defaults to a subscription (`1a839f1f-10b2-4613-95ad-0800a22abbf2`, override
with `PORTAL_SUBSCRIPTION_ID`); the signed-in identity needs **Reader** on the
subscriptions it scans.

### Sign-in configuration

- The first-party app defaults to Polaris's client ID
  `409cf302-c83f-43c3-94eb-ca581ab18c6d` and authority
  `https://login.microsoftonline.com/organizations`. Override on the backend
  with `MSAL_CLIENT_ID` / `MSAL_AUTHORITY` (served to the SPA at
  `/api/auth/config`).
- The app registration **must** list the portal origin (e.g.
  `http://localhost:5173`) as a **SPA** redirect URI, and admin consent for the
  Azure Service Management (ARM) delegated permission must be granted.

- **Agent apps** — a Function App IS a serverless agent app if — and only if — it
  carries the app-setting marker `AZURE_FUNCTIONS_AGENTS_PROVIDER` (its value is
  the model provider, e.g. `foundry`).
- **Agents** inside an app are recovered from the runtime's function naming
  convention (`agent_<name>_builtin_*`, routes `agents/<name>/…`) — no need to
  invoke the running app. If none can be parsed, the app itself is surfaced.

## Run locally

**Backend** (terminal 1):

```powershell
cd serverless-portal/app/server
npm install
npm run dev      # http://127.0.0.1:8080/  (node --watch)
```

**Frontend** — dev (terminal 2, hot reload, proxies `/api` → :8080):

```powershell
cd serverless-portal/app/frontend
npm install
npm run dev      # http://localhost:5173/
```

**Frontend** — production (single origin, served by the Node server at :8080):

```powershell
cd serverless-portal/app/frontend
npm run build    # emits dist/, which the Node server serves at http://localhost:8080/
```

## API

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/api/health` | Liveness check |
| GET | `/api/auth/config` | MSAL bootstrap values (client ID + authority) for the SPA |
| GET | `/api/identity` | Signed-in user + the default subscription |
| GET | `/api/subscriptions` | Subscriptions visible to the signed-in identity |
| GET | `/api/live/agents` | Scan a subscription (`?subscription=<id or name>`, defaults to the configured one) and list every serverless agent |

## Not yet included (next slices)

Agent detail, playground, providers, connectors, monitoring — see
[requirements.md](../requirements.md).
