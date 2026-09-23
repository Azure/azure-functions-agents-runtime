# Evaluate agent behavior

> **Preview.** This integration uses experimental Microsoft Agent Framework (MAF) evaluation APIs.
> Keep the runtime and MAF package versions pinned together when using it in release gates.

The runtime provides a thin MAF-compatible client target for evaluating an agent through its
existing synchronous chat endpoint. MAF owns test items, checks, repetitions, evaluator providers,
and results; `pytest` or another CI runner owns execution and reporting.

Evaluation covers **authored agent behavior**—responses and observed tool calls. It does not test
Azure Functions availability, trigger delivery, scaling, or platform reliability.

## Choose the three parts independently

| Choice | Preview support |
| --- | --- |
| Agent target | Local Azure Functions Core Tools host or deployed staging Function App |
| Orchestrator | Developer machine, pull-request CI, or scheduled/release CI |
| Evaluator | MAF local/custom checks and optional Microsoft Foundry evaluators |

Use the same source-controlled cases against local and staging targets by changing only the target
URL and authentication environment variables. Sending synthetic evaluation traffic to production
and passive evaluation of production traces are not part of this preview.

## Prerequisites

The evaluated agent must explicitly enable the existing built-in chat API:

```yaml
---
name: Receipt agent
builtin_endpoints:
  chat_api: true
---
```

Start the Function App with Core Tools or deploy it to a staging slot/environment. Supply the
**complete** chat endpoint URL because the Functions route prefix is configurable. Examples:

- `http://localhost:7071/api/agents/receipt/chat` with the default `/api` prefix
- `http://localhost:7071/agents/receipt/chat` when `host.json` sets an empty prefix
- `https://<staging-app>.azurewebsites.net/agents/receipt/chat`

The adapter does not register or deploy an endpoint. Trigger-only agents cannot be targeted unless
they opt into `builtin_endpoints.chat_api`.

## Create a target

Import the preview API from its submodule. It is intentionally not re-exported from the stable
package root.

```python
from azure_functions_agents.evaluation import FunctionAgentTarget

agent = FunctionAgentTarget(
    "http://localhost:7071/agents/receipt/chat",
    agent_id="receipt",
    name="Receipt agent",
)
```

Each MAF run without an explicit session gets a fresh Function session, including every repetition.
For an ordered multi-turn test, create and pass a session explicitly:

```python
session = agent.create_session(session_id="receipt-conversation-1")
first = await agent.run("Read the receipt", session=session)
second = await agent.run("What was the total?", session=session)
```

Do not invoke concurrent turns with the same session ID. The runtime persists conversation history,
but does not coordinate cross-worker turn ordering.

## Authenticate a staging target

The authentication strategy must match the endpoint's `http_auth` configuration.

### Function or host key

```python
import os

from azure_functions_agents.evaluation import FunctionAgentTarget, FunctionKeyAuth

agent = FunctionAgentTarget(
    os.environ["AGENT_EVAL_TARGET_URL"],
    agent_id="receipt",
    auth=FunctionKeyAuth(os.environ["AGENT_EVAL_FUNCTION_KEY"]),
)
```

The key is sent in `x-functions-key`. Keep it in a CI secret; never put it in a case file or test
artifact.

### Entra ID

```python
import os

from azure.identity import DefaultAzureCredential
from azure_functions_agents.evaluation import EntraTokenAuth, FunctionAgentTarget

agent = FunctionAgentTarget(
    os.environ["AGENT_EVAL_TARGET_URL"],
    agent_id="receipt",
    auth=EntraTokenAuth(
        credential=DefaultAzureCredential(),
        scope=os.environ["AGENT_EVAL_ENTRA_SCOPE"],
    ),
)
```

Use the Function App API scope accepted by Easy Auth, commonly
`api://<application-id>/.default`. The adapter acquires a token through the supplied Azure
`TokenCredential` and sends it as a bearer token. It never writes token or key values into MAF
results.

## Run deterministic checks in pytest

```python
import pytest
from agent_framework import (
    ExpectedToolCall,
    LocalEvaluator,
    evaluate_agent,
    tool_call_args_match,
    tool_calls_present,
)
from azure_functions_agents.evaluation import FunctionAgentTarget


@pytest.mark.asyncio
async def test_receipt_agent(agent: FunctionAgentTarget) -> None:
    results = await evaluate_agent(
        agent=agent,
        queries=["Read the receipt and return the total."],
        expected_tool_calls=[
            [ExpectedToolCall("read_receipt", {"currency": "USD"})]
        ],
        evaluators=LocalEvaluator(tool_calls_present, tool_call_args_match),
        num_repetitions=2,
    )

    for result in results:
        result.raise_for_status()
```

MAF's argument check uses subset semantics: every expected key and value must match, while extra
actual arguments are allowed. Types are significant. A string value of `"1"` does not match the
number `1`.

Use normal pytest options such as `--junitxml` to publish CI results. Transport, authentication,
timeout, cancellation, and malformed-response failures are invocation failures—not low quality
scores—and remain distinguishable through the adapter's typed exceptions.

## Add managed Foundry grading

Managed grading is optional. A deterministic-only suite does not require a Foundry project.
Scheduled or release suites can add `FoundryEvals` to the same `evaluate_agent()` call:

```python
from agent_framework_foundry import FoundryEvals

foundry = FoundryEvals(
    model="<judge-model-deployment>",
    evaluators=[FoundryEvals.RELEVANCE, FoundryEvals.TASK_ADHERENCE],
)
```

Configure the Foundry client using the MAF-supported environment variables or pass a configured
client. Retain `EvalResults.report_url` as a CI artifact when Foundry returns one. Pin evaluator
names, judge model, rubrics, thresholds, and repetitions for release gates.

The chat response contains observed calls and results, not the complete available tool definitions.
Do not enable Foundry tool-aware graders that require full tool schemas until your integration
supplies and validates that evidence.

## Evidence and data egress

The target converts the chat response into public MAF `AgentResponse` content:

- final assistant response;
- observed function-call name, arguments, and call ID;
- function result when the runtime returned one;
- Function session ID and client-observed elapsed time as response metadata.

Local checks keep this evidence in the test process. When a managed evaluator is configured, MAF may
send the case query, expected output, context, final response, and tool evidence to that evaluator.
Review the evaluator's region, retention, access control, and privacy configuration before enabling
it for sensitive data.

The preview does not return structured token usage/cost, query Application Insights, or promise a
direct trace link. Latency is report-only. Those capabilities require separately reviewed evidence
and correlation contracts.

## Sample

See the
[agent evaluation sample](https://github.com/Azure/azure-functions-agents-runtime/tree/main/samples/agent-evaluation)
for a JSONL loader, deterministic checks, repetitions, target authentication, optional Foundry
grading, and pytest/JUnit usage.
