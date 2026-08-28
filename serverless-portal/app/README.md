# Serverless Agent Portal — app

A runnable slice of the [portal requirements](../requirements.md): a control
plane that scans Azure for **serverless agents** built on
`azurefunctions-agents-runtime`, lists them per subscription, and manages
selected app-scoped resources such as Office 365 Outlook connections.

- **Backend:** Node.js + Express (`server/`)
- **Frontend:** a single **React + TypeScript** app (`frontend/`, Vite). The Node
  server serves the built app in production; Vite serves it in dev.
- **Auth:** browser **MSAL** sign-in (redirect flow, same first-party app as
  Polaris). The SPA acquires an **ARM** access token and forwards it as a Bearer
  token; the backend calls ARM as the signed-in user. No `az login` required.

## How it works

The user signs in through MSAL (redirect). The SPA acquires an ARM token for the
signed-in user and sends it on every `/api/*` call; the backend uses that token
to call ARM — see [server/src/azure.js](server/src/azure.js). The portal lists
the user's subscriptions (top-bar picker) and scans the selected one for agents.
It defaults to a subscription (`1a839f1f-10b2-4613-95ad-0800a22abbf2`, override
with `PORTAL_SUBSCRIPTION_ID`); the signed-in identity needs **Reader** on the
subscriptions it scans.

### Sign-in configuration

- The sign-in app defaults to the owned "Serverless Portal" client ID
  `0ceccceb-9c05-4953-9193-d94f9daa18d3` and authority
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

### Outlook connections

Open a Hosted Skill and select **What it can use**. Its **Connections** table is
the only connection-management surface. **Add connection** offers two paths:

- **Create new** provisions one deterministic Connector Gateway and Office 365
  Outlook connection for the app.
- **Use existing** has an independent subscription selector populated from the
  subscriptions visible to the current ARM sign-in. It defaults to the Function
  App subscription, but an eligible Office 365 Outlook connection may be selected
  from another visible subscription. Selecting one leaves its gateway and
  connection unchanged; the portal adds access policies for the Function App
  identity and signed-in user, plus an app-specific MCP configuration restricted
  to `SendEmailV2` in the connector subscription.

Both paths set the Function App's `O365_MCP_SERVER_URL` and non-secret
`AZURE_FUNCTIONS_AGENTS_OUTLOOK_CONNECTION_ID` application settings. The latter
stores the selected full ARM resource ID so cross-subscription attachments can
be recovered directly. An app can have one configured Outlook connection;
selecting a different one returns a conflict instead of silently switching.

Connection setup also checks the effective `mcp.json`:

- A correct deployed `office365-outlook` entry is preserved; no source deploy is needed.
- A correct portal draft is preserved and the page-level **Deploy** button remains enabled.
- A missing or unsupported entry is added/replaced in a focused draft, preserving other MCP servers; **Deploy** is required before Hosted Skills can use it.

Invalid `mcp.json` blocks Azure setup instead of overwriting unreadable source.
The completion step and a persistent amber notice state whether deployment is
required. Creating the Azure connection never auto-deploys source.

The Connections table uses ownership-aware removal. **Delete connection**
deletes a portal-created, app-owned Connector Gateway and its children.
**Remove from app** detaches an existing shared connection by deleting only this
app's MCP configuration and runtime-identity policy; the shared gateway,
connection, Microsoft sign-in, and user policy remain. Both actions remove
`O365_MCP_SERVER_URL`, `AZURE_FUNCTIONS_AGENTS_OUTLOOK_CONNECTION_ID`, and save an `mcp.json` draft with only the
`office365-outlook` server removed. Source cleanup is not auto-deployed.

Microsoft sign-in remains hosted by the Connector Namespace portal. Select
**Authorize**, open the Outlook connection at `connectors.azure.com`, choose
**Authorize**, complete Microsoft sign-in, then return and select **Check status**. The generic
`portal.azure.com` resource blade cannot authorize these connections. **Check status** verifies Azure's
provisioning and authentication status, the authenticated user, runtime and MCP
endpoints, both access policies, and the send-email-only restriction. It does
not send a message. Transient ARM failures and post-create convergence are
retried with bounded backoff.

The signed-in user needs permission to create
`Microsoft.Web/connectorGateways` resources and child access policies in the
Function App resource group. Selecting an existing connection also requires
read access to Connector Gateways in the selected subscription and permission to
create child access policies and MCP configuration on the selected gateway. For
cross-subscription attachment, the current ARM sign-in must authorize both the
Function App and connector subscriptions. The
Function App must have a resolvable system- or user-assigned managed identity
and permission to update its application settings. Connector Gateway uses the
`2026-05-01-preview` API; unknown provider states are shown as
**Action required**.

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
| GET | `/api/connections` | List the app-scoped Office 365 Outlook connection |
| POST | `/api/connections` | Create or converge the Outlook connection, access policies, and send-only MCP configuration |
| GET | `/api/connections/candidates` | List eligible Office 365 Outlook connections in an explicitly selected visible subscription |
| POST | `/api/connections/attach` | Attach a selected existing connection without updating its gateway or connection |
| GET | `/api/connections/:connectionId/status` | Refresh normalized connection status |
| GET | `/api/connections/:connectionId/auth-link` | Return the validated Connector Namespace authorization link |
| POST | `/api/connections/:connectionId/test` | Validate authentication, access policies, MCP state, and `SendEmailV2` restriction |
| DELETE | `/api/connections/:connectionId` | Delete an app-owned connection or detach a shared connection, clear app settings, and stage focused source cleanup |

## Not yet included (next slices)

Additional connector types and connector-trigger creation — see
[requirements.md](../requirements.md).
