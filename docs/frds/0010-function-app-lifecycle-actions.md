---
frd: 0010
title: Function App lifecycle actions in the Hosted Skills portal
status: Finalized
author: swapnil
created: 2026-08-31
updated: 2026-08-31
issues: []
pull_requests: []
branch: swapnil/app-lifecycle-actions
---

# FRD 0010 — Function App lifecycle actions in the Hosted Skills portal

## 1. Summary

Add guarded, app-wide lifecycle actions to the Hosted Skills portal: Stop Function App from the dashboard and Delete app from a Hosted Skill detail page. Both actions use separate confirmation modals and the signed-in user's ARM permissions. Delete removes only the `Microsoft.Web/sites` Function App and portal-local state; it preserves every companion or shared Azure resource.

## 2. Motivation / problem

The portal can create and manage Hosted Skills apps but cannot stop an idle or misbehaving Function App or remove an app that is no longer needed. Authors must leave the portal, locate the exact Function App in Azure, and perform potentially destructive operations without the Hosted Skills context that shows which skills are affected.

These operations are app-wide: one Function App can host multiple skills. The UI must not imply that stopping or deleting affects only the currently displayed skill.

## 3. Goals / Non-goals

**Goals**

- Show one Stop app action per Function App on the Hosted Skills dashboard.
- Require a modal confirmation before stopping and state that all Hosted Skills in the app become unavailable.
- Show Delete app on every Hosted Skill detail page.
- Require a separate destructive modal and exact app-name entry before enabling Delete app.
- Resolve and validate the exact Azure target server-side as a Function App carrying the agent-provider marker before mutation.
- Run Azure mutations as the signed-in ARM principal and preserve actionable `403`, `404`, conflict, and provider failures.
- Make stop idempotent and expose Azure's app state in discovery so the dashboard truthfully shows Running or Stopped.
- Remove a stopped app from the dashboard action state only after stop succeeds; remove a deleted app and all of its Hosted Skills from client caches after delete succeeds.
- After confirmed Azure deletion, purge the four proven app-keyed portal stores (`agent-drafts`, `source-drafts`, `app-sources`, and `deploy-history`) so recreating the same app name cannot inherit stale source.

**Non-goals**

- Starting or restarting a stopped Function App.
- Deleting a resource group or any storage account, App Service plan, Application Insights component, Log Analytics workspace, Foundry account/project/model, GitHub repository, Connector Gateway, or Outlook connection.
- Cascading cleanup of resources merely inferred to be related to the app.
- Scheduling, auto-stop policies, bulk stop/delete, or soft-delete recovery.
- Changes to runtime authoring, discovery, registration, or execution.

## 4. Proposed design

This is a portal control-plane feature and does not alter the runtime's discover → translate → register → execute pipeline.

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| discover | None in `src/azure_functions_agents/` | No runtime discovery changes. Portal Azure discovery adds the Function App `state` to its response. |
| translate | None | No runtime schema or authoring changes. |
| register | None | No Azure Functions registration changes. |
| execute | None | No runner or inference behavior changes. |

### Portal server

- Add lifecycle helpers in `serverless-portal/app/server/src/azure.js`. Before mutation, each helper fetches the exact ARM site at `(subscription, resourceGroup, app)`, requires case-insensitive `kind` to contain `functionapp`, and reads application settings to require a non-empty `AZURE_FUNCTIONS_AGENTS_PROVIDER`. Missing sites return `404`; sites with a missing/non-Function-App kind or no provider marker return `409`. Regional placement and custom hostnames do not change this ARM resource identity. Deleting the primary site follows Azure's normal child-slot semantics; selective slot deletion is out of scope.
- `POST /api/apps/stop` accepts `{ subscription, resourceGroup, app, confirmation }`. All four values are required; `confirmation.trim()` must case-sensitively equal `app`. The server reads `site.state`; an already `Stopped` app returns `200` without calling stop. Otherwise it calls the Azure stop operation, then polls exact site state with bounded backoff for up to 30 seconds. `Stopped` returns `200 { app, state: "Stopped" }`; a still-transitioning site returns `202 { app, state: <latest> }` rather than claiming convergence.
- `DELETE /api/apps` accepts the same target and confirmation fields. After validation it deletes only `Microsoft.Web/sites/<app>`, then polls the exact resource for up to 30 seconds. Confirmed absence returns `200 { app, deleted: true, cleanup }`; a still-visible deleting site returns `202` and does not purge local state yet. A target already absent returns `404` so a stale request is never presented as a new successful deletion.
- After confirmed Azure deletion, the server recursively removes only `.data/agent-drafts/<subscription>/<app>`, `.data/source-drafts/<subscription>/<app>`, `.data/app-sources/<subscription>/<app>`, and `.data/deploy-history/<subscription>/<app>`. Every path segment uses the existing safe-segment function. Cleanup attempts all four stores independently and returns statuses in `cleanup`; failures are logged and returned with HTTP `200` because Azure deletion cannot be rolled back. No `.data/workflows` or inferred storage is removed.
- The portal does not preflight RBAC. ARM is authoritative: stop requires effective `Microsoft.Web/sites/write`; delete requires `Microsoft.Web/sites/delete`. Authorization is returned as `403` with an action-specific message, missing target as `404`, transient/conflict state as `409`, and unexpected provider failures as `502`.
- Live discovery maps case-insensitive site state to `Running`, `Stopped`, or `Unknown`; unsupported/missing values become `Unknown`. Flattened agents remain unchanged because availability is app-owned.

### Portal UI

- The dashboard passes `app.state` to `HostedSkillRow`. Only the first row for a multi-skill app shows the app-level Stop action, avoiding duplicate controls for one Azure resource.
- Stop opens a focused modal naming the Function App and current Hosted Skill count. The modal states that requests fail until an operator restarts the app in Azure. Cancel closes without a request; the confirm button sends the app name as `confirmation`. A `200` response updates the app state in the exact `queryKeys.liveAgents(subscription)` cache and persisted snapshot, then actively refetches. A `202` response shows the transition and keeps Stop disabled pending refresh. All rows for a multi-agent app render the same state, while only the first row renders the action.
- Delete app appears in the detail-page app action bar. Its modal states that every Hosted Skill in the Function App will be removed and lists preserved Azure resources. The input label and help text say "Type <app> exactly to confirm". After trimming surrounding whitespace, comparison remains case-sensitive; no Unicode normalization occurs. The destructive button remains disabled until it matches. Cancel closes without a request.
- Successful confirmed delete removes the app and all its agents from the exact live-agent query and persisted snapshot before navigating to `/agents/<subscription>`, then invalidates that query for an active refetch. A `202` response remains in the modal as "Deletion in progress" and does not remove cache data. Other browser sessions are eventually consistent and reconcile on hard refresh; cross-session push notifications are out of scope.
- Failed actions keep the modal open with an actionable error. `403` explicitly says the signed-in identity lacks permission; `404` offers to refresh stale dashboard data. Buttons expose pending states, prevent duplicate submissions, and disable modal dismissal while a request is active. Modal title, input, errors, preserved-resource list, and affected-skill count remain in keyboard/screen-reader order.

### Authoring / API surface

No changes to `*.agent.md`, `agents.config.yaml`, `mcp.json`, triggers, built-in endpoints, or runtime APIs. The new `/api/apps` routes are internal portal control-plane endpoints.

### Compatibility

- Existing agent apps remain discoverable; missing Azure state is rendered as Unknown rather than assumed healthy.
- Existing non-agent Function Apps cannot be stopped or deleted through these endpoints even if a request is forged.
- Multi-agent apps are handled as one lifecycle target.
- Deleting the Function App intentionally leaves companion resources for manual review/reuse. The confirmation modal discloses this before deletion.
- This worktree is based on `swapnil/AiAppsPortal` because `main` does not yet contain `serverless-portal`; this continues the documented portal baseline exception from FRD 0009.
- Stop/start asymmetry is intentional: this release can stop an app but restart remains an Azure Portal operation. Delete is permanent at the Function App resource boundary and therefore uses stronger typed confirmation.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Delete scope | Function App only / whole resource group / proven portal-owned resource set | Function App only; preserve all companion and shared Azure resources | Human | 2026-08-31 |
| 2 | Delete confirmation | Single click / generic confirm / type exact app name | Separate modal requiring exact Function App name | Agent | 2026-08-31 |
| 3 | Stop confirmation | Inline action / separate modal | Separate modal naming the app-wide impact | Human | 2026-08-31 |
| 4 | Server target boundary | Trust route values / validate site type / validate site type and agent marker | Validate exact site, Function App kind, and agent-provider marker before mutation | Agent | 2026-08-31 |
| 5 | Multi-agent dashboard action | Repeat on every skill row / first app row only / restructure dashboard | Show one app-level action on the first row for each app | Agent | 2026-08-31 |
| 6 | Post-delete portal state | Preserve all local state / purge local state / block on purge failure | Purge app-scoped local state after Azure confirms deletion; report purge failure without reversing deletion | Agent | 2026-08-31 |
| 7 | Feature baseline | Branch from main / branch from portal baseline | Branch from `swapnil/AiAppsPortal` because main does not contain the portal | Agent | 2026-08-31 |
| 8 | ARM convergence | Return after SDK call / bounded exact-resource polling | Poll stop state or delete absence for 30 seconds; use `202` when still converging | Agent | 2026-08-31 |
| 9 | App state model | Pass raw values / normalized portal enum | Normalize to Running, Stopped, Unknown; preserve no optimistic Running default | Agent | 2026-08-31 |
| 10 | Local cleanup scope | Broad inferred purge / exact app-keyed stores | Purge only agent drafts, source drafts, app sources, and deploy history after confirmed Azure deletion | Agent | 2026-08-31 |
| 11 | Cleanup failure response | Fail delete / HTTP 207 / successful delete with detail | Return HTTP 200 with per-store cleanup failures because the Azure deletion already succeeded | Agent | 2026-08-31 |
| 12 | Cache convergence | Optimistic only / refetch only / targeted update then refetch | Update exact cache/snapshot immediately, then actively refetch; other sessions converge on refresh | Agent | 2026-08-31 |

## 6. Test plan

- [x] Server unit: exact target validation rejects missing, non-Function-App, and non-agent sites before mutation.
- [x] Server unit: stop invokes Azure once, is idempotent when already stopped, and maps authorization/provider failures.
- [x] Server unit: delete targets only the Function App and never deletes the resource group or companion resources.
- [x] Server unit: delete purges only the exact app's portal-local directories after Azure deletion; cleanup failure is non-fatal and reported.
- [x] Server API: confirmation mismatch returns `400` before target lookup/mutation.
- [x] Server API: missing/null fields and invalid subscription/resource-group/app shapes return `400`; confirmation trims surrounding whitespace but remains case-sensitive.
- [x] Server unit: stop polls to Stopped, reports `202` while transitioning, and does not mutate an already stopped app.
- [x] Server unit: delete polls to absence, reports `202` without local purge while still visible, and returns `404` when absent before mutation.
- [x] Server unit: `403`, `404`, `409`, and unexpected Azure failures retain action-specific status/detail.
- [x] Frontend build: discovery and cache updates support Running/Stopped/Unknown state.
- [x] Frontend browser: Stop modal opens from the dashboard, cancel performs no request, confirm updates status, and a stopped app disables Stop.
- [x] Frontend browser: Delete modal requires exact app-name input, cancel performs no request, errors remain visible, and success returns to the dashboard with every app row removed.
- [x] Frontend browser: multi-agent apps show one Stop action but delete from any skill detail confirms whole-app impact.
- [x] Existing portal server suite and frontend production build remain green.
- [x] Repository `ruff`, `mypy`, and Python tests remain green because runtime code is unchanged.

## 7. Docs impact

- [x] `serverless-portal/app/README.md` — document lifecycle actions, exact resource scope, permissions, and preserved resources.
- [x] `docs/architecture.md` — no runtime module-map change; no update required.
- [x] `docs/front-matter-spec.md` — no authoring change.
- [x] `docs/triggers.md` — no trigger change.
- [x] `docs/frds/README.md` — add FRD 0010 to the index.

## 8. Status & sign-off

- **Architecture review (phase 2):** Independent review completed 2026-08-31. Its blocking requests for explicit ARM convergence, exact local-cleanup scope, state/cache semantics, RBAC mapping, and accessible confirmation rules are incorporated above. Runtime pipeline boundaries remain unchanged.
- **Testing review (phase 4):** Independent review completed 2026-08-31. Runtime wrapper exports were verified directly; additional pending-stop, non-Function-App, malformed-target, stale-delete, and Azure error-mapping tests were added. Browser checks cover cancel, confirm, RBAC error, multi-agent, and mobile behavior without mutating Azure.
- **Human sign-off:** swapnil, 2026-08-31. Approved the reviewed design for implementation.
