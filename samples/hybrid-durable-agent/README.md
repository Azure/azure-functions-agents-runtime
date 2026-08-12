# Hybrid Durable agent binding

This sample uses `DurableAiApp` to keep deterministic Durable Functions orchestration code in Python while invoking a markdown-defined Serverless Agent through replay-safe bindings.

It demonstrates:

- an HTTP starter using the standard Durable client binding;
- an async Durable activity receiving a fresh raw `agent_framework.Agent`;
- a synchronous generator orchestrator receiving `DurableAiAgent`, which schedules the runtime-generated `_afa_agent_binding_run` activity.

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
	-d '{"items":[{"sku":"A-100","quantity":2}]}'
```

The response contains the standard Durable status URLs for the new orchestration instance.

For ordinary HTTP and queue bindings using `AiApp`, see the sibling [`hybrid-function-agent`](../hybrid-function-agent/) sample.
