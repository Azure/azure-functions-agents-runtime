using System.Diagnostics;
using System.Reflection;
using HostedSkills.Spike;

// No test framework dependencies: fault injection complements (never replaces)
// the real Core Tools HTTP / Rust / Azurite smoke.
var fixture = Path.Combine(AppContext.BaseDirectory, "rpc_fixture.py");
var stderr = Console.Error;
Console.SetError(TextWriter.Null);
try
{
    await ExpectAsync<EndOfStreamException>(async () => { await StartAsync("initialize_eof"); });
    Console.WriteLine("PASS: initialize EOF fails instead of hanging");

    await using (var client = await StartAsync("stderr_flood"))
        AssertEmpty(client);
    Console.WriteLine("PASS: >pipe-capacity stderr is drained before initialize reply");

    await using (var client = await StartAsync("run_eof"))
    {
        await ExpectAsync<EndOfStreamException>(() => client.GetHostedSkill("agent").RunAsync("hello", "eof"));
        AssertEmpty(client);
    }
    Console.WriteLine("PASS: pending run fails promptly on child EOF");

    await using (var client = await StartAsync("normal"))
    {
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
    Console.WriteLine("PASS: stream listeners are session-scoped and removed on completion");

    await using (var client = await StartAsync("stall"))
    {
        using var cancellation = new CancellationTokenSource(TimeSpan.FromMilliseconds(150));
        await ExpectAsync<OperationCanceledException>(() =>
            client.GetHostedSkill("agent").RunAsync("hello", "cancel", cancellation.Token));
        AssertEmpty(client);
        await ExpectAsync<IOException>(() => client.GetHostedSkill("agent").RunAsync("unsafe overlap", "cancel"));
    }
    Console.WriteLine("PASS: canceled run cleans requests and fails closed before another turn");

    await using (var client = await StartAsync("stall"))
    {
        await foreach (var ev in client.GetHostedSkill("agent").StreamAsync("hello", "abandon"))
            break;
        AssertEmpty(client);
        await ExpectAsync<IOException>(() => client.GetHostedSkill("agent").RunAsync("unsafe overlap", "abandon"));
    }
    Console.WriteLine("PASS: abandoned stream cancels its request and removes its listener");

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
    Console.WriteLine("PASS: unresponsive shutdown is bounded, idempotent and kills only owned child");

    var registry = new AgentToolRegistry();
    registry.RegisterFromObject(new OrderCheckTool());
    Assert(registry.Descriptors.Single().Parameters["required"]!.AsArray().Count == 1,
        "plain tool schema");
    var missing = await registry.ExecuteAsync(new ToolCallParams("lookup", "id", "session",
        System.Text.Json.JsonSerializer.SerializeToElement(new { }), null));
    Assert(missing.IsError, "missing required tool argument");
    Console.WriteLine("PASS: plain tool registration and required-argument errors");
    Console.WriteLine("All 8 C# RPC / tool lifecycle checks passed.");
}
finally
{
    Console.SetError(stderr);
}

async Task<HostedSkillRuntimeClient> StartAsync(string mode)
{
    using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(5));
    var (client, _) = await HostedSkillRuntimeClient.StartAsync(
        fixture, AppContext.BaseDirectory, new AgentToolRegistry(),
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
