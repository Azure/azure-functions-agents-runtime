# ACA Sandbox session runtime

> **Experimental.** The ACA Sandbox backend is opt-in and remains
> capability-gated until live end-to-end and load acceptance complete. The
> in-language-worker backend remains the default.

This guide is for application authors and operators. For the internal module
map and runtime pipeline, see [architecture.md](architecture.md). For the
normative design rationale and durable contracts, see
[FRD 0008](frds/0008-aca-sandbox-session-runtime.md).

## Availability and scope

Configure `session_runtime.aca_sandbox` only for HTTP-triggered MAF agents.
While the capability gate is closed, enabling it fails startup rather than
silently falling back to another backend. When the gate opens, ordinary chat remains synchronous and `Prefer: respond-async`
opts into the durable run-management URLs. A built-in chat API also exposes
`GET /agents/{slug}/history` for a selected session.

The sandbox has no public inbound port. The Functions app remains the
authenticated entry point and controller.

## Disk selection and content

By default, the runtime selects the public disk named for the Functions
worker's Python minor version, such as `python-3.13`. Select exactly one of
these Function App settings to pin a customer-managed base disk:

- `AZURE_FUNCTIONS_AGENTS_SANDBOX_DISK`
- `AZURE_FUNCTIONS_AGENTS_SANDBOX_DISK_ID`

The supported public-disk pairings are CPython 3.13 on Debian 12 with glibc
2.36 and CPython 3.14 on Ubuntu 24.04 with glibc 2.39. Version-specific
extensions must match the running interpreter; compatible `abi3` extensions
and wheel tags are accepted on later CPython versions. Musl and an unmet
manylinux glibc floor fail closed.

A disk name or ID selects the base disk only. The runtime still delivers the
application archive, including `.python_packages`, and verifies its digest and
ABI. There is no fixed-image application-content mode or implicit integrity
opt-out based on a disk override.

## Sandbox environment

The runtime forwards only this built-in non-secret profile:

```text
AZURE_FUNCTIONS_AGENTS_PROVIDER
AZURE_FUNCTIONS_AGENTS_MODEL
AZURE_FUNCTIONS_AGENTS_TIMEOUT_SECONDS
AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT
AZURE_FUNCTIONS_AGENTS_REASONING_SUMMARY
AZURE_OPENAI_ENDPOINT
AZURE_OPENAI_DEPLOYMENT
AZURE_OPENAI_API_VERSION
FOUNDRY_PROJECT_ENDPOINT
FOUNDRY_MODEL
```

Forward customer configuration with the runtime-owned prefix:

```text
AZURE_FUNCTIONS_AGENTS_SANDBOXENV_MY_API_HOST=https://api.example
```

The sandbox receives `MY_API_HOST=https://api.example`. The default backend
uses `MY_API_HOST` first and then
`AZURE_FUNCTIONS_AGENTS_SANDBOXENV_MY_API_HOST`; the sandbox receives the
prefixed value when present. The previous short-form prefix is unsupported.

The prefix is explicit guest exposure. For example,
`AZURE_FUNCTIONS_AGENTS_SANDBOXENV_AZURE_OPENAI_API_KEY` makes that key
readable by sandbox code and child processes, bypassing proxy-managed key
isolation. Use an ordinary unprefixed model key when the proxy-injected route
is intended.

## Identity and RBAC

Attach a dedicated, least-privileged managed identity to the customer-owned
Sandbox Group through customer IaC. The runtime neither attaches nor strips
that identity. Guest code can acquire tokens through the platform identity
endpoint; egress policy limits where a token is used, not whether it can be
acquired.

Do not reuse the controller identity or grant the group identity state-store,
Sandbox Group management, Storage, or Service Bus permissions unless workload
code genuinely needs them. Native `DefaultAzureCredential` handles Foundry,
Azure OpenAI, and authenticated MCP calls. Missing or incorrectly selected
identities fail when the outbound credential or request is used.

The group identity is not used to read history. ACA history is read by the
Functions controller through the existing authenticated ACA transport; the
sandbox receives no state-store credential for this feature.

## Egress and credentials

Every sandbox is created with `default_action="Deny"` and
`traffic_inspection="Full"`. The policy allows only the configured model,
telemetry, MCP, web-request, and reachable-delegate destinations. Broad rules
that hide a narrower deny are rejected.

MCP headers are static egress transformations or customer-provisioned
`secretRef` values. When an MCP server has native `auth.scope`, its managed
identity `Authorization` value wins over an authored static `Authorization`
header on both backends.

Model-key transforms follow the resolved provider: Azure OpenAI uses only
`api-key`, OpenAI uses only `Authorization: Bearer`, and Foundry uses native
managed identity. Unrelated configured keys are not sent to the selected
provider.

Policy and credential changes are create-time-only. Drain or replace a session
to apply them. Rotate a group secret the same way; active streams do not
update in place.

## Checkpoint history

For an enabled ACA session runtime, `GET /agents/{slug}/history` reads only the
latest complete conversation checkpoint selected inside the owner-authorized
sandbox. It does not use the default in-language Blob history provider and
does not copy transcript content to Blob Storage, Tables, controller
memory/disk, logs, another sandbox, or external storage.

Reading history verifies the durable owner/session binding and live sandbox
binding before it reads the immutable checkpoint. A retained stopped or
suspended sandbox is resumed through the normal activation handshake so its
history remains available. This can add wake latency and ACA cost. A history
read does **not** extend idle retention, touch activity, or immediately stop
the sandbox; normal lifecycle policy re-suspends it when idle.

The response retains the normal presentation rules: ordered user/assistant
messages only, with at most the latest 200 after filtering. A session with no
admitted turn returns an empty `200`. The remaining outcomes are deliberately
typed:

| Condition | Response |
| --- | --- |
| Retained sandbox was resumed for this read | `200` with `x-ms-aca-history-resumed: true` |
| Caller has no matching owner/session binding | `404 session_not_found` |
| Confirmed reclaim, sandbox loss, tombstone, deletion, or deployment-epoch retirement | `410 history_gone` |
| Required/legacy checkpoint is missing, corrupt, unsafe, or temporarily unreadable; or its binding cannot be trusted | `503 history_unavailable` |

`410` is permanent for the retained row's history horizon; after normal row
pruning the same request becomes `404`. Neither outcome falls back to Blob
history or a reconstructed transcript.

## Lifecycle, recovery, and troubleshooting

The runtime applies per-sandbox lifecycle policy: it disables auto-suspend
during an active run and restores the idle policy after terminal adoption.
The reconciler is the deletion authority after idle reclaim; group auto-delete
is only a backstop.

Useful failure signals include:

- `bootstrap.error.json` for digest, ABI, protocol, or archive failures;
- a durable failed or abandoned run status after sandbox loss or reaping;
- `410 Gone` for an unavailable or evicted result.

In v1, sandbox or snapshot loss tombstones the session; create a new session.
Content, egress, credential, and disk changes likewise require a replacement
session. For detailed operational diagnostics and the exact internal
state-machine behavior, use the architecture and FRD references above.
