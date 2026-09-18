import { DefaultAzureCredential, type TokenCredential } from "@azure/identity";
import {
  computeMetrics,
  stimulusPrompts,
  type Executor,
  type ExecutorOptions,
  type ExecutorRegistry,
  type Stimulus,
  type ToolCallArguments,
  type Trajectory,
  type TrajectoryEvent,
} from "@microsoft/vally";
import { randomUUID } from "node:crypto";

import {
  parseExecutorConfig,
  resolveExecutorConfig,
  type AzureFunctionsAgentExecutorConfig,
} from "./config.js";

export type {
  AzureFunctionsAgentAuth,
  AzureFunctionsAgentExecutorConfig,
} from "./config.js";

export type AzureFunctionsAgentExecutorErrorCode =
  | "authentication"
  | "configuration"
  | "http"
  | "response"
  | "session"
  | "timeout"
  | "transport"
  | "unsupported";

export class AzureFunctionsAgentExecutorError extends Error {
  constructor(
    readonly code: AzureFunctionsAgentExecutorErrorCode,
    message: string,
  ) {
    super(message);
    this.name = "AzureFunctionsAgentExecutorError";
  }
}

export interface AzureFunctionsAgentExecutorDependencies {
  fetch?: typeof globalThis.fetch;
  credential?: TokenCredential & { close?: () => Promise<void> };
}

interface RuntimeToolCall {
  tool_call_id?: unknown;
  tool_name?: unknown;
  arguments?: unknown;
  turn_id?: unknown;
  result?: unknown;
  success?: unknown;
}

interface RuntimeResponse {
  session_id: string;
  response: string;
  model: string;
  tool_calls: RuntimeToolCall[];
}

const SESSION_ID_PATTERN = /^[A-Za-z0-9._-]{1,128}$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseRuntimeResponse(value: unknown): RuntimeResponse {
  if (!isRecord(value)) {
    throw new AzureFunctionsAgentExecutorError(
      "response",
      "chat response must be an object",
    );
  }
  if (
    typeof value.session_id !== "string" ||
    !SESSION_ID_PATTERN.test(value.session_id)
  ) {
    throw new AzureFunctionsAgentExecutorError(
      "response",
      "chat response contains an invalid session_id",
    );
  }
  if (typeof value.response !== "string") {
    throw new AzureFunctionsAgentExecutorError(
      "response",
      "chat response must contain a string response",
    );
  }
  if (!Array.isArray(value.tool_calls)) {
    throw new AzureFunctionsAgentExecutorError(
      "response",
      "chat response must contain a tool_calls array",
    );
  }
  const model =
    typeof value.model === "string" && value.model.trim()
      ? value.model
      : "unknown";
  return {
    session_id: value.session_id,
    response: value.response,
    model,
    tool_calls: value.tool_calls.map((toolCall, index) => {
      if (!isRecord(toolCall)) {
        throw new AzureFunctionsAgentExecutorError(
          "response",
          `chat response tool_calls[${index}] must be an object`,
        );
      }
      return toolCall;
    }),
  };
}

function parseToolArguments(value: unknown): ToolCallArguments | undefined {
  if (value === undefined) {
    return undefined;
  }
  if (typeof value === "string") {
    try {
      return JSON.parse(value) as ToolCallArguments;
    } catch {
      return value;
    }
  }
  if (
    value === null ||
    typeof value === "number" ||
    typeof value === "boolean" ||
    Array.isArray(value) ||
    isRecord(value)
  ) {
    return value as ToolCallArguments;
  }
  return String(value);
}

function validateUnsupportedOptions(options: ExecutorOptions): void {
  const unsupported: string[] = [];
  if (options.skills?.length) unsupported.push("skills");
  if (options.mcpServers && Object.keys(options.mcpServers).length)
    unsupported.push("MCP servers");
  if (options.env && Object.keys(options.env).length)
    unsupported.push("agent environment variables");
  if (options.model !== undefined) unsupported.push("model override");
  if (options.reasoningEffort !== undefined)
    unsupported.push("reasoning effort");
  if (options.maxAgentDurationMs !== undefined)
    unsupported.push("max_agent_duration");
  if (unsupported.length) {
    throw new AzureFunctionsAgentExecutorError(
      "unsupported",
      `azure-functions-agent does not support: ${unsupported.join(", ")}`,
    );
  }
}

function sessionIdFrom(options: ExecutorOptions): string {
  const sessionID = options.sessionID ?? randomUUID();
  if (!SESSION_ID_PATTERN.test(sessionID)) {
    throw new AzureFunctionsAgentExecutorError(
      "session",
      "executor sessionID must match [A-Za-z0-9._-]{1,128}",
    );
  }
  return sessionID;
}

function appendTurnEvents(
  events: TrajectoryEvent[],
  turn: number,
  prompt: string,
  runtime: RuntimeResponse,
): void {
  const configuredTurnId = `turn-${turn}`;
  events.push({
    type: "turn_start",
    turn,
    timestamp: new Date(),
    data: { turnId: configuredTurnId },
  });
  events.push({
    type: "user_message",
    turn,
    timestamp: new Date(),
    data: { content: prompt },
  });

  runtime.tool_calls.forEach((call, callIndex) => {
    if (typeof call.tool_name !== "string" || !call.tool_name.trim()) {
      throw new AzureFunctionsAgentExecutorError(
        "response",
        `chat response tool_calls[${callIndex}] must contain a tool_name`,
      );
    }
    const toolCallId =
      typeof call.tool_call_id === "string" && call.tool_call_id.trim()
        ? call.tool_call_id
        : `turn-${turn}-call-${callIndex}`;
    const toolData: Extract<TrajectoryEvent, { type: "tool_call" }>["data"] = {
      toolName: call.tool_name,
      toolCallId,
    };
    const argumentsValue = parseToolArguments(call.arguments);
    if (argumentsValue !== undefined) toolData.arguments = argumentsValue;
    if (typeof call.turn_id === "string" && call.turn_id.trim())
      toolData.turnId = call.turn_id;
    events.push({
      type: "tool_call",
      turn,
      timestamp: new Date(),
      data: toolData,
    });

    if (Object.hasOwn(call, "result")) {
      if (Object.hasOwn(call, "success") && typeof call.success !== "boolean") {
        throw new AzureFunctionsAgentExecutorError(
          "response",
          `chat response tool_calls[${callIndex}].success must be a boolean`,
        );
      }
      const success: boolean =
        typeof call.success === "boolean" ? call.success : true;
      events.push({
        type: "tool_result",
        turn,
        timestamp: new Date(),
        data: {
          toolName: call.tool_name,
          toolCallId,
          success,
          result: call.result,
        },
      });
    }
  });

  events.push({
    type: "assistant_message",
    turn,
    timestamp: new Date(),
    data: { content: runtime.response },
  });
  events.push({
    type: "turn_end",
    turn,
    timestamp: new Date(),
    data: { turnId: configuredTurnId },
  });
}

export class AzureFunctionsAgentExecutor implements Executor {
  readonly name = "azure-functions-agent";
  readonly supportsMultiTurn = true;

  readonly #fetch: typeof globalThis.fetch;
  #credential: (TokenCredential & { close?: () => Promise<void> }) | undefined;

  constructor(dependencies: AzureFunctionsAgentExecutorDependencies = {}) {
    this.#fetch = dependencies.fetch ?? globalThis.fetch;
    this.#credential = dependencies.credential;
  }

  validateConfig(config: unknown): void {
    parseExecutorConfig(config);
  }

  async execute(
    stimulus: Stimulus,
    options: ExecutorOptions,
  ): Promise<Trajectory> {
    validateUnsupportedOptions(options);
    if (!Number.isFinite(options.timeout) || options.timeout <= 0) {
      throw new AzureFunctionsAgentExecutorError(
        "configuration",
        "executor timeout must be positive",
      );
    }

    let parsedConfig: AzureFunctionsAgentExecutorConfig;
    try {
      parsedConfig = parseExecutorConfig(options.executorConfig);
    } catch (error) {
      throw new AzureFunctionsAgentExecutorError(
        "configuration",
        error instanceof Error ? error.message : "invalid executor config",
      );
    }
    let config: ReturnType<typeof resolveExecutorConfig>;
    try {
      config = resolveExecutorConfig(parsedConfig);
    } catch (error) {
      throw new AzureFunctionsAgentExecutorError(
        "configuration",
        error instanceof Error ? error.message : "invalid executor config",
      );
    }

    const sessionID = sessionIdFrom(options);
    const events: TrajectoryEvent[] = [];
    const prompts = stimulusPrompts(stimulus);
    const startedAt = new Date();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), options.timeout);
    let output = "";
    let model = "unknown";

    try {
      const authHeaders = await this.#authHeaders(
        config.auth,
        controller.signal,
      );
      for (let turn = 0; turn < prompts.length; turn += 1) {
        const prompt = prompts[turn];
        if (prompt === undefined) continue;
        const response = await this.#request(
          config.endpointUrl,
          prompt,
          sessionID,
          authHeaders,
          controller.signal,
        );
        appendTurnEvents(events, turn, prompt, response);
        output = response.response;
        if (response.model !== "unknown") model = response.model;
      }
    } catch (error) {
      if (controller.signal.aborted) {
        throw new AzureFunctionsAgentExecutorError(
          "timeout",
          `agent evaluation timed out after ${options.timeout}ms`,
        );
      }
      if (error instanceof AzureFunctionsAgentExecutorError) throw error;
      throw new AzureFunctionsAgentExecutorError(
        "transport",
        "chat request failed",
      );
    } finally {
      clearTimeout(timer);
    }

    const completedAt = new Date();
    const metrics = computeMetrics(events);
    metrics.wallTimeMs = completedAt.getTime() - startedAt.getTime();
    return {
      id: randomUUID(),
      stimulus,
      events,
      metrics,
      output,
      workDir: options.workDir,
      endReason: "completed",
      metadata: {
        model,
        skillsLoaded: [],
        startedAt,
        completedAt,
        executor: this.name,
        sessionID,
      },
    };
  }

  async shutdown(): Promise<void> {
    const credential = this.#credential;
    this.#credential = undefined;
    await credential?.close?.();
  }

  async #authHeaders(
    auth: ReturnType<typeof resolveExecutorConfig>["auth"],
    signal: AbortSignal,
  ): Promise<Record<string, string>> {
    if (auth.type === "anonymous") return {};
    if (auth.type === "function-key") return { "x-functions-key": auth.key };

    this.#credential ??= new DefaultAzureCredential();
    try {
      const token = await this.#credential.getToken(auth.scope, {
        abortSignal: signal,
      });
      if (!token?.token) throw new Error("empty token");
      return { authorization: `Bearer ${token.token}` };
    } catch {
      throw new AzureFunctionsAgentExecutorError(
        "authentication",
        "Entra credential could not acquire an access token",
      );
    }
  }

  async #request(
    endpoint: URL,
    prompt: string,
    sessionID: string,
    authHeaders: Record<string, string>,
    signal: AbortSignal,
  ): Promise<RuntimeResponse> {
    let response: Response;
    try {
      response = await this.#fetch(endpoint, {
        method: "POST",
        headers: {
          accept: "application/json",
          "content-type": "application/json",
          "x-ms-session-id": sessionID,
          ...authHeaders,
        },
        body: JSON.stringify({ prompt }),
        signal,
      });
    } catch {
      throw new AzureFunctionsAgentExecutorError(
        "transport",
        "chat request failed",
      );
    }

    if (!response.ok) {
      const code =
        response.status === 401 || response.status === 403
          ? "authentication"
          : "http";
      throw new AzureFunctionsAgentExecutorError(
        code,
        `chat endpoint returned HTTP ${response.status}`,
      );
    }

    let value: unknown;
    try {
      value = await response.json();
    } catch {
      throw new AzureFunctionsAgentExecutorError(
        "response",
        "chat endpoint returned invalid JSON",
      );
    }
    const runtime = parseRuntimeResponse(value);
    const headerSession = response.headers.get("x-ms-session-id");
    if (runtime.session_id !== sessionID || headerSession !== sessionID) {
      throw new AzureFunctionsAgentExecutorError(
        "session",
        `chat endpoint session mismatch (expected ${sessionID}, body ${runtime.session_id}, header ${headerSession ?? "missing"})`,
      );
    }
    return runtime;
  }
}

export function registerExecutors(registry: ExecutorRegistry): void {
  registry.register(new AzureFunctionsAgentExecutor());
}
