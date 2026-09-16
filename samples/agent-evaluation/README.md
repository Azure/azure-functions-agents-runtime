# Agent evaluation

A runnable receipt-agent Function App plus a development-time evaluation harness. The Function App
under `src/` exposes the existing built-in chat API and a deterministic `read_receipt` tool. The
harness uses the preview `FunctionAgentTarget` adapter, Microsoft Agent Framework (MAF) evaluation
contracts/checks, and pytest rather than adding a runtime evaluation endpoint or CLI.

## What the sample shows

- JSON Lines cases mapped directly to MAF `ExpectedToolCall` values
- deterministic required-tool and argument-subset checks
- independent sessions for repetitions
- anonymous, Functions-key, and Entra target authentication
- optional managed Foundry relevance and task-adherence grading
- pytest exit codes and JUnit output for CI

## Project structure

```text
agent-evaluation/
├── src/                           # Runnable Azure Functions app
│   ├── function_app.py
│   ├── receipt.agent.md           # Agent slug: receipt; chat API enabled
│   ├── tools/receipt.py            # Deterministic 42.18 USD receipt
│   ├── agents.config.yaml
│   ├── host.json                   # Empty route prefix
│   ├── local.settings.template.json
│   └── requirements.txt
├── cases.jsonl                    # Source-controlled evaluation cases
└── test_agent_evaluation.py       # MAF + pytest evaluation harness
```

## Case format

One JSON object is stored per line:

```json
{
  "id": "receipt-total",
  "query": "Read the receipt and return the total.",
  "expected_output": "The receipt total is 42.18 USD.",
  "expected_tool_calls": [
    {"name": "read_receipt", "arguments": {"currency": "USD"}}
  ],
  "tags": ["smoke"],
  "repetitions": 2
}
```

The loader belongs to this sample; JSONL is not a runtime-owned dataset schema. `expected_output` is
available to managed evaluators. The local checks in this sample gate expected tool names and
argument subsets. MAF allows extra actual arguments, but expected values and their types must match.

## Run locally

### Prerequisites

- Python 3.13+
- [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local)
- [Azurite](https://learn.microsoft.com/azure/storage/common/storage-use-azurite)
- Credentials for Microsoft Foundry, Azure OpenAI, or OpenAI

The commands below assume the repository virtual environment is activated and the repository is the
current directory.

### 1. Install the sample app

The editable requirement makes the Function host use this checkout, including the preview adapter:

```powershell
Push-Location samples/agent-evaluation/src
python -m pip install -r requirements.txt
Copy-Item local.settings.template.json local.settings.json
Pop-Location
```

### 2. Configure a model provider

For Microsoft Foundry, sign in and set the provider values in the terminal that will run Core Tools:

```powershell
az login
$env:AZURE_FUNCTIONS_AGENTS_PROVIDER = "foundry"
$env:FOUNDRY_PROJECT_ENDPOINT = "https://<resource>.<region>.services.ai.azure.com/api/projects/<project>"
$env:FOUNDRY_MODEL = "<model-deployment>"
```

Azure OpenAI and OpenAI are also supported; see the repository [provider configuration](../../README.md#model-provider-configuration).

### 3. Start Azurite

In a second terminal:

```powershell
azurite
```

Alternatively, run Azurite in Docker as described in the [repository quickstart](../../README.md#7-start-azurite-local-storage-emulator).

### 4. Start the Function App

In the configured provider terminal:

```powershell
Set-Location samples/agent-evaluation/src
func start
```

The app's `host.json` uses an empty route prefix, so the anonymous chat endpoint is:

```text
http://localhost:7071/agents/receipt/chat
```

### 5. Run the evaluation

In another terminal at the repository root, with the repository environment activated:

```powershell
$env:AGENT_EVAL_TARGET_URL = "http://localhost:7071/agents/receipt/chat"
python -m pytest samples/agent-evaluation/test_agent_evaluation.py -q -s
```

The case runs twice. Each attempt must call `read_receipt` with `currency="USD"`; the tool returns
the sample total and the agent is instructed to answer `The receipt total is 42.18 USD.`.

Each query/repetition receives a fresh session. A transport, authentication, timeout, or malformed
response raises a typed invocation error rather than becoming a quality score.

To use the harness with another Function App, change `AGENT_EVAL_TARGET_URL`, the target metadata in
`test_agent_evaluation.py`, and the cases. Supply the complete endpoint URL—do not assume `/api`,
because `host.json` can change the Functions route prefix.

## Run against staging

Deploy a compatible app to staging, set `AGENT_EVAL_TARGET_URL` to its complete chat endpoint, then
configure exactly one target authentication path. The included local target is anonymous so it can
be run without a development Function key.

For Functions-key authentication:

```powershell
$env:AGENT_EVAL_FUNCTION_KEY = "<CI secret>"
```

For Entra authentication:

```powershell
$env:AGENT_EVAL_ENTRA_SCOPE = "api://<application-id>/.default"
```

Entra uses `DefaultAzureCredential`. Keep all credentials in local environment variables or CI
secrets; never add them to `cases.jsonl` or test artifacts.

## Enable optional Foundry grading

The deterministic workflow does not require Foundry. For a scheduled/release suite, configure MAF's
Foundry client environment and enable the opt-in:

```powershell
$env:AGENT_EVAL_USE_FOUNDRY = "true"
$env:FOUNDRY_PROJECT_ENDPOINT = "https://<project-endpoint>"
$env:FOUNDRY_MODEL = "<judge-model-deployment>"
```

The sample prints a Foundry report URL when one is returned. It intentionally does not enable
managed tool-aware graders: the chat response includes observed calls, but not all available tool
definitions required by some graders.

When Foundry is enabled, the query, expected output, response, context, and available tool evidence
may leave the test process. Review region, retention, access control, and privacy requirements first.

## Publish JUnit in CI

```powershell
python -m pytest samples/agent-evaluation/test_agent_evaluation.py `
  --junitxml=artifacts/agent-evaluation.xml
```

Pin the runtime/MAF versions, evaluator names, judge model, thresholds, and repetitions before using
managed scores as a release gate. See the [evaluation guide](../../docs/evaluation.md) for ownership,
evidence, and current preview limitations.
