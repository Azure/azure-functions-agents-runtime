# A2A Incident Triage

An experimental A2A 1.0 server that lets another agent send an incident report
to a focused triage specialist through an Azure Functions HTTP route.

| Trigger | Built-in Endpoints | A2A Profile | Workflows |
|---|---|---|---|
| | A2A Agent Card + JSON-RPC | Simple, non-streaming Message | |

## What this sample proves

- `GET /agents/main/.well-known/agent-card.json` returns the configured Agent
  Card rather than relying on domain-root discovery.
- `POST /agents/main/a2a` accepts an A2A 1.0 `SendMessage` JSON-RPC request.
- The request crosses the public Microsoft Agent Framework A2A adapter and the
  runtime's normal non-streaming runner before returning a direct A2A Message.
- A separate Microsoft Agent Framework `A2AAgent` client fetches the explicit
  per-agent card, selects JSONRPC 1.0, and consumes the Message through
  `A2AAgent.run()`.
- The JSON-RPC response preserves the caller's request id and conversation
  `contextId`.

This P3 profile is intentionally small. It does not support streaming, Tasks,
task continuation, subscribe/cancel, push notifications, REST bindings, or
Durable execution. `returnImmediately` is accepted with either value but has no
effect because the result is always one completed Message.

## Run locally

1. Create the server environment from the repository root. This environment
   contains the editable runtime, its `[a2a]` hosting extra, and the runtime's
   pinned MAF core 1.13 tuple:

   ```powershell
   python -m venv .venv-server
   .\.venv-server\Scripts\Activate.ps1
   python -m pip install -e ".[a2a]"
   ```

2. Create a separate client environment. It contains only the published MAF A2A
   client and its core 1.15+ closure; it does **not** install the editable
   Functions runtime:

   ```powershell
   python -m venv samples\a2a-incident-triage\.venv-client
   .\samples\a2a-incident-triage\.venv-client\Scripts\Activate.ps1
   python -m pip install -r samples\a2a-incident-triage\requirements-client.txt
   ```

   The server and client exchange the versioned A2A wire contract over HTTP, so
   their independent Python dependency graphs do not share a process. This lets
   the prototype use `agent-framework-a2a==1.0.0b260821` without changing the
   runtime's core/OpenAI/Foundry pins. A runtime-wide MAF upgrade requires its own
   compatibility PR and is not part of P3.

3. Copy `src/local.settings.template.json` to `src/local.settings.json`. Keep
   the default `foundry` provider and set `FOUNDRY_PROJECT_ENDPOINT` plus
   `FOUNDRY_MODEL`, or configure Azure OpenAI/OpenAI as described in the
   [shared local development guide](../README.md#run-locally). For Foundry or
   managed-identity Azure OpenAI, run `az login`.

4. Start Azurite in its own terminal:

   ```powershell
   azurite
   ```

5. Start the Functions host in a terminal with `.venv-server` activated:

   ```powershell
   .\.venv-server\Scripts\Activate.ps1
   cd samples\a2a-incident-triage\src
   func start
   ```

6. In another terminal with `.venv-client` activated, fetch the explicit card
   and call the agent through MAF:

   ```powershell
   .\samples\a2a-incident-triage\.venv-client\Scripts\Activate.ps1
   cd samples\a2a-incident-triage
   python client.py
   ```

   Change the story directly:

   ```powershell
   python client.py "payments-api began returning 429s after a traffic shift; queue depth is normal"
   ```

The sample publishes `http://localhost:7071/agents/main/a2a` in its Agent Card.
That URL is explicit configuration and must match the externally reachable
JSON-RPC route. Change `builtin_endpoints.a2a.url` before deploying behind
another origin or route prefix.

The sample uses anonymous HTTP auth solely for local experimentation. Before
deployment, set `builtin_endpoints.http_auth.mode` to `function` or `entra`.
Pass `--function-key` to either client when using function-key auth. The MAF
client supplies it through the public caller-owned `httpx.AsyncClient`, which is
used for both card resolution and JSON-RPC requests.

## Inspect the wire contract

The preserved raw client prints both the Agent Card and JSON-RPC envelope:

```powershell
python raw_client.py
python raw_client.py --return-immediately
```

Edit `raw_client.py` to try multiple text Parts or preserve the `contextId`
across turns. The flag demonstrates that either `returnImmediately` value returns
the same direct Message. Every protocol request must include
`A2A-Version: 1.0`; an absent header means A2A 0.3 and is rejected by this
experimental profile.
