using System.Diagnostics;
using System.Reflection;
using System.Text.Json.Nodes;
using HostedSkills.Spike;

// No test framework dependencies: fault injection complements (never replaces)
// the real Core Tools HTTP / Rust / Azurite smoke.
var fixture = Path.Combine(AppContext.BaseDirectory, "rpc_fixture.py");
var stderr = Console.Error;
var passed = 0;
Console.SetError(TextWriter.Null);
try
{
    await ExpectAsync<EndOfStreamException>(async () => { await StartAsync("initialize_eof"); });
    Pass("initialize EOF fails instead of hanging");

    await using (var client = await StartAsync("stderr_flood"))
        AssertEmpty(client);
    Pass(">pipe-capacity stderr is drained before initialize reply");

    await using (var client = await StartAsync("run_eof"))
    {
        await ExpectAsync<EndOfStreamException>(() => client.GetHostedSkill("agent").RunAsync("hello", "eof"));
        AssertEmpty(client);
    }
    Pass("pending run fails promptly on child EOF");

    await using (var client = await StartAsync("normal"))
    {
        Assert(client.InferenceMode == "mock", "initialization reports mock inference mode");
        foreach (var session in new[] { "first", "second" })
        {
            var events = new List<AgentStreamEvent>();
            await foreach (var ev in client.GetHostedSkill("agent").StreamAsync("hello", session))
                events.Add(ev);
            Assert(events.Select(ev => ev.Kind).SequenceEqual(["delta", "done"]), "stream kinds");
            Assert(events.All(ev => ev.SessionId == session), "stream session isolation");
            AssertEmpty(client);
        }
    }
    Pass("stream listeners are session-scoped and removed on completion");

    await using (var client = await StartAsync("foundry"))
        Assert(client.InferenceMode == "foundry", "initialization reports Foundry inference mode");
    Pass("Foundry inference mode is forwarded from Rust initialization");

    var originalConnectionString = Environment.GetEnvironmentVariable("APPLICATIONINSIGHTS_CONNECTION_STRING");
    try
    {
        Environment.SetEnvironmentVariable("APPLICATIONINSIGHTS_CONNECTION_STRING", null);
        Assert(SpikeTelemetry.StartAgentRun("agent", "mock") is null,
            "telemetry remains disabled without an Application Insights connection string");
        Assert(SpikeTelemetry.ParseTraceContext(new TelemetryTraceContext("bad", "bad", "bad")) is null,
            "malformed trace context does not create child spans");

        Environment.SetEnvironmentVariable(
            "APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=00000000-0000-0000-0000-000000000000");
        var stoppedActivities = new List<Activity>();
        using var listener = new ActivityListener
        {
            ShouldListenTo = source => source.Name == SpikeTelemetry.SourceName,
            Sample = (ref ActivityCreationOptions<ActivityContext> _) =>
                ActivitySamplingResult.AllDataAndRecorded,
            ActivityStopped = stoppedActivities.Add,
        };
        ActivitySource.AddActivityListener(listener);

        var telemetryRegistry = new AgentToolRegistry();
        var toolFails = false;
        telemetryRegistry.RegisterTool(
            new WorkerToolDescriptor("fixture_tool", "Fixture tool.", JsonNode.Parse("{}")!),
            _ => toolFails
                ? Task.FromException<string>(new InvalidOperationException("private tool error"))
                : Task.FromResult("fixture result"));
        await using (var client = await StartAsync("telemetry", telemetryRegistry))
            await client.GetHostedSkill("agent").RunAsync("private prompt", "telemetry-session");

        var runActivity = stoppedActivities.Single(activity => activity.OperationName == "fhs.rust.agent.run");
        var modelActivity = stoppedActivities.Single(activity => activity.OperationName == "gen_ai.request");
        var toolActivity = stoppedActivities.Single(activity => activity.OperationName == "fhs.tool.execute");
        Assert(modelActivity.TraceId == runActivity.TraceId && toolActivity.TraceId == runActivity.TraceId,
            "agent, model and tool spans share one trace");
        Assert(modelActivity.ParentSpanId == runActivity.SpanId && toolActivity.ParentSpanId == runActivity.SpanId,
            "model and tool spans are children of the agent run");
        Assert(!stoppedActivities.Any(activity => activity.TagObjects.Any(tag =>
                tag.Value?.ToString()?.Contains("private prompt", StringComparison.Ordinal) == true
                || tag.Value?.ToString()?.Contains("telemetry-session", StringComparison.Ordinal) == true)),
            "telemetry spans do not contain prompt content");

        Assert(modelActivity.GetTagItem("gen_ai.request.model") as string == "fixture-model",
            "model span retains only the configured model name");
        Assert(modelActivity.GetTagItem("fhs.outcome") as string == "success"
            && toolActivity.GetTagItem("fhs.outcome") as string == "success",
            "successful model and tool callbacks have successful outcomes");
        var allowedTags = new HashSet<string>
        {
            "fhs.agent.name", "fhs.inference.mode", "fhs.outcome",
            "fhs.tool.name", "fhs.provider", "gen_ai.request.model",
        };
        Assert(stoppedActivities.All(activity =>
                activity.TagObjects.All(tag => allowedTags.Contains(tag.Key))),
            "spans emit only allowlisted metadata attributes");

        stoppedActivities.Clear();
        toolFails = true;
        await using (var client = await StartAsync("telemetry_tool_error", telemetryRegistry))
            await client.GetHostedSkill("agent").RunAsync("private prompt", "tool-error-session");
        var failedTool = stoppedActivities.Single(activity => activity.OperationName == "fhs.tool.execute");
        Assert(failedTool.Status == ActivityStatusCode.Error
            && failedTool.GetTagItem("fhs.outcome") as string == "error",
            "tool errors set span outcome without exporting exception content");

        stoppedActivities.Clear();
        await using (var client = await StartAsync("telemetry_failure"))
            await ExpectAsync<InvalidOperationException>(() =>
                client.GetHostedSkill("agent").RunAsync("private prompt", "failure-session"));
        var failedRun = stoppedActivities.Single(activity => activity.OperationName == "fhs.rust.agent.run");
        var failedModel = stoppedActivities.Single(activity => activity.OperationName == "gen_ai.request");
        Assert(failedRun.Status == ActivityStatusCode.Error
            && failedModel.Status == ActivityStatusCode.Error,
            "failed agent runs close unmatched model spans with error status");
        Pass("Application Insights opt-in, trace propagation, model/tool spans and content exclusion");
    }
    finally
    {
        Environment.SetEnvironmentVariable(
            "APPLICATIONINSIGHTS_CONNECTION_STRING", originalConnectionString);
    }

    await using (var client = await StartAsync("stall"))
    {
        using var cancellation = new CancellationTokenSource(TimeSpan.FromMilliseconds(150));
        await ExpectAsync<OperationCanceledException>(() =>
            client.GetHostedSkill("agent").RunAsync("hello", "cancel", cancellation.Token));
        AssertEmpty(client);
        await ExpectAsync<IOException>(() => client.GetHostedSkill("agent").RunAsync("unsafe overlap", "cancel"));
    }
    Pass("canceled run cleans requests and fails closed before another turn");

    await using (var client = await StartAsync("stall"))
    {
        await foreach (var ev in client.GetHostedSkill("agent").StreamAsync("hello", "abandon"))
            break;
        AssertEmpty(client);
        await ExpectAsync<IOException>(() => client.GetHostedSkill("agent").RunAsync("unsafe overlap", "abandon"));
    }
    Pass("abandoned stream cancels its request and removes its listener");

    var shutdownClient = await StartAsync("shutdown_stall");
    var process = (Process)Field(shutdownClient, "_process");
    var pid = process.Id;
    var watch = Stopwatch.StartNew();
    await shutdownClient.DisposeAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(8));
    await shutdownClient.DisposeAsync();
    Assert(watch.Elapsed < TimeSpan.FromSeconds(8), "bounded shutdown");
    var stillRunning = false;
    try { using var child = Process.GetProcessById(pid); stillRunning = !child.HasExited; }
    catch (ArgumentException) { }
    Assert(!stillRunning, "stalled child was reaped");
    Pass("unresponsive shutdown is bounded, idempotent and kills only owned child");

    var registry = new AgentToolRegistry();
    registry.RegisterFromObject(new OrderCheckTool());
    Assert(registry.Descriptors.Single().Parameters["required"]!.AsArray().Count == 1,
        "plain tool schema");
    var missing = await registry.ExecuteAsync(new ToolCallParams("lookup", "id", "session",
        System.Text.Json.JsonSerializer.SerializeToElement(new { }), null));
    Assert(missing.IsError, "missing required tool argument");
    Pass("plain tool registration and required-argument errors");
    await HttpToolChecks.RunAsync(Pass);
    Console.WriteLine($"All {passed} C# RPC / tool / HTTP adapter checks passed.");
}
finally
{
    Console.SetError(stderr);
}

void Pass(string message)
{
    passed++;
    Console.WriteLine($"PASS {passed}: {message}");
}

async Task<HostedSkillRuntimeClient> StartAsync(string mode, AgentToolRegistry? registry = null)
{
    using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(5));
    var (client, _) = await HostedSkillRuntimeClient.StartAsync(
        fixture, AppContext.BaseDirectory, registry ?? new AgentToolRegistry(),
        new Dictionary<string, string> { ["fixture_mode"] = mode },
        mockLlm: true, cancellationToken: deadline.Token);
    return client;
}

static async Task ExpectAsync<T>(Func<Task> action) where T : Exception
{
    try { await action().WaitAsync(TimeSpan.FromSeconds(5)); }
    catch (T) { return; }
    throw new InvalidOperationException($"Expected {typeof(T).Name}");
}

static object Field(object target, string name) =>
    target.GetType().GetField(name, BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(target)!;

static void AssertEmpty(HostedSkillRuntimeClient client)
{
    foreach (var name in new[] { "_pending", "_streamListeners" })
    {
        var collection = Field(client, name);
        Assert((int)collection.GetType().GetProperty("Count")!.GetValue(collection)! == 0, name);
    }
}

static void Assert(bool condition, string message)
{
    if (!condition) throw new InvalidOperationException(message);
}

sealed class OrderCheckTool
{
    [AgentTool("lookup", "Test required and optional parameters")]
    public string Lookup(string orderId, string optional = "default") => orderId + optional;
}
