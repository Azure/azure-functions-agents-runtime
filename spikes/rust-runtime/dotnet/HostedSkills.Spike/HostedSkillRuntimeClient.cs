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
/// Spike facade supplied through ordinary worker DI, not a custom Functions binding.
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
        using var lifetime = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
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
                    lifetime.Token).ConfigureAwait(false);
                channel.Writer.TryComplete();
            }
            catch (Exception ex)
            {
                channel.Writer.TryComplete(ex);
            }
        });

        try
        {
            await foreach (var ev in channel.Reader.ReadAllAsync(cancellationToken).ConfigureAwait(false))
            {
                yield return ev;
            }
        }
        finally
        {
            lifetime.Cancel();
            await runTask.ConfigureAwait(false);
        }
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
    private readonly ConcurrentDictionary<(string Agent, string Session), Action<AgentStreamEvent>> _streamListeners = new();
    // Rust SessionFs does not provide transactional concurrent turns. Serialize runs
    // in this worker, including streams, rather than implying multi-writer safety.
    private readonly SemaphoreSlim _runLock = new(1, 1);
    private readonly Task _readerLoop;
    private readonly Task _stderrLoop;
    private readonly ConcurrentDictionary<long, Task> _callbacks = new();
    private Exception? _terminalError;
    private int _disposed;
    private long _nextId;
    private long _nextCallbackId;

    private HostedSkillRuntimeClient(Process process, AgentToolRegistry toolRegistry)
    {
        _process = process;
        _toolRegistry = toolRegistry;
        _readerLoop = Task.Run(ReadStdoutLoopAsync);
        _stderrLoop = Task.Run(async () =>
        {
            while (await process.StandardError.ReadLineAsync().ConfigureAwait(false) is { } line)
                Console.Error.WriteLine($"[fhs-runtime] {line}");
        });
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
        try
        {
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
        catch
        {
            await client.DisposeAsync().ConfigureAwait(false);
            throw;
        }
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
        await _runLock.WaitAsync(cancellationToken).ConfigureAwait(false);
        sessionId ??= Guid.NewGuid().ToString("N");
        var key = (agentName, sessionId);
        try
        {
            if (onStreamEvent is not null) _streamListeners[key] = onStreamEvent;
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
        catch (OperationCanceledException)
        {
            // The spike protocol has no cancellation RPC. Stop this child rather than
            // allowing a timed-out turn to overlap the next writer to SessionFs.
            FailPending(new IOException("Rust turn was canceled; restart the worker before running more turns."));
            StopChild();
            throw;
        }
        finally
        {
            _streamListeners.TryRemove(key, out _);
            _runLock.Release();
        }
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
        if (Volatile.Read(ref _disposed) != 0 && method != "shutdown")
            throw new ObjectDisposedException(nameof(HostedSkillRuntimeClient));
        var id = Interlocked.Increment(ref _nextId);
        var tcs = new TaskCompletionSource<JsonElement>(TaskCreationOptions.RunContinuationsAsynchronously);
        _pending[id] = tcs;

        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        deadline.CancelAfter(TimeSpan.FromSeconds(45));
        try
        {
            if (Volatile.Read(ref _terminalError) is { } error) throw error;
            var frame = new
            {
                jsonrpc = "2.0",
                id,
                method,
                @params = parameters
            };
            await WriteFrameAsync(frame, deadline.Token).ConfigureAwait(false);
            var resultElem = await tcs.Task.WaitAsync(deadline.Token).ConfigureAwait(false);
            return JsonSerializer.Deserialize<TResult>(resultElem.GetRawText())!;
        }
        catch (IOException ex)
        {
            FailPending(ex);
            throw;
        }
        finally
        {
            _pending.TryRemove(id, out _);
        }
    }

    private async Task WriteFrameAsync(object frame, CancellationToken cancellationToken = default)
    {
        var line = JsonSerializer.Serialize(frame);
        await _writeLock.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            await _process.StandardInput.WriteLineAsync(line.AsMemory(), cancellationToken).ConfigureAwait(false);
            await _process.StandardInput.FlushAsync(cancellationToken).ConfigureAwait(false);
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

                        var callbackId = Interlocked.Increment(ref _nextCallbackId);
                        var callback = Task.Run(async () =>
                        {
                            try
                            {
                                var toolResult = await _toolRegistry.ExecuteAsync(callParams).ConfigureAwait(false);
                                using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(5));
                                await WriteFrameAsync(new
                                {
                                    jsonrpc = "2.0",
                                    id = callId,
                                    result = toolResult
                                }, deadline.Token).ConfigureAwait(false);
                            }
                            catch (Exception ex)
                            {
                                FailPending(ex);
                            }
                        });
                        _callbacks[callbackId] = callback;
                        _ = callback.ContinueWith(_ => { _callbacks.TryRemove(callbackId, out var ignored); },
                            TaskScheduler.Default);
                    }
                    else if (method == "agent.stream" && root.TryGetProperty("params", out var streamParams))
                    {
                        var ev = JsonSerializer.Deserialize<AgentStreamEvent>(streamParams.GetRawText());
                        if (ev is not null)
                        {
                            if (_streamListeners.TryGetValue((ev.AgentName, ev.SessionId), out var listener))
                                listener(ev);
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
                        else
                        {
                            tcs.TrySetException(new InvalidDataException("RPC response has neither result nor error."));
                        }
                    }
                }
            }
        }
        catch (Exception ex)
        {
            FailPending(ex);
        }
        finally
        {
            FailPending(new EndOfStreamException("fhs-runtime stdout closed; the child process is unavailable."));
        }
    }

    private void FailPending(Exception error)
    {
        Interlocked.CompareExchange(ref _terminalError, error, null);
        foreach (var (id, pending) in _pending)
            if (_pending.TryRemove(id, out _)) pending.TrySetException(error);
    }

    private void StopChild()
    {
        try
        {
            if (!_process.HasExited) _process.Kill(entireProcessTree: true);
        }
        catch (InvalidOperationException)
        {
            // Another shutdown path already disposed or reaped this child.
        }
    }

    public async ValueTask DisposeAsync()
    {
        if (Interlocked.Exchange(ref _disposed, 1) != 0) return;
        try
        {
            if (!_process.HasExited)
            {
                using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(3));
                await SendRequestAsync<JsonElement>("shutdown", new { }, deadline.Token)
                    .ConfigureAwait(false);
                _process.StandardInput.Close();
                await _process.WaitForExitAsync(deadline.Token).ConfigureAwait(false);
            }
        }
        catch
        {
            // Ignore shutdown errors on disposal.
        }
        finally
        {
            StopChild();
            FailPending(new ObjectDisposedException(nameof(HostedSkillRuntimeClient)));
            try
            {
                await Task.WhenAll(_readerLoop, _stderrLoop, Task.WhenAll(_callbacks.Values))
                    .WaitAsync(TimeSpan.FromSeconds(3)).ConfigureAwait(false);
            }
            catch (Exception)
            {
                // Shutdown is bounded even if a user tool fails to finish.
            }
            _process.Dispose();
        }
    }
}
