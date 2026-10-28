using System.Reflection;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.Json.Serialization;
using Microsoft.Azure.Functions.Worker;
using Microsoft.Extensions.DependencyInjection;

namespace HostedSkills.Spike;

/// <summary>
/// Spike-only tool registration marker, not an Azure Functions binding.
/// </summary>
[AttributeUsage(AttributeTargets.Method)]
public sealed class AgentToolAttribute(string name, string description) : Attribute
{
    public string Name { get; } = name;
    public string Description { get; } = description;
    public Type? InputType { get; set; }
}

public sealed record TriggerToolMetadata(
    [property: JsonPropertyName("binding_type")] string BindingType,
    [property: JsonPropertyName("function_name")] string? FunctionName = null,
    [property: JsonPropertyName("http_method")] string? HttpMethod = null,
    [property: JsonPropertyName("route")] string? Route = null);

public sealed record WorkerToolDescriptor(
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("description")] string Description,
    [property: JsonPropertyName("parameters")] JsonNode Parameters,
    [property: JsonPropertyName("trigger_metadata")] TriggerToolMetadata? TriggerMetadata = null);

public sealed record ToolCallParams(
    [property: JsonPropertyName("tool_name")] string ToolName,
    [property: JsonPropertyName("call_id")] string CallId,
    [property: JsonPropertyName("session_id")] string SessionId,
    [property: JsonPropertyName("arguments")] JsonElement Arguments,
    [property: JsonPropertyName("trigger_metadata")] TriggerToolMetadata? TriggerMetadata);

public sealed record ToolCallResult(
    [property: JsonPropertyName("content")] string Content,
    [property: JsonPropertyName("is_error")] bool IsError = false);

/// <summary>
/// Registry of plain C# tools and direct HTTP function adapters invoked over stdio.
/// </summary>
public sealed class AgentToolRegistry
{
    private readonly Dictionary<string, (WorkerToolDescriptor Descriptor, Func<ToolCallParams, Task<ToolCallResult>> Handler)> _tools =
        new(StringComparer.Ordinal);

    public IReadOnlyList<WorkerToolDescriptor> Descriptors =>
        _tools.Values.Select(v => v.Descriptor).ToList();

    /// <summary>
    /// Register a delegate tool (`app.Tool(...)` style) or an in-app trigger function.
    /// </summary>
    public void RegisterTool(
        WorkerToolDescriptor descriptor,
        Func<ToolCallParams, Task<string>> handler)
    {
        if (!_tools.TryAdd(descriptor.Name, (descriptor, async call =>
        {
            try
            {
                var output = await handler(call).ConfigureAwait(false);
                return new ToolCallResult(output, IsError: false);
            }
            catch (Exception ex)
            {
                var error = ex is TargetInvocationException { InnerException: not null } invocation
                    ? invocation.InnerException!.Message
                    : ex.Message;
                var errJson = JsonSerializer.Serialize(new { error });
                return new ToolCallResult(errJson, IsError: true);
            }
        }
        )))
            throw new ArgumentException($"Duplicate tool name '{descriptor.Name}'.");
    }

    /// <summary>
    /// Discover plain `[AgentTool]` methods on an existing instance.
    /// </summary>
    public void RegisterFromObject(object target)
    {
        var type = target.GetType();
        foreach (var method in type.GetMethods(BindingFlags.Public | BindingFlags.Instance | BindingFlags.Static))
        {
            var toolAttr = method.GetCustomAttribute<AgentToolAttribute>();
            if (toolAttr is null)
            {
                continue;
            }

            if (method.GetCustomAttribute<FunctionAttribute>() is not null)
                throw new ArgumentException(
                    $"Function tool '{method.Name}' must use RegisterFunctions<T>(provider) for per-call DI scopes.");
            var (descriptor, handler) = BuildPlainTool(method, toolAttr);
            RegisterTool(descriptor, call => handler(call, () => target));
        }
    }

    /// <summary>
    /// Inspect tools without constructing their function class. Each invocation gets its
    /// own asynchronous DI scope, and only then resolves the target instance.
    /// </summary>
    public void RegisterFunctions<T>(IServiceProvider provider) where T : class
    {
        ArgumentNullException.ThrowIfNull(provider);
        foreach (var method in typeof(T).GetMethods(BindingFlags.Public | BindingFlags.Instance | BindingFlags.Static))
        {
            var tool = method.GetCustomAttribute<AgentToolAttribute>();
            if (tool is null)
                continue;
            var function = method.GetCustomAttribute<FunctionAttribute>();
            if (function is null)
            {
                var (plainDescriptor, handler) = BuildPlainTool(method, tool);
                RegisterTool(plainDescriptor, call => InvokeInScopeAsync<T>(provider,
                    (_, resolve) => handler(call, resolve)));
                continue;
            }
            var adapter = new FunctionHttpToolAdapter(method, tool.InputType);
            var trigger = method.GetParameters()
                .Select(p => p.GetCustomAttribute<HttpTriggerAttribute>())
                .Single(t => t is not null)!;
            var descriptor = new WorkerToolDescriptor(
                tool.Name,
                $"{tool.Description} Returns {{status_code, body:text}}; check status_code for HTTP success or failure before interpreting body. The body is UTF-8 response text, including when it contains JSON.",
                adapter.Schema,
                new TriggerToolMetadata("httpTrigger", function.Name, "POST", trigger.Route));
            RegisterTool(descriptor, call => InvokeInScopeAsync<T>(provider,
                (services, resolve) => adapter.InvokeAsync(call, services, resolve)));
        }
    }

    private static async Task<string> InvokeInScopeAsync<T>(
        IServiceProvider provider,
        Func<IServiceProvider, Func<object?>, Task<string>> handler) where T : class
    {
        await using var scope = provider.CreateAsyncScope();
        T? target = null;
        var ownsTarget = false;
        object Resolve()
        {
            if (target is not null)
                return target;
            target = scope.ServiceProvider.GetService<T>();
            if (target is null)
            {
                target = ActivatorUtilities.CreateInstance<T>(scope.ServiceProvider);
                ownsTarget = true;
            }
            return target;
        }
        try
        {
            return await handler(scope.ServiceProvider, Resolve).ConfigureAwait(false);
        }
        finally
        {
            if (ownsTarget && target is IAsyncDisposable asyncDisposable)
                await asyncDisposable.DisposeAsync().ConfigureAwait(false);
            else if (ownsTarget && target is IDisposable disposable)
                disposable.Dispose();
        }
    }

    private static (WorkerToolDescriptor Descriptor, Func<ToolCallParams, Func<object?>, Task<string>> Handler) BuildPlainTool(
        MethodInfo method,
        AgentToolAttribute toolAttr)
    {
        if (toolAttr.InputType is not null)
            throw new ArgumentException($"Plain tool '{method.Name}' cannot specify InputType; it is only supported on dual-attribute HTTP function tools.");
        var parameters = method.GetParameters();
        var props = new JsonObject();
        var required = new JsonArray();
        foreach (var p in parameters)
        {
            var pName = p.Name ?? "arg";
            props[pName] = new JsonObject { ["type"] = MapJsonSchemaType(p.ParameterType) };
            if (!p.HasDefaultValue) required.Add(pName);
        }
        var schema = new JsonObject
        {
            ["type"] = "object",
            ["properties"] = props,
            ["required"] = required
        };
        var descriptor = new WorkerToolDescriptor(
            Name: toolAttr.Name,
            Description: toolAttr.Description,
            Parameters: schema,
            TriggerMetadata: new TriggerToolMetadata(
                BindingType: "plain",
                FunctionName: method.Name));

        async Task<string> Invoke(ToolCallParams call, Func<object?> resolve)
        {
            var args = new object?[parameters.Length];
            for (var i = 0; i < parameters.Length; i++)
            {
                var p = parameters[i];
                if (call.Arguments.ValueKind == JsonValueKind.Object &&
                    call.Arguments.TryGetProperty(p.Name!, out var propVal))
                {
                    args[i] = JsonSerializer.Deserialize(propVal.GetRawText(), p.ParameterType);
                }
                else if (p.HasDefaultValue)
                {
                    args[i] = p.DefaultValue;
                }
                else
                {
                    throw new ArgumentException($"Missing required tool argument '{p.Name}'.");
                }
            }
            var invokeTarget = method.IsStatic ? null : resolve();
            var rawResult = method.Invoke(invokeTarget, args);
            return await UnwrapResultAsync(rawResult).ConfigureAwait(false);
        }
        return (descriptor, Invoke);
    }

    public async Task<ToolCallResult> ExecuteAsync(ToolCallParams call)
    {
        if (!_tools.TryGetValue(call.ToolName, out var entry))
        {
            return new ToolCallResult(
                JsonSerializer.Serialize(new { error = $"Unknown tool '{call.ToolName}'" }),
                IsError: true);
        }
        return await entry.Handler(call).ConfigureAwait(false);
    }

    private static string MapJsonSchemaType(Type t)
    {
        if (t == typeof(int) || t == typeof(long)) return "integer";
        if (t == typeof(double) || t == typeof(decimal) || t == typeof(float)) return "number";
        if (t == typeof(bool)) return "boolean";
        return "string";
    }

    private static async Task<string> UnwrapResultAsync(object? result)
    {
        if (result is null) return "null";
        if (result is string s) return s;
        if (result is Task<string> ts) return await ts.ConfigureAwait(false);
        if (result is Task t)
        {
            await t.ConfigureAwait(false);
            var prop = t.GetType().GetProperty("Result");
            var val = prop?.GetValue(t);
            return val is string str ? str : JsonSerializer.Serialize(val);
        }
        return JsonSerializer.Serialize(result);
    }
}
