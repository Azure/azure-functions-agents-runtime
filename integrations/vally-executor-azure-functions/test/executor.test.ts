import assert from "node:assert/strict";
import { afterEach, describe, it } from "node:test";

import {
  createExecutorRegistry,
  type ExecutorOptions,
  type Stimulus,
} from "@microsoft/vally";

import {
  AzureFunctionsAgentExecutor,
  AzureFunctionsAgentExecutorError,
  registerExecutors,
} from "../src/index.js";

const originalEnvironment = { ...process.env };

afterEach(() => {
  for (const name of Object.keys(process.env)) {
    if (!(name in originalEnvironment)) delete process.env[name];
  }
  Object.assign(process.env, originalEnvironment);
});

function stimulus(turns?: string[]): Stimulus {
  return {
    name: "receipt",
    prompt: turns ? turns.join("\n\n") : "Find the receipt total",
    ...(turns ? { turns } : {}),
  };
}

function options(
  config: unknown,
  overrides: Partial<ExecutorOptions> = {},
): ExecutorOptions {
  return {
    timeout: 5_000,
    workDir: process.cwd(),
    executorConfig: config,
    ...overrides,
  };
}

function runtimeResponse(
  sessionID: string,
  response = "The total is 42.18 USD",
  model = "gpt-test",
  toolCalls: unknown[] = [],
): Response {
  return new Response(
    JSON.stringify({
      session_id: sessionID,
      response,
      model,
      tool_calls: toolCalls,
    }),
    {
      status: 200,
      headers: {
        "content-type": "application/json",
        "x-ms-session-id": sessionID,
      },
    },
  );
}

describe("AzureFunctionsAgentExecutor", () => {
  it("registers with Vally", () => {
    const registry = createExecutorRegistry();
    registerExecutors(registry);
    assert.equal(
      registry.get("azure-functions-agent")?.supportsMultiTurn,
      true,
    );
  });

  it("fails closed on invalid or secret-bearing config", () => {
    const executor = new AzureFunctionsAgentExecutor();
    assert.throws(
      () =>
        executor.validateConfig({
          endpointUrl: "http://localhost",
          key: "secret",
        }),
      /unsupported field.*key/,
    );
    assert.throws(
      () =>
        executor.validateConfig({
          endpointUrl: "http://localhost",
          endpointUrlEnv: "URL",
        }),
      /exactly one/,
    );
    assert.throws(
      () => executor.validateConfig({ endpointUrl: "file:///tmp/agent" }),
      /HTTP or HTTPS/,
    );
    assert.throws(
      () =>
        executor.validateConfig({
          endpointUrl: "https://example.test/agents/main/chat?code=secret",
        }),
      /must not contain.*query/,
    );
  });

  it("maps multi-turn runtime evidence into a Vally trajectory", async () => {
    const requests: Array<{ url: string; init: RequestInit }> = [];
    const responses = [
      runtimeResponse("trial-1", "I found it", "gpt-4.1", [
        {
          tool_call_id: "call-1",
          tool_name: "read_receipt",
          arguments: '{"currency":"USD"}',
          turn_id: "response-0",
          result: { total: 42.18 },
          success: true,
        },
      ]),
      runtimeResponse("trial-1", "The total is 42.18 USD", "gpt-4.1"),
    ];
    const executor = new AzureFunctionsAgentExecutor({
      fetch: async (url, init) => {
        requests.push({ url: String(url), init: init ?? {} });
        const response = responses.shift();
        assert.ok(response);
        return response;
      },
    });

    const trajectory = await executor.execute(
      stimulus(["Read the receipt", "What was the total?"]),
      options(
        { endpointUrl: "http://127.0.0.1:7071/api/agents/main/chat" },
        { sessionID: "trial-1" },
      ),
    );

    assert.equal(requests.length, 2);
    assert.equal(
      requests[0]?.url,
      "http://127.0.0.1:7071/api/agents/main/chat",
    );
    assert.equal(requests[0]?.init.method, "POST");
    assert.equal(
      new Headers(requests[0]?.init.headers).get("content-type"),
      "application/json",
    );
    assert.equal(
      new Headers(requests[0]?.init.headers).get("x-ms-session-id"),
      "trial-1",
    );
    assert.deepEqual(JSON.parse(String(requests[0]?.init.body)), {
      prompt: "Read the receipt",
    });
    assert.equal(trajectory.output, "The total is 42.18 USD");
    assert.equal(trajectory.metadata.model, "gpt-4.1");
    assert.equal(trajectory.metadata.sessionID, "trial-1");
    assert.ok(trajectory.metrics.wallTimeMs >= 0);
    assert.equal(trajectory.metrics.turnCount, 2);
    assert.equal(trajectory.metrics.toolCallCount, 1);
    assert.equal(trajectory.metrics.skillActivationCount, 0);
    assert.equal(trajectory.metrics.tokenUsage.inputTokens, 0);
    assert.equal(trajectory.metrics.tokenUsage.outputTokens, 0);
    assert.equal(trajectory.metrics.tokenUsage.totalTokens, 0);
    assert.equal(trajectory.metrics.tokenUsage.cacheReadTokens, 0);
    assert.equal(trajectory.metrics.tokenUsage.cacheWriteTokens, 0);
    assert.equal(trajectory.metrics.tokenUsage.callCount, 0);
    assert.deepEqual(Object.keys(trajectory.metrics.tokenUsage.byModel), []);
    assert.deepEqual(
      trajectory.events.map((event) => event.type),
      [
        "turn_start",
        "user_message",
        "tool_call",
        "tool_result",
        "assistant_message",
        "turn_end",
        "turn_start",
        "user_message",
        "assistant_message",
        "turn_end",
      ],
    );
    const toolCall = trajectory.events.find(
      (event) => event.type === "tool_call",
    );
    assert.deepEqual(toolCall?.data, {
      toolName: "read_receipt",
      toolCallId: "call-1",
      arguments: { currency: "USD" },
      turnId: "response-0",
    });
    const toolResult = trajectory.events.find(
      (event) => event.type === "tool_result",
    );
    assert.deepEqual(toolResult?.data, {
      toolName: "read_receipt",
      toolCallId: "call-1",
      success: true,
      result: { total: 42.18 },
    });
    assert.equal(
      new Headers(requests[1]?.init.headers).get("x-ms-session-id"),
      "trial-1",
    );
  });

  it("uses a function key without exposing it in the trajectory", async () => {
    process.env.AGENT_ENDPOINT = "https://example.test/api/agents/main/chat";
    process.env.AGENT_FUNCTION_KEY = "top-secret-key";
    let headers = new Headers();
    const executor = new AzureFunctionsAgentExecutor({
      fetch: async (_url, init) => {
        headers = new Headers(init?.headers);
        return runtimeResponse("trial-key");
      },
    });

    const trajectory = await executor.execute(
      stimulus(),
      options(
        {
          endpointUrlEnv: "AGENT_ENDPOINT",
          auth: { type: "function-key", keyEnv: "AGENT_FUNCTION_KEY" },
        },
        { sessionID: "trial-key" },
      ),
    );

    assert.equal(headers.get("x-functions-key"), "top-secret-key");
    assert.doesNotMatch(JSON.stringify(trajectory), /top-secret-key/);
  });

  it("uses an Entra credential and closes an owned dependency", async () => {
    process.env.AGENT_SCOPE = "api://agent/.default";
    let closed = false;
    const credential = {
      async getToken(scope: string | string[]) {
        assert.equal(scope, "api://agent/.default");
        return {
          token: "bearer-secret",
          expiresOnTimestamp: Date.now() + 60_000,
        };
      },
      async close() {
        closed = true;
      },
    };
    let headers = new Headers();
    const executor = new AzureFunctionsAgentExecutor({
      credential,
      fetch: async (_url, init) => {
        headers = new Headers(init?.headers);
        return runtimeResponse("trial-entra");
      },
    });

    const trajectory = await executor.execute(
      stimulus(),
      options(
        {
          endpointUrl: "https://example.test/api/agents/main/chat",
          auth: { type: "entra", scopeEnv: "AGENT_SCOPE" },
        },
        { sessionID: "trial-entra" },
      ),
    );
    await executor.shutdown();
    await executor.shutdown();

    assert.equal(headers.get("authorization"), "Bearer bearer-secret");
    assert.doesNotMatch(JSON.stringify(trajectory), /bearer-secret/);
    assert.equal(closed, true);
  });

  it("rejects response session mismatches", async () => {
    const executor = new AzureFunctionsAgentExecutor({
      fetch: async () => runtimeResponse("wrong-session"),
    });

    await assert.rejects(
      executor.execute(
        stimulus(),
        options(
          { endpointUrl: "https://example.test/api/agents/main/chat" },
          { sessionID: "expected" },
        ),
      ),
      (error: unknown) =>
        error instanceof AzureFunctionsAgentExecutorError &&
        error.code === "session",
    );

    const missingHeader = new AzureFunctionsAgentExecutor({
      fetch: async () =>
        new Response(
          JSON.stringify({
            session_id: "expected",
            response: "ok",
            model: "gpt-test",
            tool_calls: [],
          }),
          { headers: { "content-type": "application/json" } },
        ),
    });
    await assert.rejects(
      missingHeader.execute(
        stimulus(),
        options(
          { endpointUrl: "https://example.test/api/agents/main/chat" },
          { sessionID: "expected" },
        ),
      ),
      (error: unknown) =>
        error instanceof AzureFunctionsAgentExecutorError &&
        error.code === "session",
    );
  });

  it("classifies authentication and unsupported options", async () => {
    const executor = new AzureFunctionsAgentExecutor({
      fetch: async () => new Response("denied", { status: 401 }),
    });

    await assert.rejects(
      executor.execute(
        stimulus(),
        options({ endpointUrl: "https://example.test/api/agents/main/chat" }),
      ),
      (error: unknown) =>
        error instanceof AzureFunctionsAgentExecutorError &&
        error.code === "authentication",
    );
    await assert.rejects(
      executor.execute(
        stimulus(),
        options(
          { endpointUrl: "https://example.test/api/agents/main/chat" },
          { model: "override" },
        ),
      ),
      (error: unknown) =>
        error instanceof AzureFunctionsAgentExecutorError &&
        error.code === "unsupported",
    );
    await assert.rejects(
      executor.execute(
        stimulus(),
        options(
          { endpointUrl: "https://example.test/api/agents/main/chat" },
          { sessionID: "invalid/session" },
        ),
      ),
      (error: unknown) =>
        error instanceof AzureFunctionsAgentExecutorError &&
        error.code === "session",
    );
    await assert.rejects(
      executor.execute(
        stimulus(),
        options(
          { endpointUrl: "https://example.test/api/agents/main/chat" },
          {
            skills: [
              {
                name: "unsupported",
                description: "unsupported skill",
                path: "/tmp/skill",
                rawContent: "",
                frontmatter: {},
                fileReferences: [],
              },
            ],
          },
        ),
      ),
      (error: unknown) =>
        error instanceof AzureFunctionsAgentExecutorError &&
        error.code === "unsupported",
    );
  });

  it("classifies transport, HTTP, and malformed response failures", async () => {
    const cases: Array<{
      fetch: typeof globalThis.fetch;
      code: AzureFunctionsAgentExecutorError["code"];
    }> = [
      {
        fetch: async () => {
          throw new Error("network secret should not escape");
        },
        code: "transport",
      },
      {
        fetch: async () => new Response("failed", { status: 500 }),
        code: "http",
      },
      {
        fetch: async () => new Response("denied", { status: 403 }),
        code: "authentication",
      },
      { fetch: async () => new Response("not-json"), code: "response" },
      {
        fetch: async () =>
          new Response(
            JSON.stringify({ session_id: "trial", response: "ok" }),
            {
              headers: { "x-ms-session-id": "trial" },
            },
          ),
        code: "response",
      },
    ];

    for (const testCase of cases) {
      const executor = new AzureFunctionsAgentExecutor({
        fetch: testCase.fetch,
      });
      await assert.rejects(
        executor.execute(
          stimulus(),
          options(
            { endpointUrl: "https://example.test/api/agents/main/chat" },
            { sessionID: "trial" },
          ),
        ),
        (error: unknown) =>
          error instanceof AzureFunctionsAgentExecutorError &&
          error.code === testCase.code &&
          !error.message.includes("network secret"),
      );
    }
  });

  it("maps legacy evidence and rejects malformed success values", async () => {
    const legacy = new AzureFunctionsAgentExecutor({
      fetch: async () =>
        runtimeResponse("legacy", "ok", "", [
          { tool_name: "legacy", arguments: "[1,true]", result: null },
        ]),
    });
    const trajectory = await legacy.execute(
      stimulus(),
      options(
        { endpointUrl: "https://example.test/api/agents/main/chat" },
        { sessionID: "legacy" },
      ),
    );
    assert.equal(trajectory.metadata.model, "unknown");
    assert.deepEqual(
      trajectory.events.find((event) => event.type === "tool_call")?.data,
      {
        toolName: "legacy",
        toolCallId: "turn-0-call-0",
        arguments: [1, true],
      },
    );
    assert.equal(
      trajectory.events.find((event) => event.type === "tool_result")?.data
        .success,
      true,
    );

    const malformed = new AzureFunctionsAgentExecutor({
      fetch: async () =>
        runtimeResponse("malformed", "ok", "gpt-test", [
          { tool_name: "bad", result: "failed", success: "false" },
        ]),
    });
    await assert.rejects(
      malformed.execute(
        stimulus(),
        options(
          { endpointUrl: "https://example.test/api/agents/main/chat" },
          { sessionID: "malformed" },
        ),
      ),
      (error: unknown) =>
        error instanceof AzureFunctionsAgentExecutorError &&
        error.code === "response",
    );
  });

  it("isolates concurrent executions with generated sessions", async () => {
    const sessions: string[] = [];
    const executor = new AzureFunctionsAgentExecutor({
      fetch: async (_url, init) => {
        const sessionID = new Headers(init?.headers).get("x-ms-session-id");
        assert.ok(sessionID);
        sessions.push(sessionID);
        return runtimeResponse(sessionID);
      },
    });

    const [first, second] = await Promise.all([
      executor.execute(
        stimulus(),
        options({ endpointUrl: "https://example.test/api/agents/main/chat" }),
      ),
      executor.execute(
        stimulus(),
        options({ endpointUrl: "https://example.test/api/agents/main/chat" }),
      ),
    ]);

    assert.equal(sessions.length, 2);
    assert.notEqual(sessions[0], sessions[1]);
    assert.notEqual(first.metadata.sessionID, second.metadata.sessionID);
  });

  it("enforces one hard timeout across requests", async () => {
    const executor = new AzureFunctionsAgentExecutor({
      fetch: async (_url, init) =>
        await new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener(
            "abort",
            () => reject(new Error("aborted")),
            { once: true },
          );
        }),
    });

    await assert.rejects(
      executor.execute(
        stimulus(),
        options(
          { endpointUrl: "https://example.test/api/agents/main/chat" },
          { timeout: 5 },
        ),
      ),
      (error: unknown) =>
        error instanceof AzureFunctionsAgentExecutorError &&
        error.code === "timeout",
    );
  });
});
