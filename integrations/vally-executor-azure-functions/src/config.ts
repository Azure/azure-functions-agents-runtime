export type AzureFunctionsAgentAuth =
  | { type?: "anonymous" }
  | { type: "function-key"; keyEnv: string }
  | ({ type: "entra" } & (
      { scope: string; scopeEnv?: never } | { scope?: never; scopeEnv: string }
    ));

export type AzureFunctionsAgentExecutorConfig =
  | {
      endpointUrl: string;
      endpointUrlEnv?: never;
      auth?: AzureFunctionsAgentAuth;
    }
  | {
      endpointUrl?: never;
      endpointUrlEnv: string;
      auth?: AzureFunctionsAgentAuth;
    };

export interface ResolvedAzureFunctionsAgentExecutorConfig {
  endpointUrl: URL;
  auth:
    | { type: "anonymous" }
    | { type: "function-key"; key: string }
    | { type: "entra"; scope: string };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function assertKnownKeys(
  value: Record<string, unknown>,
  known: readonly string[],
  context: string,
): void {
  const unknown = Object.keys(value).filter((key) => !known.includes(key));
  if (unknown.length > 0) {
    throw new Error(
      `${context} contains unsupported field(s): ${unknown.join(", ")}`,
    );
  }
}

function requireNonEmptyString(value: unknown, field: string): string {
  if (typeof value !== "string" || value.trim().length === 0) {
    throw new Error(`${field} must be a non-empty string`);
  }
  return value.trim();
}

function validateEnvironmentName(value: unknown, field: string): string {
  const name = requireNonEmptyString(value, field);
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) {
    throw new Error(`${field} must be a valid environment variable name`);
  }
  return name;
}

function validateEndpoint(value: string): URL {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error("executor endpoint must be an absolute HTTP(S) URL");
  }
  if (
    !(["http:", "https:"] as const).includes(url.protocol as "http:" | "https:")
  ) {
    throw new Error("executor endpoint must use HTTP or HTTPS");
  }
  if (!url.hostname || url.username || url.password || url.search || url.hash) {
    throw new Error(
      "executor endpoint must not contain user information, a query, or a fragment",
    );
  }
  return url;
}

function parseAuth(value: unknown): AzureFunctionsAgentAuth {
  if (value === undefined) {
    return { type: "anonymous" };
  }
  if (!isRecord(value)) {
    throw new Error("executor auth must be an object");
  }
  const type = value.type ?? "anonymous";
  if (type === "anonymous") {
    assertKnownKeys(value, ["type"], "executor auth");
    return { type: "anonymous" };
  }
  if (type === "function-key") {
    assertKnownKeys(value, ["type", "keyEnv"], "function-key auth");
    return {
      type,
      keyEnv: validateEnvironmentName(value.keyEnv, "auth.keyEnv"),
    };
  }
  if (type === "entra") {
    assertKnownKeys(value, ["type", "scope", "scopeEnv"], "Entra auth");
    const hasScope = value.scope !== undefined;
    const hasScopeEnv = value.scopeEnv !== undefined;
    if (hasScope === hasScopeEnv) {
      throw new Error(
        "Entra auth requires exactly one of auth.scope or auth.scopeEnv",
      );
    }
    return hasScope
      ? { type, scope: requireNonEmptyString(value.scope, "auth.scope") }
      : {
          type,
          scopeEnv: validateEnvironmentName(value.scopeEnv, "auth.scopeEnv"),
        };
  }
  throw new Error(
    "executor auth.type must be anonymous, function-key, or entra",
  );
}

export function parseExecutorConfig(
  config: unknown,
): AzureFunctionsAgentExecutorConfig {
  if (!isRecord(config)) {
    throw new Error("executor config must be an object");
  }
  assertKnownKeys(
    config,
    ["endpointUrl", "endpointUrlEnv", "auth"],
    "executor config",
  );
  const hasUrl = config.endpointUrl !== undefined;
  const hasUrlEnv = config.endpointUrlEnv !== undefined;
  if (hasUrl === hasUrlEnv) {
    throw new Error(
      "executor config requires exactly one of endpointUrl or endpointUrlEnv",
    );
  }
  const auth = parseAuth(config.auth);
  if (hasUrl) {
    const endpointUrl = requireNonEmptyString(
      config.endpointUrl,
      "endpointUrl",
    );
    validateEndpoint(endpointUrl);
    return { endpointUrl, auth };
  }
  return {
    endpointUrlEnv: validateEnvironmentName(
      config.endpointUrlEnv,
      "endpointUrlEnv",
    ),
    auth,
  };
}

function resolveEnvironment(name: string, field: string): string {
  const value = process.env[name]?.trim();
  if (!value) {
    throw new Error(
      `${field} environment variable ${name} is not set or is empty`,
    );
  }
  return value;
}

export function resolveExecutorConfig(
  config: AzureFunctionsAgentExecutorConfig,
): ResolvedAzureFunctionsAgentExecutorConfig {
  const endpoint =
    "endpointUrl" in config && config.endpointUrl !== undefined
      ? config.endpointUrl
      : resolveEnvironment(config.endpointUrlEnv, "endpoint URL");
  const auth = config.auth ?? { type: "anonymous" };

  if (auth.type === "function-key") {
    return {
      endpointUrl: validateEndpoint(endpoint),
      auth: {
        type: auth.type,
        key: resolveEnvironment(auth.keyEnv, "function key"),
      },
    };
  }
  if (auth.type === "entra") {
    const scope =
      "scope" in auth && auth.scope !== undefined
        ? auth.scope
        : resolveEnvironment(auth.scopeEnv, "Entra scope");
    return {
      endpointUrl: validateEndpoint(endpoint),
      auth: { type: auth.type, scope },
    };
  }
  return {
    endpointUrl: validateEndpoint(endpoint),
    auth: { type: "anonymous" },
  };
}
