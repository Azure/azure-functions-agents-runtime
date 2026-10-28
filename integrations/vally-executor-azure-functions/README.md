# Vally executor for Azure Functions Agents Runtime

Vally 0.16.0 or later executor for an Azure Functions Agents Runtime synchronous chat
endpoint. It converts generic runtime response and tool evidence into a Vally trajectory; it does not
start, deploy, or reconfigure the Function App.

## Requirements

- Node.js 22.12 or later
- an agent with `builtin_endpoints.chat_api: true`
- the complete local or staging chat endpoint URL

## Build and validate

```powershell
npm ci
npm run typecheck
npm test
npm run format:check
npm run pack:check
npm run test:cli
```

`test:cli` runs positive and negative `--require-pass` evaluations against a temporary local HTTP
server. Runtime and development dependencies allow newer minor and patch versions within the
existing major version, retaining their current minimums. Vally and its CLI use `>=0.16.0 <1.0.0`,
including newer `0.x` minor releases. The lockfile retains exact versions for reproducible repository
builds; review compatibility and rerun these checks when upgrading, especially for pre-1.0 releases.

## Configure

The registered executor name is `azure-functions-hosted-skill`:

```yaml
defaults:
  executor:
    name: azure-functions-hosted-skill
    config:
      endpointUrlEnv: AGENT_EVAL_TARGET_URL
      auth:
        type: anonymous
```

Specify exactly one of `endpointUrl` or `endpointUrlEnv`. Authentication can be:

- `anonymous`;
- `function-key` with `keyEnv` naming the variable containing a Functions key;
- `entra` with either `scope` or `scopeEnv`, using `DefaultAzureCredential`.

Literal keys, bearer tokens, arbitrary headers, non-HTTP(S) URLs, cleartext HTTP URLs for
non-loopback hosts, URL credentials/query strings, unknown fields, and missing referenced
environment variables are rejected. Redirects are not followed, and resolved credentials are never
included in trajectories or executor errors.

Run it with `vally eval --executor-plugin <path-to-dist/index.js>`. Plug-in paths are resolved
relative to the eval specification. See the repository evaluation guide and sample for complete
commands, grading examples, evidence limits, and CI guidance.
