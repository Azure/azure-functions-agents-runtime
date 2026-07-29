# Mockups Function App

Serves the static [`../mocks`](../mocks) portal mockups from an Azure Function
App so you get a single, shareable URL:
`https://<app-name>.azurewebsites.net/`.

A single anonymous, catch-all HTTP route ([`function_app.py`](function_app.py))
streams the files under `content/`, which is copied from `../mocks` at deploy
time (and kept out of source control).

## Deploy & share

Prerequisites: [Azure CLI](https://aka.ms/install-az) (`az`),
[Azure Functions Core Tools](https://aka.ms/azfunc-core-tools) (`func`), and
`az login`.

```powershell
./deploy.ps1
```

```bash
./deploy.sh
```

The script copies the mockups, provisions the resources
([`infra/main.bicep`](infra/main.bicep): Linux Function App on a Basic (B1)
App Service plan + storage + Application Insights), publishes the code, and
prints the URL to share. Override defaults with `-ResourceGroup` / `-Location`
/ `-NamePrefix` (PowerShell) or the matching env vars (bash).

> The plan is Basic (B1) rather than serverless Consumption because the target
> subscription has no Consumption (Dynamic) quota. Switch the plan `sku` in
> `infra/main.bicep` back to `Y1`/`Dynamic` on a subscription that allows it.

## Run locally

```powershell
Copy-Item ../mocks content -Recurse -Force
func start
```

Then open <http://localhost:7071/>.

## Security note

The site is intentionally **public with no authentication** so it can be
shared by URL. It contains only static mockups — no live data, secrets, or
backend. To restrict access later, put it behind Entra ID (App Service
authentication) or change the route's `auth_level`.
