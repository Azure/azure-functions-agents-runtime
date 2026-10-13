import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { createInterface } from "node:readline";
import { Worker } from "node:worker_threads";

const PROTOCOL_VERSION = "2025-11-25";
const MAX_LINE_LENGTH = 1024 * 1024;
const protocolWrite = process.stdout.write.bind(process.stdout);

function diagnosticWrite(value) {
  process.stderr.write(String(value));
}

process.stdout.write = (chunk, encoding, callback) => {
  diagnosticWrite(chunk);
  if (typeof encoding === "function") {
    encoding();
  }
  if (typeof callback === "function") {
    callback();
  }
  return true;
};
console.log = (...values) => diagnosticWrite(`${values.join(" ")}\n`);
console.info = console.log;
console.warn = console.log;
console.error = console.log;

function send(payload) {
  protocolWrite(`${JSON.stringify(payload)}\n`);
}

function response(id, result) {
  send({ jsonrpc: "2.0", id, result });
}

function errorResponse(id, code, message) {
  send({ jsonrpc: "2.0", id, error: { code, message } });
}

function decodeContract(encoded) {
  const normalized = encoded.replaceAll("-", "+").replaceAll("_", "/");
  return JSON.parse(Buffer.from(normalized, "base64").toString("utf8"));
}

const [encodedContract, sourcePath, expectedHash] = process.argv.slice(2);
if (!encodedContract || !sourcePath || !expectedHash) {
  diagnosticWrite("JavaScript MCP adapter requires contract, source path, and source hash.\n");
  process.exit(2);
}

const contract = decodeContract(encodedContract);
let customerWorker;
let startupWorker;
let workerStartup;
let workerGeneration = 0;
const activeCalls = new Map();

function sanitizeDiagnostic(value) {
  return String(value).replaceAll(
    /data:text\/javascript;base64,[A-Za-z0-9+/=]+/g,
    `<javascript-tool:${contract.name}>`,
  );
}

function rejectActiveCalls(error) {
  for (const { reject } of activeCalls.values()) {
    reject(error);
  }
  activeCalls.clear();
}

async function createWorker(generation) {
  const source = await readFile(sourcePath);
  const actualHash = createHash("sha256").update(source).digest("hex");
  if (actualHash !== expectedHash) {
    throw new Error("JavaScript source changed after application startup; restart the app.");
  }
  if (generation !== workerGeneration || activeCalls.size === 0) {
    throw new Error("JavaScript tool call was cancelled.");
  }
  const worker = new Worker(new URL("./node_mcp_worker.mjs", import.meta.url), {
    workerData: {
      entrypoint: contract.entrypoint,
      sourceBase64: source.toString("base64"),
      },
    stdout: true,
    stderr: true,
  });
  startupWorker = worker;
  worker.stdout.on("data", diagnosticWrite);
  worker.stderr.on("data", diagnosticWrite);
  const ready = new Promise((resolve, reject) => {
    const cleanup = () => {
      worker.off("message", handleReady);
      worker.off("error", handleError);
      worker.off("exit", handleExit);
    };
    const handleReady = (message) => {
      if (message.ready) {
        cleanup();
        resolve();
      }
    };
    const handleError = (error) => {
      cleanup();
      reject(error);
    };
    const handleExit = (code) => {
      cleanup();
      reject(new Error(`JavaScript tool worker exited during startup with code ${code}.`));
    };
    worker.on("message", handleReady);
    worker.on("error", handleError);
    worker.on("exit", handleExit);
  });
  worker.on("message", (message) => {
    if (message.ready) {
      return;
    }
    const pending = activeCalls.get(message.id);
    if (!pending) {
      return;
    }
    activeCalls.delete(message.id);
    if (message.error) {
      const error = new Error(message.error.message);
      error.stack = message.error.stack;
      pending.reject(error);
      return;
    }
    pending.resolve(message.content);
  });
  worker.on("error", (error) => {
    if (customerWorker === worker) {
      customerWorker = undefined;
      rejectActiveCalls(error);
    }
  });
  worker.on("exit", (code) => {
    if (customerWorker === worker) {
      customerWorker = undefined;
      if (code !== 0) {
        rejectActiveCalls(new Error(`JavaScript tool worker exited with code ${code}.`));
      }
    }
  });
  await ready;
  if (
    generation !== workerGeneration
    || startupWorker !== worker
    || activeCalls.size === 0
  ) {
    if (startupWorker === worker) {
      startupWorker = undefined;
    }
    await worker.terminate();
    throw new Error("JavaScript tool call was cancelled.");
  }
  startupWorker = undefined;
  customerWorker = worker;
  return worker;
}

async function loadWorker() {
  if (customerWorker) {
    return customerWorker;
  }
  if (!workerStartup) {
    const generation = ++workerGeneration;
    workerStartup = createWorker(generation);
  }
  const startup = workerStartup;
  try {
    return await startup;
  } finally {
    if (workerStartup === startup) {
      workerStartup = undefined;
    }
  }
}

async function callWorker(id, values) {
  return new Promise((resolve, reject) => {
    activeCalls.set(id, { reject, resolve });
    void loadWorker().then((worker) => {
      if (activeCalls.has(id)) {
        worker.postMessage({ id, values });
      }
    }).catch((error) => {
      const pending = activeCalls.get(id);
      if (pending) {
        activeCalls.delete(id);
        pending.reject(error);
      }
    });
  });
}

function cancelCall(requestId) {
  const pending = activeCalls.get(requestId);
  if (!pending) {
    return;
  }
  activeCalls.delete(requestId);
  pending.reject(new Error("JavaScript tool call was cancelled."));
  if (!customerWorker) {
    if (activeCalls.size === 0) {
      workerGeneration += 1;
      workerStartup = undefined;
      const worker = startupWorker;
      startupWorker = undefined;
      if (worker) {
        void worker.terminate();
      }
    }
    return;
  }
  const worker = customerWorker;
  customerWorker = undefined;
  rejectActiveCalls(new Error("JavaScript tool call was cancelled."));
  void worker.terminate();
}

function isPortableInteger(source) {
  const match = /^(-?)(0|[1-9]\d*)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$/.exec(source ?? "");
  if (!match) {
    return false;
  }
  let digitsText = `${match[2]}${match[3] ?? ""}`;
  if (/^0+$/.test(digitsText)) {
    return true;
  }
  const exponent = Number(match[4] ?? "0");
  if (!Number.isSafeInteger(exponent) || Math.abs(exponent) > 100) {
    return false;
  }
  let scale = (match[3]?.length ?? 0) - exponent;
  while (scale > 0 && digitsText.endsWith("0")) {
    digitsText = digitsText.slice(0, -1);
    scale -= 1;
  }
  if (scale > 0 || (scale < 0 && digitsText.length - scale > 16)) {
    return false;
  }
  let integer = BigInt(digitsText);
  if (scale < 0) {
    integer *= 10n ** BigInt(-scale);
  }
  if (match[1] === "-") {
    integer = -integer;
  }
  return integer >= -9007199254740991n && integer <= 9007199254740991n;
}

function typeMatches(value, type, source) {
  if (type === "string") {
    return typeof value === "string";
  }
  if (type === "integer") {
    return typeof value === "number" && isPortableInteger(source);
  }
  if (type === "number") {
    return typeof value === "number" && Number.isFinite(value);
  }
  if (type === "boolean") {
    return typeof value === "boolean";
  }
  return false;
}

function validateArguments(argumentsValue, numberSources) {
  const values = argumentsValue ?? {};
  if (typeof values !== "object" || Array.isArray(values)) {
    throw new Error("Tool arguments must be an object.");
  }
  const allowed = new Set(contract.parameters.map((parameter) => parameter.name));
  const unknown = Object.keys(values).filter((name) => !allowed.has(name));
  if (unknown.length > 0) {
    throw new Error(`Unexpected arguments: ${unknown.sort().join(", ")}`);
  }
  return contract.parameters.map((parameter) => {
    if (!Object.hasOwn(values, parameter.name)) {
      throw new Error(`Missing required argument: ${parameter.name}`);
    }
    const value = values[parameter.name];
    const source = numberSources.get(values)?.get(parameter.name);
    if (!typeMatches(value, parameter.type, source)) {
      throw new Error(`Argument '${parameter.name}' must be ${parameter.type}.`);
    }
    return value;
  });
}

function toolError(message) {
  return {
    content: [{ type: "text", text: message }],
    isError: true,
  };
}

async function handleRequest(message, numberSources) {
  const id = message.id ?? null;
  if (message.method === "initialize") {
    if (message.params?.protocolVersion !== PROTOCOL_VERSION) {
      errorResponse(id, -32602, `Unsupported protocol version; expected ${PROTOCOL_VERSION}.`);
      process.exitCode = 1;
      return;
    }
    response(id, {
      protocolVersion: PROTOCOL_VERSION,
      capabilities: { tools: { listChanged: false } },
      serverInfo: {
        name: "azure-functions-agents-javascript-tool",
        version: "1.0.0",
      },
    });
    return;
  }
  if (message.method === "ping") {
    response(id, {});
    return;
  }
  if (message.method === "tools/list") {
    response(id, {
      tools: [{
        name: contract.name,
        description: contract.description,
        inputSchema: contract.inputSchema,
      }],
    });
    return;
  }
  if (message.method === "tools/call") {
    if (message.params?.name !== contract.name) {
      response(id, toolError(`Unknown tool: ${message.params?.name ?? ""}`));
      return;
    }
    try {
      const values = validateArguments(message.params?.arguments, numberSources);
      const content = await callWorker(id, values);
      response(id, { content, isError: false });
    } catch (error) {
      const messageText = error instanceof Error ? error.message : String(error);
      const diagnostic = error instanceof Error ? error.stack ?? messageText : messageText;
      diagnosticWrite(`${sanitizeDiagnostic(diagnostic)}\n`);
      response(id, toolError(messageText));
    }
    return;
  }
  errorResponse(id, -32601, `Method not found: ${message.method}`);
}

const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
input.on("line", (line) => {
  if (line.length > MAX_LINE_LENGTH) {
    errorResponse(null, -32600, "MCP message exceeds the 1 MiB limit.");
    return;
  }
  let message;
  const numberSources = new WeakMap();
  try {
    message = JSON.parse(line, function parseValue(key, value, context) {
      if (typeof value === "number" && context?.source) {
        let sources = numberSources.get(this);
        if (!sources) {
          sources = new Map();
          numberSources.set(this, sources);
        }
        sources.set(key, context.source);
      }
      return value;
    });
  } catch {
    errorResponse(null, -32700, "Invalid JSON.");
    return;
  }
  if (message.method === "notifications/cancelled") {
    cancelCall(message.params?.requestId);
    return;
  }
  if (message.method === "notifications/initialized"
      || (message.method?.startsWith("notifications/") && message.id === undefined)) {
    return;
  }
  void handleRequest(message, numberSources).catch((error) => {
    const messageText = error instanceof Error ? error.message : String(error);
    diagnosticWrite(`${messageText}\n`);
    errorResponse(message.id ?? null, -32603, messageText);
  });
});
