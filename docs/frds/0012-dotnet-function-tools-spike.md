---
frd: 0012
title: Dual-attribute HTTP function tools in the .NET spike
status: Finalized
author: larohra
created: 2026-10-27
updated: 2026-10-27
issues: []
pull_requests: []
branch: larohra/legendary-journey
---

# FRD 0012 - Dual-attribute HTTP function tools in the .NET spike

## 1. Summary

The experimental C# worker supports a method carrying both `[Function]` and
`[AgentTool]`, with an HTTP trigger parameter. Ordinary HTTP requests still use
the Functions host. Agent calls invoke the same handler directly after the
tool registry adapts validated JSON arguments into a synthetic HTTP request.
The application no longer authors a separate shipping-tool adapter.
This follows the Python function-tools spike's authoring and invocation model,
not its complete trigger inventory.

## 2. Motivation / problem

The current C# sample exposes shipping as both an HTTP function and a tool, but
requires explicit tool registration that calls shared application logic instead
of the function handler. Putting both attributes on the handler should be
sufficient once its containing type is registered for tool discovery.
Framework request objects must not become model-facing tool arguments.

## 3. Goals / Non-goals

**Goals**
- Demonstrate `[Function]`, `[AgentTool]`, and `[HttpTrigger]` on one handler.
- Keep the tool's explicit JSON input contract separate from SDK request types.
- Preserve ordinary trigger indexing and real HTTP execution.
- Retain existing plain `[AgentTool]` methods.
- Fail registration for unsupported dual-attribute signatures.
- Exercise the handler through reverse Rust stdio callbacks and real HTTP.

**Non-goals**
- Production Python changes or a production .NET binding/package.
- HTTP loopback or reproduction of the Functions invocation pipeline.
- Queue, Service Bus, timer, or other non-HTTP trigger adapters.
- Real inbound authentication, route/query binding, or output bindings on tool calls.
- Live Foundry enablement or validation.
- Automatic assembly scanning, new agent scoping, or Python registry parity.

## 4. Proposed design

These modules are entirely under `spikes/rust-runtime/`. The production pipeline
and module map in `docs/architecture.md` remain unchanged.

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| discover | `dotnet/HostedSkills.Spike/AgentToolRegistry.cs` | Inspect explicitly registered types for both attributes; retain plain-tool discovery. |
| translate | Registry and a spike-only HTTP adapter | Build a tool schema from an explicit input DTO and validate arguments before constructing a synthetic request. |
| register | `dotnet/HostedSkills.Spike/Program.cs` | Register shipping function tools without a user-authored adapter; resolve their instances lazily through DI. |
| execute | HTTP adapter and shipping handler | Directly invoke the attributed handler, await it, and normalize its HTTP response. Rust RPC is unchanged. |

### Authoring / API surface

The intended shipping handler has this shape:

```csharp
[Function("ShippingQuote")]
[AgentTool("calculate_shipping_http", "Calculate shipping.", InputType = typeof(ShippingRequest))]
public Task<HttpResponseData> ShippingQuoteAsync(
    [HttpTrigger(AuthorizationLevel.Anonymous, "post", Route = "shipping/quote")]
    HttpRequestData request,
    CancellationToken cancellationToken = default)
```

`ShippingRequest` supplies the agent-facing `orderId` and `zip` fields.
The bounded DTO contract supports scalar JSON properties with explicit required
fields; unsupported property types fail registration. Missing required fields,
null required values, wrong JSON types, and unknown arguments fail before the
handler runs. Web JSON naming is used for schema generation and serialization.

Dual-attribute handlers must have exactly one `[HttpTrigger] HttpRequestData`
parameter and return `HttpResponseData` or `Task<HttpResponseData>`. Optional
`CancellationToken` uses its default value; other required parameters are
rejected. Unsupported triggers, additional binding parameters, and absent input
DTOs fail at registration with actionable messages. Attribute order does not
change discovery. Duplicate tool names fail instead of overwriting registration.
Validation explicitly checks required presence, null values, and JSON kinds;
deserializing a DTO alone is not considered validation.

Registration inspects the function type without constructing it. Each tool call
creates and disposes its own asynchronous DI scope. Handler instances are resolved
inside that scope at execution, so registry creation does not require
`HostedSkill` or create a runtime-startup dependency cycle.
Shipping can live in its own function class with only business-service
dependencies. Function discovery remains owned by the Functions SDK.

### Direct invocation contract

The adapter constructs an in-memory POST request with a fixed local URL
`http://localhost/api/tool`, JSON body, application/json content type, empty
query string, and no caller headers or authenticated identities. It supplies
spike-owned subclasses of the SDK's abstract `HttpRequestData`,
`HttpResponseData`, and `FunctionContext`. It does not construct internal gRPC
worker implementations. The context's `InstanceServices` exposes the tool call's
DI scope, including the worker's configured `IOptions<WorkerOptions>` and
`ObjectSerializer` needed by `WriteAsJsonAsync`. The adapter must verify that
these serialization services exist; absence is an explicit execution error,
not a fallback to different serialization behavior.
This context is not a real Functions invocation. Access to unsupported context
features fails explicitly. No route matching or ingress occurs.

The adapter returns `{ "status_code": <integer>, "body": "<UTF-8 response text>" }`
as tool content, matching the Python HTTP adapter. Response headers are not
forwarded, and a JSON response body stays a string. An HTTP error status remains
visible in that envelope; it is not automatically a tool execution exception.
Thrown handler exceptions use the existing tool-error channel.
Request and response streams are disposed after conversion.

The tool path does not run worker middleware, host authorization, binding
converters, retries, or output-binding orchestration. Handlers needing real
caller identity or other framework facilities are not suitable for this adapter.
The ordinary HTTP path continues to receive genuine framework objects.
The sample identifies synthetic direct invocation separately from HTTP ingress.

### Compatibility

Existing plain tools and tool filtering remain unchanged, except duplicate names
now fail. The shipping tool name, route, business output, and mock provider remain.
Its tool result gains the HTTP response envelope and its invocation-path evidence
changes from shared-handler to direct function-handler execution. Smoke assertions
must check the envelope and both paths rather than only matching response text.
There is no change to `.agent.md`, Python schemas, or production endpoints.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Agent invocation path | HTTP loopback / direct handler | Direct invocation aligned with the Python function-tools spike | Human (larohra) | 2026-10-27 |
| 2 | Initial trigger support | All trigger SDKs / bounded HTTP adapter | Only the existing isolated-worker HTTP request/response contract | Human (larohra), approving reviewed proposal | 2026-10-27 |
| 3 | Tool input contract | SDK object schema / explicit DTO | Explicit DTO converted to synthetic JSON HTTP body | Human (larohra), approving Python-aligned proposal | 2026-10-27 |
| 4 | HTTP result representation | Parsed JSON / status and text | Preserve status and UTF-8 body string; describe status checking to the agent | Human (larohra), approving explained response contract | 2026-10-27 |

## 6. Test plan

- [x] C# checks: plain tools preserved; both attribute orders; DTO schema and validation.
- [x] C# checks: sync and async HTTP handlers, exception conversion, response status/body.
- [x] C# checks: unsupported signature/trigger/input DTO and duplicate-name rejection.
- [x] C# checks: lazy DI resolution, per-call scoped dependency disposal, absent
      synthetic caller identity, and stream disposal.
- [x] C# checks: a synthetic request's `CreateResponse` and `WriteAsJsonAsync`
      work using the configured worker serializer; missing serialization services
      fail explicitly. Establish this feasibility before completing the adapter.
- [x] Real host: SDK metadata still indexes shipping as an HTTP trigger.
- [x] Real host: direct HTTP shipping remains successful.
- [x] Rust stdio: support invokes both tools; shipping response contains its HTTP envelope
      and direct-handler invocation-path evidence.
- [x] Local and Azurite smoke: same-session turns and whole-host restart persistence.
- [x] Existing Rust and C# checks remain green.

Python config fixtures are not applicable: no Python authoring surface changes.
The C# executable checks and real-host smoke are the spike's existing test surfaces.

## 7. Docs impact

- `spikes/rust-runtime/README.md`: dual-attribute example, DTO contract, synthetic
  request/response behavior, explicit framework boundaries, revised source map.
- `docs/frds/README.md`: index this experimental design.
- `docs/architecture.md`: add a brief experimental-boundary note; do not describe
  this spike as part of the production pipeline or general trigger support.
- No frontmatter reference generation, schema documentation, or public quickstart changes.

## 8. Status & sign-off

- **Architecture review (phase 2):** Dedicated review identified per-call DI scope
  and synthetic-response serialization requirements. Both are now explicit
  contracts with targeted tests. Null validation and duplicate-name rejection
  must be enforced rather than assumed from existing helpers.
- **Human sign-off:** larohra, 2026-10-27: "looks good to me", requesting
  implementation and an updated sample scenario after the invocation and response
  boundaries were explained. Approved for implementation.
- **Testing review (phase 4):** Independent review found no blocking contract
  issues. Nullable value-type inputs and defaulted non-framework parameters now
  have explicit coverage in addition to the required signature rejection checks.
