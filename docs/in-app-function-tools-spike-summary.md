# In-app Functions as agent tools: experiment summary

**Status:** Python proof of concept implemented. Local Functions-host validation
passed; the project owner confirmed the live model experiment worked.
This is a spike, not a production-ready feature.

## Opportunity

Let developers expose existing Azure Functions as tools an AI agent can choose
and execute. The same application operation can respond to its ordinary trigger
or be called during an agent run, without extracting a separate handler or
maintaining a second implementation in a `tools/` directory.

For existing Function Apps, this offers an incremental path to adding agentic
behavior while retaining their event-driven entry points.

## What was built

- An `app.tool` decorator on the runtime's enhanced Function App, supporting both
  existing trigger handlers and standalone Python functions.
- A tool description supplied by a docstring or explicit `description`, allowing
  the model to decide when the operation is useful.
- An explicit Pydantic input schema for trigger-backed tools. The runtime
  validates model-supplied arguments, constructs a synthetic JSON request or
  message, and calls the existing handler in-process.
- Adapters for the runtime's FastAPI HTTP requests, Storage Queue messages, and
  Service Bus messages. Standalone typed functions derive their tool schema
  from their signature.
- Late-bound tool registration so decorators applied after app construction
  become available to agent execution, including the HostedSkill execution path.
- A runnable [order-assistant sample](../samples/in-app-function-tools/README.md)
  with an HTTP lookup, a queue-triggered shipping action, and a standalone
  business-day calculation.

## Design decisions

| Area | Agreed direction |
| --- | --- |
| Authoring | One function implementation with an additive `app.tool` declaration; no author-visible trigger/handler split. |
| Execution | Agent calls execute the handler directly, rather than issuing HTTP requests or publishing to a broker. Normal trigger invocations continue to work. |
| Input contract | Authors explicitly describe the JSON body expected by trigger-backed handlers. A request or message type alone does not reveal the business fields the handler needs. |
| Intent | The description explains when to call an operation. Trigger type does not imply read versus write behavior. |
| Availability | In-app tools are available to all agents by default. Optional `agents=[...]` limits access to named agent slugs. |
| Existing filtering | Preserve the current denylist and tools-disabled behavior; do not introduce a new allowlist. |
| Agent definitions | Keep requiring an agent markdown file for HostedSkill. A plain markdown file without front matter is a desired follow-up, not part of this implementation. |
| Language direction | Use Python as the reference. Idiomatic TypeScript and C# equivalents were discussed but are not implemented. |

## Evidence

- Core Tools indexed the decorated Functions. A real Storage Queue message
  updated an order, and a separate HTTP request confirmed the changed state.
- Automated tests exercised synthetic tool invocation, input schemas,
  description requirements, agent scoping, duplicate names, and the sample.
  The last full run passed **2,602 tests**, with Ruff and mypy clean.
- Live model testing from the remote development host was blocked by network
  restrictions. After local testing, the project owner reported that the
  experiment worked. Detailed local model traces are not captured in this branch.

## Boundaries and next steps

Direct invocation is not a broker or Functions-host invocation. It does not
reproduce delivery retries, settlement, dead-letter handling, host-managed
bindings, or HTTP ingress authentication. Applications must authorize access
through the agent-facing surface. A message handler returning no value produces
a `completed` tool result; the sample re-reads state to confirm the action.

The sample simulates shipping in a single worker's memory and resets on restart.
It is not a durable order-processing service. Service Bus adaptation has unit
coverage, but no live Service Bus validation is recorded here.

Before productionizing, complete the formal requirements and architecture
review, define the supported binding and payload contracts, strengthen startup
validation and execution coverage, and reconcile late registration with the
runtime's immutable catalog design. Optional front matter and additional
languages remain separate follow-ups.
