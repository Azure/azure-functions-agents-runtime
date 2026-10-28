using System.Reflection;
using System.Runtime.CompilerServices;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.Json.Serialization;

namespace HostedSkills.Spike;

internal sealed class ScalarDtoToolAdapter
{
    private sealed record Field(string Name, Type Type, bool Required, bool Nullable);
    private readonly Dictionary<string, Field> _fields = new(StringComparer.Ordinal);
    private static readonly JsonSerializerOptions WebOptions = new(JsonSerializerDefaults.Web);

    public ScalarDtoToolAdapter(Type inputType)
    {
        if (!inputType.IsClass || inputType.IsAbstract || inputType.ContainsGenericParameters ||
            inputType == typeof(string) || inputType.GetConstructor(Type.EmptyTypes) is null ||
            inputType.GetCustomAttribute<JsonConverterAttribute>(inherit: true) is not null ||
            inputType.GetCustomAttribute<JsonPolymorphicAttribute>(inherit: true) is not null ||
            inputType.GetCustomAttributes<JsonDerivedTypeAttribute>(inherit: true).Any() ||
            inputType.GetCustomAttribute<JsonNumberHandlingAttribute>(inherit: true) is not null ||
            inputType.GetFields(BindingFlags.Public | BindingFlags.Instance).Length != 0 ||
            inputType.GetMembers(BindingFlags.NonPublic | BindingFlags.Instance).Any(member =>
                member.GetCustomAttribute<JsonIncludeAttribute>() is not null) ||
            inputType.GetConstructors().Any(c => c.GetCustomAttribute<JsonConstructorAttribute>() is not null))
            throw Unsupported(inputType, "use a concrete class with a public parameterless constructor, scalar properties, and no custom JSON converters or polymorphism");

        var properties = new JsonObject();
        var required = new JsonArray();
        var nullability = new NullabilityInfoContext();
        var jsonNames = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var property in inputType.GetProperties(BindingFlags.Public | BindingFlags.Instance))
        {
            if (property.GetIndexParameters().Length != 0 ||
                property.GetMethod?.IsPublic != true || property.SetMethod?.IsPublic != true ||
                property.GetCustomAttribute<JsonIgnoreAttribute>() is not null ||
                property.GetCustomAttribute<JsonExtensionDataAttribute>() is not null ||
                property.GetCustomAttribute<JsonConverterAttribute>() is not null ||
                property.GetCustomAttribute<JsonNumberHandlingAttribute>() is not null)
                throw Unsupported(inputType, $"property '{property.Name}' must be a public readable/writable scalar property without custom JSON handling");

            var actualType = Nullable.GetUnderlyingType(property.PropertyType) ?? property.PropertyType;
            var schemaType = MapType(actualType) ??
                throw Unsupported(inputType, $"property '{property.Name}' has unsupported type '{property.PropertyType}'; only string, bool, numeric CLR builtins and nullable versions are supported");
            var name = property.GetCustomAttribute<JsonPropertyNameAttribute>()?.Name ??
                JsonNamingPolicy.CamelCase.ConvertName(property.Name);
            var isRequired = property.GetCustomAttribute<RequiredMemberAttribute>() is not null ||
                property.GetCustomAttribute<JsonRequiredAttribute>() is not null;
            var isNullable = Nullable.GetUnderlyingType(property.PropertyType) is not null ||
                (actualType == typeof(string) && nullability.Create(property).WriteState == NullabilityState.Nullable);
            if (!jsonNames.Add(name))
                throw Unsupported(inputType, $"multiple properties map to JSON name '{name}'");
            _fields.Add(name, new Field(name, actualType, isRequired, isNullable));
            properties[name] = new JsonObject
            {
                ["type"] = isNullable && !isRequired
                    ? new JsonArray(JsonValue.Create(schemaType), JsonValue.Create("null"))
                    : JsonValue.Create(schemaType)
            };
            if (isRequired)
                required.Add(name);
        }
        if (_fields.Count == 0)
            throw Unsupported(inputType, "the input DTO must declare at least one supported scalar property");
        Schema = new JsonObject
        {
            ["type"] = "object",
            ["properties"] = properties,
            ["required"] = required,
            ["additionalProperties"] = false
        };
    }

    public JsonNode Schema { get; }

    public void Validate(JsonElement arguments)
    {
        if (arguments.ValueKind != JsonValueKind.Object)
            throw new ArgumentException("Function tool arguments must be a JSON object.");
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (var property in arguments.EnumerateObject())
        {
            if (!_fields.TryGetValue(property.Name, out var field))
                throw new ArgumentException($"Unknown function tool argument '{property.Name}'.");
            if (!seen.Add(property.Name))
                throw new ArgumentException($"Duplicate function tool argument '{property.Name}'.");
            var value = property.Value;
            if (value.ValueKind == JsonValueKind.Null)
            {
                if (field.Required || !field.Nullable)
                    throw new ArgumentException($"Function tool argument '{field.Name}' cannot be null.");
                continue;
            }
            var expected = MapType(field.Type)!;
            if ((expected == "string" && value.ValueKind != JsonValueKind.String) ||
                (expected == "boolean" && value.ValueKind is not (JsonValueKind.True or JsonValueKind.False)) ||
                (expected is "integer" or "number" && value.ValueKind != JsonValueKind.Number))
                throw new ArgumentException($"Function tool argument '{field.Name}' must be a JSON {expected}.");
            try
            {
                // Deserialize only the already kind-checked scalar, including range/integer checks.
                var scalar = JsonSerializer.Deserialize(value.GetRawText(), field.Type, WebOptions);
                if (scalar is double doubleValue && !double.IsFinite(doubleValue) ||
                    scalar is float floatValue && !float.IsFinite(floatValue))
                    throw new JsonException("Non-finite numbers are unsupported.");
            }
            catch (Exception ex) when (ex is JsonException or NotSupportedException or OverflowException)
            {
                throw new ArgumentException(
                    $"Function tool argument '{field.Name}' is not a valid {field.Type.Name} value.", ex);
            }
        }
        foreach (var field in _fields.Values)
        {
            if (field.Required && !seen.Contains(field.Name))
                throw new ArgumentException($"Missing required function tool argument '{field.Name}'.");
        }
    }

    private static ArgumentException Unsupported(Type type, string reason) =>
        new($"Unsupported function tool input DTO '{type.Name}': {reason}.");

    private static string? MapType(Type type)
    {
        if (type == typeof(string)) return "string";
        if (type == typeof(bool)) return "boolean";
        if (type == typeof(byte) || type == typeof(sbyte) ||
            type == typeof(short) || type == typeof(ushort) ||
            type == typeof(int) || type == typeof(uint) ||
            type == typeof(long) || type == typeof(ulong))
            return "integer";
        if (type == typeof(float) || type == typeof(double) || type == typeof(decimal))
            return "number";
        return null;
    }
}
