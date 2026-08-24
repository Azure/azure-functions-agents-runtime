# Workflow Retry Policy

This sample tells one story: an operations engineer recovers delayed order
`ORD-1001` after its inventory reservation API fails transiently. The workflow
loads the order, retries inventory reservation, and confirms the order.

The important behavior is policy precedence. The workflow plan asks for five
attempts and a 30-second timeout, while the `reserve_inventory` tool author
declares three attempts and a five-second timeout with `@workflow_tool`.
Tool-author declarations win, so the reservation fails twice and succeeds on
attempt three.

| Trigger | Custom Tools | Connectors | MCP Servers | Skills | Sandbox | Chat UI |
|---|---|---|---|---|---|---|
| HTTP | ✅ (workflow-safe) | | | ✅ | | ✅ |

## Why the plan is a Skill resource

The canonical plan lives at
[`src/skills/resilient-order-recovery/references/order-recovery-plan.json`](src/skills/resilient-order-recovery/references/order-recovery-plan.json).
The Skill loads it on demand with `read_skill_resource`.

Keeping the JSON as a Skill resource matters for two reasons:

1. `main.agent.md` stays a readable user persona instead of embedding a large
   system-test fixture.
2. The precedence demonstration has one source of truth. If a model rewrites
   the intentionally conflicting DAG policy, the sample no longer proves that
   decorator policy is authoritative.

The resource is under `src/`, so it is deployed with the Function App and is
available to the model at runtime. A repository path mentioned only in agent
instructions would not make the file readable by the model.

## Run locally

Follow the [shared local development guide](../README.md#run-locally) to create
a virtual environment, install `src/requirements.txt`, start Azurite, create
`src/local.settings.json`, and run `func start` from `src/`.

Open <http://localhost:7071/agents/main/> and ask:

> Recover delayed order ORD-1001 and complete it safely.

The agent should load the `resilient-order-recovery` Skill, read the canonical
plan resource, and call `start_workflow`. The workflow should finish
`Completed`. In status schema v3:

- `reserve_inventory.attempt` is `3`;
- `reserve_inventory.max_attempts` is `3`, not the DAG's requested `5`;
- `confirm_order.state` is `completed`.

## Opt-in model-backed E2E

The E2E script covers the boundary that unit tests cannot:

```text
natural-language prompt → model → Skill resource → start_workflow
→ Durable execution → terminal status
```

It is opt-in because it requires a real Foundry deployment and credentials.
Set both uniquely named variables:

```powershell
$env:AZURE_FUNCTIONS_AGENTS_SAMPLE_E2E_FOUNDRY_PROJECT_ENDPOINT = "https://..."
$env:AZURE_FUNCTIONS_AGENTS_SAMPLE_E2E_FOUNDRY_MODEL = "<deployment-name>"
python scripts/run-e2e.py
```

If both variables are absent, the script reports `SKIPPED` and exits zero. If
only one is set, it fails with a configuration error. When opted in, the script:

1. refuses to overwrite an existing `src/local.settings.json`;
2. creates ignored local settings from the two environment variables;
3. reuses a reachable Azurite or starts its own local Azurite process;
4. starts the Functions host on a free local port;
5. submits the documented natural-language prompt and validates the Skill
   resource calls and terminal workflow status;
6. stops only processes it started and removes only settings/data it created.

Run `az login` first so `DefaultAzureCredential` can authenticate to Foundry.
