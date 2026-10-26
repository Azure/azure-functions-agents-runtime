using System.Text.Json;
using HostedSkills.Spike;

// ============================================================================
// Sample C# Azure Functions Worker Application using the shared Rust runtime
// (`fhs-runtime`) over stdio for:
//   1. `.agent.md` + `agents.config.yaml` discovery & `$VAR` substitution
//   2. `skills/` discovery & SKILL.md injection
//   3. `[HostedSkillInput("order_assistant")] HostedSkill` binding (`RunAsync` & `StreamAsync`)
//   4. `[AgentTool]` plain method + in-app `[HttpTrigger]` function synthesized in-process over stdio
//   5. Multi-turn session state persistence via `SessionFsProvider`
// ============================================================================

public sealed class OrderFunctions
{
    /// <summary>
    /// 1. Plain C# tool method (`[AgentTool]` / `app.tool()` equivalent).
    /// </summary>
    [AgentTool("lookup_order", "Look up an order's payment and item status by orderId.")]
    public object LookupOrder(string orderId)
    {
        return new
        {
            orderId,
            customer = "Ada Lovelace",
            status = "PAID",
            totalUsd = 149.99
        };
    }

    /// <summary>
    /// 2. In-app HTTP-triggered Azure Function exposed as an agent tool (`[AgentTool]` + `[HttpTrigger]`).
    /// When `fhs-runtime` sends `tool.call` over stdio, `AgentToolRegistry` synthesizes
    /// `SynthesizedHttpRequestData` in-process (zero HTTP loopback) and invokes this trigger directly!
    /// </summary>
    [AgentTool(
        "calculate_shipping_http",
        "In-app HTTP trigger function that calculates shipping rates.",
        Route = "/api/shipping/quote",
        HttpMethod = "POST")]
    public Task<object> CalculateShippingHttp(
        [HttpTrigger("Anonymous", "POST", Route = "/api/shipping/quote")]
        SynthesizedHttpRequestData req)
    {
        var orderId = req.Body.TryGetProperty("orderId", out var oid) ? oid.GetString() : "UNKNOWN";
        var zip = req.Body.TryGetProperty("zip", out var z) ? z.GetString() : "00000";

        return Task.FromResult<object>(new
        {
            inProcessTrigger = "httpTrigger",
            method = req.Method,
            route = req.Route,
            sessionIdHeader = req.Headers["x-fhs-session-id"],
            orderId,
            zip,
            carrier = "PriorityExpress",
            rateUsd = 14.25
        });
    }

    /// <summary>
    /// 3. Simulated Azure Function using the `[HostedSkillInput("order_assistant")] HostedSkill` binding.
    /// </summary>
    public async Task<AgentRunResult> HandleCustomerSupportRequestAsync(
        [HostedSkillInput("order_assistant")] HostedSkill skill,
        string userPrompt,
        string sessionId)
    {
        return await skill.RunAsync(userPrompt, sessionId: sessionId);
    }
}

public static class Program
{
    public static async Task<int> Main(string[] args)
    {
        var spikeRoot = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "..", ".."));
        var runtimeBinary = Path.Combine(spikeRoot, "target", "debug", "fhs-runtime");
        if (!File.Exists(runtimeBinary))
        {
            Console.Error.WriteLine($"fhs-runtime binary not found at {runtimeBinary}. Run `cargo build` first.");
            return 1;
        }

        // Create a temporary app directory with `agents.config.yaml`, `agents/order_assistant.agent.md`, and `skills/`.
        var tempAppRoot = Path.Combine(Path.GetTempPath(), $"fhs-csharp-spike-{Guid.NewGuid():N}");
        Directory.CreateDirectory(Path.Combine(tempAppRoot, "agents"));
        Directory.CreateDirectory(Path.Combine(tempAppRoot, "skills", "order-policy"));

        try
        {
            await File.WriteAllTextAsync(
                Path.Combine(tempAppRoot, "agents.config.yaml"),
                "model: $DEFAULT_AGENT_MODEL\ntimeout: 120\n");

            await File.WriteAllTextAsync(
                Path.Combine(tempAppRoot, "agents", "order_assistant.agent.md"),
                """
                ---
                name: Order Assistant ($ENVIRONMENT_NAME)
                description: Assists customers with orders and shipping quotes
                skills:
                  - order-policy
                tools:
                  - lookup_order
                  - calculate_shipping_http
                ---
                You are the Order Assistant for $TENANT_NAME.
                Always use lookup_order and calculate_shipping_http when asked about an order.
                """);

            await File.WriteAllTextAsync(
                Path.Combine(tempAppRoot, "skills", "order-policy", "SKILL.md"),
                "# Order Policy: Verify order status and shipping quote before replying.");

            // Register tools from `OrderFunctions` (both plain `[AgentTool]` and in-app `[HttpTrigger]` tool).
            var functions = new OrderFunctions();
            var toolRegistry = new AgentToolRegistry();
            toolRegistry.RegisterFromObject(functions);

            var envOverrides = new Dictionary<string, string>
            {
                ["DEFAULT_AGENT_MODEL"] = "gpt-4.1",
                ["ENVIRONMENT_NAME"] = "dotnet-isolated",
                ["TENANT_NAME"] = "ContosoRetail",
                ["FOUNDRY_PROJECT_ENDPOINT"] = "https://contoso-foundry.services.ai.azure.com/api/projects/retail"
            };

            Console.WriteLine("1. Starting Rust runtime (`fhs-runtime`) over stdio from C# worker...");
            await using var clientAndInit = await StartRuntimeWrapperAsync(
                runtimeBinary,
                tempAppRoot,
                toolRegistry,
                envOverrides);

            var (client, init) = (clientAndInit.Client, clientAndInit.Init);
            Console.WriteLine($"   Runtime v{init.RuntimeVersion} initialized | Provider={init.Provider.Kind} | Storage={init.StorageMode}");
            Console.WriteLine($"   Discovered agent: {init.Agents[0].Slug} ('{init.Agents[0].Name}', model={init.Agents[0].Model})");
            Console.WriteLine($"   Enabled skills: [{string.Join(", ", init.Agents[0].EnabledSkills)}]");
            Console.WriteLine($"   Enabled tools:  [{string.Join(", ", init.Agents[0].EnabledTools)}]");

            Assert(init.Agents.Count == 1, "Expected 1 discovered agent");
            Assert(init.Agents[0].Name == "Order Assistant (dotnet-isolated)", "Expected env var substitution in agent name");
            Assert(init.Agents[0].EnabledSkills.Contains("order-policy"), "Expected order-policy skill enabled");
            Assert(init.Agents[0].EnabledTools.Contains("lookup_order"), "Expected lookup_order tool enabled");
            Assert(init.Agents[0].EnabledTools.Contains("calculate_shipping_http"), "Expected calculate_shipping_http tool enabled");

            // Bind `[HostedSkillInput("order_assistant")] HostedSkill`:
            var orderSkill = client.GetHostedSkill("order_assistant");
            const string sessionId = "csharp-session-001";

            Console.WriteLine("\n2. Executing Turn 1 via HostedSkill.RunAsync (invoking C# tools over stdio)...");
            var turn1 = await functions.HandleCustomerSupportRequestAsync(
                orderSkill,
                "Please lookup_order and calculate_shipping_http for {\"orderId\": \"ORD-900\", \"zip\": \"98052\"}",
                sessionId);

            Console.WriteLine($"   Turn 1 Resumed={turn1.Resumed} | ToolsCalled=[{string.Join(", ", turn1.ToolCallsMade)}]");
            Console.WriteLine($"   Turn 1 Reply: {turn1.Content}");

            Assert(!turn1.Resumed, "Turn 1 should create a new session (Resumed == false)");
            Assert(turn1.ToolCallsMade.Contains("lookup_order"), "Expected lookup_order callback over stdio");
            Assert(turn1.ToolCallsMade.Contains("calculate_shipping_http"), "Expected in-app HTTP trigger callback over stdio");
            Assert(turn1.Content.Contains("Ada Lovelace"), "Expected plain tool output in reply");
            Assert(turn1.Content.Contains("PriorityExpress"), "Expected synthesized HTTP trigger output in reply");
            Assert(turn1.Content.Contains("ContosoRetail"), "Expected substituted instruction variable in reply");

            Console.WriteLine("\n3. Executing Turn 2 via HostedSkill.StreamAsync (resuming persisted session)...");
            var streamKinds = new List<string>();
            await foreach (var ev in orderSkill.StreamAsync(
                "Confirm my shipping carrier from our current session.",
                sessionId: sessionId))
            {
                streamKinds.Add(ev.Kind);
            }
            Console.WriteLine($"   Streamed {streamKinds.Count} events: [{string.Join(", ", streamKinds)}]");
            Assert(streamKinds.Contains("delta") && streamKinds.Contains("done"), "Expected delta and done stream events");

            var inspect = await client.InspectSessionAsync("order_assistant", sessionId);
            Console.WriteLine($"   SessionFs persisted state={inspect.HasPersistedState} | files=[{string.Join(", ", inspect.SessionStateFiles)}]");
            Assert(inspect.HasPersistedState, "Expected SessionFs persisted state after 2 turns");

            Console.WriteLine("\nSUCCESS: All C# HostedSkill + in-app tool callbacks over stdio assertions passed!");
            return 0;
        }
        finally
        {
            if (Directory.Exists(tempAppRoot))
            {
                Directory.Delete(tempAppRoot, recursive: true);
            }
        }
    }

    private static async Task<RuntimeHandle> StartRuntimeWrapperAsync(
        string runtimeBinary,
        string appRoot,
        AgentToolRegistry toolRegistry,
        IReadOnlyDictionary<string, string> envOverrides)
    {
        var (client, init) = await HostedSkillRuntimeClient.StartAsync(
            runtimeBinary,
            appRoot,
            toolRegistry,
            envOverrides,
            mockLlm: true);
        return new RuntimeHandle(client, init);
    }

    private static void Assert(bool condition, string message)
    {
        if (!condition)
        {
            throw new InvalidOperationException($"Assertion failed: {message}");
        }
    }

    private sealed record RuntimeHandle(HostedSkillRuntimeClient Client, InitializeResult Init) : IAsyncDisposable
    {
        public ValueTask DisposeAsync() => Client.DisposeAsync();
    }
}
