# In-app Function tools (Python spike)

An order assistant calls existing HTTP and Storage Queue Functions as tools,
plus a standalone typed Python tool. No `tools/` directory is needed.

- `get_order`: HTTP Function and order-lookup tool.
- `ship_order`: queue-triggered Function and order-shipping tool.
- `business_days_between`: standalone tool, scoped to the `main` agent.

Trigger-backed tools declare a Pydantic `schema` for their JSON body.
Agent calls run the handler in-process; they do not send HTTP requests or enqueue
messages. Real HTTP requests and queue messages still use the ordinary triggers.
The existing `tools.exclude` denylist and `tools: false` remain applicable.

## Run locally

Requires Python 3.13+, Azure Functions Core Tools v4, Azurite, and access to a
Foundry model. This sample uses the modified runtime in this checkout.

From `src`:

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp local.settings.template.json local.settings.json
```

Set `FOUNDRY_PROJECT_ENDPOINT` and `FOUNDRY_MODEL` in the ignored
`local.settings.json`. Authenticate with `az login` on the machine running the
host, or configure credentials supported by `DefaultAzureCredential`.
Never commit credentials.

Start `azurite --skipApiVersionCheck` in another terminal, then run:

```bash
func start
```

Check the ordinary HTTP Function without calling a model:

```bash
curl http://localhost:7071/api/orders \
  -H 'Content-Type: application/json' -d '{"order_id":"1002"}'
```

Then exercise agent-selected tools:

```bash
curl http://localhost:7071/api/agents/main/chat \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Look up order 1002, ship it with UPS, then look it up again and report its status."}'
```

The resulting order status should be `shipped via UPS`. Independently verify
it by repeating the `/api/orders` request. The chat UI is at
`http://localhost:7071/api/agents/main/`. More requests are in `test.http`.

## Limits and cleanup

Shipping only updates an in-memory dictionary; nothing is actually shipped.
Use one Python worker for this demonstration. State is neither shared across
workers nor durable, and restarting the host resets it. A successful queue-tool
call returns `completed`; the agent re-reads the order to confirm the change.
Synthetic payloads do not reproduce broker settlement, host bindings, or HTTP
ingress authentication. HTTP handlers use the runtime's FastAPI `Request`.

Stop Core Tools and Azurite with Ctrl+C. Do not deploy this spike as a production
order service.
