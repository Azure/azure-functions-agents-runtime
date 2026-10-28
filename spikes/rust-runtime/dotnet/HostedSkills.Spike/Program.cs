using Azure.Monitor.OpenTelemetry.Exporter;
using HostedSkills.Spike;
using Microsoft.Azure.Functions.Worker.OpenTelemetry;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;

var host = new HostBuilder()
    .ConfigureServices(services =>
    {
        var connectionString = Environment.GetEnvironmentVariable("APPLICATIONINSIGHTS_CONNECTION_STRING");
        if (!string.IsNullOrWhiteSpace(connectionString))
        {
            services.AddOpenTelemetry()
                .WithTracing(tracing => tracing
                    .AddSource(SpikeTelemetry.SourceName)
                    .ConfigureResource(resource => resource.AddService(
                        Environment.GetEnvironmentVariable("OTEL_SERVICE_NAME")
                        ?? "fhs-rust-dotnet-spike")))
                .UseFunctionsWorkerDefaults()
                .UseAzureMonitorExporter();
        }

        services.AddSingleton<OrderTools>();
        services.AddSingleton<AgentToolRegistry>(provider =>
        {
            var tools = provider.GetRequiredService<OrderTools>();
            var registry = new AgentToolRegistry();
            registry.RegisterFromObject(tools);
            registry.RegisterFunctions<ShippingFunctions>(provider);
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
