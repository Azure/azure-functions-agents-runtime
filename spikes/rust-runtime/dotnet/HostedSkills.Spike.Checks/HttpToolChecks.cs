using System.Net;
using System.Text;
using System.Text.Json;
using Azure.Core.Serialization;
using HostedSkills.Spike;
using Microsoft.Azure.Functions.Worker;
using Microsoft.Azure.Functions.Worker.Extensions.Abstractions;
using Microsoft.Azure.Functions.Worker.Http;
using Microsoft.Extensions.DependencyInjection;

internal static class HttpToolChecks
{
    internal static async Task RunAsync(Action<string> pass)
    {
        var probe = new InvocationProbe();
        await using var provider = Services(probe);
        var registry = new AgentToolRegistry();
        registry.RegisterFunctions<CheckFunctions>(provider);
        Assert(probe.Constructed == 0 && probe.DependencyConstructed == 0,
            "registration must not construct function or scoped dependency");
        pass("function registration is lazy and discovers both attribute orders");

        var descriptor = registry.Descriptors.Single(item => item.Name == "http_async");
        Assert(registry.Descriptors.Any(item => item.Name == "http_sync"), "reverse attribute order");
        var schema = descriptor.Parameters;
        var properties = schema["properties"]!.AsObject();
        var expected = new Dictionary<string, string>
        {
            ["name"] = "string", ["count"] = "integer", ["largeCount"] = "integer",
            ["amount"] = "number", ["ratio"] = "number", ["weight"] = "number",
            ["enabled"] = "boolean"
        };
        Assert(properties.Count == expected.Count + 2, "no framework parameters in schema");
        foreach (var (name, type) in expected)
            Assert(properties[name]?["type"]?.GetValue<string>() == type, $"scalar schema {name}");
        Assert(properties["note"]?["type"]?.AsArray().Select(node => node!.GetValue<string>())
            .ToHashSet().SetEquals(["string", "null"]) == true, "nullable optional scalar schema");
        Assert(properties["optionalCount"]?["type"]?.AsArray().Select(node => node!.GetValue<string>())
            .ToHashSet().SetEquals(["integer", "null"]) == true, "nullable optional value-type schema");
        var required = schema["required"]!.AsArray()
            .Select(node => node!.GetValue<string>()).ToHashSet();
        Assert(required.SetEquals(expected.Keys.Where(name => name != "note")), "required init fields");
        Assert(schema["additionalProperties"]?.GetValue<bool>() == false, "unknown fields forbidden");
        Assert(descriptor.Description.Contains("status", StringComparison.OrdinalIgnoreCase),
            "tool description explains checking HTTP status");
        pass("camelCase scalar DTO schema excludes framework parameters and documents HTTP status");

        var invalid = new[]
        {
            "{}", "null", "[]", "\"not an object\"",
            Valid.Replace("\"name\":\"Ada\"", "\"name\":null"),
            Valid.Replace("\"name\":\"Ada\"", "\"name\":42"),
            Valid.Replace("\"count\":2", "\"count\":null"),
            Valid.Replace("\"count\":2", "\"count\":2.5"),
            Valid.Replace("\"count\":2", "\"count\":\"2\""),
            Valid.Replace("\"largeCount\":3000000000", "\"largeCount\":false"),
            Valid.Replace("\"amount\":14.25", "\"amount\":null"),
            Valid.Replace("\"amount\":14.25", "\"amount\":\"14.25\""),
            Valid.Replace("\"ratio\":0.5", "\"ratio\":[]"),
            Valid.Replace("\"weight\":1.25", "\"weight\":{}"),
            Valid.Replace("\"enabled\":true", "\"enabled\":null"),
            Valid.Replace("\"enabled\":true", "\"enabled\":\"true\""),
            Valid.Replace("}", ",\"unknown\":\"value\"}"),
            Valid.Replace("}", ",\"note\":123}"),
            Valid.Replace("}", ",\"optionalCount\":\"2\"}"),
            Valid.Replace("}", ",\"name\":\"duplicate\"}"),
            Valid.Replace("\"count\":2", "\"count\":2147483648")
        };
        foreach (var json in invalid)
        {
            var result = await CallAsync(registry, "http_async", json);
            Assert(result.IsError, $"must reject arguments: {json}");
        }
        Assert(probe.Invocations == 0 && probe.Constructed == 0 && probe.DependencyConstructed == 0,
            "invalid arguments must fail before resolving or invoking handler");
        pass($"{invalid.Length} missing/null/type/unknown-field inputs fail before handler and DI resolution");

        var asyncResult = await CallAsync(registry, "http_async", Valid);
        var asyncEnvelope = Envelope(asyncResult, 200);
        using (var body = JsonDocument.Parse(asyncEnvelope.GetProperty("body").GetString()!))
        {
            Assert(body.RootElement.GetProperty("echoName").GetString() == "Ada",
                "configured web serializer is used by WriteAsJsonAsync");
            Assert(body.RootElement.GetProperty("count").GetInt32() == 2, "body survives DTO round trip");
        }
        Assert(probe.Constructed == 1 && probe.DependencyConstructed == 1 && probe.Disposed == 1,
            "first invocation resolves and asynchronously disposes one scope");
        Assert(probe.Streams.All(stream => !stream.CanRead), "successful request and response streams disposed");
        pass("async HTTP handler uses configured worker serializer and preserves JSON response as text");

        var syncResult = await CallAsync(registry, "http_sync",
            Valid.Replace("}", ",\"note\":null,\"optionalCount\":null}"));
        var syncEnvelope = Envelope(syncResult, 409);
        Assert(syncEnvelope.GetProperty("body").GetString() == "conflict: café", "UTF-8 response text");
        Assert(probe.Constructed == 2 && probe.DependencyConstructed == 2 && probe.Disposed == 2,
            "new DI scope for each call");
        Assert(probe.ScopeIds.Distinct().Count() == 2, "no scoped dependency reuse");
        Assert(probe.Streams.All(stream => !stream.CanRead), "sync streams disposed");
        pass("sync HTTP error status is not a tool error; UTF-8 body retained and headers discarded");
        pass("synthetic POST has marker, fixed URL, empty query, no caller identity/headers and default token");
        pass("each invocation constructs and asynchronously disposes an independent DI scope");

        var thrown = await CallAsync(registry, "http_throw", Valid);
        Assert(thrown.IsError && thrown.Content.Contains("intentional handler failure"),
            "handler exception uses tool-error channel and preserves useful message");
        Assert(probe.Disposed == 3 && probe.Streams.All(stream => !stream.CanRead),
            "throwing handler still disposes request and DI scope");
        var asyncThrown = await CallAsync(registry, "http_async_throw", Valid);
        Assert(asyncThrown.IsError && asyncThrown.Content.Contains("intentional asynchronous failure"),
            "awaited handler exception uses tool-error channel");
        Assert(probe.Disposed == 4 && probe.Streams.All(stream => !stream.CanRead),
            "asynchronously throwing handler still disposes request and DI scope");
        pass("sync/async handler exceptions become tool errors and dispose request streams and async scopes");

        var readFailure = await CallAsync(registry, "http_read_failure", Valid);
        Assert(readFailure.IsError && readFailure.Content.Contains("intentional response read failure"),
            "response conversion failure is a tool error");
        Assert(probe.Disposed == 5 && probe.Streams.All(stream => !stream.CanRead),
            "failed conversion disposes response, request and DI scope");
        pass("response-read failure disposes both streams and the async DI scope");

        await using (var missingProvider = Services(new InvocationProbe(), serializer: false))
        {
            var missingRegistry = new AgentToolRegistry();
            missingRegistry.RegisterFunctions<CheckFunctions>(missingProvider);
            var missing = await CallAsync(missingRegistry, "http_async", Valid);
            Assert(missing.IsError && missing.Content.Contains("serializ", StringComparison.OrdinalIgnoreCase),
                "missing configured worker serializer must fail explicitly");
        }
        pass("missing worker serializer fails explicitly instead of using a fallback");

        Reject<MissingDto>(provider);
        Reject<WrongRequest>(provider);
        Reject<WrongReturn>(provider);
        Reject<RequiredToken>(provider);
        Reject<ExtraParameter>(provider);
        Reject<NoHttpTrigger>(provider);
        Reject<NonHttpTrigger>(provider);
        Reject<AdditionalBinding>(provider);
        Reject<ArrayDtoFunction>(provider);
        Reject<NestedDtoFunction>(provider);
        pass("registration rejects missing/unsupported DTOs, wrong signatures, non-HTTP triggers and extra bindings");

        var duplicates = new AgentToolRegistry();
        duplicates.RegisterFromObject(new PlainCheckTool());
        ExpectRegistrationFailure(() => duplicates.RegisterFromObject(new PlainCheckTool()));
        ExpectRegistrationFailure(() => duplicates.RegisterTool(
            duplicates.Descriptors.Single(), _ => Task.FromResult("overwrite")));
        var duplicateFunctions = new AgentToolRegistry();
        duplicateFunctions.RegisterFunctions<CheckFunctions>(provider);
        ExpectRegistrationFailure(() => duplicateFunctions.RegisterFunctions<CheckFunctions>(provider));
        var plain = await CallAsync(duplicates, "plain_check", "{\"value\":\"preserved\"}");
        Assert(!plain.IsError && plain.Content == "preserved/default", "plain default argument preserved");
        pass("duplicate plain/delegate/function tools reject without replacing existing plain tools");

        Envelope(await CallAsync(registry, "http_sync",
            Valid.Replace("}", ",\"optionalCount\":3}")), 409);
        Assert(probe.Disposed == 6 && probe.Streams.All(stream => !stream.CanRead),
            "optional-field call disposes all resources");
        pass("nullable value-type inputs accept omission, null and numbers; optional handler arguments keep defaults");
    }

    private const string Valid =
        """{"name":"Ada","count":2,"largeCount":3000000000,"amount":14.25,"ratio":0.5,"weight":1.25,"enabled":true}""";

    private static ServiceProvider Services(InvocationProbe probe, bool serializer = true)
    {
        var services = new ServiceCollection();
        services.AddSingleton(probe);
        services.AddScoped<ScopedDependency>();
        services.AddScoped<CheckFunctions>();
        services.AddOptions<WorkerOptions>();
        if (serializer)
            services.Configure<WorkerOptions>(options =>
                options.Serializer = new JsonObjectSerializer(JsonSerializerOptions.Web));
        return services.BuildServiceProvider(new ServiceProviderOptions { ValidateScopes = true });
    }

    private static Task<ToolCallResult> CallAsync(AgentToolRegistry registry, string name, string json) =>
        registry.ExecuteAsync(new ToolCallParams(name, "check-call", "check-session",
            JsonSerializer.Deserialize<JsonElement>(json), null));

    private static JsonElement Envelope(ToolCallResult result, int status)
    {
        Assert(!result.IsError, $"unexpected tool error: {result.Content}");
        using var document = JsonDocument.Parse(result.Content);
        var root = document.RootElement;
        Assert(root.EnumerateObject().Count() == 2, "only status_code/body; no forwarded response headers");
        Assert(root.GetProperty("status_code").GetInt32() == status, "HTTP status envelope");
        Assert(root.GetProperty("body").ValueKind == JsonValueKind.String, "body remains a string");
        return root.Clone();
    }

    private static void Reject<T>(IServiceProvider provider) where T : class =>
        ExpectRegistrationFailure(() => new AgentToolRegistry().RegisterFunctions<T>(provider));

    private static void ExpectRegistrationFailure(Action action)
    {
        try { action(); }
        catch (Exception exception) when (exception is ArgumentException or InvalidOperationException
            or NotSupportedException)
        {
            Assert(!string.IsNullOrWhiteSpace(exception.Message), "actionable registration error");
            return;
        }
        throw new InvalidOperationException("Expected an actionable registration failure");
    }

    internal static void Assert(bool condition, string message)
    {
        if (!condition) throw new InvalidOperationException(message);
    }
}

public sealed record ScalarInput
{
    public required string Name { get; init; }
    public required int Count { get; init; }
    public required long LargeCount { get; init; }
    public required decimal Amount { get; init; }
    public required double Ratio { get; init; }
    public required float Weight { get; init; }
    public required bool Enabled { get; init; }
    public string? Note { get; init; }
    public int? OptionalCount { get; init; }
}

public sealed class InvocationProbe
{
    public int Constructed;
    public int DependencyConstructed;
    public int Disposed;
    public int Invocations;
    public List<Guid> ScopeIds { get; } = [];
    public List<Stream> Streams { get; } = [];
}

public sealed class ScopedDependency(InvocationProbe probe) : IAsyncDisposable
{
    public Guid Id { get; } = NewId(probe);
    private static Guid NewId(InvocationProbe probe)
    {
        probe.DependencyConstructed++;
        return Guid.NewGuid();
    }
    public async ValueTask DisposeAsync()
    {
        await Task.Yield();
        probe.Disposed++;
    }
}

public sealed class CheckFunctions
{
    private readonly InvocationProbe _probe;
    private readonly ScopedDependency _dependency;

    public CheckFunctions(InvocationProbe probe, ScopedDependency dependency)
    {
        _probe = probe;
        _dependency = dependency;
        probe.Constructed++;
    }

    [Function("CheckAsync")]
    [AgentTool("http_async", "Async HTTP check.", InputType = typeof(ScalarInput))]
    public async Task<HttpResponseData> Async(
        [HttpTrigger(AuthorizationLevel.Anonymous, "post")] HttpRequestData request,
        CancellationToken cancellationToken = default)
    {
        Inspect(request, cancellationToken);
        var input = await JsonSerializer.DeserializeAsync<ScalarInput>(request.Body, JsonSerializerOptions.Web);
        HttpToolChecks.Assert(input is { Name: "Ada", Count: 2, LargeCount: 3000000000, Amount: 14.25m,
            Ratio: 0.5, Weight: 1.25f, Enabled: true, Note: null }, "validated body matches scalar DTO");
        var response = request.CreateResponse(HttpStatusCode.OK);
        _probe.Streams.Add(response.Body);
        response.Headers.Add("x-private-response", "must-not-forward");
        await response.WriteAsJsonAsync(new { EchoName = input!.Name, input.Count }, cancellationToken);
        return response;
    }

    [AgentTool("http_sync", "Sync HTTP check.", InputType = typeof(ScalarInput))]
    [Function("CheckSync")]
    public HttpResponseData Sync(
        [HttpTrigger(AuthorizationLevel.Anonymous, "post")] HttpRequestData request,
        string tag = "default")
    {
        HttpToolChecks.Assert(tag == "default", "optional non-framework parameter retains default");
        Inspect(request, default);
        var response = request.CreateResponse(HttpStatusCode.Conflict);
        _probe.Streams.Add(response.Body);
        response.Headers.Add("x-private-response", "must-not-forward");
        response.Body.Write(Encoding.UTF8.GetBytes("conflict: café"));
        return response;
    }

    [Function("CheckThrows")]
    [AgentTool("http_throw", "Throwing HTTP check.", InputType = typeof(ScalarInput))]
    public HttpResponseData Throw(
        [HttpTrigger(AuthorizationLevel.Anonymous, "post")] HttpRequestData request)
    {
        Inspect(request, default);
        throw new InvalidOperationException("intentional handler failure");
    }

    [Function("CheckAsyncThrows")]
    [AgentTool("http_async_throw", "Async throwing HTTP check.", InputType = typeof(ScalarInput))]
    public async Task<HttpResponseData> AsyncThrow(
        [HttpTrigger(AuthorizationLevel.Anonymous, "post")] HttpRequestData request)
    {
        Inspect(request, default);
        await Task.Yield();
        throw new InvalidOperationException("intentional asynchronous failure");
    }

    [Function("CheckReadFailure")]
    [AgentTool("http_read_failure", "Broken response check.", InputType = typeof(ScalarInput))]
    public HttpResponseData ReadFailure(
        [HttpTrigger(AuthorizationLevel.Anonymous, "post")] HttpRequestData request)
    {
        Inspect(request, default);
        var response = request.CreateResponse(HttpStatusCode.OK);
        response.Body.Dispose();
        response.Body = new FailedReadStream();
        _probe.Streams.Add(response.Body);
        return response;
    }

    private void Inspect(HttpRequestData request, CancellationToken token)
    {
        _probe.Invocations++;
        _probe.ScopeIds.Add(_dependency.Id);
        _probe.Streams.Add(request.Body);
        HttpToolChecks.Assert(token == default, "optional cancellation token retains default");
        HttpToolChecks.Assert(request.Method == "POST" && request.Url.AbsoluteUri == "http://localhost/api/tool",
            "fixed synthetic POST URL");
        HttpToolChecks.Assert(request.Url.Query == "" && request.Query.Count == 0, "no synthetic query");
        HttpToolChecks.Assert(!request.Identities.Any(identity => identity.IsAuthenticated || identity.Claims.Any()),
            "no synthetic authenticated identity or caller claims");
        HttpToolChecks.Assert(!request.Identities.Any(), "no synthetic identities");
        var headers = request.Headers.ToList();
        HttpToolChecks.Assert(headers.Count == 1 &&
            request.Headers.GetValues("Content-Type").Single().StartsWith("application/json"),
            "only generated JSON content-type header, no caller headers");
        HttpToolChecks.Assert(request.FunctionContext.Items.TryGetValue(nameof(AgentToolAttribute), out var marker)
            && marker is true, "synthetic invocation marker");
        HttpToolChecks.Assert(ReferenceEquals(request.FunctionContext.InstanceServices
            .GetRequiredService<ScopedDependency>(), _dependency), "context exposes invocation DI scope");
    }
}

internal sealed class FailedReadStream : MemoryStream
{
    public override int Read(byte[] buffer, int offset, int count) =>
        throw new IOException("intentional response read failure");
    public override int Read(Span<byte> buffer) =>
        throw new IOException("intentional response read failure");
    public override ValueTask<int> ReadAsync(Memory<byte> buffer, CancellationToken cancellationToken = default) =>
        ValueTask.FromException<int>(new IOException("intentional response read failure"));
    public override Task<int> ReadAsync(byte[] buffer, int offset, int count, CancellationToken cancellationToken) =>
        Task.FromException<int>(new IOException("intentional response read failure"));
}

public sealed class PlainCheckTool
{
    [AgentTool("plain_check", "Plain compatibility check.")]
    public string Run(string value, string suffix = "default") => $"{value}/{suffix}";
}

public sealed class MissingDto
{
    [Function("BadMissingDto"), AgentTool("bad", "Missing DTO")]
    public HttpResponseData Run([HttpTrigger(AuthorizationLevel.Anonymous)] HttpRequestData request) =>
        throw new NotImplementedException();
}

public sealed class WrongRequest
{
    [Function("BadRequest"), AgentTool("bad", "Wrong request", InputType = typeof(ScalarInput))]
    public HttpResponseData Run([HttpTrigger(AuthorizationLevel.Anonymous)] string request) =>
        throw new NotImplementedException();
}

public sealed class WrongReturn
{
    [Function("BadReturn"), AgentTool("bad", "Wrong return", InputType = typeof(ScalarInput))]
    public Task<string> Run([HttpTrigger(AuthorizationLevel.Anonymous)] HttpRequestData request) =>
        throw new NotImplementedException();
}

public sealed class RequiredToken
{
    [Function("BadToken"), AgentTool("bad", "Required token", InputType = typeof(ScalarInput))]
    public HttpResponseData Run([HttpTrigger(AuthorizationLevel.Anonymous)] HttpRequestData request,
        CancellationToken cancellationToken) => throw new NotImplementedException();
}

public sealed class ExtraParameter
{
    [Function("BadExtra"), AgentTool("bad", "Extra parameter", InputType = typeof(ScalarInput))]
    public HttpResponseData Run([HttpTrigger(AuthorizationLevel.Anonymous)] HttpRequestData request,
        string other) => throw new NotImplementedException();
}

public sealed class NoHttpTrigger
{
    [Function("BadNoTrigger"), AgentTool("bad", "No trigger", InputType = typeof(ScalarInput))]
    public HttpResponseData Run(HttpRequestData request) => throw new NotImplementedException();
}

[AttributeUsage(AttributeTargets.Parameter)]
public sealed class CheckTriggerAttribute : TriggerBindingAttribute;

[AttributeUsage(AttributeTargets.Parameter)]
public sealed class CheckInputAttribute : BindingAttribute;

public sealed class NonHttpTrigger
{
    [Function("BadTrigger"), AgentTool("bad", "Non HTTP", InputType = typeof(ScalarInput))]
    public HttpResponseData Run([CheckTrigger] string input) => throw new NotImplementedException();
}

public sealed class AdditionalBinding
{
    [Function("BadBinding"), AgentTool("bad", "Extra binding", InputType = typeof(ScalarInput))]
    public HttpResponseData Run([HttpTrigger(AuthorizationLevel.Anonymous)] HttpRequestData request,
        [CheckInput] string other = "") => throw new NotImplementedException();
}

public sealed record ArrayInput
{
    public required string[] Values { get; init; }
}

public sealed record NestedInput
{
    public required ScalarInput Value { get; init; }
}

public sealed class ArrayDtoFunction
{
    [Function("BadArray"), AgentTool("bad", "Array DTO", InputType = typeof(ArrayInput))]
    public HttpResponseData Run([HttpTrigger(AuthorizationLevel.Anonymous)] HttpRequestData request) =>
        throw new NotImplementedException();
}

public sealed class NestedDtoFunction
{
    [Function("BadNested"), AgentTool("bad", "Nested DTO", InputType = typeof(NestedInput))]
    public HttpResponseData Run([HttpTrigger(AuthorizationLevel.Anonymous)] HttpRequestData request) =>
        throw new NotImplementedException();
}
