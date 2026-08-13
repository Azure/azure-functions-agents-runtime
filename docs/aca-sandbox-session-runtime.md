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
they never silently select another backend. Ordinary chat remains synchronous
and `Prefer: respond-async` opts into the durable run-management URLs.

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
inference only, with no MCP or state-store permissions. OBO is reserved and
inert: do not expect user-token forwarding.

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

## Lifecycle, recovery, and troubleshooting

The runtime applies per-sandbox lifecycle policy: it disables auto-suspend
during an active run and restores the idle policy after terminal adoption.
Normal v1 durability is same-sandbox disk auto-suspend/resume; the platform
does not expose an explicit snapshot resource for that normal path. The
controller timer is the reclamation authority after idle expiry and deletes any
owned snapshot resources if present before tombstoning; group auto-delete is
only a backstop.

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
