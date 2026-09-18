# Agent evaluation

This sample pairs a runnable receipt-agent Function App with a native Vally evaluation. The app
under `src/` exposes the existing built-in chat API and a deterministic `read_receipt` tool. Vally
calls that API through this repository's custom executor; no evaluation endpoint or Python harness
is added to the Function App.

## What the sample shows

- native Vally stimuli and graders in `eval.yaml`;
- deterministic output, required-tool, argument, and result checks;
- ordered multi-turn confirmation-code recall on one persisted runtime session;
- two independent trials through `defaults.runs`;
- anonymous local access, with function-key and Entra configurations available for staging;
- `--require-pass` and JUnit output for CI.

## Project structure

```text
agent-evaluation/
├── eval.yaml                      # Source-controlled Vally evaluation
└── src/                           # Runnable Azure Functions app
    ├── function_app.py
    ├── receipt.agent.md           # Agent slug: receipt; chat API enabled
    ├── tools/receipt.py           # Deterministic 42.18 USD receipt
    ├── agents.config.yaml
    ├── host.json                  # Empty route prefix
    ├── local.settings.template.json
    └── requirements.txt
```

## Run locally

Prerequisites are Python 3.13+, Node.js 22.12+, Azure Functions Core Tools, Azurite, and model
provider credentials.

From the repository root, install the app and the executor:

```powershell
python -m pip install -r samples/agent-evaluation/src/requirements.txt
Copy-Item samples/agent-evaluation/src/local.settings.template.json `
  samples/agent-evaluation/src/local.settings.json
Push-Location integrations/vally-executor-azure-functions
npm ci
npm run build
Pop-Location
```

Configure a supported model provider as described in the repository's
[model provider configuration](../../README.md#model-provider-configuration), then start Azurite.
In the provider-configured terminal, start the app:

```powershell
Set-Location samples/agent-evaluation/src
func start
```

The empty route prefix in `host.json` exposes the endpoint at
`http://localhost:7071/agents/receipt/chat`. In another terminal at the repository root, run:

```powershell
$env:AGENT_EVAL_TARGET_URL = "http://localhost:7071/agents/receipt/chat"
node integrations/vally-executor-azure-functions/node_modules/@microsoft/vally-cli/dist/index.js eval `
  --eval-spec samples/agent-evaluation/eval.yaml `
  --executor-plugin ../../integrations/vally-executor-azure-functions/dist/index.js `
  --require-pass `
  --junit `
  --output-dir artifacts/agent-evaluation
```

The plug-in path is relative to `eval.yaml`. Receipt trials must call `read_receipt` with `USD`,
observe the `42.18` result, and answer with the expected total. The multi-turn case supplies a
confirmation code in turn one and requires exact recall without a tool in turn two. Each trial
receives a fresh runtime session; turns within one trial share it.

Repository maintainers can exercise the same file through the opt-in Core Tools E2E after building
the executor and configuring the provider and Azurite:

```powershell
$env:RUN_VALLY_E2E = "1"
python -m pytest -m e2e tests/endtoend/test_vally_sample.py -q
```

## Target staging

Set `AGENT_EVAL_TARGET_URL` to the complete staging chat endpoint. If its route requires a Functions
key, change the eval's auth block to:

```yaml
auth:
  type: function-key
  keyEnv: AGENT_EVAL_FUNCTION_KEY
```

Set `AGENT_EVAL_FUNCTION_KEY` from a local secret store or CI secret. For an Easy Auth protected
route, use:

```yaml
auth:
  type: entra
  scopeEnv: AGENT_EVAL_ENTRA_SCOPE
```

Set the scope to the API accepted by the Function App, commonly
`api://<application-id>/.default`. The executor obtains credentials through `DefaultAzureCredential`.
Never commit credentials to `eval.yaml` or publish them in artifacts.

See the [evaluation guide](../../docs/evaluation.md) for multi-turn behavior, failure semantics,
evidence limits, optional judge graders, and privacy guidance.
