# Serverless Agent Portal — React frontend

Vite + React + TypeScript UI for the portal. Talks to the Node.js + Express
backend ([../server](../server)) via a dev proxy.

## Screens

- **Agents** (`/`) — list agents from storage
- **Create agent** (`/create`) — form with live `*.agent.md` preview
- **Edit agent** (`/edit/:name`) — raw editor with dirty-tracking + save

## Run

The backend must be running on `http://127.0.0.1:8080` first:

```powershell
# terminal 1 — backend (from serverless-portal/app/server)
npm install
npm run dev

# terminal 2 — frontend (from serverless-portal/app/frontend)
npm install
npm run dev
```

Open <http://localhost:5173/>. Vite proxies `/api/*` to the backend
(see [vite.config.ts](vite.config.ts)).

## Build

```powershell
npm run build   # outputs dist/
```

Serve `dist/` behind the backend (or any static host) for production. Deep-link
routes need a catch-all rewrite to `index.html`.
