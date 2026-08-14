# Hybrid Durable agent binding

This sample uses `DurableAiApp` to keep deterministic Durable Functions orchestration code in Python while invoking a markdown-defined Serverless Agent through replay-safe bindings.

It demonstrates:

- an HTTP starter using the standard Durable client binding;
- a deterministic activity that validates, normalizes, calculates totals, derives
	review signals, and removes unnecessary customer PII;
- an async Durable activity receiving a fresh raw `agent_framework.Agent`;
- a synchronous generator orchestrator that chains preprocessing, risk assessment,
	and planning with `DurableAiAgent`.

The preprocessing activity turns the raw order into a compact decision packet.
Application code owns facts such as monetary calculations and threshold checks; the
agent first interprets fulfillment risk, then creates a plan from that assessment.
Keeping preprocessing in an activity preserves orchestrator replay determinism and
gives production applications a natural place for database or service enrichment.

`DurableAiAgent` performs no model, network, or tool I/O in the orchestrator. The generated activity hydrates a fresh Agent, performs the runtime-managed call, closes the Agent, and records the JSON-safe result in Durable history.

The binding projection reads only `name`, `description`, and the markdown body from `order-fulfillment.agent.md`. Model, timeout, tools, skills, MCP servers, and system tools come from app-level configuration and discovery.

Activity handlers using `agent_input` must be declared with `async def`. Each activity invocation receives its own entered Agent; the runtime closes it when the handler exits, so do not retain it beyond that invocation.

## Run locally

From `src/`, copy `local.settings.template.json` to `local.settings.json`, fill in the Foundry settings, start Azurite, then run:

```bash
func start
```

Start `order_orchestrator` with a JSON order object:

```bash
curl -X POST http://localhost:7071/orders/orchestrations \
	-H "Content-Type: application/json" \
	-d '{"order_id":"D-2048","customer":{"id":"C-1007","email":"buyer@example.com","loyalty_tier":"gold"},"currency":"usd","shipping":{"country":"ca","method":"overnight"},"items":[{"sku":"A-100","quantity":2,"unit_price":"24.95"},{"sku":"B-200","quantity":30,"unit_price":"40.00"}]}'
```

The response contains the standard Durable status URLs for the new orchestration
instance. The completed orchestration output contains the order ID, risk assessment,
and fulfillment plan.

For ordinary HTTP and queue bindings using `AiApp`, see the sibling [`hybrid-function-agent`](../hybrid-function-agent/) sample.
