# Hybrid Durable activity binding

This sample uses `DurableAiApp` to keep deterministic Durable Functions orchestration code in Python while invoking a markdown-defined Serverless Agent from explicit customer-owned activities.

It demonstrates:

- an HTTP starter using the standard Durable client binding;
- a deterministic activity that validates, normalizes, calculates totals, derives
	review signals, and removes unnecessary customer PII;
- async Durable activities receiving a fresh raw `agent_framework.Agent`;
- a synchronous generator orchestrator that explicitly names and calls its agent
	activities.

The preprocessing activity turns the raw order into a compact decision packet.
Application code owns facts such as monetary calculations and threshold checks; the
agent first interprets fulfillment risk, then creates a plan from that assessment.
Keeping preprocessing and Agent calls in activities preserves orchestrator replay
determinism and gives production applications a natural place for database or service
enrichment.

The library does not generate an Agent activity for orchestrators. This sample owns
the activity names, JSON payload and result shapes, and retry policy. Durable Functions
therefore records only the customer-selected activity inputs and outputs in history,
while `agent` owns Agent hydration and cleanup inside each
activity invocation. The Agent activities return only final text; they do not return
`AgentResponse.messages`, because the complete model/tool transcript would increase
orchestration history and replay payload size and could retain sensitive content. Add
only required identifiers or aggregate usage fields, and include or summarize transcript
content only when the workflow explicitly needs it.

The binding projection reads `name`, `description`, the markdown body, and its `substitute_variables` parsing control from `order-fulfillment.agent.md`. Model, timeout, tools, skills, MCP servers, and system tools come from app-level configuration and discovery.

Activity handlers using `agent` must be declared with `async def`. Each activity invocation receives its own entered Agent; the runtime closes it when the handler exits, so do not retain it beyond that invocation.

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
