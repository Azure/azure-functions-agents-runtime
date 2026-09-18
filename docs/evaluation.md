# Evaluate agent behavior with Vally

> **Preview.** The executor is pinned to Vally 0.16.0. Vally is pre-1.0, so review and rerun the
> compatibility gates before upgrading it.

[Vally](https://microsoft.github.io/vally/) evaluates an agent through its existing synchronous
chat endpoint. Vally owns stimuli, graders, repeated trials, scores, reports, and CI verdicts. This
repository supplies a custom `azure-functions-agent` executor that calls a local or staging Function
App and converts its generic response and tool evidence into a Vally trajectory.

Evaluation covers **authored agent behavior**. It does not test Functions availability, scaling,
trigger delivery, or platform reliability. The runtime does not expose a separate evaluation route
and does not import Vally.

## Prerequisites

- Node.js 22.12 or later
- Vally and the executor's locked dependencies, installed from this repository
- an agent with `builtin_endpoints.chat_api: true`
- a Function App running under Core Tools or deployed to staging

The endpoint URL must be complete because the Functions route prefix is configurable:

- `http://localhost:7071/api/agents/receipt/chat` with the default `/api` prefix
- `http://localhost:7071/agents/receipt/chat` with an empty prefix
- `https://<staging-app>.azurewebsites.net/agents/receipt/chat`

Install and build the pinned executor from the repository root:

```powershell
Push-Location integrations/vally-executor-azure-functions
npm ci
npm run build
Pop-Location
```

## Write an eval specification

Use Vally's native `eval.yaml` format:

```yaml
name: receipt-agent-evaluation

defaults:
  executor:
    name: azure-functions-agent
    config:
      endpointUrlEnv: AGENT_EVAL_TARGET_URL
      auth:
        type: anonymous
  runs: 2
  timeout: 120s

stimuli:
  - name: receipt-total
    prompt: Read the receipt and return the total.
    graders:
      - type: output-contains
        config:
          substring: The receipt total is 42.18 USD.
      - type: tool-calls
        config:
          required:
            - name: ^read_receipt$
              args:
                currency: ^USD$
```

The `tool-calls.args` values are regular expressions and match string-valued arguments. Use the
grader's serialized-argument `pattern` for nested, numeric, boolean, or array arguments. Vally also
supports ordered multi-turn stimuli; the executor sends those prompts sequentially with one shared
runtime session. Independent trials receive independent generated sessions.

## Run locally

Start the Function App, set its complete chat URL, and invoke the pinned CLI:

```powershell
$env:AGENT_EVAL_TARGET_URL = "http://localhost:7071/agents/receipt/chat"
node integrations/vally-executor-azure-functions/node_modules/@microsoft/vally-cli/dist/index.js eval `
  --eval-spec samples/agent-evaluation/eval.yaml `
  --executor-plugin ../../integrations/vally-executor-azure-functions/dist/index.js `
  --require-pass `
  --junit `
  --output-dir artifacts/agent-evaluation
```

Executor plug-in paths are resolved relative to the eval specification, which explains the leading
`../../` in the command. `--require-pass` is essential for CI: without it, a completed evaluation
with failing grader verdicts exits successfully. Configuration, execution, and tooling failures
still exit nonzero. `--junit` writes CI-compatible test output alongside Vally's result artifacts.

## Authenticate a staging target

Do not put keys or bearer tokens in an eval specification. The executor supports three fail-closed
authentication configurations.

Anonymous endpoints need no extra fields:

```yaml
auth:
  type: anonymous
```

For a Functions or host key, reference an environment variable:

```yaml
auth:
  type: function-key
  keyEnv: AGENT_EVAL_FUNCTION_KEY
```

```powershell
$env:AGENT_EVAL_FUNCTION_KEY = "<CI secret>"
```

For Microsoft Entra authentication, provide the accepted API scope directly or through `scopeEnv`:

```yaml
auth:
  type: entra
  scopeEnv: AGENT_EVAL_ENTRA_SCOPE
```

```powershell
$env:AGENT_EVAL_ENTRA_SCOPE = "api://<application-id>/.default"
```

Entra authentication uses `DefaultAzureCredential`. Use managed identity in Azure-hosted CI where
possible. Literal function keys, arbitrary headers, bearer tokens, and endpoint query strings are
rejected by configuration validation.

## Runtime evidence and failure semantics

The executor maps each configured turn into user/assistant messages, completed tool-call/result
pairs, and turn boundaries. It preserves:

- the runtime session ID and resolved model;
- tool names, JSON-compatible arguments, results, and success classification;
- model-response batch identity for reliable parallel-tool grading;
- client-observed wall time.

It does not invent token usage, cost, reasoning, skill activation, subagent, or workspace evidence.
Transport errors, authentication failures, timeouts, malformed responses, and mismatched session IDs
are execution failures rather than low quality scores.

The endpoint's tool-result `success` field is based on the runtime's sanitized error-envelope
classification. A semantically unsuccessful plain-text result can still be classified as
successful; assert important result content with a grader as well.

Do not run concurrent turns with the same explicit session ID. The runtime persists conversation
history but does not coordinate cross-worker turn ordering.

## Privacy and optional judge graders

Vally artifacts can contain prompts, responses, tool arguments/results, endpoint metadata, and
session IDs. Keep artifacts access-controlled and never place credentials in eval files. Optional
Vally prompt or panel graders can send selected trajectory evidence to their configured model
provider. Review region, retention, access control, and privacy requirements before enabling them.

See the [agent evaluation sample](https://github.com/Azure/azure-functions-agents-runtime/tree/main/samples/agent-evaluation)
for a runnable receipt agent and deterministic eval specification.
