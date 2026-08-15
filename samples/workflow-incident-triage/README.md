# Workflow Incident Triage

Sample app for the experimental v1 **dynamic workflows** feature. The
agent investigates production incidents by fanning out evidence-gathering
tools, optionally waiting for in-flight signal to settle, and correlating
the results into a structured incident report.

See the [dynamic workflows reference](../../docs/workflows.md) for the full
feature design.

| Trigger | Custom Tools | Connectors | MCP Servers | Skills | Sandbox | Chat UI |
|---|---|---|---|---|---|---|
| HTTP | ✅ (workflow-safe) | | | | | ✅ |

## Feature status

This sample exercises the public experimental v1 workflow surface:
workflow-safe custom tools, LLM-authored DAGs, fan-out/fan-in, result
templating, durable timers, cooperative cancel, live-progress chat UI, and
the optional Durable Task Scheduler backend. It also demonstrates the
data-driven control flow from Issue #1276: a discovery tool returns a
bounded array, a per-service task fans out over it with `for_each`, an
item-level `when` predicate skips out-of-scope services, and a downstream
task consumes the ordered `{index, status, result}` aggregate. It
deliberately does not demonstrate v2 features such as sub-orchestrations or
sub-agent tasks.

## Run locally

Follow the [shared local development guide](../README.md#run-locally) for
Python env setup, `local.settings.json`, Azurite, and `func start`. This sample
has no extra prerequisites beyond `GITHUB_TOKEN` (used by the placeholder
agent) and Azurite (used by Durable Functions' default Azure Storage backend).

> [!IMPORTANT]
> Activate the venv in the same shell as `func start`. Core Tools uses the
> Python worker from whatever interpreter is on `PATH`; if the venv isn't
> active, the worker will miss `azure-functions-durable` and fail indexing.

## Run on Durable Task Scheduler (DTS)

This sample also runs unchanged on the **Durable Task Scheduler (DTS)**
backend, which is the recommended runtime for stakeholder demos: it
gives operators a portal at `localhost:8082` with per-instance task
state, retry history, and lineage for free — no new UI to build.

The agent persona, the workflow tools, the chat UI, and the engine all
stay the same. Only `host.json` and two app settings change.

### 1. Start the DTS emulator

```bash
docker run -d --name dts-emulator -p 8080:8080 -p 8082:8082 \
    -e DTS_USE_DYNAMIC_TASK_HUBS=true \
    mcr.microsoft.com/dts/dts-emulator:latest
```

- Port `8080` — gRPC endpoint the Functions host binds to.
- Port `8082` — the dashboard. Open <http://localhost:8082> in a browser.
- `DTS_USE_DYNAMIC_TASK_HUBS=true` lets the emulator auto-create the task hub the sample asks for. Data is in-memory and is lost on container restart, which is exactly what we want for a demo.

> [!IMPORTANT]
> Azurite must still be running. The Functions runtime requires
> `AzureWebJobsStorage` regardless of which Durable backend is in
> play; only orchestration state moves to DTS. Start Azurite the same
> way you would for the Storage backend.

### 2. Swap to the DTS `host.json` variant

A canonical DTS `host.json` lives at `src/host.dts.json`; it differs
from the default in two places:

- the `extensions.durableTask` block adds `hubName` and a
  `storageProvider` of type `azureManaged`; and
- the `extensionBundle` version range is pinned to **`[4.32.0, 5.0.0)`**
  rather than the looser `[4.*, 5.0.0)` the Storage variant uses.

The pin matters: the
`Microsoft.Azure.WebJobs.Extensions.DurableTask.AzureManaged` provider
first ships in **standard v4 extension bundle 4.32.0**. Earlier 4.x
bundles do not include it, and the Functions host will refuse to
start with `Storage provider type (azureManaged) was not found.
Available storage providers: Netherite, mssql, AzureStorage`. If you
already have an older 4.x bundle cached locally
(`%USERPROFILE%\.azure-functions-core-tools\Functions\ExtensionBundles`
on Windows, `~/.azure-functions-core-tools/Functions/ExtensionBundles`
elsewhere), the host will use it as long as the range still allows
it; pinning the lower bound to `4.32.0` forces a fresh download of a
bundle that contains the AzureManaged provider.

Use the helper script to swap it in and back. Run it from the sample
root in a PowerShell session — Windows PowerShell or `pwsh` on Windows,
[`pwsh`](https://learn.microsoft.com/powershell/scripting/install/installing-powershell)
on macOS/Linux:

```powershell
cd samples/workflow-incident-triage
./scripts/swap-backend.ps1 dts          # apply DTS host.json
./scripts/swap-backend.ps1 -Status      # show currently-active backend
./scripts/swap-backend.ps1 storage      # restore the default Azure Storage host.json
```

> [!NOTE]
> After a DTS swap, `git status` will show `host.json` as modified —
> that is expected; it now contains the DTS variant. The `storage`
> sub-command runs `git checkout HEAD -- host.json`, which restores
> the file from the last commit (not the index), so the swap-back is
> reliable even if you happen to have staged the swapped file. If you
> have other uncommitted edits in `host.json` you want to keep, copy
> them aside before running `swap-backend storage`.

### 3. Add the DTS app settings to `local.settings.json`

`src/local.settings.dts.json.template` has the two extra values DTS
needs:

```json
"DURABLE_TASK_SCHEDULER_CONNECTION_STRING": "Endpoint=http://localhost:8080;Authentication=None",
"TASKHUB_NAME": "default"
```

If you don't yet have a `local.settings.json`, copy the DTS template
straight in:

```bash
cp src/local.settings.dts.json.template src/local.settings.json
```

Otherwise, merge those two keys into your existing
`src/local.settings.json` (alongside `AzureWebJobsStorage`, which the
Functions runtime still requires regardless of Durable backend).

### 4. Start the host as usual

```bash
func start
```

Any workflow you launch from the chat UI now lands in the DTS
emulator. Open <http://localhost:8082> in a browser to see the
instance, drill into per-task state, replay history, and so on. Use
this view as the operator surface during demos — it is the part of
the story stakeholders are buying.

### Switching back to Azure Storage

```powershell
./scripts/swap-backend.ps1 storage
```

Restart `func start` after any swap so the host reloads `host.json`.

## Workflow-safe tools registered by this sample

`src/tools/incident_tools.py` defines seven synthetic-but-realistic
handlers decorated with `@workflow_tool`. `create_function_app()`
discovers them from the normal `tools/` directory and registers them with
the workflows engine when `main.agent.md` sets `workflows.enabled: true`.
They are workflow-only tools because the sample does not also decorate
them with `@tool` and does not expose plain public normal-tool functions
from that module:

| Tool | Args | Result shape |
|---|---|---|
| `fetch_logs` | `{service, window_minutes?: int = 30}` | `{service, window_minutes, lines: [str], errors, warnings}` |
| `fetch_metrics` | `{service, window_minutes?: int = 30}` | `{service, window_minutes, cpu_p99, memory_p99, latency_p99_ms, saturation}` |
| `fetch_deploys` | `{service, lookback_hours?: int = 24}` | `{service, lookback_hours, deploys: [{id, actor, summary, minutes_ago}]}` |
| `summarize_findings` | `{logs, metrics, deploys, service?}` (consume whole `${node.result}` values) | `{service, likely_cause, confidence: 'low'\|'medium'\|'high', evidence: [str], recommended_action}` |
| `discover_services` | `{incident}` | `{incident, count, services: [{name, tier, in_scope}]}` (bounded 3–5, low-tier items `in_scope: false`) |
| `inspect_service` | `{service, index?: int = 0}` | `{service, index, errors, saturation, healthy, headline}` |
| `summarize_scan` | `{incident?, findings}` (whole ordered `${node.result}` aggregate of `{index, status, result}` envelopes) | `{incident, scanned, skipped, unhealthy: [str], headline}` |

Outputs are deterministic functions of inputs so the demo narrative is
reproducible across runs and replays. The summary tools deliberately
consume the whole upstream result via `${node.result}` — there is no
need (and no benefit) to drill into nested paths from the plan.
`discover_services` always returns at least one out-of-scope service so
the data-driven `when` demo has an item to skip.

## Demo prompt

Open the chat UI at <http://localhost:7071/> and paste:

> *"We're seeing latency spikes and intermittent 502s on the `orders-api`
> service for the last 20 minutes. Pull recent logs, metrics, and the deploy
> history in parallel; let in-flight work drain for 30 seconds; then
> summarize what you find."*

The agent should:

1. Author a five-task workflow: three parallel fetches against `orders-api`,
   a `wait` task with `duration: PT30S` that depends on all three, and a
   final `summarize_findings` task that depends on the wait and consumes
   the three fetch results via `${...result}` templates.
2. Call `start_workflow`, return the `workflow_id` to the chat, and let the
   built-in live-progress card take over.
3. Within ~35 seconds, the workflow should reach `Completed`. The chat UI
   then auto-injects a synthetic user message containing a
   `<workflow-notification>` envelope; the agent
   calls `get_workflow_status` once and writes a short natural-language
   summary inline as a normal Copilot turn — closing the loop without you
   typing anything.

If you want to see cooperative cancellation, ask "actually cancel that"
while the workflow is mid-wait — the agent will call `cancel_workflow`,
the orchestration unwinds at the next wave boundary, the live card flips
to `Canceled`, and the auto-notification kicks in so the agent
acknowledges the cancellation in its own turn (with whatever partial
results were already gathered).

## Demo prompt (data-driven scan)

To exercise the Issue #1276 control flow, don't name a service — let the
workflow discover and fan out over them:

> *"Something is degrading across our platform but I'm not sure which
> services are involved. Discover the affected services, inspect each one
> that's in scope, and give me a consolidated summary."*

The agent should:

1. Author a three-task workflow: a `discover_services` task, one
   `inspect_service` task fanned out with `for_each: ${discover.result.services}`
   and an item-level `when: {ref: ${item.in_scope}, operator: equals, value: true}`
   (using `${item.name}` / `${index}` for its args), and a final
   `summarize_scan` task that depends on the logical `inspect` id and consumes
   its whole ordered aggregate via `${inspect.result}`.
2. Call `start_workflow` and hand off to the live-progress card. The card's
   structured status shows the fan-out expanding (e.g. `inspect: expanded (3/4)`)
   with one instance `skipped` — the out-of-scope low-tier service.
3. `summarize_scan` receives the ordered `{index, status, result}` envelopes
   (with `result: null` at the skipped position), reports how many services
   were scanned vs. skipped, and the workflow reaches `Completed`. The same
   auto-notification loop closes the summary inline.

If a discovery, inspection, or aggregation step hits a controlled error
(for example an out-of-range fan-out or an unresolved reference), the engine
returns a stable failure envelope and the workflow surfaces as `Failed`
rather than crashing the host — the live card reflects that terminal state.

## Demo dry-run script

`scripts/demo.ps1` is a presenter aid. It auto-detects the active
Durable backend (Azure Storage or DTS) from `host.json` and runs a
short pre-flight tailored to it: Azurite always (the Functions runtime
requires `AzureWebJobsStorage` regardless of the Durable backend), the
DTS gRPC endpoint when DTS is selected, and finally the Functions host
plus the workflow tools route. It then walks you through the narration
steps one at a time, pausing between each so you can read along to your
audience. When DTS is the active backend, an extra step appears between
"watch the workflow card" and "terminal state lands" that points the
presenter at the operator dashboard at <http://localhost:8082>. The
script does **not** drive the chat itself — pasting the prompt and
watching the live card is intentionally manual so the audience sees the
agent author the plan in real time.

Run from a PowerShell session (Windows PowerShell or `pwsh` on Windows;
`pwsh` on macOS/Linux):

```powershell
cd samples/workflow-incident-triage
./scripts/demo.ps1                      # full dry-run
./scripts/demo.ps1 -NoBrowser           # skip auto-opening the chat UI
./scripts/demo.ps1 -SkipPause           # rehearsal mode (no Enter prompts)
```

The script exits non-zero (with a clear remediation hint) if any
pre-flight check fails, so you can run it as the first thing before any
stakeholder demo and know within seconds whether the environment is
ready. Functional validation lives in the pytest suite — the script is
not a substitute for tests.

## What's still mocked

The four workflow-safe tools synthesize their evidence from a
deterministic hash of the args — there is no real log / metric /
deploy backend behind them yet. That is deliberate: we want the sample
to exercise every v1 workflow primitive end-to-end without dragging in
external service dependencies. A future milestone (or a fork of this
sample) can swap the handlers for real backends without touching the
agent persona, the workflow plan shape, or the engine.
