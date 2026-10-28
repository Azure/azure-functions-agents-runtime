# Rust stdio runtime + real .NET isolated Functions host (experimental)

This spike runs a **real Azure Functions v4 host and .NET 10 isolated worker**.
An HTTP function uses an ordinary DI-injected `HostedSkill` service to talk to
one managed Rust `fhs-runtime` subprocess over bidirectional JSON-lines stdio.
**Inference is deterministic MOCK, not a live model or Copilot session.**
This does not change the production Python runtime or add a production binding.

For the visual overview and demonstrated outcomes, see the
[leadership README](LEADERSHIP-README.md).

## What is real, and what is not

| Surface | Behavior |
| --- | --- |
| `HostedSkillSupport` | Real `[Function]` + `[HttpTrigger]`, `POST /api/support`, genuine `HttpRequestData` / `HttpResponseData` |
| `ShippingQuote` | One handler with `[Function]`, `[AgentTool]` and `[HttpTrigger]`; real `POST /api/shipping/quote` plus direct tool invocation |
| `HostedSkill` | Ordinary singleton worker DI service; **not** a Functions input binding. There is no `HostedSkillInput` attribute |
| `lookup_order` | Rust reverse `tool.call` executes a plain C# `[AgentTool]` method in the worker |
| `calculate_shipping_http` | Rust reverse `tool.call` executes **the same `ShippingQuoteAsync` handler** using runtime-owned synthetic HTTP objects; no app-authored adapter or HTTP loopback |
| Trigger metadata | Describes the attributed function, method and route. It is not an instruction to the Functions binding system |
| Session persistence | Rust SessionFs writes actual local files or actual Azurite Blobs; mock conversation turns, not a live SDK transcript |
| Provider | Configuration is resolved, but no model/token endpoint is contacted by the smoke test |

The shipping tool **directly invokes the handler in the same .NET worker**, in a
fresh DI scope, but **does not enter the Functions invocation pipeline**. Its
synthetic request/context supplies a JSON body and configured response serializer,
not real caller identity, ingress headers, middleware, host authorization, binding
converters, retries, or output-binding orchestration. The direct HTTP smoke request
tests the framework path separately. The outputs identify the path as
`stdio-function-handler` versus `http-trigger`. `[AgentTool]` is a spike-only
registration marker, not an Azure binding attribute.

```text
curl /api/support
  -> actual Functions host HTTP binding -> .NET isolated OrderFunctions
  -> DI HostedSkill -> JSON-RPC agent.run -> Rust mock agent loop
       -> reverse tool.call -> C# LookupOrder
       -> reverse tool.call -> synthetic HTTP request -> ShippingQuoteAsync
       -> SessionFs persistence -> agent.run result
  -> actual HttpResponseData

curl /api/shipping/quote
  -> actual Functions host HTTP binding -> ShippingQuote -> same shipping logic
```

## One function, two entry paths

The sample's shipping function carries both method attributes. Attribute order
does not matter; the Functions SDK still owns normal trigger indexing.

```csharp
public sealed record ShippingRequest
{
    public required string OrderId { get; init; }
    public required string Zip { get; init; }
}

public sealed class ShippingFunctions(OrderTools tools)
{
    [Function("ShippingQuote")]
    [AgentTool("calculate_shipping_http", "Calculate shipping for an order and destination ZIP.",
        InputType = typeof(ShippingRequest))]
    public async Task<HttpResponseData> ShippingQuoteAsync(
        [HttpTrigger(AuthorizationLevel.Anonymous, "post", Route = "shipping/quote")]
        HttpRequestData request,
        CancellationToken cancellationToken = default)
    {
        var input = await JsonSerializer.DeserializeAsync<ShippingRequest>(
            request.Body, JsonSerializerOptions.Web, cancellationToken);
        if (input is null)
            throw new JsonException("Shipping input is required.");
        var response = request.CreateResponse(HttpStatusCode.OK);
        await response.WriteAsJsonAsync(
            tools.CalculateShipping(input, null, "example"), cancellationToken);
        return response;
    }
}
```

In `Program.cs`, `registry.RegisterFunctions<ShippingFunctions>(provider)` inspects
the type without constructing it. Each tool call creates an async DI scope and
resolves the function instance only then; startup does not resolve `HostedSkill`
while its Rust process is still being initialized. Existing plain methods such
as `OrderTools.LookupOrder` still use `RegisterFromObject(tools)`.

The tool schema is generated from `ShippingRequest`, **not** from
`HttpRequestData` or `CancellationToken`. Required DTO fields must be present
and non-null; wrong JSON types and unknown fields are rejected before invocation.
The bounded input contract supports scalar string, boolean and numeric properties
and nullable scalar properties, with web JSON naming. Unsupported DTO forms,
missing input types, unsupported trigger/signature shapes, and duplicate tool
names fail registration rather than silently registering an unusable tool.

For the HTTP adapter, use exactly one `[HttpTrigger] HttpRequestData` parameter,
return `HttpResponseData` or `Task<HttpResponseData>`, and give any other supported
optional parameters defaults. Additional binding parameters are not supported.
There is **no** Queue, Service Bus, timer, or generic trigger adaptation.

The synthetic request is `POST http://localhost/api/tool` with an
`application/json` body, empty query, and no inbound headers or identities.
The sample recognizes the direct-call context marker only for diagnostic
`invocationPath` output. This marker is **not** authorization. Handlers that
depend on middleware, authenticated callers, or other host facilities are not
suitable for direct invocation without a separately designed application policy.

Tool results preserve the Python spike's HTTP envelope:

```json
{
  "status_code": 200,
  "body": "{\"carrier\":\"PriorityExpress\",\"rateUsd\":14.25}"
}
```

The body stays UTF-8 text even when it contains JSON; headers are discarded.
The generated tool description tells the agent to inspect `status_code` and
interpret `body`. A handler returning 400 is still a completed tool call carrying
an HTTP error response, not a thrown tool exception. This is information for the
model, not deterministic enforcement of business success. Thrown exceptions use
the tool-error channel. Request/response streams and the DI scope are disposed.

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

Local mode leaves `AzureWebJobsStorage` empty: these HTTP-only functions work
without it, but Core Tools can log a storage-health warning. Azurite mode supplies
real emulator storage to both the host and Rust; it does not use that fallback.

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
   `Ada Lovelace`, `PAID`, `149.99`, and shipping's actual status/body envelope
   with `invocationPath=stdio-function-handler` and no synthetic caller session
   header. The dual-attributed handler remains present in SDK trigger metadata.
   An empty ZIP returns 400 from the same handler on both HTTP and tool paths.
   Loaded skill and substituted tenant instructions appear in the mock response.
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
dotnet run --project dotnet/HostedSkills.Spike.Checks
```

The C# executable checks use a deliberately faulty Python stdio peer
(**not** Rust or an LLM) to check initialize/run EOF, stderr backpressure,
stream listener scoping/cleanup, cancellation, abandoned streams, bounded
idempotent shutdown, and required plain-tool arguments. Additional adapter checks
exercise dual attributes, DTO validation, HTTP status/body conversion, scoped DI,
serialization, cleanup, and unsupported signatures without a live host or model.
The checks use the same pinned worker/HTTP libraries as the sample. The executable fixture
needs its checked-in executable bit on Linux.

Verified with Core Tools 4.16.0 and .NET SDK 10.0.402: all 21 C# checks and
11 Rust tests pass. Both local and Azurite real-host smoke modes pass, including
dual-attribute metadata, the handler's success/error responses on both entry
paths, and whole-host restart persistence. These checks use mock inference only.

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

The first request exercises the attributed shipping handler as an agent tool;
the last exercises the **same method** through the real HTTP trigger. To inspect
the error-status envelope, use a separate session and an empty ZIP:

```bash
curl --fail-with-body -sS --max-time 55 http://localhost:7071/api/support \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"calculate_shipping_http {\"orderId\":\"ORD-900\",\"zip\":\"\"}","sessionId":"manual-shipping-error"}'
```

The outer support HTTP response is 200 because the agent turn completed; its mock
tool output contains `status_code=400` and the handler's error body. The mock
prints tool results deterministically; it does not demonstrate a live model's
ability to interpret or react to HTTP statuses.

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
  sample-only DI facade, dual-attribute function discovery, and runtime-owned
  direct HTTP adapter. See [FRD 0012](../../docs/frds/0012-dotnet-function-tools-spike.md).
- `RuntimeService`: owns a singleton Rust subprocess per worker; initializes
  before the worker hosted service starts, and shuts it down through
  `IHostedService` / async DI disposal.
- `HostedSkillRuntimeClient`: drains stderr, correlates requests, fails pending
  calls on EOF, removes listeners/requests on completion or cancellation,
  serializes agent turns, and bounds RPC/shutdown waits. No automatic restart
  or replay after a crash; restart the worker. Since there is no cancellation
  RPC, canceling/abandoning an active turn **terminates that Rust child and
  fails closed**; restart the worker before more turns (committed storage may
  survive, interrupted-turn completion is not guaranteed). Stream notifications
  are scoped by agent/session; this host exposes only nonstreaming HTTP responses.

There is no NuGet/native-binary deployment packaging, production custom input
binding, SDK/CLI bundling, arbitrary trigger synthesis, distributed session
locking, MCP auth parity, structured-output parity, or telemetry propagation.
Do not infer multi-language support or production deployment readiness from
this single C# worker experiment. Production Python remains unchanged.
