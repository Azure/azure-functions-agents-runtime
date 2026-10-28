using System.Diagnostics;
using System.Text.Json.Serialization;

namespace HostedSkills.Spike;

public sealed record TelemetryTraceContext(
    [property: JsonPropertyName("trace_id")] string TraceId,
    [property: JsonPropertyName("span_id")] string SpanId,
    [property: JsonPropertyName("trace_flags")] string TraceFlags);

public sealed record ModelCallTelemetry(
    [property: JsonPropertyName("run_id")] string RunId,
    [property: JsonPropertyName("call_id")] string CallId,
    [property: JsonPropertyName("phase")] string Phase,
    [property: JsonPropertyName("model")] string Model,
    [property: JsonPropertyName("outcome")] string? Outcome);

internal static class SpikeTelemetry
{
    internal const string SourceName = "AzureFunctionsAgents.RustSpike";
    private const string ConnectionStringEnvironmentVariable = "APPLICATIONINSIGHTS_CONNECTION_STRING";
    private static readonly ActivitySource Source = new(SourceName);

    internal static Activity? StartAgentRun(string agentName, string inferenceMode)
    {
        if (!IsEnabled())
            return null;

        var activity = Source.StartActivity("fhs.rust.agent.run", ActivityKind.Internal);
        activity?.SetTag("fhs.agent.name", agentName);
        activity?.SetTag("fhs.inference.mode", inferenceMode);
        return activity;
    }

    internal static TelemetryTraceContext? GetTraceContext(Activity? activity)
    {
        if (activity is null)
            return null;

        var context = activity.Context;
        var traceContext = new TelemetryTraceContext(
            context.TraceId.ToString(),
            context.SpanId.ToString(),
            ((byte)context.TraceFlags).ToString("x2"));
        return ParseTraceContext(traceContext) is null ? null : traceContext;
    }

    internal static Activity? StartToolExecution(ToolCallParams call, ActivityContext? parent)
    {
        if (!IsEnabled() || parent is null)
            return null;

        var activity = Source.StartActivity("fhs.tool.execute", ActivityKind.Internal, parent.Value);
        activity?.SetTag("fhs.tool.name", call.ToolName);
        return activity;
    }

    internal static Activity? StartModelCall(ModelCallTelemetry telemetry, ActivityContext parent)
    {
        if (!IsEnabled())
            return null;

        var activity = Source.StartActivity("gen_ai.request", ActivityKind.Client, parent);
        activity?.SetTag("gen_ai.request.model", telemetry.Model);
        activity?.SetTag("fhs.provider", "foundry");
        return activity;
    }

    internal static ActivityContext? ParseTraceContext(TelemetryTraceContext? traceContext)
    {
        if (traceContext is null)
            return null;

        var traceParent = $"00-{traceContext.TraceId}-{traceContext.SpanId}-{traceContext.TraceFlags}";
        return ActivityContext.TryParse(traceParent, null, isRemote: false, out var context)
            ? context
            : null;
    }

    internal static void SetToolOutcome(Activity? activity, bool isError)
    {
        if (activity is null)
            return;

        var outcome = isError ? "error" : "success";
        activity.SetTag("fhs.outcome", outcome);
        activity.SetStatus(isError ? ActivityStatusCode.Error : ActivityStatusCode.Ok);
    }

    internal static void SetModelOutcome(Activity? activity, string? outcome)
    {
        if (activity is null)
            return;

        var boundedOutcome = outcome is "success" or "error" or "cancelled" or "rejected"
            ? outcome
            : "unknown";
        activity.SetTag("fhs.outcome", boundedOutcome);
        activity.SetStatus(
            boundedOutcome == "success" ? ActivityStatusCode.Ok : ActivityStatusCode.Error);
    }

    internal static void SetRunOutcome(Activity? activity, bool isError)
    {
        if (activity is null)
            return;

        activity.SetTag("fhs.outcome", isError ? "error" : "success");
        activity.SetStatus(isError ? ActivityStatusCode.Error : ActivityStatusCode.Ok);
    }

    private static bool IsEnabled() =>
        !string.IsNullOrWhiteSpace(
            Environment.GetEnvironmentVariable(ConnectionStringEnvironmentVariable));
}
