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
- The JSON-RPC response preserves the caller's request id and conversation
  `contextId`.

This P3 profile is intentionally small. It does not support streaming, Tasks,
task continuation, subscribe/cancel, push notifications, REST bindings, or
Durable execution. `returnImmediately` is accepted with either value but has no
effect because the result is always one completed Message.

## Run locally

1. Create and activate a virtual environment from the repository root, then
   install the sample:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -e ".[a2a]"
   ```

2. Copy `src/local.settings.template.json` to `src/local.settings.json`. Keep
   the default `foundry` provider and set `FOUNDRY_PROJECT_ENDPOINT` plus
   `FOUNDRY_MODEL`, or configure Azure OpenAI/OpenAI as described in the
   [shared local development guide](../README.md#run-locally). For Foundry or
   managed-identity Azure OpenAI, run `az login`.

3. Start Azurite, then start the Functions host in the activated environment:

   ```powershell
   azurite
   cd samples\a2a-incident-triage\src
   func start
   ```

4. In another activated terminal, fetch the card and send the demo incident:

   ```powershell
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
Pass `--function-key` to the client when using function-key auth.

## Inspect the wire contract

The client prints both the Agent Card and raw JSON-RPC response. Edit
`client.py` to try multiple text Parts, preserve the `contextId` across turns,
or toggle `returnImmediately`. Every protocol request must include
`A2A-Version: 1.0`; an absent header means A2A 0.3 and is rejected by this
experimental profile.
