---
frd: 0008
title: Portal Custom Tool recipes
status: Finalized
author: swapnil
created: 2026-08-25
updated: 2026-08-25
issues: []
pull_requests: []
branch: swapnil/custom-tools-portal
---

# FRD 0008 - Portal Custom Tool recipes

## 1. Summary

Add a reusable Custom Tools authoring experience to the portal's **What it can use**
section. The first deterministic recipe creates an editable Azure Resource Manager REST
tool based on `samples/daily-azure-report/src/tools/azure_rest.py`, merges its Python
dependencies into `requirements.txt`, and optionally grants the Function App managed
identity a selected Azure role at a selected scope. AI-assisted generation uses the model
already configured for the Function App rather than asking the customer to select another
Foundry model.

This feature is based on `swapnil/AiAppsPortal` because the portal is not yet present on
`main`. It does not change the runtime's Custom Tool contract.

## 2. Motivation / problem

The runtime already discovers Python callables under `tools/`, but portal users must know
the file convention, write correct asynchronous authentication code, update dependencies,
and configure managed-identity RBAC separately. The existing portal path asks a Foundry
model to generate arbitrary Python and saves only one source file. That is insufficient for
the Daily Azure Report scenario and makes a common security-sensitive integration depend on
non-deterministic code generation.

Customers need a guided flow that creates a reviewable tool and all of its deployment
prerequisites while preserving the portal's draft-then-deploy model.

## 3. Goals / Non-goals

**Goals**

- Add a Custom Tool template gallery under **What it can use**.
- Ship an Azure REST recipe derived from the Daily Azure Report sample.
- Expose `path`, `method`, optional JSON `body`, and optional JMESPath `query` as the
  generated tool's model-callable arguments.
- Generate an editable preview of `tools/azure_rest.py` before saving.
- Merge `aiohttp`, `azure-identity`, and `jmespath` into a reviewable
  `requirements.txt` draft without removing or rewriting existing entries.
- Let the customer choose subscription or resource-group scope and an Azure role, with
  subscription scope and Reader as defaults.
- Grant the role to the identity the runtime uses: resolve `AZURE_CLIENT_ID` to a
  user-assigned identity, otherwise use the system-assigned identity, and ask only when
  the identity is ambiguous.
- Grant RBAC as a separate, best-effort operation with an explicit success, partial-success,
  or failure result. A failed grant must not discard valid source drafts.
- Use the Function App's configured model for optional AI-assisted generation and remove
  the custom-tool Foundry model picker.
- Preserve the existing source-draft, deployment overlay, and capability discovery flows.

**Non-goals**

- Reproduce the complete Daily Azure Report app or its timer, HTTP, connector, MCP, or
  knowledge-skill features.
- Change runtime tool discovery, filtering, registration, or execution behavior.
- Execute arbitrary generated tools inside the portal.
- Store credentials, tokens, or secrets in generated source.
- Automatically infer least-privilege custom roles from arbitrary ARM paths.
- Add resource-level scopes or custom-role creation in the first increment.
- Make AI generation mandatory for deterministic recipes.

### Integration dependency

The portal implementation currently exists only on `swapnil/AiAppsPortal`, which is 51
commits ahead of `main`. Local design and implementation may proceed on the approved stacked
branch, but this feature cannot merge independently: the portal branch must land first, or
the Custom Tools change must be reviewed as a stacked PR whose base is
`swapnil/AiAppsPortal`. No feature work is pushed before human review.

## 4. Proposed design

The portal is an authoring layer before the runtime startup pipeline. It writes standard
project files; the existing runtime then discovers and registers them without special cases.

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| author | `serverless-portal/app/frontend/src/components/AddCapability.tsx`, portal API client | Add a Custom Tool gallery and Azure REST recipe form, previews, configured-model generation, save results, and RBAC status. |
| persist | `serverless-portal/app/server/src/index.js`, Azure helper modules | Validate recipe requests, merge dependencies, save both source drafts, resolve the app's configured model, list assignable roles, and create a role assignment. |
| discover | `azure_functions_agents/discovery/tools.py` | No product change. Verify the generated `tools/azure_rest.py` is discovered through the existing contract. |
| translate | `azure_functions_agents/config/*` | No change. Custom Tool files do not add configuration schema. |
| register | `azure_functions_agents/registration/capabilities.py` | No change. Existing per-agent tool filters continue to apply. |
| execute | `azure_functions_agents/runner.py` / `client_manager.py` | No change. The generated async tool is passed to MAF through the existing tool inventory. |

### Portal experience

Selecting **Add capability > Custom tool** opens a recipe gallery. The first card is
**Azure REST API**; **Generate with AI** remains available as an advanced generic path.

The Azure REST recipe shows:

- tool name, default `azure_rest`;
- the four fixed runtime arguments and their generated schema;
- permission scope type: Subscription or Resource group;
- subscription, and resource group when that scope is selected;
- searchable Azure role selector, default Reader;
- resolved managed identity name and client ID, with a selector only when runtime identity
  resolution is ambiguous;
- editable Python preview;
- read-only dependency additions and a preview of the merged `requirements.txt`.

The generated tool uses this model-callable shape (the implementation includes the sample's
full `Field(description=...)` text):

```python
class AzureRestParams(BaseModel):
    path: str
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    body: str | None = None
    query: str | None = None

@tool
async def azure_rest(params: AzureRestParams) -> str: ...
```

MAF must receive this equivalent JSON Schema:

```json
{
  "type": "object",
  "properties": {
    "path": {"type": "string", "description": "ARM path relative to management.azure.com; include api-version."},
    "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"], "default": "GET"},
    "body": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null},
    "query": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null}
  },
  "required": ["path"]
}
```

All four fields are always present in JSON Schema; `path` is required and the other three
have defaults. `body` remains a JSON string, matching the sample, so the model cannot pass
an arbitrary Python object. Pydantic constrains the method enum. The generated function
performs URL-aware runtime validation because values are supplied on each model tool call:

- trim `path`, require it to start with exactly one `/`, and parse it with `urlsplit()`;
- reject a non-empty scheme or authority (`netloc`), while allowing URL-looking values in
  query parameter values;
- parse query parameters and require a non-empty, case-insensitive `api-version` key; the
  tool never guesses or injects an API version;
- uppercase `method` and reject values outside GET, POST, PUT, PATCH, and DELETE. HEAD and
  OPTIONS are intentionally outside the first recipe and may be added by editing the preview;
- parse a non-empty `body` with `json.loads()` before any request and return a structured
  validation error without making a network call when parsing fails.

The tool authenticates only to `https://management.azure.com/.default`, constructs the URL
from the fixed `https://management.azure.com` origin plus the validated path, reports HTTP
and JMESPath failures as structured JSON, bounds response text included in errors, and does
not interpolate customer secrets. This is intentionally a security-hardened derivative of
the sample, not a byte-for-byte copy; it preserves the sample's arguments and behavior while
preventing origin changes and malformed requests.

Saving creates or updates two drafts:

1. `tools/<sanitized-name>.py`
2. `requirements.txt` with missing dependencies appended once

The server validates both target paths and contents before replacing either draft and
serializes writes with a per-app mutex. Under the app's source-draft directory,
`.transactions/<uuid>/journal.json` records `prepared`, `tool_replaced`, and
`requirements_replaced` phases, target paths, prior-file existence, and old/new SHA-256
hashes. Prior bytes and same-directory new temporary files remain beside the journal until
commit. Each file and journal transition is flushed before rename. Transaction files are
excluded from source listing and deployment overlays.

If a normal write fails, prior contents are restored. Recovery runs under the same app mutex
before every draft read/write/list and deployment overlay. It compares current hashes to the
journal: if the remaining new temp file is intact it rolls forward; otherwise it restores
both old states, including deleting a target that previously did not exist. Recovery then
marks the journal complete before cleanup. Re-running recovery is idempotent. Tests inject a
mocked failure after the first rename, create a new draft-store instance to simulate process
restart, and require all-new when staged bytes are intact and all-old when they are not.

The requirements merge follows PEP 503 name normalization:
`lowercase(replace-runs-of("-", "_", ".") with "-")`. It parses the distribution name
from normal PEP 508 requirement lines while preserving every original line byte-for-byte.
Existing comments, options, editable references, URLs, pins, extras, markers, and line order
are never rewritten. If a normalized dependency name already exists in a parseable
requirement, that line wins regardless of version, extras, or markers and no second entry is
added. Named direct references such as `aiohttp @ https://...` are parsed by the name before
`@` and count as existing; unnamed VCS/URL lines are preserved but cannot satisfy a named
dependency. Missing dependencies are appended as unpinned `aiohttp`, `azure-identity`, and
`jmespath` lines. Consistent CRLF or LF is retained; empty, newline-free, or mixed files
append LF. Unparseable lines are preserved but do not count as satisfying a dependency.
Repeating preview/save is idempotent.

After source drafts are saved, the portal attempts the selected role assignment. Draft save
and RBAC are intentionally separate outcomes because Azure authorization can fail or take
time to propagate. The UI keeps the tool draft and shows actionable remediation when the
caller lacks `Microsoft.Authorization/roleAssignments/write`.

The server constructs scope as either `/subscriptions/{subscriptionId}` or
`/subscriptions/{subscriptionId}/resourceGroups/{resourceGroup}` from validated route/body
identifiers. The role-list endpoint returns only role definitions assignable at that scope.
The assign endpoint accepts one returned role-definition resource ID, verifies its tenant
subscription and assignable scopes again, and uses a deterministic role-assignment UUID
derived from scope + principal ID + role-definition ID. Repeating a request or receiving
`RoleAssignmentExists` returns `{ outcome: "existing" }`; a new assignment returns
`{ outcome: "granted" }`.

Identity resolution reads the Function App's `AZURE_CLIENT_ID`. When set, it must match
exactly one attached user-assigned identity's client ID. When absent and a system-assigned
principal exists, that principal is used even when user-assigned identities are also
attached. When no system identity exists, attached user-assigned identities are returned for
confirmation; confirming one updates `AZURE_CLIENT_ID` before granting RBAC so the runtime
and role target cannot diverge. This update requires
`Microsoft.Web/sites/config/write` and preserves all unrelated app settings.

Identity failures use `{ error, detail, candidates? }`: `managed_identity_missing` (409),
`configured_identity_unattached` (409), `identity_ambiguous` (409), and
`identity_configuration_forbidden` (403). Code and requirements may still be saved when
permission setup fails.

### Configured model resolution

Deterministic recipes do not call a model. For **Generate with AI** or an optional
"customize this template" action, the server strictly reads Function App settings using the
forwarded ARM token and mirrors `MAFClientManager` resolution:

1. non-blank `AZURE_FUNCTIONS_AGENTS_PROVIDER` wins and must equal `openai`,
  `azure_openai`, or `foundry`; any other value returns `configured_model_invalid`;
2. otherwise, short-circuit at the first non-blank setting in this exact order:
  `AZURE_OPENAI_ENDPOINT`, `FOUNDRY_PROJECT_ENDPOINT`, then `OPENAI_API_KEY`.

Model precedence also mirrors the runtime: Azure OpenAI uses
`AZURE_OPENAI_DEPLOYMENT`, then `AZURE_FUNCTIONS_AGENTS_MODEL`, then `gpt-4o-mini`;
Foundry uses `FOUNDRY_MODEL`, then `AZURE_FUNCTIONS_AGENTS_MODEL`, then `gpt-4o-mini`;
OpenAI uses `AZURE_FUNCTIONS_AGENTS_MODEL`, then `gpt-4o-mini`. Provider-required endpoint
and credential settings are validated before generation. A missing
`AZURE_OPENAI_DEPLOYMENT` is valid because the runtime intentionally falls back to
`AZURE_FUNCTIONS_AGENTS_MODEL` and then `gpt-4o-mini`. Azure account metadata and keys are
resolved server-side through ARM where the current portal already does so; OpenAI uses its
configured key server-side. Secrets and raw app settings are never returned, logged, or
included in generated code.

The generation request contains app identity plus prompt inputs only and cannot accept a
provider, account, endpoint, deployment, model, API key, or Foundry selection. Typed failures
are: `configured_model_missing` (409), `configured_model_invalid` (422),
`app_settings_forbidden` (403), `configured_model_not_found` (404), `generation_throttled`
(429), `generation_timeout` (504), and `generation_failed` (502). The UI displays the
actionable detail and keeps deterministic templates enabled.

### Authoring / API surface

Add authenticated portal endpoints for:

- previewing an Azure REST tool and merged requirements;
- saving the validated tool and requirements drafts;
- listing Azure role definitions assignable at subscription or resource-group scope;
- assigning the selected role to the Function App managed identity;
- generating generic Custom Tool code with the Function App's configured model.

All endpoints require subscription, resource group, and Function App identity. Paths and
role scopes are constructed server-side from validated identifiers. Role assignment requests
accept a role definition resource ID returned by the role-list endpoint, not an arbitrary
scope string.

The browser forwards its ARM bearer token, as existing portal APIs do. Reading app settings
requires `Microsoft.Web/sites/config/list/action`; listing roles requires
`Microsoft.Authorization/roleDefinitions/read`; assigning a role requires
`Microsoft.Authorization/roleAssignments/write` at the selected target scope. Source drafts
remain portal-local files and require an authenticated portal request, not a storage token.
The storage-scoped token remains read-only input to the existing deployed-source fallback and
is not used by Custom Tool save endpoints.

Role definitions are queried at the selected assignment scope; Azure evaluates
`roleDefinitions/read` there. A role that is absent, belongs to another subscription/tenant,
or has no `assignableScopes` ancestor of the target is rejected as
`role_not_assignable_at_scope` (422) before role-assignment PUT. ARM 401/403 responses retain
their status with sanitized detail.

No new `.agent.md`, `agents.config.yaml`, or runtime HTTP/MCP surface is introduced.

### Compatibility

Existing `tools/*.py`, `requirements.txt`, tool filters, and generic AI-generated tool
drafts continue to work. Existing drafts are never silently overwritten: the preview shows
replacement content, and saving an existing tool requires explicit confirmation. The
requirements merge is additive. Apps without managed identity can save code but receive a
clear permission setup error.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Initial creation experience | Azure REST only / generic AI only / template gallery with Azure REST | Template gallery with Azure REST as the first deterministic recipe | Human | 2026-08-25 |
| 2 | Azure REST runtime arguments | Reduced argument sets / all sample arguments | `path`, `method`, optional JSON `body`, optional JMESPath `query` | Human | 2026-08-25 |
| 3 | Managed-identity permissions | automatic subscription Reader / guidance only / configurable scope and role | Configurable subscription or resource-group scope and role, default Reader | Human | 2026-08-25 |
| 4 | Generated artifacts | tool only / save without preview / editable tool and dependency drafts | Editable preview, tool draft, and merged `requirements.txt` draft | Human | 2026-08-25 |
| 5 | Model selection for customer code generation | separate Foundry picker / Function App model / no AI | Always use the model configured for the Function App | Human | 2026-08-25 |
| 6 | Feature branch base | wait for portal merge to main / branch from current portal work | Base the local worktree on `swapnil/AiAppsPortal`; do not push before review | Human | 2026-08-25 |
| 7 | RBAC failure semantics | rollback drafts / hide failure / retain drafts and report separately | Retain valid drafts and report RBAC as a separate best-effort outcome | Agent | 2026-08-25 |
| 8 | Managed identity target | system only / always ask / resolve runtime identity | Resolve `AZURE_CLIENT_ID` to user-assigned identity, otherwise system-assigned; ask only if ambiguous | Human | 2026-08-25 |
| 9 | Requirements conflict handling | replace constraints / append another requirement / preserve existing | Preserve any existing normalized requirement, including its version, extras, and markers | Agent | 2026-08-25 |
| 10 | Configured provider behavior | Foundry only / separate picker / mirror runtime providers | Mirror runtime provider and model precedence; keep credentials server-side | Agent | 2026-08-25 |
| 11 | Identity configuration when only user-assigned identities exist | grant without configuring runtime / fail / confirm and set `AZURE_CLIENT_ID` | Require confirmation, preserve unrelated settings, then set `AZURE_CLIENT_ID` before RBAC | Agent | 2026-08-25 |
| 12 | Multi-file draft recovery | best-effort writes / rollback only / journaled roll-forward-or-rollback | Per-app journal with SHA-256 state checks and idempotent recovery before every draft operation | Agent | 2026-08-25 |

## 6. Test plan

- [ ] Frontend (`serverless-portal/app/frontend/src/**/*.test.tsx`, adding Vitest, jsdom, and
  Testing Library): gallery navigation, Azure REST defaults, scope-dependent fields, editable
  preview, overwrite confirmation, save states, configured-model unavailable state, and RBAC
  partial-success messaging. Deterministic recipes remain enabled while **Generate with AI**
  is disabled with the typed configured-model error detail.
- [ ] API (`serverless-portal/app/server/test/*.test.js`, using `node:test`): reject
  unauthenticated requests, invalid identifiers, traversal paths, arbitrary
  scopes, unknown role IDs, malformed generated content, and unsupported methods.
- [ ] Requirements merge: empty/missing file, comments and options, pinned packages,
  case/separator variants, extras, markers, editable/URL requirements, conflicting versions,
  duplicate dependencies, newline preservation, and idempotent repeated saves.
- [ ] Draft persistence: both files succeed, validation failure writes neither file, and a
  failed RBAC call retains both drafts. Inject failure after the first rename, recreate the
  draft store, and verify deterministic all-new/all-old recovery plus repeated recovery.
- [ ] RBAC: subscription scope, resource-group scope, Reader default, no managed identity,
  `AZURE_CLIENT_ID` user-assigned resolution, system fallback, ambiguous identities,
  unattached configured identity, confirmed identity app-setting update, insufficient caller
  permission, role/scope mismatch returning 422, deterministic existing assignment, and
  successful assignment.
- [ ] Configured model: resolves app endpoint/model server-side, ignores client model input,
  mirrors all three runtime providers and model precedence, never returns secrets, and maps
  missing, invalid, forbidden, not-found, throttled, timeout, and upstream failure responses.
- [ ] AI generation: malformed output, empty output, provider rate limit, timeout, and retry
  after a transient provider failure preserve the customer's form and preview.
- [ ] Runtime integration (`tests/test_discovery_tools.py` with a temporary app root): compile
  generated Python, discover it through `discover_project_tools()`, assert the exact JSON
  Schema above, and invoke it with mocked HTTP/auth for valid and invalid arguments.
- [ ] Portal build and server syntax checks.
- [ ] Browser verification at desktop and mobile widths.

No config fixture is required because the runtime authoring schema is unchanged.

## 7. Docs impact

- [ ] `docs/architecture.md` - note that the portal authors ordinary Custom Tool files; no
  runtime pipeline change.
- [ ] `docs/front-matter-spec.md` - no change.
- [ ] `docs/triggers.md` - no change.
- [ ] `README.md` - document the portal Custom Tool recipe workflow.
- [ ] `serverless-portal/requirements.md` or equivalent portal documentation - document
  recipe inputs, configured-model behavior, dependency merging, and RBAC permissions.
- [ ] `docs/frds/README.md` - add FRD 0008 to the index.

## 8. Status & sign-off

- **Architecture review (phase 2):** Two independent read-only reviews completed on
  2026-08-25. The first requested exact model, merge, RBAC, validation, authorization, and
  recovery semantics. The second confirmed the runtime boundary and decisions, then requested
  final provider-error, identity-error, schema, journal, and test-location details, now
  incorporated above. A final focused architecture gate returned **APPROVE** with no blockers.
  Implementation has intentionally not started because human sign-off is the phase-2 gate.
- **Human sign-off:** swapnil, 2026-08-25. Approved for local implementation; do not push
  before review.