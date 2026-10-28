# Release evidence review

Review release `REL-4821` with parallel test, security, and deployment checks.
The checks use synthetic data and different delays. The report keeps the input
order even when the checks finish in another order.

```text
tests ------+
security ---+-- summarize_release
deployment -+
```

## Run locally

Use Python 3.13+, Azure Functions Core Tools v4, and Azurite. Activate a virtual
environment. From this sample directory:

```powershell
Copy-Item src\local.settings.template.json src\local.settings.json
Set-Location src
python -m pip install -r requirements.txt
func start
```

For chat, configure a model provider in `local.settings.json`. Open
<http://localhost:7071/agents/main/> and send:

> Review release REL-4821 with parallel test, security, and deployment checks.

The model-free verifier does not need a model provider. In another terminal,
from the sample directory:

```powershell
python scripts\verify.py
```

The verifier validates each plan with the runtime and submits it through the
local Durable management API. It tests the engine, not model plan generation.
Normal chat submission uses `fhs_start_workflow`, which stores the new wait mode.

## Scenarios

| Scenario | Expected result |
| --- | --- |
| `parallel` | Three checks run in one wave, then the report completes. |
| `collection` | Discovery supplies three items to `for_each`; the report receives their ordered aggregate. |
| `failure` | The security check fails after two seconds. The wave waits for the remaining checks before the workflow fails. |
| `cancel` | The security check fails while another check and a timer remain pending. An operator cancellation stops the wait and discards the unfinished wave. |
| `continuation` | The optional security check fails. The report still runs with its bounded failure result. |

Use `--scenario collection` to run one case. Use `--legacy` to omit the internal
wait mode for comparison. In the legacy `cancel` case, the child failure wins.
Each run has a unique instance ID. Do not use the management API without
authentication in a public deployment.

## Inspect DTS

Start a DTS emulator with task hub `workflowfanoutreview`, then set the scheduler
connection string and task hub in `src/local.settings.json`. Stop the host, copy
`src/host.dts.json` over `src/host.json`, and restart it. Azurite is still required
for Functions host storage.

Run the verifier and find its printed instance IDs in the DTS dashboard.
For `parallel` and `collection`, inspect the native Fan-out group and the
`check_release` Activity labels. The grouping unit is a wave, not the complete
authored graph. Continuation waves retain their per-task wait path.

Stop only the host and emulator processes that you started. To return to Azure
Storage, remove the `durableTask` block from the local `host.json` copy and
restart the host. Do not remove task hubs that other apps use.
