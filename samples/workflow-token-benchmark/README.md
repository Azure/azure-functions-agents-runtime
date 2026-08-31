# Dynamic Workflow token benchmark

This sample measures when Dynamic Workflow reduces model-token usage compared
with ordinary tool calling. Both conditions process the same deterministic
service evidence, use the same model deployment, and publish the same canonical
JSON report. The only intended difference is whether intermediate inspection
results pass through the model context.

This is a benchmark sample, not a claim that Dynamic Workflow is always cheaper.
Small workloads may use more tokens because the model must author a workflow
plan. The benchmark reports those cases and identifies the crossover point
instead of selecting only favorable results.

## Compared conditions

| Condition | Queue | Execution |
| --- | --- | --- |
| Baseline | `token-benchmark-baseline` | The model calls `inspect_service_evidence` once per service, receives every full result, then passes all results to `publish_benchmark_report`. |
| Dynamic Workflow | `token-benchmark-workflow` | The model authors a fan-out/fan-in workflow. Durable Activities call the same inspection and publisher implementations, so intermediate results stay outside model context. |

Both agents use the same dual-registered tools and private deterministic core.
The workflow condition has normal tools disabled; the baseline has workflows
disabled. Workflow Sub Agents are not used.

## Request and report

Each paired trial sends the same request to both queues, changing only the
terminal Blob name:

```json
{
  "trial_id": "services-10-repeat-1",
  "services": [
    "checkout-api-00",
    "checkout-api-01"
  ],
  "evidence_lines": 40,
  "report_blob": "runs/services-10-repeat-1/baseline.json"
}
```

`evidence_lines` controls realistic per-service log volume. The default 40 lines
is roughly a few kilobytes per service. Evidence also includes metrics and
deployment history. It is derived from `trial_id` and service name without
wall-clock time, random numbers, UUIDs, or hash-order dependence.

The shared publisher reduces raw evidence into a compact report, canonicalizes
it with sorted object keys while preserving service order, and uploads UTF-8
JSON. A pair is valid only when the canonical baseline and workflow reports are
byte-for-byte equal.

## Measurement method

PR #147 added one compact local system log per completed MAF invocation:

```text
Agent token usage: {"agent_name":"...","input_tokens":...,"output_tokens":...}
```

The logger is under `azure.functions.*`, so these records appear in local Azure
Functions Core Tools output but are not exported to Application Insights
`AppTraces`. This sample also opts in to the runtime's versioned detail record:

```text
Agent token usage detail: {"schema_version":1,"usage_details":{...}}
```

The detail object preserves every non-negative integer dimension reported by MAF,
including provider-specific cache or reasoning fields when available. The
benchmark captures both records from local host stdout and stores the detail
mapping with each mode result.

Attribution is deliberately fail-closed:

- queue `batchSize` is 1 and `newBatchThreshold` is 0;
- `maxDequeueCount` is 1, so a failed message is not retried on the input queue;
- trials run serially and the next message is not submitted until the current
  report and usage record arrive;
- baseline requires exactly one compact and one detailed `primary` usage record
  from the baseline agent;
- workflow requires exactly one compact and one detailed `primary` record and no
  `workflow_subagent` compact record;
- detailed input/output counts must match the stable compact event;
- missing, duplicate, wrong-agent, or forbidden records invalidate the pair
  instead of being guessed or discarded.

For each valid pair:

```text
total_tokens = input_tokens + output_tokens
reduction = 1 - workflow_total_tokens / baseline_total_tokens
```

The harness alternates execution order and reports raw trials, median input,
output, and total tokens, paired median reduction, quartiles, latency, quality,
failures, and report mismatches for every workload size. The initial series is
1, 3, 5, 10, and 20 services.

### Latency

`report_latency_ms` is end-to-end completion latency from queue submission until
the canonical report Blob can be downloaded. It includes queue dispatch, model
work, tools or Durable Activities, and report publication. It excludes emulator
and Functions host startup. `elapsed_ms` additionally includes waiting for and
validating usage telemetry, so it is a harness diagnostic rather than the
published latency metric.

### Quality

Quality is measured two ways:

1. The deterministic oracle independently builds the expected report from the
   complete source evidence. `quality_score` is matching leaf fields divided by
   the union of expected and actual leaf fields, so missing, wrong, reordered,
   and fabricated fields reduce the `[0, 1]` score. `quality_exact` requires the
   complete report to equal the oracle.
2. The optional Vally prompt judge evaluates the same candidate and reference
   report with a fixed rubric. This is a semantic secondary measure, not a
   replacement for the exact oracle.

## Prerequisites

- Python 3.13+
- Azure Functions Core Tools v4
- Docker
- Azure CLI authenticated with `az login`
- A Microsoft Foundry project endpoint and deployed model

Create `src/local.settings.json` from the template and set
`FOUNDRY_PROJECT_ENDPOINT` and `FOUNDRY_MODEL`. Never commit this file.

```powershell
Set-Location samples\workflow-token-benchmark\src
Copy-Item local.settings.template.json local.settings.json
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Run

The E2E harness starts isolated Azurite and Durable Task Scheduler containers,
starts the Functions host, runs paired trials, validates reports, and cleans up
only resources it created. It prepends this checkout's `src/` directory to the
worker `PYTHONPATH`, so another worktree's editable install cannot change the
runtime under test.

```powershell
Set-Location samples\workflow-token-benchmark
python scripts\benchmark.py --repeats 3 `
  --results-dir results\2026-08-25-foundry
```

Use `--service-counts 1 3 5 10 20` and `--evidence-lines 40` to control the
series. Raw JSON results and the host log are written under
`.benchmark-results/`, which is ignored by Git. `--results-dir` additionally
writes shareable `benchmark.json` and per-mode ATIF trajectories. A named
directory under `results/` can be committed when preserving a benchmark run.

To grade one repeat per service count with Vally 0.14.0:

```powershell
python scripts\grade_quality.py `
  --results-dir results\2026-08-25-foundry `
  --repeat 1 `
  --judge-model gpt-5.4-mini
```

This requires Node.js 22 or newer and access to the selected Copilot judge model.
Each trajectory invokes the judge and incurs its normal cost. Omit `--repeat` to
grade every exported trajectory. Machine-readable JSONL verdicts are written
under the run's `vally/` directory. The fixed rubric is in `evals/eval.yaml`;
the benchmark workload is not rerun by Vally.

### Manual queue submission

To see the moving parts separately, start the dependencies and Functions host
yourself, then use `send_trial.py`. The sender only creates/submits queue messages;
it does not start or stop the host or emulators.

Terminal 1:

```powershell
docker run --rm --name token-benchmark-azurite `
  -p 10000:10000 -p 10001:10001 -p 10002:10002 `
  mcr.microsoft.com/azure-storage/azurite:latest azurite `
  --silent --skipApiVersionCheck --blobHost 0.0.0.0 `
  --queueHost 0.0.0.0 --tableHost 0.0.0.0
```

Terminal 2:

```powershell
docker run --rm --name token-benchmark-dts `
  -e DTS_TASK_HUB_NAMES=tokenbenchmark -p 8080:8080 -p 8082:8082 `
  mcr.microsoft.com/dts/dts-emulator:latest
```

Terminal 3:

```powershell
Set-Location samples\workflow-token-benchmark\src
.\.venv\Scripts\Activate.ps1
func start 2>&1 | Tee-Object -FilePath .\host.log
```

Terminal 4:

```powershell
Set-Location samples\workflow-token-benchmark
.\src\.venv\Scripts\Activate.ps1
python scripts\send_trial.py --mode baseline --trial-id manual-comparison `
  --service-count 3
# Wait for the baseline report/token log before submitting the next mode.
python scripts\send_trial.py --mode workflow --trial-id manual-comparison `
  --service-count 3
```

For a quick demonstration, `--mode both` submits both queues together:

```powershell
python scripts\send_trial.py --mode both --trial-id manual-comparison `
  --service-count 3 --evidence-lines 40
```

Token counts are not written into a project directory by the runtime. In manual
mode, watch `func start` or search the captured `src\host.log`:

```powershell
Select-String -Path src\host.log -Pattern "Agent token usage"
```

- `Agent token usage: {...}` contains stable input/output totals.
- `Agent token usage detail: {...}` contains `usage_details`, including cache or
  reasoning dimensions when the provider reports them.
- The terminal report is stored in Azurite under
  `token-benchmark-reports/runs/<trial-id>/<mode>.json`.
- In automatic `benchmark.py` mode, parsed token counts are saved in
  `.benchmark-results\benchmark-*.json`, and complete host output is saved in
  `.benchmark-results\host-*.log`.

## Interpreting results

A useful result reports the full curve. Dynamic Workflow may lose at one service,
approach parity at three or five, and improve at larger sizes. If no crossover
appears, first inspect the complete raw trials and model-call behavior. Adjust
only realistic dimensions such as service count or evidence lines, rerun the
entire series, and retain unfavorable sizes and failures.

Provider-reported input and output totals are authoritative. This initial sample
does not estimate instruction, history, tool-schema, or tool-result components.
Detailed dimensions are MAF invocation aggregates, and available keys vary by
provider, model, and SDK version. Missing cache/reasoning fields are not treated
as zero or estimated.
