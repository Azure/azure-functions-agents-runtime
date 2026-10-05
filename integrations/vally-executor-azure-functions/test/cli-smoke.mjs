import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
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
const temporaryRoot = await mkdtemp(
  path.join(tmpdir(), "vally-azure-functions-smoke-"),
);

const server = createServer((request, response) => {
  let body = "";
  request.setEncoding("utf8");
  request.on("data", (chunk) => {
    body += chunk;
  });
  request.on("end", () => {
    const sessionID = request.headers["x-ms-session-id"];
    const prompt = JSON.parse(body).prompt;
    response.writeHead(200, {
      "content-type": "application/json",
      "x-ms-session-id": sessionID,
    });
    response.end(
      JSON.stringify({
        session_id: sessionID,
        response: `echo:${prompt}`,
        model: "smoke-model",
        tool_calls: [],
      }),
    );
  });
});

function runVally(evalSpec, outputDir, requirePass = true) {
  const args = [
    cliPath,
    "eval",
    "--eval-spec",
    evalSpec,
    "--executor-plugin",
    pluginPath,
    "--workers",
    "1",
    "--junit",
    "--output-dir",
    outputDir,
  ];
  if (requirePass) args.push("--require-pass");
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, args, {
      cwd: packageRoot,
      env: { ...process.env, VALLY_TELEMETRY_OPTOUT: "1" },
      stdio: ["ignore", "pipe", "pipe"],
    });
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

async function artifactNames(root) {
  const entries = await readdir(root, { recursive: true });
  return new Set(entries.map((entry) => path.basename(entry)));
}

async function findArtifact(root, name) {
  const entries = await readdir(root, { recursive: true });
  const entry = entries.find((candidate) => path.basename(candidate) === name);
  assert.ok(entry, `missing ${name}`);
  return path.join(root, entry);
}

try {
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  assert.ok(typeof address === "object" && address !== null);
  const endpoint = `http://127.0.0.1:${address.port}/api/agents/main/chat`;
  const evalSpec = path.join(temporaryRoot, "eval.yaml");
  const negativeSpec = path.join(temporaryRoot, "eval-negative.yaml");
  const executionErrorSpec = path.join(
    temporaryRoot,
    "eval-execution-error.yaml",
  );
  const multiTurnSpec = path.join(temporaryRoot, "eval-multi-turn.yaml");
  const invalidConfigSpec = path.join(
    temporaryRoot,
    "eval-invalid-config.yaml",
  );
  const yaml = `name: executor-smoke
defaults:
  executor:
    name: azure-functions-agent
    config:
      endpointUrl: "${endpoint}"
  runs: 1
  timeout: 10s
stimuli:
  - name: echo
    prompt: hello
    graders:
      - type: output-contains
        config:
          substring: echo:hello
`;
  await writeFile(evalSpec, yaml, "utf8");
  await writeFile(
    negativeSpec,
    yaml.replace("echo:hello", "never-match"),
    "utf8",
  );
  await writeFile(
    invalidConfigSpec,
    yaml.replace(
      `      endpointUrl: "${endpoint}"`,
      `      endpointUrl: "${endpoint}"
      endpointUrlEnv: AGENT_EVAL_TARGET_URL`,
    ),
    "utf8",
  );
  await writeFile(
    executionErrorSpec,
    yaml.replace(endpoint, "http://127.0.0.1:1/api/agents/main/chat"),
    "utf8",
  );
  await writeFile(
    multiTurnSpec,
    `name: multi-turn-smoke
defaults:
  executor:
    name: azure-functions-agent
    config:
      endpointUrl: "${endpoint}"
  runs: 1
  timeout: 10s
stimuli:
  - name: turn-selection
    turns:
      - first
      - second
    graders:
      - type: output-contains
        turn: 0
        config:
          substring: echo:first
      - type: output-contains
        turn: 1
        config:
          substring: echo:second
`,
    "utf8",
  );

  const positive = await runVally(
    evalSpec,
    path.join(temporaryRoot, "positive"),
  );
  assert.equal(positive.code, 0, positive.output);
  const positiveArtifacts = await artifactNames(
    path.join(temporaryRoot, "positive"),
  );
  assert.ok(positiveArtifacts.has("results.jsonl"));
  assert.ok(positiveArtifacts.has("eval-results.junit.xml"));
  const junit = await readFile(
    await findArtifact(
      path.join(temporaryRoot, "positive"),
      "eval-results.junit.xml",
    ),
    "utf8",
  );
  assert.match(junit, /failures="0"/);

  const negative = await runVally(
    negativeSpec,
    path.join(temporaryRoot, "negative"),
  );
  assert.notEqual(negative.code, 0, negative.output);
  const negativeWithoutGate = await runVally(
    negativeSpec,
    path.join(temporaryRoot, "negative-without-gate"),
    false,
  );
  assert.equal(negativeWithoutGate.code, 0, negativeWithoutGate.output);

  const executionError = await runVally(
    executionErrorSpec,
    path.join(temporaryRoot, "execution-error"),
    false,
  );
  assert.notEqual(executionError.code, 0, executionError.output);

  const multiTurn = await runVally(
    multiTurnSpec,
    path.join(temporaryRoot, "multi-turn"),
  );
  assert.equal(multiTurn.code, 0, multiTurn.output);

  const invalidConfig = await runVally(
    invalidConfigSpec,
    path.join(temporaryRoot, "invalid-config"),
    false,
  );
  assert.notEqual(invalidConfig.code, 0, invalidConfig.output);
} finally {
  await new Promise((resolve) => server.close(resolve));
  await rm(temporaryRoot, { recursive: true, force: true });
}
