# Hybrid Durable Agent calls

This sample uses `DurableAiApp` to keep deterministic Durable Functions orchestration code in Python while invoking a markdown-defined Serverless Agent through `DurableAgentContext.call_agent()`.

It demonstrates:

- an HTTP starter using the standard Durable client binding;
- a deterministic activity that validates, normalizes, calculates totals, derives
	review signals, and removes unnecessary customer PII;
- a synchronous generator orchestrator that schedules stateless Agent calls;
- optional Durable retry policy on an Agent call.

The preprocessing activity turns the raw order into a compact decision packet.
Application code owns facts such as monetary calculations and threshold checks; the
agent first interprets fulfillment risk, then creates a plan from that assessment.
Keeping preprocessing and Agent execution in activities preserves orchestrator replay
determinism. The preprocessing activity remains customer-owned because it is the
natural place for database or service enrichment.

`DurableAiApp` registers one internal Agent activity when the first orchestrator is
decorated. `call_agent()` schedules that activity with a string or JSON-safe input and
returns only final response text. Each call hydrates a fresh Agent with persistent
history disabled, so the orchestrator passes the first assessment explicitly into the
second call. Complete model/tool transcripts are not recorded in orchestration history.

Use an explicit async `@app.activity_trigger` plus `@app.markdown_agent` when an
application needs a custom activity name, structured result, session behavior, or
idempotency contract.

The binding projection reads `name`, `description`, the markdown body, and its `substitute_variables` parsing control from `order-fulfillment.agent.md`. Model, timeout, tools, skills, MCP servers, and system tools come from app-level configuration and discovery.

The internal Agent activity is visible in Azure Functions indexing and Durable history
as `azure_functions_agents_run_markdown_agent`. Model timeout applies inside each
activity attempt, the Functions host timeout is the outer bound, and `RetryOptions`
schedules a complete fresh attempt after a failure.

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
