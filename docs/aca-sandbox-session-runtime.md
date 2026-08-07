# ACA Sandbox session runtime

The ACA Sandbox session runtime is an experimental execution backend for
HTTP-triggered MAF agents. It keeps the Functions app as the authenticated
controller and runs one session harness in a customer-owned ACA Sandbox. The
normal in-language-worker backend remains the default.

The startup capability gate remains closed until live end-to-end acceptance.
This document describes the controller and harness contracts already wired
behind that gate.

## Integration boundary

The activation layer builds one immutable create profile, preserves the durable
operation labels, delivers content and bootstrap artifacts in one fenced
operation, and validates the harness protocol/capability map before accepting
the journal. The startup gate intentionally keeps ACA execution unavailable
until live end-to-end and load acceptance completes.

## Disk boot and content delivery

By default, a session sandbox uses the public disk named for the Functions
worker's Python minor version, such as `python-3.13`. This requires no customer
image or registry setup. Public names are mutable aliases; set exactly one of
these Function App settings to use a customer-pinned disk instead:

- `AZURE_FUNCTIONS_AGENTS_SANDBOX_DISK`
- `AZURE_FUNCTIONS_AGENTS_SANDBOX_DISK_ID`

The runtime uses `Disk` auto-suspend mode. On resume, ACA starts a fresh
process and re-executes the disk entrypoint. The one-shot entrypoint waits for
`.boot-ready`, runs the delivered bootstrap with canonical child paths under
the single sandbox root, writes output to `bootstrap.log`, publishes readiness, and exits. Each
run is then a separately supervised
`python -m azure_functions_agents.harness --run-id ...` process.

The create profile supplies `/app` and its captured site-packages directory in
`PYTHONPATH`. Bootstrap writes a persistent `sitecustomize.py` so fresh harness
interpreters also process delivered `.pth` files rather than relying on the
bootstrap process's in-memory `sys.path`.

The verified public-disk ABI pairing is CPython 3.13 on Debian 12 with glibc
2.36 and CPython 3.14 on Ubuntu 24.04 with glibc 2.39. This is a current
platform fact, not an immutable public-image contract. The bootstrap reads the
actual interpreter and glibc version on every boot; customers that need a
fixed image can provision a compatible custom disk and select its name or ID
with the settings above.

The controller delivers the captured application archive, digest sidecar,
manifest seed, bootstrap source, bootstrap digest, and finally `.boot-ready`.
ACA file writes are not atomic, so the sentinel is intentionally last and only
after read-back verifies every preceding artifact. The bootstrap independently
parses the seed, checks both archive digests, validates CPython and manylinux
ABI compatibility, safely stages `/app`, and atomically publishes the live
manifest. A matching on-disk digest skips extraction after resume.

The stock disk's public name can roll to a different base image. The bootstrap
therefore fails closed for an incompatible CPython ABI, unsupported glibc floor,
musl, malformed archive, digest mismatch, or protocol mismatch. It writes a
typed `bootstrap.error.json` and never publishes readiness in those cases.

## Sandbox environment

The runtime never copies the entire Functions worker environment. It forwards
only this built-in non-secret profile:

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

For customer-specific configuration, set `SandboxEnv__NAME=value` in the
Function App. The sandbox receives `NAME=value`. The prefix is explicit
customer intent: it has no denylist, including for credential-shaped names.
Sandbox code and child processes can read and exfiltrate every forwarded
setting, so use this route only for values the workload is allowed to hold.

The default backend looks up an unprefixed setting first and falls back to
`SandboxEnv__NAME`. If both exist, the default backend uses the unprefixed
value while the sandbox receives the stripped prefixed value. Unresolved
placeholders retain normal backend behavior instead of causing an ACA-only
startup failure.

For example, `SandboxEnv__AZURE_OPENAI_API_KEY` is allowed by design, but it
deliberately bypasses proxy-managed key isolation: the actual key becomes
readable by guest code and child processes. Use the ordinary unprefixed model
key setting when the static proxy-header route is desired instead.

## Identity and model authentication

A managed identity attached to the customer-owned Sandbox Group is directly
available to guest code through the platform identity endpoint. Egress policy
controls destinations where a token can be used; it does not prevent token
acquisition. The runtime does not attach, remove, or strip this identity; the
controller identity remains separate and is the sole state writer.

Use a dedicated, least-privileged group user-assigned identity. Do not reuse
the controller identity and do not grant the group identity state-store,
Sandbox Group Data Owner, Storage, or Service Bus permissions unless workload
code genuinely needs them. Foundry and Azure OpenAI can use
`DefaultAzureCredential` inside the sandbox with that group identity.

An MCP server declared with `auth: { scope, client_id }` keeps its native
in-process `DefaultAzureCredential` behavior. On the default backend this
selects the Function App identity; in a sandbox it selects the Sandbox Group
identity. A reachable authenticated MCP endpoint requires an appropriate group
identity, and an explicit client ID must select an identity available to that
group. Long-lived streams reconnect when their token expires; neither native
auth nor proxy headers rotate a credential in an already-open stream.

## Egress and header credentials

Every sandbox has an explicit per-sandbox policy with `default_action="Deny"`
and `traffic_inspection="Full"`. The policy derives destinations from
`web_request` hosts, MCP URLs, the model endpoint, telemetry, and reachable
delegates. Full rules are ordered from most specific to broadest; a broad Allow
that would hide a narrower Deny is rejected. ARM and the ACA data plane are
explicitly denied before broad allows.

When `web_request.allowed_hosts` is omitted, the existing tool contract allows
any public host subject to its SSRF floor. The sandbox compiler preserves that
behavior with a wildcard Allow after the explicit control-plane Denies. Set
`allowed_hosts` to use an exact destination boundary instead.

The platform re-evaluates HTTP redirects and already blocks IMDS and the
wireserver. It does not inspect UDP DNS to Azure DNS; DNS-based exfiltration is
a known platform limitation. `get_egress_decisions()` is a rolling sample, not
a complete audit log. Egress is create-time-only in this version: policy or
credential changes do **not** reach live sessions. Drain the session and start
a new one to apply them.

The runtime supports at most 500 combined host and full rules per sandbox
policy. This is the default service limit; it intentionally does not depend on
optional service-level quota overrides.

MCP header strings are structurally headers, not values inferred from their
names. By default, a referenced Function App setting is resolved by the
controller into a static `Set` transformation. The actual key never reaches
the sandbox process or filesystem, but static policy values are visible to
sandbox data-plane read/list callers. Treat those permissions as
secret-bearing.

Customers who need stronger policy metadata separation can provision a group
secret and use an ACA-specific `secretRef` at a header value:

```json
{
  "headers": {
    "Authorization": {
      "secretRef": {
        "secret": "mcp-github",
        "key": "TOKEN",
        "format": "Bearer {value}"
      }
    }
  }
}
```

`format` must be non-empty and contain the literal `{value}` placeholder. The
runtime only references the secret; it never reads, writes, rotates, or deletes
it. A missing secret or key fails sandbox creation. Rotation requires draining
and replacing the session; deletion is not credential revocation and there is
no mid-stream update. Group-secret provisioning is a data-plane operation
performed with an SDK, deployment script, or `az rest`, not a runtime-created
resource.

## Delegation and conformance

The sandbox rebuilds the existing immutable `AgentCatalog` from captured
application content. It reuses the normal `delegate_<slug>` tools and
single-level delegated role: specialists are fresh per call, receive their own
static capabilities, and never create another sandbox, session, or top-level
run.

Harness capabilities are described by one exact map: `atomic_commit_v1`,
`watchdog_v1`, `bootstrap_v1`, and `delegation_v1`. Unknown features fail
closed, and every advertised capability requires a runtime-produced semantic
trace. Those traces compare event order, stable fields, and terminal state
while excluding reasoning text, wording, timing, and provider metadata.

## Lifecycle notes

Auto-suspend starts enabled at creation because of the current SDK behavior.
The controller disables it before an active run and re-arms it after terminal
adoption. CPU work and egress alone do not count as ACA activity, so detached
async work needs this lifecycle protection. The durable content commit happens
at turn boundaries: on node drain, the disk snapshot precedes the process
termination signal, so last-gasp shutdown writes cannot be relied on.
