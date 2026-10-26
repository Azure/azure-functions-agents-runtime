using HostedSkills.Spike;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;

var host = new HostBuilder()
    .ConfigureServices(services =>
    {
        services.AddSingleton<OrderTools>();
        services.AddSingleton<AgentToolRegistry>(provider =>
        {
            var tools = provider.GetRequiredService<OrderTools>();
            var registry = new AgentToolRegistry();
            registry.RegisterFromObject(tools);
            // This adapter calls the HTTP function's shared application logic, not the
            // Functions invocation pipeline (no synthetic binding or loopback request).
            registry.RegisterTool(new WorkerToolDescriptor(
                "calculate_shipping_http",
                "Calculate shipping using the same handler logic as ShippingQuote.",
                System.Text.Json.Nodes.JsonNode.Parse("""
                {"type":"object","properties":{"orderId":{"type":"string"},"zip":{"type":"string"}},"required":["orderId","zip"]}
                """)!,
                new TriggerToolMetadata("httpTrigger", "ShippingQuote", "POST", "shipping/quote")),
                call => Task.FromResult(System.Text.Json.JsonSerializer.Serialize(
                    tools.CalculateShipping(call.Arguments, call.SessionId, "stdio-shared-handler"))));
            return registry;
        });
        services.AddSingleton<RuntimeService>();
        services.AddHostedService(provider => provider.GetRequiredService<RuntimeService>());
        // HostedSkill is an ordinary DI service, NOT an Azure Functions input binding.
        services.AddSingleton(provider =>
            provider.GetRequiredService<RuntimeService>().Client.GetHostedSkill("order_assistant"));
    })
    .ConfigureFunctionsWorkerDefaults()
    .Build();

await host.RunAsync();
