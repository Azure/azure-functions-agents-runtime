import { parentPort, workerData } from "node:worker_threads";

function serializeResult(value) {
  if (value === null || value === undefined) {
    return [];
  }
  if (typeof value === "string") {
    return [{ type: "text", text: value }];
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new Error("Result must be finite.");
    }
    return [{ type: "text", text: String(value) }];
  }
  if (typeof value === "boolean") {
    return [{ type: "text", text: String(value) }];
  }
  const serialized = JSON.stringify(value, (_key, nestedValue) => {
    if (typeof nestedValue === "number" && !Number.isFinite(nestedValue)) {
      throw new Error("Result must be finite.");
    }
    return nestedValue;
  });
  return [{ type: "text", text: serialized }];
}

const moduleUrl = `data:text/javascript;base64,${workerData.sourceBase64}`;
const customerModule = await import(moduleUrl);
const customerFunction = customerModule[workerData.entrypoint];
if (typeof customerFunction !== "function") {
  throw new Error(`Named export '${workerData.entrypoint}' is not a function.`);
}

parentPort.postMessage({ ready: true });

parentPort.on("message", async ({ id, values }) => {
  try {
    const result = await customerFunction(...values);
    parentPort.postMessage({ id, content: serializeResult(result) });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    const stack = error instanceof Error ? error.stack ?? message : message;
    parentPort.postMessage({ id, error: { message, stack } });
  }
});
