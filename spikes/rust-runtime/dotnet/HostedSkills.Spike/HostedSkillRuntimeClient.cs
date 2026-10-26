using System.Collections.Concurrent;
using System.Diagnostics;
using System.Runtime.CompilerServices;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading.Channels;

namespace HostedSkills.Spike;

public sealed record AgentSummary(
    [property: JsonPropertyName("slug")] string Slug,
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("description")] string Description,
    [property: JsonPropertyName("model")] string Model,
    [property: JsonPropertyName("enabled_skills")] IReadOnlyList<string> EnabledSkills,
    [property: JsonPropertyName("enabled_tools")] IReadOnlyList<string> EnabledTools);

public sealed record SkillDescriptor(
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("path")] string Path);

public sealed record ProviderSummary(
    [property: JsonPropertyName("kind")] string Kind,
    [property: JsonPropertyName("base_url")] string BaseUrl,
    [property: JsonPropertyName("auth_mode")] string AuthMode,
    [property: JsonPropertyName("default_model")] string? DefaultModel);

public sealed record InitializeResult(
    [property: JsonPropertyName("runtime_version")] string RuntimeVersion,
    [property: JsonPropertyName("agents")] IReadOnlyList<AgentSummary> Agents,
    [property: JsonPropertyName("skills")] IReadOnlyList<SkillDescriptor> Skills,
    [property: JsonPropertyName("provider")] ProviderSummary Provider,
    [property: JsonPropertyName("storage_mode")] string StorageMode);

public sealed record AgentRunResult(
    [property: JsonPropertyName("content")] string Content,
    [property: JsonPropertyName("session_id")] string SessionId,
    [property: JsonPropertyName("agent_name")] string AgentName,
    [property: JsonPropertyName("model")] string Model,
    [property: JsonPropertyName("storage_mode")] string StorageMode,
    [property: JsonPropertyName("resumed")] bool Resumed,
    [property: JsonPropertyName("tool_calls_made")] IReadOnlyList<string> ToolCallsMade);

public sealed record AgentStreamEvent(
    [property: JsonPropertyName("session_id")] string SessionId,
    [property: JsonPropertyName("agent_name")] string AgentName,
    [property: JsonPropertyName("kind")] string Kind,
    [property: JsonPropertyName("content")] string? Content,
    [property: JsonPropertyName("tool_name")] string? ToolName,
    [property: JsonPropertyName("tool_call_id")] string? ToolCallId);

public sealed record SessionInspectResult(
    [property: JsonPropertyName("has_persisted_state")] bool HasPersistedState,
    [property: JsonPropertyName("workspace_files")] IReadOnlyList<string> WorkspaceFiles,
    [property: JsonPropertyName("session_state_files")] IReadOnlyList<string> SessionStateFiles,
    [property: JsonPropertyName("transcript")] string? Transcript);

/// <summary>
/// C# facade injected into Azure Functions via `[HostedSkillInput("agent_name")] HostedSkill skill`.
/// Mirrors `src/azure_functions_agents/hosted_skill_binding.py`.
/// </summary>
public sealed class HostedSkill(string agentName, HostedSkillRuntimeClient client)
{
    public string AgentName { get; } = agentName;

    public Task<AgentRunResult> RunAsync(
        string prompt,
        string? sessionId = null,
        CancellationToken cancellationToken = default) =>
        client.RunAgentAsync(AgentName, prompt, sessionId, stream: false, onStreamEvent: null, cancellationToken);

    public async IAsyncEnumerable<AgentStreamEvent> StreamAsync(
        string prompt,
        string? sessionId = null,
        [EnumeratorCancellation] CancellationToken cancellationToken = default)
    {
        var channel = Channel.CreateUnbounded<AgentStreamEvent>();
        var runTask = Task.Run(async () =>
        {
            try
            {
                await client.RunAgentAsync(
                    AgentName,
                    prompt,
                    sessionId,
                    stream: true,
                    onStreamEvent: ev => channel.Writer.TryWrite(ev),
                    cancellationToken).ConfigureAwait(false);
                channel.Writer.TryComplete();
            }
            catch (Exception ex)
            {
                channel.Writer.TryComplete(ex);
            }
        }, cancellationToken);

        await foreach (var ev in channel.Reader.ReadAllAsync(cancellationToken).ConfigureAwait(false))
        {
            yield return ev;
        }

        await runTask.ConfigureAwait(false);
    }
}

/// <summary>
/// Manages the child `fhs-runtime` Rust process and multiplexes bi-directional JSON-RPC 2.0
/// over stdio (handling inbound `tool.call` callbacks and `agent.stream` notifications while
/// `agent.run` requests are in flight).
/// </summary>
public sealed class HostedSkillRuntimeClient : IAsyncDisposable
{
    private readonly Process _process;
    private readonly AgentToolRegistry _toolRegistry;
    private readonly SemaphoreSlim _writeLock = new(1, 1);
    private readonly ConcurrentDictionary<long, TaskCompletionSource<JsonElement>> _pending = new();
    private readonly ConcurrentBag<Action<AgentStreamEvent>> _streamListeners = [];
    private readonly Task _readerLoop;
    private long _nextId;

    private HostedSkillRuntimeClient(Process process, AgentToolRegistry toolRegistry)
    {
        _process = process;
        _toolRegistry = toolRegistry;
        _readerLoop = Task.Run(ReadStdoutLoopAsync);
    }

    public static async Task<(HostedSkillRuntimeClient Client, InitializeResult Init)> StartAsync(
        string runtimeBinaryPath,
        string appRoot,
        AgentToolRegistry toolRegistry,
        IReadOnlyDictionary<string, string>? envOverrides = null,
        bool mockLlm = false,
        CancellationToken cancellationToken = default)
    {
        var psi = new ProcessStartInfo
        {
            FileName = runtimeBinaryPath,
            UseShellExecute = false,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardInputEncoding = new UTF8Encoding(encoderShouldEmitUTF8Identifier: false),
            StandardOutputEncoding = Encoding.UTF8,
            CreateNoWindow = true
        };

        var proc = Process.Start(psi)
            ?? throw new InvalidOperationException($"Failed to start fhs-runtime at {runtimeBinaryPath}");

        var client = new HostedSkillRuntimeClient(proc, toolRegistry);
        var initResult = await client.SendRequestAsync<InitializeResult>(
            "initialize",
            new
            {
                app_root = appRoot,
                env = envOverrides ?? new Dictionary<string, string>(),
                tools = toolRegistry.Descriptors,
                mock_llm = mockLlm
            },
            cancellationToken).ConfigureAwait(false);

        return (client, initResult);
    }

    public HostedSkill GetHostedSkill(string agentName) => new(agentName, this);

    public async Task<AgentRunResult> RunAgentAsync(
        string agentName,
        string prompt,
        string? sessionId,
        bool stream,
        Action<AgentStreamEvent>? onStreamEvent,
        CancellationToken cancellationToken = default)
    {
        if (onStreamEvent is not null)
        {
            _streamListeners.Add(onStreamEvent);
        }

        return await SendRequestAsync<AgentRunResult>(
            "agent.run",
            new
            {
                agent_name = agentName,
                prompt,
                session_id = sessionId,
                stream
            },
            cancellationToken).ConfigureAwait(false);
    }

    public Task<SessionInspectResult> InspectSessionAsync(
        string agentName,
        string sessionId,
        CancellationToken cancellationToken = default) =>
        SendRequestAsync<SessionInspectResult>(
            "session.inspect",
            new
            {
                agent_name = agentName,
                session_id = sessionId
            },
            cancellationToken);

    private async Task<TResult> SendRequestAsync<TResult>(
        string method,
        object parameters,
        CancellationToken cancellationToken)
    {
        var id = Interlocked.Increment(ref _nextId);
        var tcs = new TaskCompletionSource<JsonElement>(TaskCreationOptions.RunContinuationsAsynchronously);
        _pending[id] = tcs;

        using var reg = cancellationToken.Register(() =>
        {
            if (_pending.TryRemove(id, out var removed))
            {
                removed.TrySetCanceled(cancellationToken);
            }
        });

        var frame = new
        {
            jsonrpc = "2.0",
            id,
            method,
            @params = parameters
        };
        await WriteFrameAsync(frame).ConfigureAwait(false);

        var resultElem = await tcs.Task.ConfigureAwait(false);
        return JsonSerializer.Deserialize<TResult>(resultElem.GetRawText())!;
    }

    private async Task WriteFrameAsync(object frame)
    {
        var line = JsonSerializer.Serialize(frame);
        await _writeLock.WaitAsync().ConfigureAwait(false);
        try
        {
            await _process.StandardInput.WriteLineAsync(line).ConfigureAwait(false);
            await _process.StandardInput.FlushAsync().ConfigureAwait(false);
        }
        finally
        {
            _writeLock.Release();
        }
    }

    private async Task ReadStdoutLoopAsync()
    {
        try
        {
            while (await _process.StandardOutput.ReadLineAsync().ConfigureAwait(false) is { } line)
            {
                if (string.IsNullOrWhiteSpace(line))
                {
                    continue;
                }

                using var doc = JsonDocument.Parse(line);
                var root = doc.RootElement;

                if (root.TryGetProperty("method", out var methodProp))
                {
                    var method = methodProp.GetString();
                    if (method == "tool.call" && root.TryGetProperty("id", out var callIdProp))
                    {
                        var callId = callIdProp.Clone();
                        var callParams = JsonSerializer.Deserialize<ToolCallParams>(
                            root.GetProperty("params").GetRawText())!;

                        // Dispatch tool execution on a background task so the stdio reader loop stays unblocked.
                        _ = Task.Run(async () =>
                        {
                            var toolResult = await _toolRegistry.ExecuteAsync(callParams).ConfigureAwait(false);
                            var respFrame = new
                            {
                                jsonrpc = "2.0",
                                id = callId,
                                result = toolResult
                            };
                            await WriteFrameAsync(respFrame).ConfigureAwait(false);
                        });
                    }
                    else if (method == "agent.stream" && root.TryGetProperty("params", out var streamParams))
                    {
                        var ev = JsonSerializer.Deserialize<AgentStreamEvent>(streamParams.GetRawText());
                        if (ev is not null)
                        {
                            foreach (var listener in _streamListeners)
                            {
                                listener(ev);
                            }
                        }
                    }
                }
                else if (root.TryGetProperty("id", out var idProp) && idProp.ValueKind == JsonValueKind.Number)
                {
                    var id = idProp.GetInt64();
                    if (_pending.TryRemove(id, out var tcs))
                    {
                        if (root.TryGetProperty("error", out var errProp) && errProp.ValueKind != JsonValueKind.Null)
                        {
                            var msg = errProp.GetProperty("message").GetString() ?? "Unknown RPC error";
                            tcs.TrySetException(new InvalidOperationException($"fhs-runtime RPC error: {msg}"));
                        }
                        else if (root.TryGetProperty("result", out var resProp))
                        {
                            tcs.TrySetResult(resProp.Clone());
                        }
                    }
                }
            }
        }
        catch (Exception ex)
        {
            foreach (var kv in _pending)
            {
                kv.Value.TrySetException(ex);
            }
        }
    }

    public async ValueTask DisposeAsync()
    {
        try
        {
            if (!_process.HasExited)
            {
                await SendRequestAsync<JsonElement>("shutdown", new { }, CancellationToken.None)
                    .ConfigureAwait(false);
            }
        }
        catch
        {
            // Ignore shutdown errors on disposal.
        }
        finally
        {
            if (!_process.HasExited)
            {
                _process.Kill(entireProcessTree: true);
            }
            _process.Dispose();
            _writeLock.Dispose();
            await _readerLoop.ConfigureAwait(false);
        }
    }
}
