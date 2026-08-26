# ACA Sandbox session runtime

> **Experimental.** The ACA Sandbox backend is opt-in and available on Linux
> x86_64 CPython 3.13/3.14. Platform and configuration validation remain
> fail-closed. The in-language-worker backend remains the default.

This guide is for application authors and operators. For the internal module
map and runtime pipeline, see [architecture.md](architecture.md). For the
normative design rationale and durable contracts, see
[FRD 0008](frds/0008-aca-sandbox-session-runtime.md).

## Availability and scope

Install the optional transport dependency before deploying:

```bash
python -m pip install "azurefunctions-agents-runtime[aca_sandbox]"
```

Configure `session_runtime.aca_sandbox` only for HTTP-triggered MAF agents on a
supported Linux x86_64 Functions worker. Unsupported hosts, invalid retention,
missing ACA prerequisites, and incompatible Dynamic Workflows fail startup;
they never silently select another backend. ordinary chat remains synchronous and `Prefer: respond-async` opts into the
durable run-management URLs.

The sandbox has no public inbound port. The Functions app remains the
authenticated entry point and controller.

## Disk selection and content

By default, `SandboxCreateProfile` selects the public disk named for the
Functions worker's Python minor version: `python-3.13` or `python-3.14`.
These public aliases are mutable and intentionally remain the GA default.
Set `AZURE_FUNCTIONS_AGENTS_SANDBOX_DISK` to select a customer disk by name;
use `AZURE_FUNCTIONS_AGENTS_SANDBOX_DISK_ID` when pinning a literal private
`disk_id` for reproducibility. Set only one override.

The supported public-disk pairings are CPython 3.13 on Debian 12 with glibc
2.36 and CPython 3.14 on Ubuntu 24.04 with glibc 2.39. Version-specific
extensions must match the running interpreter; compatible `abi3` extensions
and wheel tags are accepted on later CPython versions. Musl and an unmet
manylinux glibc floor fail closed.

A disk selection chooses only the base disk. There is no custom bootstrap image:
the controller delivers the bootstrap, application archive, and complete
dependency closure (including `.python_packages`) over the file plane, then
verifies its digest and ABI. A disk override does not disable integrity checks.

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
Sandbox Group. Guest code can acquire tokens through the platform identity
endpoint; egress policy limits where a token is used, not whether it can be
acquired.

The runtime attaches or forwards no identity or credentials, including
controller, token, or storage credentials. A customer-attached Sandbox Group identity is directly
guest-usable because platform token acquisition is egress-exempt; egress policy
still limits token-use destinations. Make it dedicated and least-privileged. It
may receive explicitly required workload permissions, including authenticated
MCP access where needed, but never controller, Sandbox Group management, or
state-store rights. The controller managed identity is the sole Table writer.
Native `DefaultAzureCredential` can use that workload identity for its
authorized workload calls. The U3 qualification grants the group identity model
inference only, with no MCP or state-store permissions. This model-only,
no-state/no-group RBAC is a protected infrastructure prerequisite verified by
customer IaC or operations. CI verifies the sole attached UAMI and a successful
model turn; it does not attest the absence of every role assignment. OBO is
reserved and inert: do not expect user-token forwarding.

The Function controller identity separately requires `Container Apps
SandboxGroup Data Owner` on the configured Sandbox Group. `Container Apps
SandboxGroup Contributor` is control-plane access and is insufficient for
listing, creating, or attaching data-plane sandboxes. Missing data-plane access
fails fast with HTTP `503` and `sandbox_group_authorization_failed`; it is not a
retryable setup timeout. Grant the role at the individual Sandbox Group scope.

## Setup admission deadline

ACA session admission uses one 90-second setup budget anchored before targeted
reconciliation. Synchronous execution retains its 180-second wall cap; a
full-cap request therefore leaves a 90-second execution floor. Every durable
operation uses a sliding 120-second lease. A pre-reservation setup `504` retains
`retry_with=respond-async` with `Retry-After: 120`; once a response includes a
durable management ticket, `Retry-After: 2` is the polling cadence for that
ticket.

The synchronous `/chatstream` response is an SSE lease, not an implicit async
conversion. It preserves the caller's `Prefer` choice: a committed setup
timeout without `respond-async` is a linked `504`, while an explicit async
request is `202`. Every successful synchronous stream response includes
`x-ms-session-id`, `x-ms-run-id`, and `Location` pointing to the run status URL.
The `done` event means output is complete; the session can remain `settling`
until the status URL reports `phase=terminal`, which clients must observe before
submitting another run with the same session key.

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

## Admission, setup timeouts, and recovery

Use `Prefer: respond-async` for new sessions. If setup exceeds its request
budget after the runtime reserves a session and run in Tables, the recovery
response contains:

```json
{
  "session_id": "...",
  "run_id": "...",
  "status": "accepted",
  "phase": "provisioning",
  "admission": "committed",
  "status_url": "/agents/main/sessions/.../runs/...",
  "result_url": "/agents/main/sessions/.../runs/.../result",
  "events_url": "/agents/main/sessions/.../runs/.../events",
  "cancel_url": "/agents/main/sessions/.../runs/.../cancel"
}
```

Keep those identifiers and URLs. They are sufficient to poll, stream, read the
result, or cancel; the original prompt and idempotency key are not required for
management calls.

Setup timeout responses distinguish three outcomes:

| `admission` | Meaning | Client action |
| --- | --- | --- |
| `not_reserved` | No durable session or run was created. | Submit again. The same key plus a byte-equivalent request is safe. |
| `committed` | The durable session and run exist, but setup exceeded the synchronous budget. | Use the returned URLs; async receives `202` and synchronous receives a linked `504`. |
| `possibly_committed` | The Table transaction acknowledgement was lost, so the candidate reservation may exist. | Poll the returned `status_url` for `200` or exact-replay the same key and byte-equivalent request. A `404` never proves absence. If exact replay is unavailable, start a fresh independent session with a new key and no session header. |

A committed synchronous timeout includes the same identifiers, URLs,
`Location`, `Retry-After`, and `x-ms-session-id` as the async ticket. Do not
replay a POST merely to discover its identifiers.

The public `phase` explains where work is without adding new run states:

- `provisioning`: the sandbox is being prepared and the prompt has not launched;
- `executing`: the journal-launch fence has won or the journal reports running;
- `settling`: the prompt is terminal but fenced cleanup still owns the session slot;
- `terminal`: cleanup has cleared the slot and a new run may be admitted.

Status and result return `200` with the durable `accepted`/`provisioning`
projection throughout setup. Events emit heartbeats until journal launch rather
than inventing run events. A distinct key targeting the same busy session
receives a linked `409 active_run_exists` with the existing run's phase and
management URLs.

Canceling during `provisioning` atomically prevents prompt launch for either a
new-session provision or an existing-session submission and returns a terminal
canceled run in `settling`. If the journal-launch fence has already won but the
live journal is not yet available, cancel returns `202` with `Retry-After: 2`
instead of claiming success; retry cancel or poll status until cancellation
settles. Wait for `phase=terminal` before submitting a replacement run to that
same session. A completely new session is independent of that per-session slot,
although canceling unwanted setup avoids wasting capacity.

If a `possibly_committed` candidate remains absent and exact replay is no longer
available, start an independent new session with a fresh key and no session
header. Do not treat the absent candidate as disproved or submit changed input
under its key.

`Idempotency-Key` names one logical attempt. The runtime separately hashes the
agent slug, exact prompt, and timeout to prevent that key from changing meaning:
the same key plus a byte-equivalent request safely replays the original IDs,
changed input returns `422`, and a different key is a distinct attempt subject
to the one-active-run rule.

## Lifecycle, recovery, and troubleshooting

The runtime applies per-sandbox lifecycle policy: it disables auto-suspend
during an active run and restores the idle policy after terminal adoption.
Normal v1 durability is same-sandbox disk auto-suspend/resume; the platform
does not expose an explicit snapshot resource for that normal path. The
controller timer is the reclamation authority after idle expiry and deletes any
owned snapshot resources if present before tombstoning; group auto-delete is only a backstop.

Request-path reconciliation is deliberately targeted to the requested
session/operation (and is bounded to a small quota). It never lists or probes
unrelated app-owned sessions; global orphan, expiry, inventory, and backlog
cleanup belongs to the timer. Timer passes page inventory with bounded
concurrency and a cursor that advances only after a page completes, reporting
deferred and partial progress when its deadline is reached.

File-plane `409` readiness is lifecycle-aware: the controller resumes only
when it owns that mutation, honors provider `Retry-After`, and uses capped
jittered backoff with per-candidate and whole-flow budgets. Absent backing is
never probed. ARM binding failures retain safe `401/403/404/429/5xx`
classification and retryability; authorization failures surface as
`sandbox_group_authorization_failed` rather than an opaque setup timeout.

Useful failure signals include:

- `bootstrap.error.json` for digest, ABI, protocol, or archive failures;
- a durable failed or abandoned run status after sandbox loss or reaping;
- `410 Gone` for an unavailable or evicted result.

In v1, normal disk suspension resumes the same sandbox and generation; it is
not loss and does not imply snapshot-backed durability. When the reconciler
detects missing backing, it tombstones the session and preserves its durable
status; subsequent unavailable result or session behavior is `410 Gone`. The
committed live backing-loss proof deletes only its exact-label backing and
verifies the controller's abandoned/tombstoned state and public `410` result
behavior. Content, egress, credential, and disk changes require a replacement
session. The committed live qualification path in
[the live ACA test guide](../tests/live/README.md) covers adapter create, delivery,
lower-level model turn, public Easy Auth turn, lifecycle, and backing loss.
`N=5` is the sole agent/CI load diagnostic; `N=100` formal acceptance is
human-only for one selected runtime. The Manual/Scheduled runtime matrix is
nonblocking. For detailed internal state-machine behavior, use the architecture
and FRD references above.
