using System.Net;
using System.Reflection;
using System.Runtime.ExceptionServices;
using System.Security.Claims;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Azure.Functions.Worker;
using Microsoft.Azure.Functions.Worker.Extensions.Abstractions;
using Microsoft.Azure.Functions.Worker.Http;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;

namespace HostedSkills.Spike;

internal sealed class FunctionHttpToolAdapter
{
    private readonly MethodInfo _method;
    private readonly int _requestIndex;
    private readonly object?[] _defaults;
    private readonly ScalarDtoToolAdapter _input;

    public FunctionHttpToolAdapter(MethodInfo method, Type? inputType)
    {
        _method = method;
        var label = $"{method.DeclaringType?.Name}.{method.Name}";
        if (method.ContainsGenericParameters ||
            (method.ReturnType != typeof(HttpResponseData) &&
             method.ReturnType != typeof(Task<HttpResponseData>)))
            throw new ArgumentException(
                $"Function tool '{label}' must be non-generic and return HttpResponseData or Task<HttpResponseData>.");

        var parameters = method.GetParameters();
        var triggers = parameters.Where(p => p.GetCustomAttribute<HttpTriggerAttribute>() is not null).ToArray();
        if (triggers.Length != 1 || triggers[0].ParameterType != typeof(HttpRequestData))
            throw new ArgumentException(
                $"Function tool '{label}' requires exactly one [HttpTrigger] HttpRequestData parameter; non-HTTP triggers are unsupported.");
        _requestIndex = triggers[0].Position;
        _defaults = new object?[parameters.Length];
        foreach (var parameter in parameters)
        {
            if (parameter.GetCustomAttributes().Any(attribute =>
                    IsBinding(attribute) &&
                    attribute is not HttpTriggerAttribute))
                throw new ArgumentException(
                    $"Function tool '{label}' has unsupported binding parameter '{parameter.Name}'.");
            if (parameter.Position == _requestIndex)
                continue;
            if (parameter.ParameterType.IsByRef ||
                parameter.ParameterType == typeof(HttpRequestData) ||
                !parameter.HasDefaultValue)
                throw new ArgumentException(
                    $"Function tool '{label}' has unsupported required parameter '{parameter.Name}'; extra parameters must be optional and cannot be bindings.");
            _defaults[parameter.Position] =
                parameter.ParameterType == typeof(CancellationToken) && parameter.DefaultValue is null
                    ? CancellationToken.None
                    : parameter.DefaultValue;
        }
        if (method.GetCustomAttributes().Any(IsBinding) ||
            method.ReturnParameter.GetCustomAttributes().Any(IsBinding))
            throw new ArgumentException($"Function tool '{label}' cannot use an output binding.");
        _input = new ScalarDtoToolAdapter(inputType ??
            throw new ArgumentException($"Function tool '{label}' requires AgentTool.InputType with a scalar input DTO."));
    }

    public JsonNode Schema => _input.Schema;

    private static bool IsBinding(object attribute) =>
        attribute is BindingAttribute || attribute is TriggerBindingAttribute;

    public async Task<string> InvokeAsync(
        ToolCallParams call, IServiceProvider services, Func<object?> resolveTarget)
    {
        _input.Validate(call.Arguments);
        if (services.GetService<IOptions<WorkerOptions>>()?.Value.Serializer is null)
            throw new InvalidOperationException(
                "Direct function tool invocation requires configured IOptions<WorkerOptions>.Serializer in the call's DI scope.");

        var context = new ToolFunctionContext(services);
        await using var request = new ToolHttpRequest(context, call.Arguments.GetRawText());
        var args = (object?[])_defaults.Clone();
        args[_requestIndex] = request;
        HttpResponseData? response = null;
        try
        {
            object? result;
            try
            {
                result = _method.Invoke(_method.IsStatic ? null : resolveTarget(), args);
            }
            catch (TargetInvocationException ex) when (ex.InnerException is not null)
            {
                ExceptionDispatchInfo.Capture(ex.InnerException).Throw();
                throw;
            }
            response = result is Task<HttpResponseData> task
                ? await task.ConfigureAwait(false)
                : result as HttpResponseData;
            if (response is null)
                throw new InvalidOperationException($"Function tool '{_method.Name}' returned a null HTTP response.");
            request.Track(response.Body);
            if (!response.Body.CanSeek)
                throw new NotSupportedException("Direct function tool responses must have a seekable body stream.");
            response.Body.Position = 0;
            using var reader = new StreamReader(response.Body, Encoding.UTF8, true, leaveOpen: true);
            var body = await reader.ReadToEndAsync().ConfigureAwait(false);
            return JsonSerializer.Serialize(new { status_code = (int)response.StatusCode, body });
        }
        finally
        {
            if (response is not null)
                request.Track(response.Body);
        }
    }

    // Keep the worker's request/response abstractions public-SDK-only, not its gRPC internals.
    private sealed class ToolFunctionContext(IServiceProvider services) : FunctionContext
    {
        private static NotSupportedException Unsupported() =>
            new("This is a synthetic direct tool call, not a Functions invocation; invocation, binding, trace and feature services are unavailable.");

        public override string InvocationId => throw Unsupported();
        public override string FunctionId => throw Unsupported();
        public override TraceContext TraceContext => throw Unsupported();
        public override BindingContext BindingContext => throw Unsupported();
        public override RetryContext RetryContext => throw Unsupported();
        public override IServiceProvider InstanceServices { get; set; } = services;
        public override FunctionDefinition FunctionDefinition => throw Unsupported();
        public override IDictionary<object, object> Items { get; set; } =
            new Dictionary<object, object> { [nameof(AgentToolAttribute)] = true };
        public override IInvocationFeatures Features => throw Unsupported();
    }

    private sealed class ToolHttpRequest : HttpRequestData, IAsyncDisposable
    {
        private readonly HashSet<Stream> _streams = new(ReferenceEqualityComparer.Instance);

        public ToolHttpRequest(FunctionContext context, string body) : base(context)
        {
            Body = new MemoryStream(Encoding.UTF8.GetBytes(body), writable: false);
            Track(Body);
            Headers.Add("Content-Type", "application/json");
        }

        public override Stream Body { get; }
        public override HttpHeadersCollection Headers { get; } = new();
        public override IReadOnlyCollection<IHttpCookie> Cookies => Array.Empty<IHttpCookie>();
        public override Uri Url { get; } = new("http://localhost/api/tool");
        public override IEnumerable<ClaimsIdentity> Identities => Array.Empty<ClaimsIdentity>();
        public override string Method => "POST";
        public override HttpResponseData CreateResponse() => new ToolHttpResponse(FunctionContext, this);
        public void Track(Stream stream) => _streams.Add(stream);

        public async ValueTask DisposeAsync()
        {
            List<Exception>? errors = null;
            foreach (var stream in _streams)
            {
                try { await stream.DisposeAsync().ConfigureAwait(false); }
                catch (Exception ex) { (errors ??= []).Add(ex); }
            }
            if (errors is not null)
                throw new AggregateException("Disposing direct function tool streams failed.", errors);
        }
    }

    private sealed class ToolHttpResponse : HttpResponseData
    {
        private readonly ToolHttpRequest _owner;
        private Stream _body;

        public ToolHttpResponse(FunctionContext context, ToolHttpRequest owner) : base(context)
        {
            _owner = owner;
            _body = new MemoryStream();
            owner.Track(_body);
        }

        public override HttpStatusCode StatusCode { get; set; } = HttpStatusCode.OK;
        public override HttpHeadersCollection Headers { get; set; } = new();
        public override Stream Body
        {
            get => _body;
            set
            {
                ArgumentNullException.ThrowIfNull(value);
                _owner.Track(value);
                _body = value;
            }
        }
        public override HttpCookies Cookies =>
            throw new NotSupportedException("Response cookie orchestration is unsupported for direct function tool calls.");
    }
}
