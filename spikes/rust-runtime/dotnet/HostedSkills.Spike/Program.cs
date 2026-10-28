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
