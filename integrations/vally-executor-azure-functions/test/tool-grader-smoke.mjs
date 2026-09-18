import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const packageRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);
const pluginPath = path.join(packageRoot, "dist", "index.js");
const cliPath = path.join(
  packageRoot,
  "node_modules",
  "@microsoft",
  "vally-cli",
  "dist",
  "index.js",
);
const temporaryRoot = await mkdtemp(path.join(tmpdir(), "vally-tool-smoke-"));
const rememberedCodes = new Map();

const server = createServer((request, response) => {
  let body = "";
  request.setEncoding("utf8");
  request.on("data", (chunk) => {
    body += chunk;
  });
  request.on("end", () => {
    const sessionID = request.headers["x-ms-session-id"];
    const prompt = JSON.parse(body).prompt;
    if (prompt.includes("Remember confirmation code")) {
      rememberedCodes.set(sessionID, "BLUE-ORBIT-731");
    }
    const responseText = prompt.includes("What confirmation code")
      ? request.url?.startsWith("/reset/")
        ? "I do not know."
        : (rememberedCodes.get(sessionID) ?? "I do not know.")
      : prompt.includes("Remember confirmation code")
        ? "OK"
        : "The receipt total is 42.18 USD.";
    const base = {
      session_id: sessionID,
      response: responseText,
      model: "smoke-model",
    };
    const complete = {
      tool_call_id: "call-1",
      tool_name: "read_receipt",
      arguments: { currency: "USD" },
      turn_id: "response-0",
      result: { total: 42.18 },
      success: true,
    };
    const tool_calls = prompt.includes("confirmation code")
      ? []
      : prompt === "missing"
        ? []
        : prompt === "uncompleted"
          ? [{ ...complete, result: undefined, success: undefined }]
          : prompt === "wrong-name"
            ? [{ ...complete, tool_name: "other" }]
            : prompt === "wrong-argument"
              ? [{ ...complete, arguments: { currency: "EUR" } }]
              : prompt === "wrong-result"
                ? [{ ...complete, result: { total: 0 } }]
                : prompt === "parallel"
                  ? [complete, { ...complete, tool_call_id: "call-2" }]
                  : prompt === "sequential"
                    ? [
                        complete,
                        {
                          ...complete,
                          tool_call_id: "call-2",
                          turn_id: "response-1",
                        },
                      ]
                    : [complete];
    response.writeHead(200, {
      "content-type": "application/json",
      "x-ms-session-id": sessionID,
    });
    response.end(JSON.stringify({ ...base, tool_calls }));
  });
});

function runVally(evalSpec, outputDir, environment = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(
      process.execPath,
      [
        cliPath,
        "eval",
        "--eval-spec",
        evalSpec,
        "--executor-plugin",
        pluginPath,
        "--workers",
        "1",
        "--require-pass",
        "--output-dir",
        outputDir,
      ],
      {
        cwd: packageRoot,
        env: {
          ...process.env,
          ...environment,
          VALLY_TELEMETRY_OPTOUT: "1",
        },
        stdio: ["ignore", "pipe", "pipe"],
      },
    );
    let output = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      output += chunk;
    });
    child.stderr.on("data", (chunk) => {
      output += chunk;
    });
    child.once("error", reject);
    child.once("close", (code) => resolve({ code, output }));
  });
}

function yaml(endpoint, prompt, config) {
  return `name: tool-grader-smoke
defaults:
  executor:
    name: azure-functions-agent
    config:
      endpointUrl: "${endpoint}"
  runs: 1
  timeout: 10s
stimuli:
  - name: tool-check
    prompt: ${prompt}
    graders:
      - type: tool-calls
        config:
${config}
`;
}

try {
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  assert.ok(typeof address === "object" && address !== null);
  const endpoint = `http://127.0.0.1:${address.port}/agents/main/chat`;
  const required = `          required:
            - name: ^read_receipt$
              args:
                currency: ^USD$
              result: '"total":42\\.18'`;
  const parallel = `          parallel:
            - ^read_receipt$
            - ^read_receipt$`;
  const cases = [
    ["complete", required, true],
    ["missing", required, false],
    ["uncompleted", required, false],
    ["wrong-name", required, false],
    ["wrong-argument", required, false],
    ["wrong-result", required, false],
    ["parallel", parallel, true],
    ["sequential", parallel, false],
  ];

  for (const [name, config, shouldPass] of cases) {
    const evalSpec = path.join(temporaryRoot, `${name}.yaml`);
    await writeFile(evalSpec, yaml(endpoint, name, config), "utf8");
    const result = await runVally(evalSpec, path.join(temporaryRoot, name));
    assert.equal(result.code === 0, shouldPass, `${name}: ${result.output}`);
  }

  const sampleSpec = path.resolve(
    packageRoot,
    "..",
    "..",
    "samples",
    "agent-evaluation",
    "eval.yaml",
  );
  const sample = await runVally(
    sampleSpec,
    path.join(temporaryRoot, "sample"),
    { AGENT_EVAL_TARGET_URL: endpoint },
  );
  assert.equal(sample.code, 0, sample.output);

  const resetSample = await runVally(
    sampleSpec,
    path.join(temporaryRoot, "sample-reset"),
    {
      AGENT_EVAL_TARGET_URL: endpoint.replace(
        "/agents/main/chat",
        "/reset/agents/main/chat",
      ),
    },
  );
  assert.notEqual(resetSample.code, 0, resetSample.output);
  assert.match(resetSample.output, /receipt-memory/);
} finally {
  await new Promise((resolve) => server.close(resolve));
  await rm(temporaryRoot, { recursive: true, force: true });
}
