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

- **Agent apps** — `AZURE_FUNCTIONS_AGENTS_PROVIDER` identifies Function Apps
  managed as agent apps. A portal-managed provisioning shell stays hidden from
  Hosted Skills until an agent indexes or final source deployment sets
  `AZURE_FUNCTIONS_AGENTS_PORTAL_DEPLOYED`.
- **Agents** inside an app are recovered from the runtime's function naming
  convention (`agent_<name>_builtin_*`, routes `agents/<name>/…`) — no need to
  invoke the running app. If none can be parsed, the app itself is surfaced.

### Function App lifecycle

The Hosted Skills dashboard shows one **Stop app** action per Function App,
including apps that host multiple skills. Stop opens a confirmation modal and
makes every Hosted Skill in that app unavailable. It does not delete data or
other Azure resources. Starting a stopped app remains an Azure Portal action.

Each Hosted Skill detail page exposes **Delete app** in the app action bar.
Delete requires a separate modal and typing the exact Function App name. It
deletes only the `Microsoft.Web/sites` Function App and clears app-keyed portal
draft/cache state. It deliberately preserves the resource group, storage
account, App Service plan, Application Insights, Log Analytics, Foundry
resources, GitHub repositories, Connector Gateways, and Outlook connections.
Those preserved resources can be reviewed, reused, or deleted separately.

Both operations run as the signed-in ARM identity and validate that the exact
target is a Function App carrying `AZURE_FUNCTIONS_AGENTS_PROVIDER`. Stop
requires effective `Microsoft.Web/sites/write`; delete requires
`Microsoft.Web/sites/delete`. The portal waits up to 30 seconds for Azure state
convergence and reports an in-progress state when ARM needs longer.

### Outlook connections

The **New Skill** flow is Model → Instructions → Deployment target → **Tools &
connections (optional)** → Review and deploy. Authors can select **Skip for now**
without preparing Azure resources. For an existing Function App, the optional
step opens the same live Outlook flow used by **What it can use**. For a new
target, the author first chooses **Create new** or selects an eligible existing
Outlook connection, without changing Azure. **Create identity & configure
Outlook** then creates the Function App infrastructure, waits for its managed
identity, and automatically applies that saved choice before skill source is
deployed. If connection setup fails after preparation, the prepared app is kept
and only Outlook setup is retried. A per-draft preparation identifier prevents
final deployment from adopting an unrelated existing app.

Selecting **New Skill** from the global header or Hosted Skills page starts a
fresh session draft. Back and step navigation within the active wizard retain
the current draft. Regenerating instructions from a changed description also
regenerates the hidden filename-safe skill name instead of retaining a name
from an earlier draft.

Generated New Skill source always includes the runtime-required `description`
field. Initial deployment and redeployment validate every final `.agent.md`
file after draft overlays and before upload. Invalid source is reported instead
of replacing a working app with a package that indexes zero agents and loses
its built-in Test endpoint. Test routes use the sanitized `.agent.md` filename
slug registered by the runtime; the post-deploy cache stores that slug and the
chat proxy normalizes older cached display names for compatibility.

Open a Hosted Skill and select **What it can use**. Its **Connections** table is
the only connection-management surface. **Add MCP server or tool** opens a
catalog with **Add Outlook MCP server** and **Add tool** (Coming soon). The
Outlook MCP server flow offers two paths:

- **Create new** provisions one deterministic Connector Gateway and Office 365
  Outlook connection for the app.
- **Use existing** has an independent searchable subscription selector populated from the
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

### GitHub source

Open a Hosted Skill and select **Source & GitHub**. After connecting a GitHub
account, the portal can create a repository or use an existing repository. Every
publication contains a complete azd-deployable project: `azure.yaml`, Bicep under
`infra/`, project documentation, `.gitignore`, and the current Function App source
under `src/`, with saved portal drafts overlaid.

Choose one publication mode:

- **Create pull request** writes to a rolling app-specific branch and opens or
  updates a PR against the repository's default branch.
- **Push to default branch** commits the same complete source directly. The UI
  requires confirmation because this bypasses review and can trigger an existing
  GitHub Actions deployment.

An already linked repository exposes both actions directly in Source & GitHub.
Repository creation defaults to private and supports either publication mode.
The generated Azure Functions workflow uses Python 3.13 and refuses to replace a
different existing `.github/workflows/deploy.yml`.

GitHub OAuth requires these backend settings:

```text
GITHUB_OAUTH_CLIENT_ID=<GitHub App client ID>
GITHUB_OAUTH_CLIENT_SECRET=<GitHub App client secret>
GITHUB_OAUTH_STATE_SECRET=<shared random value for every portal replica>
```

Set `GITHUB_OAUTH_CALLBACK` to the production callback, for example
`https://<portal-host>/api/github/callback`. Local development does not require a
registered localhost callback: install GitHub CLI, run
`gh auth login --hostname github.com`, and select **Connect**. The localhost-only
backend route reads that token without returning it to JavaScript. The raw
GitHub token is encrypted inside an HttpOnly cookie bound to the caller's ARM
object ID, so the connection survives backend revisions and works across
replicas without storing tokens on container disk.

Production OAuth authorization does not install the GitHub App. Install the App
on every account that will own repositories. Repository publication requires
**Contents: read and write**; pull requests require **Pull requests: read and
write**; creating repositories requires **Administration: read and write**.
Choose **All repositories** when the portal should create new repositories. If
the installation is limited to selected repositories, create and select the
repository in GitHub first, then use the portal's existing-repository flow.

When the GitHub App issues expiring user access tokens, the portal keeps the
returned expiration and refresh credentials in that encrypted cookie. It
refreshes the access token five minutes before expiration and persists GitHub's
rotated access and refresh tokens. A revoked session returns to the explicit
**Connect** state without signing the user out of Azure. Cookies created before
refresh support do not contain a refresh token and require one reconnect after
the upgrade.

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
| POST | `/api/apps/stop` | Validate and stop one agent Function App after explicit confirmation |
| DELETE | `/api/apps` | Validate and delete only one agent Function App, then clear its portal-local state |
| GET | `/api/connections` | List the app-scoped Office 365 Outlook connection |
| POST | `/api/connections` | Create or converge the Outlook connection, access policies, and send-only MCP configuration |
| GET | `/api/connections/candidates` | List eligible Office 365 Outlook connections in an explicitly selected visible subscription; `planned=true` supports read-only selection before a new app exists |
| POST | `/api/connections/attach` | Attach a selected existing connection without updating its gateway or connection |
| GET | `/api/connections/:connectionId/status` | Refresh normalized connection status |
| GET | `/api/connections/:connectionId/auth-link` | Return the validated Connector Namespace authorization link |
| POST | `/api/connections/:connectionId/test` | Validate authentication, access policies, MCP state, and `SendEmailV2` restriction |
| DELETE | `/api/connections/:connectionId` | Delete an app-owned connection or detach a shared connection, clear app settings, and stage focused source cleanup |
| POST | `/api/prepare-app` | Prepare a new Function App and managed identity without deploying skill source |
| GET | `/api/prepare-app/:jobId` | Poll New Skill app-preparation status before live connection setup |
| GET | `/api/github/status` | Report GitHub OAuth configuration and the current user's encrypted session status |
| POST | `/api/github/login-url` | Create a user-bound OAuth URL using the configured production callback or validated localhost callback |
| POST | `/api/github/local-session` | On localhost only, seal the authenticated GitHub CLI identity into the current ARM user's session |
| GET | `/api/github/repos` | List repositories available to the connected GitHub user |
| POST | `/api/github/connect` | Create/select a repository and publish the complete deployable source through a PR or direct default-branch push |
| POST | `/api/github/provision-deployment` | Add a non-conflicting Python 3.13 GitHub Actions workflow and passwordless Azure deployment configuration |

## Not yet included (next slices)

Additional connector types and connector-trigger creation — see
[requirements.md](../requirements.md).
