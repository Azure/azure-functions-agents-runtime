# Hybrid Function agent binding

This sample uses `AiApp` to keep ordinary Azure Functions triggers and deterministic application logic in Python while injecting a markdown-defined Serverless Agent in process.

It demonstrates:

- an HTTP-triggered function using a fresh raw `agent_framework.Agent`;
- a queue-triggered function using a fresh raw Agent.

The binding projection reads only `name`, `description`, and the markdown body from `order-fulfillment.agent.md`. Model, timeout, tools, skills, MCP servers, and system tools come from app-level configuration and discovery.

Functions using `agent_input` must be declared with `async def`. Each invocation receives its own entered Agent and may control sessions, options, middleware, streaming, and model-call timeout. The runtime closes the Agent when the handler exits; do not retain it beyond that invocation.

## Run locally

From `src/`, copy `local.settings.template.json` to `local.settings.json`, fill in the Foundry settings, start Azurite, then run:

```bash
func start
```

Invoke the HTTP function:

```bash
curl -X POST http://localhost:7071/orders/42 \
  -H "Content-Type: application/json" \
  -d '{"items":[{"sku":"A-100","quantity":2}]}'
```

Add JSON messages to the `orders` queue to invoke the event-driven handler.

For Durable activity and orchestrator bindings, see the sibling [`hybrid-durable-agent`](../hybrid-durable-agent/) sample.
