# Durable Agent Loop leadership demo narration

## 01 — Opening

Agents need powerful shell, file, and customer tools. But executing
model-directed code inside the trusted Functions worker expands the blast
radius and makes recovery depend on one opaque agent run. This spike separates
the trusted brain from isolated hands and adds a durable checkpoint at every
model and tool boundary.

## 02 — Architecture

Azure Functions and Durable Task Scheduler own the explicit reasoning loop,
session fences, retries, checkpoints, and human waits. APIM governs model and
privileged MCP traffic. Customer-owned ACA Sandbox runs local executable tools
with a separate identity and deny-by-default egress. Blob stores
integrity-bound content documents; Durable history stores references, hashes,
and counters.

## 03 — Live chapter

The centerpiece uses the real deployed app. The browser never receives a
Function key. A loopback proxy keeps authentication server-side and renders
only bounded status, counters, and safe aliases.

## 04 — New retained session

The first run creates a retained session. DTS starts the orchestration, the
model asks for a workspace write and read, and ACA creates a customer-owned
sandbox for the local tools.

## 05 — Checkpointed completion

The turn completes as three model steps and two distinct tool calls, rather
than one full-agent activity. The workspace is exported as an integrity-bound
checkpoint, while Durable status carries only safe metadata.

## 06 — Stopped sandbox reuse

After five minutes of inactivity, ACA stops the sandbox. The next turn uses the
same session and resumes the same sandbox alias instead of creating a new one.

## 07 — Workspace continuity

The resumed turn reads the exact marker written by the previous turn. This
proves both same-sandbox reuse and durable workspace continuity across HTTP
requests.

## 08 — Fault recovery

Now the fixed fault profile loses one tool activity acknowledgement after the
work is checkpointed. DTS retries that activity boundary. The durable receipt
reconciles the completed work instead of restarting the reasoning loop.

## 09 — Recovery result

The adaptive chain completes with seven model steps and six logical tool
calls. Earlier acknowledged work was not replayed from step zero.

## 10 — Human wait

For human clarification, the model emits the runtime-owned
request-human-input tool by itself. The orchestration advances to a durable
waiting state.

## 11 — Human resume

While waiting, no model or tool compute continues. An authenticated answer is
committed, an external event wakes the same orchestration, and execution
continues from the checkpoint.

## 12 — Observability

The same safe run alias can be correlated across the control room, DTS,
Application Insights, APIM, and ACA. Isolation and durability are visible as
independent operational boundaries.

## 13 — DTS timeline

The DTS dashboard exposes the orchestration timeline directly: entity fences,
model activities, tool activities, attempts, waits, and completion. This is
the step-level visibility that a single full-agent activity cannot provide.

## 14 — Application Insights

Application Insights recognizes the agent, model, tools, tokens, and
operational metrics. Sensitive prompt and tool content remains disabled. DTS
is the authoritative activity timeline; Insights provides correlated service
telemetry and measured dependencies.

## 15 — APIM

APIM separates model start, stable model-control polling, and Microsoft Learn
MCP into dedicated governed APIs. Request and response body capture is
disabled.

## 16 — ACA lifecycle

The Sandbox Group is customer-owned. The live inventory moved from running, to
stopped, back to running on reuse, and finally to zero through the configured
automatic inactivity policy. Failure and cancellation still retain explicit
runtime cleanup.

## 17 — Results

The bounded c-ten qualification completed ten of ten runs. A long adaptive
chain completed eighteen model steps and seventeen tool calls. Latest service
telemetry shows model requests at one point seven eight seconds p-fifty,
sandbox creation at nine hundred six milliseconds p-fifty, and sandbox
execution at fifty milliseconds p-fifty. These are private spike
measurements, not an SLA.

## 18 — Recommendation

The spike proves architectural feasibility: isolation, adaptive execution,
durable recovery, human control, and operational visibility can coexist. The
recommendation is a bounded productization design phase with explicit go or no
go gates, not a production launch.
