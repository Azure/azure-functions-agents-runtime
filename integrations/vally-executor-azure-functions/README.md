# Vally executor for Azure Functions Agents Runtime

Vally 0.16.0 executor for an Azure Functions Agents Runtime synchronous chat
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
server. Vally and its CLI are pinned to 0.16.0; review compatibility before changing either version.

### Security overrides

Azure Identity depends on MSAL Node 6, which is affected by
[GHSA-xcx6-jx8p-x3h5](https://github.com/advisories/GHSA-xcx6-jx8p-x3h5).
This package overrides that dependency to `>=7.1.0 <8.0.0`, which includes the fix.
Remove the override when Azure Identity uses a fixed MSAL version.

npm applies overrides only from the root project. If you install this evaluator as a
dependency, add this override to your root package manifest and update your lockfile:

```json
{
  "overrides": {
    "@azure/identity": {
      "@azure/msal-node": ">=7.1.0 <8.0.0"
    }
  }
}
```

Run the authentication and evaluation checks after you apply the override.
The lockfile also uses a fixed Zod version. Update downstream lockfiles and run
`npm audit --audit-level=moderate`.

## Configure

The registered executor name is `azure-functions-agent`:

```yaml
defaults:
  executor:
    name: azure-functions-agent
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
