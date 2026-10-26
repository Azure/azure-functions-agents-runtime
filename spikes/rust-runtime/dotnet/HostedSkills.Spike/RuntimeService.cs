using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace HostedSkills.Spike;

public sealed class RuntimeService(
    IConfiguration configuration,
    AgentToolRegistry registry,
    ILogger<RuntimeService> logger) : IHostedService, IAsyncDisposable
{
    private HostedSkillRuntimeClient? _client;
    public HostedSkillRuntimeClient Client =>
        _client ?? throw new InvalidOperationException("Rust runtime has not started.");

    public async Task StartAsync(CancellationToken cancellationToken)
    {
        // App files are copied by MSBuild, so discovery also works from bin/Debug/net10.0.
        var appRoot = AppContext.BaseDirectory;
        var binary = configuration["FHS_RUNTIME_PATH"];
        if (string.IsNullOrWhiteSpace(binary) || !Path.IsPathFullyQualified(binary) || !File.Exists(binary))
            throw new InvalidOperationException("FHS_RUNTIME_PATH must be the absolute path to the built fhs-runtime binary.");
        if (!string.Equals(configuration["FHS_MOCK_LLM"], "true", StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("This host sample requires FHS_MOCK_LLM=true; no live inference is validated.");

        var (client, init) = await HostedSkillRuntimeClient.StartAsync(
            binary, appRoot, registry, mockLlm: true, cancellationToken: cancellationToken);
        _client = client;
        logger.LogInformation("Rust stdio ready: MOCK inference, storage={Storage}, agents={Agents}, skills={Skills}",
            init.StorageMode, string.Join(",", init.Agents.Select(a => a.Slug)),
            string.Join(",", init.Skills.Select(s => s.Name)));
    }

    public async Task StopAsync(CancellationToken cancellationToken) => await DisposeAsync();

    public async ValueTask DisposeAsync()
    {
        var client = Interlocked.Exchange(ref _client, null);
        if (client is not null) await client.DisposeAsync();
    }
}
