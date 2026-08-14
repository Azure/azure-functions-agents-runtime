# Hybrid Function agent binding

This sample uses `AiApp` to keep ordinary Azure Functions triggers and deterministic application logic in Python while injecting a markdown-defined Serverless Agent in process.

It demonstrates:

- an HTTP-triggered function that validates an order before using a fresh raw
  `agent_framework.Agent`;
- a queue-triggered function that applies the same preprocessing before triage;
- a Pydantic validation boundary that normalizes identifiers, country, currency,
  shipping method, and line items;
- deterministic `Decimal` calculations and rule-based review signals;
- data minimization that excludes customer name, email, and unknown fields from
  the model prompt.

`order_processing.py` turns an operational order into a compact decision packet.
Application code owns facts such as subtotals and threshold checks; the agent owns
the contextual fulfillment assessment. Invalid HTTP orders receive `400`, while an
invalid queue message fails the invocation so normal queue retry and poison-message
handling can take effect.

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
  -d '{"customer":{"id":"C-1007","email":"buyer@example.com","loyalty_tier":"gold"},"currency":"usd","shipping":{"country":"ca","method":"overnight"},"items":[{"sku":"A-100","quantity":2,"unit_price":"24.95"},{"sku":"B-200","quantity":30,"unit_price":"40.00"}]}'
```

Add the same JSON shape with an `order_id` field to the `orders` queue to invoke
the event-driven handler.

For Durable activity and orchestrator bindings, see the sibling [`hybrid-durable-agent`](../hybrid-durable-agent/) sample.
