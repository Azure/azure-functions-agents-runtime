# Rust stdio runtime + real .NET isolated Functions host (experimental)

This spike runs a **real Azure Functions v4 host and .NET 10 isolated worker**.
An HTTP function uses an ordinary DI-injected `HostedSkill` service to talk to
one managed Rust `fhs-runtime` subprocess over bidirectional JSON-lines stdio.
**Inference is deterministic MOCK, not a live model or Copilot session.**
This does not change the production Python runtime or add a production binding.

## What is real, and what is not

| Surface | Behavior |
| --- | --- |
| `HostedSkillSupport` | Real `[Function]` + `[HttpTrigger]`, `POST /api/support`, genuine `HttpRequestData` / `HttpResponseData` |
| `ShippingQuote` | Real `[Function]` + `[HttpTrigger]`, `POST /api/shipping/quote` |
| `HostedSkill` | Ordinary singleton worker DI service; **not** a Functions input binding. There is no `HostedSkillInput` attribute |
| `lookup_order` | Rust reverse `tool.call` executes a plain C# `[AgentTool]` method in the worker |
| `calculate_shipping_http` | Rust reverse `tool.call` executes an explicit adapter to the **same `OrderTools.CalculateShipping` application logic** used by `ShippingQuote`; no HTTP loopback |
| Trigger metadata | Describes the adapter's related function, method and route. It is not an instruction to the Functions binding system |
| Session persistence | Rust SessionFs writes actual local files or actual Azurite Blobs; mock conversation turns, not a live SDK transcript |
| Provider | Configuration is resolved, but no model/token endpoint is contacted by the smoke test |

The shipping tool **does not invoke the Functions trigger method or invocation
pipeline**: no synthetic `FunctionContext`, middleware, authorization, HTTP
headers, binding converters, or output bindings run on this callback path.
The direct HTTP smoke request tests that framework path separately. The outputs
identify the path as `stdio-shared-handler` versus `http-trigger`. `[AgentTool]`
is a spike-only registration marker, not an Azure binding attribute.

```text
curl /api/support
  -> actual Functions host HTTP binding -> .NET isolated OrderFunctions
  -> DI HostedSkill -> JSON-RPC agent.run -> Rust mock agent loop
       -> reverse tool.call -> C# LookupOrder
       -> reverse tool.call -> C# shared CalculateShipping logic
       -> SessionFs persistence -> agent.run result
  -> actual HttpResponseData

curl /api/shipping/quote
  -> actual Functions host HTTP binding -> ShippingQuote -> same shipping logic
```

## Prerequisites

- .NET 10 SDK, Rust/Cargo, Python 3, Linux (`ps` and POSIX signals for smoke cleanup).
- Azure Functions Core Tools v4 supporting .NET 10. Verified with **4.16.0**.
- Optional Azurite (`azurite-blob`); no Azure subscription or credentials needed.
- Initial NuGet/Cargo restore needs network access. Worker **2.52.0**, Worker.Sdk
  **2.1.0**, HTTP extension **3.3.0** are pinned in the project.

Reuse an existing `func` installation with `--func "$(command -v func)"`.
Alternatively extract the official Linux x64 package into the ignored
`spikes/rust-runtime/.tools/core-tools/` directory:

```bash
cd spikes/rust-runtime
mkdir -p .tools/core-tools .tools/scratch
export TMPDIR="$PWD/.tools/scratch" TMP="$PWD/.tools/scratch" TEMP="$PWD/.tools/scratch"
curl --fail --location --retry 3 \
  https://github.com/Azure/azure-functions-core-tools/releases/download/4.16.0/Azure.Functions.Cli.linux-x64.4.16.0.zip \
  --output .tools/core-tools.zip
python -m zipfile -e .tools/core-tools.zip .tools/core-tools
chmod u+x .tools/core-tools/func
.tools/core-tools/func --version
```

Install Azurite locally only if needed:

```bash
npm install --prefix .tools/azurite azurite
# Then pass --azurite "$PWD/.tools/azurite/node_modules/.bin/azurite-blob".
```

Do not commit `.tools`, logs, storage, `local.settings.json`, `bin`, or `obj`.
The smoke test does not create/overwrite local settings. It supplies nonsecret
app settings to its own subprocesses, chooses unused ports, waits for **both
indexed functions, initialized worker, Rust initialization and Running host**,
and stops only its own processes with bounded waits. It retains evidence and
storage for inspection under ignored `.tools/smoke/<run-id>/` by default.

## Repeatable real-host verification

From `spikes/rust-runtime`:

```bash
python smoke_host.py --storage local
python smoke_host.py --storage azurite
# External tools and evidence locations can be supplied explicitly:
python smoke_host.py --storage azurite --func "$(command -v func)" \
  --azurite "$(command -v azurite-blob)" --evidence-dir "$PWD/.tools/my-evidence"
```

Each run builds Rust and C#, starts **`func host start --no-build --port <unused>`**
from `dotnet/HostedSkills.Spike/bin/Debug/net10.0` (where SDK-generated
`functions.metadata`, `worker.config.json`, `.azurefunctions` and app content
actually live), and asserts:

1. Direct HTTP shipping returns `PriorityExpress`, `14.25`, `ORD-900`, `98052`
   and `invocationPath=http-trigger`.
2. Missing support prompt returns HTTP 400.
3. First support HTTP turn calls **both C# tools over Rust stdio**, returns
   `Ada Lovelace`, `PAID`, `149.99`, and shipping output with
   `invocationPath=stdio-shared-handler`. Loaded skill and substituted tenant
   instructions appear in the mock response.
4. Second HTTP turn with the same session ID reports `resumed=true`, `turn=2`,
   and persisted transcript records 1 and 2.
5. After stopping and restarting the **entire host, worker and Rust process**,
   a third HTTP turn reports `resumed=true`, `turn=3` with all three records.

`build.log`, `host.log`, `host-restarted.log`, raw HTTP JSON responses and
`summary.json` are saved in the evidence directory. Azurite runs also keep
`azurite.log` and its storage directory. Every HTTP request has a timeout;
startup, build and shutdown are bounded.

Rust's existing tests can be run independently (including its in-process Blob
HTTP emulator tests, which are **not** the same as the real Azurite smoke):

```bash
export TMPDIR="$PWD/.tools/scratch" TMP="$PWD/.tools/scratch" TEMP="$PWD/.tools/scratch"
cargo test
```

## Manual host and HTTP requests

After `cargo build --bin fhs-runtime` and
`dotnet build dotnet/HostedSkills.Spike`, export the following from the spike:

```bash
export FUNCTIONS_WORKER_RUNTIME=dotnet-isolated AzureWebJobsSecretStorageType=files
export AzureWebJobsStorage='' FHS_MOCK_LLM=true
export FHS_RUNTIME_PATH="$PWD/target/debug/fhs-runtime"
export DEFAULT_AGENT_MODEL=mock-model ENVIRONMENT_NAME=dotnet-isolated TENANT_NAME=ContosoRetail
export AZURE_FUNCTIONS_AGENTS_PROVIDER=foundry
export FOUNDRY_PROJECT_ENDPOINT=https://contoso-foundry.services.ai.azure.com/api/projects/retail
export AZURE_FUNCTIONS_AGENTS_LOCAL_STORAGE_DIR="$PWD/.tools/manual-sessions"
FUNC="$PWD/.tools/core-tools/func"
cd dotnet/HostedSkills.Spike/bin/Debug/net10.0
"$FUNC" host start --no-build --port 7071
```

Use a free port if 7071 is occupied. In another shell:

```bash
curl --fail-with-body -sS --max-time 55 http://localhost:7071/api/support \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"lookup_order calculate_shipping_http {\"orderId\":\"ORD-900\",\"zip\":\"98052\"}","sessionId":"manual-001"}'
curl --fail-with-body -sS --max-time 55 http://localhost:7071/api/support \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Continue the conversation.","sessionId":"manual-001"}'
curl --fail-with-body -sS --max-time 55 http://localhost:7071/api/shipping/quote \
  -H 'Content-Type: application/json' \
  -d '{"orderId":"ORD-900","zip":"98052"}'
```

These anonymous endpoints expose mock customer/session data and are **local
test endpoints only**. Do not publish this sample unchanged to the internet.

## Source map and limitations

- `src/config.rs`, `env.rs`, `skills.rs`: Rust config/markdown discovery,
  substitution, skill filtering. These are a separate experimental
  implementation, **not full parity** with Python validation/authoring.
- `src/engine.rs`, `protocol.rs`: bidirectional stdio, mock and unverified live
  SDK execution branches, stream notifications and session inspection.
- `src/session_fs.rs`: local filesystem or SharedKey Blob SessionFs. Azurite
  smoke uses the public emulator account/key on a private, unused loopback
  port. **Blob identity/Bearer auth is incomplete** (the authorization header
  is a placeholder); do not claim Managed Identity storage support.
- `src/providers.rs`: provider resolution and token-provider code. Real
  Foundry/Azure OpenAI/OpenAI authentication and inference are **not tested**
  by this integration. `mock-model` is only a diagnostic label.
- `dotnet/HostedSkills.Spike`: genuine Functions SDK metadata and HTTP bindings;
  sample-only DI facade and explicit shared-handler adapter.
- `RuntimeService`: owns a singleton Rust subprocess per worker; initializes
  at startup and shuts it down through `IHostedService` / async DI disposal.
- `HostedSkillRuntimeClient`: drains stderr, correlates requests, fails pending
  calls on EOF, removes listeners/requests on completion or cancellation,
  serializes agent turns, and bounds RPC/shutdown waits. No automatic restart
  or replay after a crash; restart the worker. Stream notifications are scoped
  by agent/session; this host exposes only nonstreaming HTTP responses.

There is no NuGet/native-binary deployment packaging, production custom input
binding, SDK/CLI bundling, arbitrary trigger synthesis, distributed session
locking, MCP auth parity, structured-output parity, or telemetry propagation.
Do not infer multi-language support or production deployment readiness from
this single C# worker experiment. Production Python remains unchanged.
