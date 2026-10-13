#nullable enable
using System;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

internal static class Program
{
    private const long MaxSafeInteger = 9_007_199_254_740_991;

    private static readonly SemaphoreSlim WriterLock = new(1, 1);
    private static TextWriter ProtocolWriter = Console.Out;
    private static ToolContract Contract = null!;
    private static MethodInfo Method = null!;

    public static async Task Main(string[] args)
    {
        if (args.Length != 2)
        {
            throw new InvalidOperationException("Expected encoded contract and customer assembly.");
        }

        ProtocolWriter = Console.Out;
        Console.SetOut(Console.Error);
        Contract = JsonSerializer.Deserialize<ToolContract>(
            Decode(args[0]),
            new JsonSerializerOptions { PropertyNameCaseInsensitive = true })
            ?? throw new InvalidOperationException("Invalid tool contract.");
        Method = ResolveMethod(Assembly.LoadFrom(Path.GetFullPath(args[1])), Contract);

        while (await Console.In.ReadLineAsync() is { } line)
        {
            if (Encoding.UTF8.GetByteCount(line) > 1024 * 1024)
            {
                await WriteError(null, -32600, "Request exceeds the 1 MiB limit.");
                continue;
            }

            JsonDocument request;
            try
            {
                request = JsonDocument.Parse(line);
            }
            catch (JsonException)
            {
                await WriteError(null, -32700, "Invalid JSON.");
                continue;
            }

            _ = HandleRequest(request);
        }
    }

    private static async Task HandleRequest(JsonDocument request)
    {
        using (request)
        {
            JsonElement root = request.RootElement;
            string? method = root.TryGetProperty("method", out JsonElement methodElement)
                ? methodElement.GetString()
                : null;
            JsonElement? id = root.TryGetProperty("id", out JsonElement idElement)
                ? idElement.Clone()
                : null;

            if (method == "notifications/cancelled")
            {
                Environment.Exit(0);
            }
            if (method == "notifications/initialized")
            {
                return;
            }
            if (method == "initialize")
            {
                await WriteResult(id, new
                {
                    protocolVersion = "2025-11-25",
                    capabilities = new { tools = new { } },
                    serverInfo = new { name = "azure-functions-agents-csharp-tool", version = "1" },
                });
                return;
            }
            if (method == "ping")
            {
                await WriteResult(id, new { });
                return;
            }
            if (method == "tools/list")
            {
                await WriteResult(id, new
                {
                    tools = new[]
                    {
                        new
                        {
                            name = Contract.Name,
                            description = Contract.Description,
                            inputSchema = Contract.InputSchema,
                        },
                    },
                });
                return;
            }
            if (method == "tools/call")
            {
                await CallTool(id, root);
                return;
            }
            if (id is not null)
            {
                await WriteError(id, -32601, $"Unsupported method: {method}");
            }
        }
    }

    private static async Task CallTool(JsonElement? id, JsonElement root)
    {
        try
        {
            JsonElement parameters = root.GetProperty("params");
            if (parameters.GetProperty("name").GetString() != Contract.Name)
            {
                throw new InvalidOperationException("Unknown tool name.");
            }
            JsonElement arguments = parameters.TryGetProperty("arguments", out JsonElement value)
                ? value
                : JsonDocument.Parse("{}").RootElement.Clone();
            object?[] values = ConvertArguments(arguments, Contract.Parameters);
            object? result = await Task.Run(() => Method.Invoke(null, values));
            result = await AwaitResult(result, Method.ReturnType);
            object[] content = result is null
                ? Array.Empty<object>()
                : new object[] { new { type = "text", text = SerializeResult(result) } };
            await WriteResult(id, new
            {
                content,
                isError = false,
            });
        }
        catch (Exception exception)
        {
            Exception actual = exception is TargetInvocationException { InnerException: not null }
                ? exception.InnerException
                : exception;
            await WriteResult(id, new
            {
                content = new[] { new { type = "text", text = actual.Message } },
                isError = true,
            });
        }
    }

    private static MethodInfo ResolveMethod(Assembly assembly, ToolContract contract)
    {
        int separator = contract.Entrypoint.LastIndexOf('.');
        string typeName = contract.Entrypoint[..separator];
        string methodName = contract.Entrypoint[(separator + 1)..];
        Type type = assembly.GetType(typeName, throwOnError: true)
            ?? throw new InvalidOperationException("Entrypoint type was not found.");
        if (!type.IsPublic || type.IsNested || type.IsGenericType)
        {
            throw new InvalidOperationException("Entrypoint type must be public and non-generic.");
        }
        MethodInfo[] matches = type.GetMethods(
                BindingFlags.Public | BindingFlags.Static | BindingFlags.DeclaredOnly)
            .Where(candidate => candidate.Name == methodName && !candidate.IsGenericMethod)
            .ToArray();
        if (matches.Length != 1)
        {
            throw new InvalidOperationException("Entrypoint must resolve to exactly one public static method.");
        }
        MethodInfo method = matches[0];
        ParameterInfo[] parameters = method.GetParameters();
        if (parameters.Length != contract.Parameters.Length)
        {
            throw new InvalidOperationException("Entrypoint parameter count does not match tool.md.");
        }
        Type[] expected = contract.Parameters.Select(parameter => parameter.Type switch
        {
            "string" => typeof(string),
            "integer" => typeof(long),
            "number" => typeof(double),
            "boolean" => typeof(bool),
            _ => throw new InvalidOperationException("Unsupported portable parameter type."),
        }).ToArray();
        if (parameters.Where((parameter, index) => parameter.ParameterType != expected[index]).Any())
        {
            throw new InvalidOperationException("Entrypoint parameter types do not match tool.md.");
        }
        if (method.ReturnType == typeof(void) && method.GetCustomAttribute<System.Runtime.CompilerServices.AsyncStateMachineAttribute>() is not null)
        {
            throw new InvalidOperationException("async void entrypoints are unsupported.");
        }
        return method;
    }

    private static object?[] ConvertArguments(JsonElement arguments, ToolParameter[] parameters)
    {
        if (arguments.ValueKind != JsonValueKind.Object)
        {
            throw new InvalidOperationException("Tool arguments must be an object.");
        }
        var known = parameters.Select(parameter => parameter.Name).ToHashSet(StringComparer.Ordinal);
        foreach (JsonProperty property in arguments.EnumerateObject())
        {
            if (!known.Contains(property.Name))
            {
                throw new InvalidOperationException($"Unknown argument: {property.Name}");
            }
        }
        return parameters.Select(parameter =>
        {
            if (!arguments.TryGetProperty(parameter.Name, out JsonElement value))
            {
                throw new InvalidOperationException($"Missing required argument: {parameter.Name}");
            }
            return parameter.Type switch
            {
                "string" when value.ValueKind == JsonValueKind.String => (object?)value.GetString(),
                "integer" when value.TryGetInt64(out long integer)
                    && integer is >= -MaxSafeInteger and <= MaxSafeInteger => integer,
                "number" when value.TryGetDouble(out double number) && double.IsFinite(number)
                    => number,
                "boolean" when value.ValueKind is JsonValueKind.True or JsonValueKind.False
                    => value.GetBoolean(),
                _ => throw new InvalidOperationException($"Argument {parameter.Name} has the wrong type."),
            };
        }).ToArray();
    }

    private static async Task<object?> AwaitResult(object? result, Type returnType)
    {
        if (returnType == typeof(void))
        {
            return null;
        }
        if (result is Task task)
        {
            await task.ConfigureAwait(false);
            return returnType.IsGenericType ? returnType.GetProperty("Result")!.GetValue(task) : null;
        }
        if (returnType == typeof(ValueTask))
        {
            await ((ValueTask)result!).ConfigureAwait(false);
            return null;
        }
        if (returnType.IsGenericType && returnType.GetGenericTypeDefinition() == typeof(ValueTask<>))
        {
            dynamic valueTask = result!;
            return await valueTask;
        }
        return result;
    }

    private static string SerializeResult(object? value) => value switch
    {
        null => "",
        string text => text,
        bool boolean => boolean ? "true" : "false",
        float single when !float.IsFinite(single)
            => throw new InvalidOperationException("Result must be finite."),
        double number when !double.IsFinite(number)
            => throw new InvalidOperationException("Result must be finite."),
        byte or sbyte or short or ushort or int or uint or long or ulong or float or double or decimal
            => Convert.ToString(value, System.Globalization.CultureInfo.InvariantCulture)!,
        _ => JsonSerializer.Serialize(value, new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.CamelCase }),
    };

    private static string Decode(string encoded)
    {
        string padded = encoded.PadRight(encoded.Length + (4 - encoded.Length % 4) % 4, '=')
            .Replace('-', '+')
            .Replace('_', '/');
        return Encoding.UTF8.GetString(Convert.FromBase64String(padded));
    }

    private static async Task WriteResult(JsonElement? id, object result)
    {
        await Write(new { jsonrpc = "2.0", id, result });
    }

    private static async Task WriteError(JsonElement? id, int code, string message)
    {
        await Write(new { jsonrpc = "2.0", id, error = new { code, message } });
    }

    private static async Task Write(object payload)
    {
        await WriterLock.WaitAsync();
        try
        {
            await ProtocolWriter.WriteLineAsync(JsonSerializer.Serialize(payload));
            await ProtocolWriter.FlushAsync();
        }
        finally
        {
            WriterLock.Release();
        }
    }
}

internal sealed class ToolContract
{
    public required string Name { get; init; }
    public required string Description { get; init; }
    public required string Entrypoint { get; init; }
    public required ToolParameter[] Parameters { get; init; }
    public required JsonElement InputSchema { get; init; }
}

internal sealed class ToolParameter
{
    public required string Name { get; init; }
    public required string Type { get; init; }
    public required string Description { get; init; }
}
