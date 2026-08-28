---
frd: 0009
title: Outlook connections in the Capabilities tab
status: Finalized
author: swapnil
created: 2026-08-27
updated: 2026-08-28
issues: []
pull_requests: []
branch: swapnil/outlook-connections
---

# FRD 0009 — Outlook connections in the Capabilities tab

## 1. Summary

Add an app-scoped Connections table to the Hosted Skill `What it can use` tab for creating or selecting and managing one supported connection type: Office 365 Outlook. A customer can create a connection or attach an eligible existing connection from the same subscription, complete Microsoft sign-in, expose only the `SendEmailV2` operation, and see a normalized `Connected`, `Expired`, or `Action required` status.

## 2. Motivation / problem

The portal can add an Outlook MCP entry to `mcp.json`, but it cannot create, authenticate, verify, or monitor the Azure Connector Gateway resources behind that entry. Customers must leave the workflow, reproduce sample infrastructure manually, find the connection in Azure portal, and infer whether it is usable. This blocks guided scenarios such as scheduled reports that deliver results by email.

The first release deliberately supports only outbound Outlook email. It establishes the connection-management flow without introducing a general connector catalog or inbound connector triggers.

## 3. Goals / Non-goals

**Goals**

- Add an app-scoped `Connections` destination and page.
- Offer Office 365 Outlook as the only connection type.
- Let the customer provide a display name while the portal derives an Azure-safe resource name.
- Discover eligible Office 365 connections in Connector Gateways in the Function App subscription and let the customer select one instead of creating another gateway or connection.
- Provision a Connector Gateway, Office 365 connection, Function App identity access policy, signed-in deployer access policy, and MCP server configuration.
- When an existing connection is selected, leave its gateway and connection unchanged; add only app access policies and an app-specific send-only MCP server configuration.
- Set the Function App's `O365_MCP_SERVER_URL` application setting to the generated MCP endpoint for either setup path.
- Restrict the MCP server configuration to `SendEmailV2` and display that fixed permission before creation.
- Open a Microsoft-hosted authentication experience and refresh status when the customer returns.
- Let the customer test authentication and the complete send-email-only control-plane configuration without sending a message.
- Normalize provider states into `Connected`, `Expired`, and `Action required`, while preserving provider detail for troubleshooting.
- Support reconnecting and retesting an existing connection.
- Let the customer delete a portal-created connection or detach a selected existing connection, while removing this app's endpoint setting and `office365-outlook` source configuration.

**Non-goals**

- A generic connector marketplace or arbitrary operations.
- Reading, drafting, replying to, deleting, or triggering on Outlook email.
- Creating connections outside the context of one Hosted Skills Function App.
- Selecting connections from a tenant/subscription not available through the current ARM sign-in.
- Adopting a gateway tagged as portal-managed by a different Function App.
- Modifying or replacing the selected existing gateway or connection resource.
- Automatically deploying source changes. Connection setup and deletion may save focused `mcp.json` drafts; publishing remains an explicit customer action.
- Storing OAuth tokens or collecting Microsoft credentials in the portal.
- Stock-analysis sample content, market-data MCP tools, or scheduling UI.
- Sending a real test email from the portal.
- Runtime schema, discovery, registration, or execution changes.

## 4. Proposed design

This is a portal control-plane feature. It does not change the runtime's discover → translate → register → execute pipeline.

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| discover | None | No runtime project discovery changes. The portal discovers Connector Gateway resources through ARM independently of runtime startup. |
| translate | None | No runtime schema or authoring changes. Portal API responses normalize ARM resource states for the UI. |
| register | None | No Azure Functions registration changes. The portal creates Connector Gateway control-plane resources and access policies through ARM. |
| execute | None | No runner or client-manager changes. `Check status` validates only the Azure control-plane contract and never invokes Outlook or sends email. |

### Portal information architecture

- The only supported surface is the `What it can use` tab at `/agents/:subscriptionId/:app/:name?tab=capabilities`. Connections are app-shared but managed in the context of the selected Hosted Skill.
- Remove the legacy `/apps/:subscriptionId/:app` and `/apps/:subscriptionId/:app/connections` routes and delete their page modules. Remove the unused legacy agent-detail module and the generic `AddCapability` module once the focused tab component replaces it.
- The tab shows a Connections table first, followed by the existing MCP, Python tool, knowledge, and app-function tables. It shows connection name, source, resource group/gateway, allowed operation, truthful status, and actions.
- The table empty state explains that connections let Hosted Skills call external services and offers `Add MCP server or tool`.
- `Add MCP server or tool` opens a catalog with `Add Outlook MCP server` and a disabled `Add tool` option marked Coming soon.
- `Add Outlook MCP server` opens the focused Outlook wizard:
  1. Source: customer chooses `Create new` or `Use existing` for Office 365 Outlook. The existing path uses a searchable subscription picker.
  2. Details: create mode accepts a display name; existing mode lists eligible connections with gateway, resource group, authentication status, and signed-in account.
  3. Configure: create mode provisions the full resource set; existing mode validates the selected ARM ID and connector type, then adds the two app access policies and an app-specific MCP configuration without updating the selected gateway or connection.
  4. Authorize: portal opens the Connector Namespace gateway at `connectors.azure.com` in a new browser tab. The customer opens the Outlook connection, chooses `Authorize`, and completes Microsoft sign-in. The Hosted Skills portal never renders or proxies credential entry.
  5. Verify: portal writes `O365_MCP_SERVER_URL`, refreshes on return or on `Check status`, and reports readiness only when Azure authentication and the complete app configuration pass.
- New Skill Step 4 supports connection selection before a new Function App
  exists. Candidate discovery validates and uses the planned Function App ARM
  resource ID but performs no mutation. The author chooses create-new or an
  eligible existing connection, then one action prepares the Function App,
  waits for its managed identity, and runs the existing create/attach
  coordinator with the saved choice. A post-preparation Outlook failure keeps
  the prepared app and retries only connection setup.
- The table exposes `Reconnect`, `Check status`, and a source-aware removal action. `Delete connection` applies to app-owned resources; `Remove from app` applies to a selected shared connection.
- Removal opens a confirmation that lists the exact Azure and source effects. A successful removal invalidates both connection and source queries so the table and MCP inventory update together.

### Azure resource contract

For the selected Hosted Skills Function App, the server resolves its resource group, tenant, location, and managed identity. It creates or updates these resources:

- `Microsoft.Web/connectorGateways@2026-05-01-preview`
- `Microsoft.Web/connectorGateways/connections@2026-05-01-preview` with `connectorName: office365`
- `Microsoft.Web/connectorGateways/connections/accessPolicies@2026-05-01-preview` for the Function App managed identity and signed-in deployer
- `Microsoft.Web/connectorGateways/mcpserverconfigs@2026-05-01-preview` with only the `SendEmailV2` operation and `textOnlyContent: true`

Names are deterministic and idempotent. The gateway name is `cg-o365-<hash>`, where `<hash>` is the first 12 lowercase hexadecimal characters of SHA-256 over the lowercase full Function App ARM resource ID. The connection name is `office365-outlook`, and the MCP server configuration name is `Office-365-Outlook-send-email-only`. The display name remains customer-controlled. Repeating create after a recoverable failure converges on the same resources; an occupied deterministic ID with conflicting ownership returns `409 Conflict` rather than generating another name.

For an existing connection, the customer selects a connector subscription independently from the Function App subscription. The picker lists subscriptions visible through the current ARM sign-in and defaults to the app subscription. The portal enumerates Connector Gateways in the selected subscription through the preview API's subscription-level list endpoint, follows ARM `nextLink` pagination, and then lists each gateway's child connections with bounded concurrency. Candidates must use `connectorName: office365` and not belong to a gateway tagged as portal-managed by another Function App. A failure of the selected subscription gateway list fails the request; an inaccessible or deleted individual gateway is skipped and the response is marked partial so the UI can disclose that some candidates could not be read. The selected connection ID is revalidated server-side against the explicitly selected connector subscription. Attachment does not PUT the gateway or connection. It creates or converges access policies for the Function App managed identity and signed-in deployer, plus `Office-365-Outlook-send-email-only-<app-hash>` under the selected gateway. `<app-hash>` is the same first 12 lowercase SHA-256 hexadecimal characters over the canonical Function App ARM ID used by the managed gateway name.

The MCP configuration name and connection reference provide ARM-discoverable attachment state without a portal database. The portal also persists the selected raw, full connection ARM resource ID in the non-secret Function App setting `AZURE_FUNCTIONS_AGENTS_OUTLOOK_CONNECTION_ID`. Recovery reads that exact ID first and validates its resource shape, ownership boundary, connector type, and app-hashed MCP configuration. If the setting is absent, malformed, or points to a missing resource, fallback scanning is limited to the Function App subscription; the portal never scans all visible subscriptions implicitly. Existing same-subscription attachments created before this metadata setting remain compatible through that deterministic/local subscription scan fallback. A cross-subscription attachment cannot be rediscovered without its persisted ID and is reported as unconfigured rather than guessed. After ARM returns its HTTPS `mcpEndpointUrl`, the portal merges both `O365_MCP_SERVER_URL` and `AZURE_FUNCTIONS_AGENTS_OUTLOOK_CONNECTION_ID` into the Function App application settings and verifies both persisted values. If endpoint or connection-reference wiring fails, status remains `Action required` and retry converges the same resources and settings.

Removal is source-aware and differs by ownership:

- For `Created`, the server verifies gateway ownership and deletes the deterministic app-owned Connector Gateway. Its child connection, policies, and MCP configuration are deleted with the parent.
- For `Existing`, the server never deletes or updates the selected gateway or connection. It deletes the app-hashed MCP configuration and the Function App runtime-identity access policy. It retains the signed-in user policy because the same principal policy may predate this portal or serve another app.
- Both paths remove `O365_MCP_SERVER_URL` and `AZURE_FUNCTIONS_AGENTS_OUTLOOK_CONNECTION_ID` from Function App settings and save an `mcp.json` draft with only `servers["office365-outlook"]` removed. Other root properties and MCP servers remain intact. Missing source or an already-absent entry is idempotent; invalid JSON blocks deletion before Azure mutation.
- The server stages the source draft before control-plane mutation. If endpoint-setting or Connector Gateway cleanup fails, it restores the previous draft state and attempts to restore the prior endpoint setting. A rollback failure is reported explicitly as partial cleanup requiring manual review.
- Source drafts are the only journaled resource; Azure resources and app settings use compensating actions because there is no cross-system transaction. The response carries `cleanup.sourceDraft`, `cleanup.appSetting`, and `cleanup.azure` outcomes. Success values are `updated|unchanged`, `removed|absent`, and `deleted|detached|already_absent|deletion_pending`. Failure metadata uses `rolled_back|rollback_failed`, `restored|restore_failed`, and `failed`, plus actionable detail.
- Before staging source, and again immediately before Azure deletion, the server resolves the currently configured resource set and requires the opaque route ID to match it exactly. A stale/forged ID, missing current attachment, or ownership mismatch returns `409` before destructive mutation.
- Existing detach deletes the runtime-identity policy first and the app-hashed MCP configuration last. If the first delete fails, recovery remains intact; if the final delete fails, retry can still rediscover the attachment. The endpoint setting and source draft are restored on either failure.
- Created gateway deletion and existing child deletion may return ARM `202`. The server polls the exact resource with bounded backoff. A confirmed `404` returns `deleted` or `detached`; an accepted operation still visible after the bound returns successful `deletion_pending` rather than attempting to restore resources already being deleted. The UI removes the row optimistically and later refreshes ARM state.
- Removal is idempotent for a matching configured connection. Azure `404` responses for exact owned child resources count as already removed during an in-progress operation, but a request received after attachment recovery is gone returns `409` rather than acting on a stale ID.

### Ownership and discovery

- Each Function App has at most one configured Office 365 connection in this release, either portal-created or explicitly selected. Changing the display name updates a portal-created connection instead of creating another.
- The gateway and supported child resources receive `azfunc-agents-portal: managed` and `azfunc-agents-app-id: <function-app-resource-id>` ownership metadata where the resource type supports tags. The deterministic gateway and child names remain the fallback because Azure child-resource tagging support varies.
- The gateway name is derived from a lowercase hash of the full Function App ARM resource ID, not only the app name. Normal listing starts from that deterministic gateway, then looks for the exact app-specific MCP configuration name across subscription gateways to recover an explicit existing-connection attachment. Candidate discovery enumerates gateways only while the picker is open.
- When tags are unsupported or absent on a child resource, validation falls back to the exact deterministic resource ID plus its parent gateway ownership. A pre-existing resource with missing or conflicting ownership is not adopted and returns `409 Conflict`.
- Create and retry target the same deterministic resource IDs. A resource at one of those IDs with conflicting ownership, connector type, or operations causes `409 Conflict` and is never adopted silently.
- Existing attachment validates the exact selected connection ID, selected subscription, and connector type on every mutation. Gateways carrying portal ownership for another app are excluded and rejected server-side even if a stale client submits one.
- Cross-subscription attachment is supported only for subscriptions returned by the current ARM sign-in and requires the caller to read the selected gateway and create child policies/MCP configuration there, plus update settings/source in the Function App subscription. Cross-tenant attachment is not inferred or silently attempted; the current ARM token must authorize both subscriptions.
- Attach is idempotent when the same connection is already configured. If the app already has a different managed or attached connection, create/attach returns `409 Conflict`; this release does not switch connections or leave orphaned MCP configurations.

### Portal server API

The browser continues to send its ARM bearer token to the portal server. New server endpoints mediate validation and ARM calls so preview API details are not duplicated in the frontend:

- `GET /api/connections?subscription=&resourceGroup=&app=` lists supported Connector Gateway connections for the app.
- `GET /api/connections/candidates?subscription=&resourceGroup=&app=&connectorSubscription=&planned=` lists eligible Office 365 connections in the explicitly selected connector subscription. `planned=true` derives a validated future Function App ARM ID and does not resolve a live site or managed identity.
- `POST /api/connections` validates the app identity and creates the Outlook resource set.
- `POST /api/connections/attach` validates an opaque candidate connection ID, converges app access and MCP configuration on its gateway, and wires the generated endpoint into the Function App settings.
- `GET /api/connections/:connectionId/status` refreshes the provider status.
- `POST /api/connections/:connectionId/test` verifies `overallStatus`, both required access policies, the enabled MCP server configuration, and that `SendEmailV2` is its only operation. It returns checks and remediation details; it does not invoke MCP or send email.
- `GET /api/connections/:connectionId/auth-link` constructs a Connector Namespace HTTPS deep link to the already validated connection's parent gateway for interactive authorization or reconnection.
- `DELETE /api/connections/:connectionId?subscription=&resourceGroup=&app=` validates the configured connection, stages focused source cleanup, clears the app setting, then deletes app-owned resources or detaches app-specific shared-connection resources. It returns the ownership mode and whether an `mcp.json` draft changed.

Successful DELETE responses include `{ removed: true, source, sourceDraftChanged, cleanup }`. Errors include the same `cleanup` object in HTTP metadata whenever mutation began, allowing the UI to distinguish a fully compensated failure from `rollback_failed` or `restore_failed` manual-recovery states.

The route ID is the base64url encoding of the full connection ARM resource ID. For example, `/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Web/connectorGateways/cg-o365-a1b2c3d4e5f6/connections/office365-outlook` is encoded as the `:connectionId` segment. Before every operation, the server decodes it and validates its exact resource-type shape. Portal-created connections must match the deterministic gateway, connection, and ownership metadata. Explicitly selected connections must match the same subscription and the app-specific MCP configuration discovered under their gateway. Scope mismatches return `404` to avoid disclosing unrelated resources. Authorization links use the fixed `https://connectors.azure.com/<subscription>/<resource-group>/<gateway>/overview` shape; generic `portal.azure.com` resource blades do not host Connector Namespace OAuth authorization.

### Status model

The server maps the case-insensitive provider status and available error metadata:

| Portal status | Meaning |
| --- | --- |
| `Connected` | Case-insensitive `properties.overallStatus == Connected` and the resource configuration is complete. This value was observed against the preview API on 2026-08-27. |
| `Expired` | Case-insensitive `properties.overallStatus == Expired`. This mapping is accepted only when Azure reports it explicitly; it has not yet been reproduced in a smoke test. |
| `Action required` | Status is absent, non-connected, unknown, or provisioning/configuration is incomplete. This is the fail-closed fallback for undocumented preview states. |

Provisioning progress is shown separately and is not persisted as a fourth connection status. The UI includes the raw provider message when available and never guesses `Connected` from resource existence alone.

When the preview API returns an unknown non-empty `overallStatus`, the server emits a structured warning containing the Function App resource ID, connection resource ID, and raw status, but no credentials or tokens. The API still returns `Action required`, preserves the raw value as `providerStatus`, and provides remediation text. This makes new preview states observable without treating them as healthy.

`Check status` reports authentication and configuration checks independently from the displayed status. It can therefore explain cases such as an authenticated connection with a missing access policy or an MCP configuration exposing the wrong operation. A successful check proves that the Azure control-plane contract is ready for `SendEmailV2`; it does not prove mailbox delivery.

### Authoring / API surface

No runtime authoring format changes. Existing applications continue to reference the generated MCP endpoint through `O365_MCP_SERVER_URL` and limit tools in `mcp.json` to `office365_SendEmailV2`. The portal wires that generated endpoint into the selected Function App's application settings and may stage a focused `mcp.json` draft; it never auto-deploys source.

Connection setup is source-aware and uses the effective `mcp.json` (portal draft first, deployed source otherwise):

- If a deployed `office365-outlook` entry already has `$O365_MCP_SERVER_URL`, `SendEmailV2`, and the Connector Namespace auth scope, source remains unchanged and no deployment is required.
- If the correct entry exists in a portal draft, source remains unchanged and deployment is required. The existing page-level `Deploy` button is enabled by the draft inventory.
- If the entry is missing or differs from the supported send-only contract, the portal saves a focused draft that adds/replaces only `servers["office365-outlook"]`, preserving all other root fields and servers. Deployment is required.
- Invalid JSON blocks connection setup before Azure mutation. If a new source draft is staged and Azure setup fails, the prior draft state is restored.

The canonical entry uses HTTP transport, `$O365_MCP_SERVER_URL`, only `office365_SendEmailV2`, scope `https://apihub.azure.com/.default`, and optional client ID `$O365_MCP_CLIENT_ID`. An unresolved or empty client ID intentionally falls back to the app-wide managed identity. Create/attach responses report `source.changed` and `source.deploymentRequired`; the UI invalidates the source and source-list queries, shows whether deployment is required, and thereby enables the existing page-level `Deploy` button only when an unpublished draft exists.

```json
{
  "type": "http",
  "url": "$O365_MCP_SERVER_URL",
  "tools": ["office365_SendEmailV2"],
  "auth": {
    "scope": "https://apihub.azure.com/.default",
    "client_id": "$O365_MCP_CLIENT_ID"
  }
}
```

The portal API is new and internal to the Hosted Skills portal. Responses use a typed connection summary with opaque ID, display name, service, allowed operations, normalized status, provider status/detail, MCP endpoint URL when available, and last-test metadata.

### Compatibility

- Existing agent apps, manually provisioned connectors, and `mcp.json` files are unchanged.
- The page recognizes portal-created resources through deterministic tags/names and does not mutate unrelated Connector Gateways.
- The preview ARM API version is isolated in the server implementation for later GA replacement.
- This branch is based on `swapnil/AiAppsPortal`, with human approval on 2026-08-27, because `main` does not yet contain `serverless-portal`. This is an explicit exception to the normal main-based worktree convention.
- After `swapnil/AiAppsPortal` lands on `main`, this branch must be rebased onto `main` and the portal build and test suite rerun before PR readiness.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Initial connector scope | Outlook only / generic catalog | Outlook only | Human | 2026-08-27 |
| 2 | Allowed Outlook capability | Send only / selectable Outlook operations | Fixed `SendEmailV2` only | Human | 2026-08-27 |
| 3 | Connection ownership | App-scoped / subscription-wide shared | App-scoped | Agent | 2026-08-27 |
| 4 | Credential handling | Portal-hosted OAuth / Microsoft-hosted sign-in | Microsoft-hosted sign-in; portal stores no credentials | Agent | 2026-08-27 |
| 5 | Feature baseline | Wait for portal merge to main / branch from `swapnil/AiAppsPortal` | Branch from `swapnil/AiAppsPortal` and document the exception | Human | 2026-08-27 |
| 6 | Test semantics | ARM/configuration validation / send a real test email | Validate authentication, policies, MCP state, and `SendEmailV2` without sending email | Agent | 2026-08-27 |
| 7 | Runtime impact | Add runtime connection schema / portal-only control plane | Portal-only; no runtime pipeline change | Agent | 2026-08-27 |
| 8 | Sign-in mechanism | Undocumented direct consent API / Azure Portal connection deep link | Open the exact connection resource in Azure Portal | Agent | 2026-08-27 |
| 9 | App ownership | Portal database / names only / deterministic IDs plus ownership metadata | One deterministic gateway and Outlook connection per app, verified by ID and metadata | Agent | 2026-08-27 |
| 10 | Preview status handling | Infer broad status enum / map only evidenced states | Map explicit Connected and Expired; all other values require action | Agent | 2026-08-27 |
| 11 | Branch integration | Keep stacked permanently / rebase after portal lands | Rebase onto `main` and rerun gates after `swapnil/AiAppsPortal` merges | Agent | 2026-08-27 |
| 12 | Architecture approval | Continue review / approve finalized design | Approved with control-plane-only connection testing | Human | 2026-08-27 |
| 13 | Existing connection setup | Create only / select an existing Office 365 connection | Offer both paths in the New connection wizard | Human | 2026-08-27 |
| 14 | Existing candidate scope | Same resource group / same subscription / cross-subscription | Superseded by Decision 30 | Agent | 2026-08-27 |
| 15 | Existing resource mutation | Recreate/update connection / attach app configuration only | Never PUT the selected gateway or connection; add app policies, app-specific MCP config, and endpoint setting | Agent | 2026-08-27 |
| 16 | Shared configuration identity | Portal database / Function App setting only / deterministic MCP config | Deterministic app-hashed MCP config under the selected gateway | Agent | 2026-08-27 |
| 17 | Existing ownership boundary | Allow every readable gateway / exclude gateways managed for another app | Exclude and server-reject portal-managed gateways owned by another app | Agent | 2026-08-27 |
| 18 | Portal surface | Keep app detail and standalone Connections pages / agent Capabilities tab only | Support only the agent `What it can use` tab; delete legacy routes, pages, and generic capability modal | Human | 2026-08-27 |
| 19 | Candidate discovery failures | Fail on any unreadable gateway / silently skip / partial result | Follow pagination; fail the root list, skip unreadable children, and disclose a partial result | Agent | 2026-08-27 |
| 20 | Attach retry and switching | Overwrite/switch / same-ID idempotency with different-ID conflict | Same selected connection converges; a different configured connection returns 409 | Agent | 2026-08-27 |
| 21 | Runtime endpoint readiness | Trust successful write / verify app setting | `Connected` requires `O365_MCP_SERVER_URL` to match the current HTTPS MCP endpoint | Agent | 2026-08-27 |
| 22 | Removal semantics | Always delete selected Azure connection / ownership-aware delete or detach | Delete app-owned gateway; detach app-specific resources from existing shared connections | Agent | 2026-08-27 |
| 23 | User access policy cleanup | Always delete / always retain / delete when provably exclusive | Retain on existing shared connections because exclusivity cannot be proven; app-owned gateway deletion removes it naturally | Agent | 2026-08-27 |
| 24 | Source cleanup | Leave `mcp.json` / string edit / structured draft edit | Parse JSON and save a draft removing only `servers["office365-outlook"]`; never auto-deploy | Agent | 2026-08-27 |
| 25 | Failure ordering | Azure first / source first with rollback | Stage source first, clear setting, clean Azure, and roll back source/setting on failure where possible | Agent | 2026-08-27 |
| 26 | Async ARM deletion | Treat 202 as failure / wait without bound / bounded poll with pending success | Poll exact resources with bounded backoff; return `deletion_pending` when ARM accepted but has not converged | Agent | 2026-08-27 |
| 27 | Cleanup observability | Generic error / per-step outcomes | Return source-draft, app-setting, and Azure cleanup outcomes on success and post-mutation failure | Agent | 2026-08-27 |
| 28 | Authorization destination | Generic Azure resource blade / Connector Namespace portal | Use `connectors.azure.com/<subscription>/<resource-group>/<gateway>/overview`; the generic Azure portal blade cannot authorize Connector Namespace connections | Agent | 2026-08-27 |
| 29 | Source/deploy behavior after setup | Never edit source / always create a draft / effective-source-aware | Preserve a correct deployed entry; preserve a correct draft; otherwise stage only the Outlook entry and require explicit Deploy | Human | 2026-08-27 |
| 30 | Existing connector subscription scope | Function App subscription only / independent visible-subscription picker | Let the customer select any subscription visible through the current ARM sign-in; persist the exact selected connection ARM ID for recovery | Human | 2026-08-27 |
| 31 | Capabilities tab availability | Disable Add after one connection / connector-type-aware catalog | Keep Add connection enabled regardless of existing rows; prevent only duplicate Outlook configuration while other connector types can be added later. Mark Python tools and Skills as Coming soon and remove App Functions from this tab. | Human | 2026-08-27 |
| 32 | Existing connection wizard navigation | Select path then Continue / open the picker immediately | Selecting Use existing advances directly to the subscription and connection picker without an extra Continue click | Human | 2026-08-27 |
| 33 | Capability add entry point | Outlook-specific Add connection / MCP server and tool catalog | Label the action Add MCP server or tool; offer Outlook MCP server now and show Add tool as Coming soon; search connector subscriptions in the existing Outlook path | Human | 2026-08-28 |
| 34 | New Skill connection setup | Configure after deployment / optional live setup before review | Add Tools & connections as optional Step 4 after Deployment target. Existing targets reuse the live Outlook panel immediately; new targets prepare Azure infrastructure and identity first, then reuse the same panel before source deployment. Skip performs no early preparation | Human | 2026-08-28 |
| 35 | New Skill connection ordering | Prepare identity, then choose / choose first, then prepare and auto-configure | Supersede the new-target ordering in Decision 34: choose create-new or an existing Outlook connection first; then create the identity and automatically configure that saved choice. Keep preparation when Outlook setup fails so only connection setup is retried. | Human | 2026-08-28 |
| 36 | Preserve built-in endpoints across preparation and connection deployment | Expose provisioning shells immediately / distinguish pending and active apps; trust generated source / validate final deployment bundle | Keep portal-managed app shells out of Hosted Skills until an agent indexes or final deployment sets an activation marker. Always generate required `description` front matter and validate every overlaid `.agent.md` before initial deploy or redeploy so the runtime cannot skip the agent and its built-in endpoints. | Human | 2026-08-28 |
| 37 | Built-in Test route identity after New Skill deployment | Cache front-matter display name / cache filename-derived runtime slug | Cache the sanitized `.agent.md` filename slug used by runtime functions. Normalize legacy cached display names at both chat proxy paths and when reconciling detail/Playground deep links. | Agent | 2026-08-28 |
| 38 | New Skill draft lifecycle | Resume the previous draft from every New Skill entry / reset explicit New Skill entry points | Global and Hosted Skills page New Skill actions clear the prior session draft before navigation; only navigation within an active wizard resumes it. AI regeneration derives the skill name from the current description so a resumed draft cannot silently deploy a previous hidden name. | Agent | 2026-08-28 |

## 6. Test plan

- [ ] Frontend unit/component: empty, creating, sign-in required, connected, expired, action-required, reconnect, and test-send states.
- [ ] Frontend route/navigation: app-scoped Connections route preserves subscription and app context.
- [ ] Server unit: request validation, Azure-safe deterministic naming, ARM payloads, idempotent create, and partial-failure responses.
- [ ] Server unit: provider-status normalization for explicit `Connected`, explicit `Expired`, unknown, non-connected, mixed-case, and missing states.
- [x] Server unit: base64url ARM resource-ID decoding, exact scope/ownership validation, cross-app rejection, and fixed-origin Connector Namespace links.
- [ ] Server unit: connection test reports status, access-policy, MCP-state, and allowed-operation failures independently.
- [ ] Server integration with mocked ARM: create → sign-in-required → connected → successful control-plane test flow.
- [ ] Server integration: create → retry → discover targets the same resources; conflicting ownership returns 409.
- [ ] Server unit: candidate discovery filters to same-subscription `office365` connections, excludes other-app portal-managed gateways, and tolerates inaccessible child lists without exposing them.
- [ ] Server integration: attach existing → create policies and app-hashed MCP config → set `O365_MCP_SERVER_URL` without PUT requests to the selected gateway or connection.
- [ ] Server integration: forged cross-subscription IDs, non-Office 365 connections, stale/deleted selections, and already-configured conflicts fail closed.
- [x] Server integration: candidate discovery honors explicit `connectorSubscription`; attach accepts a matching remote-subscription ID and rejects an ID that does not match the selected subscription.
- [x] Server recovery: `AZURE_FUNCTIONS_AGENTS_OUTLOOK_CONNECTION_ID` resolves a remote attachment directly and legacy same-subscription attachments still recover through fallback scanning.
- [x] Server app-setting integration: setup atomically merges and verifies endpoint plus selected connection ID; removal clears/restores both settings.
- [x] Frontend flow: independent subscription selector defaults to the app subscription, clears stale row selection on change, and permits selecting a candidate from another visible subscription.
- [x] Frontend flow: selecting Use existing advances directly from Source to the subscription and connection picker without an intermediate Continue action.
- [x] Frontend flow: Add MCP server or tool opens a responsive catalog, Add tool is disabled as Coming soon, and the existing Outlook path can filter subscriptions by name or ID.
- [x] Frontend flow: New Skill includes optional Tools & connections before Review; Skip creates no early Azure resources, while Configure reuses the live Outlook create/existing flow on the selected target.
- [x] Frontend flow: a new target chooses create-new or an existing Outlook connection before preparation; the combined action prepares the identity and then applies the saved choice, with Outlook-only retry after preparation.
- [x] Server unit: planned Function App ARM IDs are validated for read-only pre-provision candidate discovery.
- [x] Server regression: missing required `description` is rejected before deployment, final overlaid agent bundles are validated before upload, and empty portal-managed app shells are not exposed as Hosted Skills.
- [x] Portal regression: optimistic post-deploy cache uses the runtime filename slug; streaming and non-streaming chat proxies normalize stale display-name slugs before invoking built-in endpoints.
- [x] Frontend regression: explicit New Skill entry points clear previous session state, while in-wizard navigation preserves it; AI regeneration does not retain a name derived from an older description.
- [x] Frontend flow: Add connection stays enabled with existing rows; the Outlook flow explains its per-type uniqueness instead of submitting a duplicate. Python tools and Skills show Coming soon; App Functions is absent.
- [ ] Frontend flow: create/existing choice, loading/empty/error candidate states, candidate selection, configuration disclosure, and successful attachment.
- [ ] Frontend route audit: no `/apps/` routes or links remain; legacy app detail, standalone Connections, legacy agent detail, and generic AddCapability modules are deleted.
- [ ] Server recovery: zero, one, and multiple app-hashed MCP configuration matches produce empty, configured, and fail-closed ambiguous results respectively.
- [ ] Server app-setting readiness: missing, stale, failed-write, and matching `O365_MCP_SERVER_URL` states are reported accurately.
- [x] Server source unit: structured removal preserves unrelated MCP servers/root properties, handles missing entries idempotently, and rejects invalid JSON.
- [x] Server integration: deleting a created connection issues DELETE only for the verified app-owned gateway; ownership conflicts fail closed.
- [x] Server integration: removing an existing connection deletes only the app-hashed MCP configuration and runtime principal policy, never the shared gateway, connection, or signed-in user policy.
- [x] Server integration: source draft and endpoint setting roll back when control-plane cleanup fails; rollback failures return explicit partial-cleanup detail.
- [x] Server integration: stale ID mismatch is rejected before source/settings/Azure mutation; the current ID is revalidated immediately before deletion.
- [x] Server integration: `202` deletion converges to `404` or returns `deletion_pending`; the handler never restores an endpoint for an accepted asynchronous deletion.
- [x] Frontend flow: source-aware confirmation labels and effects, pending/error states, successful table removal, and MCP/source query invalidation.
- [ ] Server source unit: setup preserves a correct deployed entry, recognizes a correct draft, adds/replaces only the Outlook entry when needed, and rejects invalid JSON.
- [ ] Server integration: a newly staged setup draft rolls back when Azure create/attach fails.
- [ ] Frontend flow: setup response invalidates `mcp.json` and source-list queries; `Deploy` enables for missing/draft-only source and remains unchanged for already-deployed source.
- [ ] Manual Azure smoke test: provision, complete Microsoft sign-in, pass the control-plane test, reproduce an authentication failure where feasible, and reconnect.
- [ ] Manual status acceptance: attempt to reproduce token expiry and verify Azure returns `Expired`. If the preview API does not expose that value, record the observed value and map it fail-closed to `Action required`; do not ship an unverified `Expired` mapping as though it were observed.
- [ ] Existing portal frontend build and server tests remain green.
- [ ] Full runtime test suite remains green and the feature diff contains no changes under `src/azure_functions_agents/`.
- [ ] Portal gate: `npm --prefix serverless-portal/app/frontend run build` and `npm --prefix serverless-portal/app/server test` pass. Frontend component-test infrastructure may be added only if the portal already has or separately approves that test surface.
- [ ] PR readiness: after `swapnil/AiAppsPortal` merges, rebase this branch onto `main`, confirm the feature diff is still surgical, and rerun the portal gate plus the repository's full `ruff`, `mypy`, and `pytest` gate before opening the PR.
- [ ] Runtime fixture scenario: not required because runtime authoring and discovery do not change.

## 7. Docs impact

- [ ] `serverless-portal/app/README.md` — connection setup, required permissions, preview API dependency, and local development notes.
- [ ] Portal help/error text — explain Microsoft-hosted sign-in and remediation states.
- [ ] `docs/architecture.md` — no runtime module-map change; mention only if portal architecture is documented there before implementation completes.
- [ ] `docs/front-matter-spec.md` — no change.
- [ ] `docs/triggers.md` — no change.
- [ ] `README.md` — no change unless the portal feature is promoted in the repository overview.
- [ ] `docs/frds/README.md` — add FRD 0009 to the index.

## 8. Status & sign-off

- **Architecture review (phase 2):** Decision 30 review passed on 2026-08-27. The design uses an independent visible-subscription picker, explicit server validation, a persisted raw connection ARM ID, and same-subscription-only fallback when remote metadata is unavailable.
- **Human sign-off:** swapnil, 2026-08-27. Explicitly requested selecting existing connectors from the Function App subscription and other visible subscriptions.
