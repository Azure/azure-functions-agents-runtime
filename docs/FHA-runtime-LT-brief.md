# Azure Functions + Foundry Hosted Agent Runtime Benchmark

## Executive summary

The FHA spike completed **495/495 corrected requests successfully** with HTTP
200, exact output `OK`, no throttling, no application/server errors, and one
recovered DNS retry whose full retry time remained in the latency measurement.

The strongest result is session reuse:

- A reused immediate continuation reached first output in **1.992 s p50** and
  completed in **2.510 s p50**.
- A fresh request on an initialized Function role reached first output in
  **9.482 s p50** and completed in **9.906 s p50**.
- Immediate continuation therefore reduced median TTFT by **79.0%** and median
  total latency by **74.7%**.

The current FC1 Flex Consumption configuration is production-like rather than
stable-warm. It has zero Always Ready instances, and 20 of 455 matrix requests
overlapped natural on-demand role allocation. Those requests reached first
output in **17.514 s p50 / 23.149 s p95 / 31.664 s p99**. This is the closest
measured proxy to cold-start impact, but it is not a controlled cold-start
benchmark.

After role-start segmentation, concurrency 5 and 10 showed no monotonic tail
degradation relative to concurrency 1. Observed stage completion throughput
increased approximately 10x at concurrency 10.

## How to read this report

This scorecard answers a simple question:

> How long does a user wait before the agent visibly starts responding under
> different runtime and conversation conditions?

### Basic terms

- **Cohort:** A group of comparable requests. Separating requests into cohorts
  prevents fast continuations, new sessions, and Function startup from being
  blended into one misleading average.
- **TTFT (time to first token):** Time from submitting the request until the
  first response text appears. This is the best measure of how responsive the
  experience feels to the user.
- **Total latency:** Time from submitting the request until the complete
  response has arrived.
- **p50:** The median or typical result. Half of requests were faster and half
  were slower.
- **p95:** 95% of requests completed at or below this value; 5% were slower.
- **p99:** 99% completed at or below this value. It describes the extreme tail,
  but is exploratory here because some cohorts are small.

Because the test response was only `OK`, TTFT and total latency are close
together. A longer response would make total latency and streaming speed more
important.

### Two different kinds of warmth

There are two independent things that may already be ready:

1. **Function compute warmth:** An Azure Functions role/worker is already
   running and can accept the request.
2. **Conversation warmth:** The FHA/provider session was used recently and can
   continue without substantial reactivation work.

An existing conversation does not guarantee a fast response. Its history can
remain durable while the provider session becomes idle and requires additional
setup on the next turn.

### Cohorts in plain English

| Cohort | What was ready? | What the user experiences |
| --- | --- | --- |
| **Fresh, reused Function role** | Function compute was ready, but a new runtime/FHA conversation had to be created | A new conversation typically starts responding in about 9.5 seconds |
| **Immediate continuation, reused role** | Function compute and the recently used conversation were both ready | A follow-up typically starts responding in about 2 seconds |
| **Idle continuation, reused role** | Function compute was ready and history still existed, but the provider session had been idle for at least five minutes | History continues successfully, but response time returns to roughly 9.5 seconds |
| **On-demand role-allocation overlap** | Flex Consumption was starting new Function compute while handling the request | The closest measured proxy to cold-start impact; typical TTFT was about 17.5 seconds |
| **Combined production mix** | All of the above conditions mixed together | Represents what this exact AlwaysReady=0 deployment produced overall, but hides the reason for fast and slow requests |

The simplest interpretation is:

- **Active follow-up:** about **2 seconds** to first output.
- **New conversation:** about **9.5 seconds**.
- **Idle conversation:** about **9.5 seconds**, even though history is retained.
- **New Flex compute allocation:** about **17.5 seconds**.

The role-allocation cohort is natural FC1 Flex behavior, not a controlled
cold-start experiment. Stable-warm latency requires an Always Ready comparison.

## Tested configuration

| Item | Value |
| --- | --- |
| Source commit | `6304ca77` |
| FHA version | 10 |
| Function hosting | FC1 Flex Consumption |
| Runtime | Python 3.13 |
| Instance memory | 2048 MB |
| Maximum instances | 100 |
| Always Ready | 0 / unset |
| Minimum elastic instances | 0 |
| Worker count | 1 |
| Workload | Streaming chat request with fixed two-character output `OK` |
| Corrected sample | 495 valid-timing attempts |
| Matrix | 70/70/80 fresh-continuation pairs at C1/C5/C10; 15 idle continuations |

All primary client clocks began immediately before the first network attempt
and included DNS, connection setup, retry, headers, first SSE output, and
terminal completion.

## Latency scorecard

Times are seconds. p99 is exploratory, particularly for the 20-request
startup cohort and 15-request idle cohort.

| Cohort | n | TTFT p50 | TTFT p95 | TTFT p99 | Total p50 | Total p95 | Total p99 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Fresh, reused Function role | 203 | 9.482 | 11.174 | 11.901 | 9.906 | 11.790 | 12.462 |
| Immediate continuation, reused role | 216 | 1.992 | 2.913 | 3.640 | 2.510 | 3.668 | 4.228 |
| Idle continuation, reused role | 15 | 9.491 | 11.067 | 11.067 | 9.885 | 11.336 | 11.336 |
| On-demand role-allocation overlap | 20 | 17.514 | 23.149 | 31.664 | 17.831 | 23.573 | 32.194 |
| Combined corrected production mix | 495 | 8.975 | 12.667 | 20.891 | 9.332 | 13.249 | 21.531 |

The combined production-mix percentiles include fresh, continuation, idle, and
role-allocation traffic. They must not be presented as stable-warm latency.

## Concurrency behavior

Reused-role TTFT remained stable through concurrency 10:

| Scenario | C1 p50 / p95 / p99 | C5 p50 / p95 / p99 | C10 p50 / p95 / p99 |
| --- | --- | --- | --- |
| Fresh | 9.576 / 11.517 / 11.787 | 9.429 / 11.076 / 12.827 | 9.525 / 10.750 / 12.217 |
| Immediate continuation | 2.133 / 3.292 / 3.729 | 1.945 / 2.203 / 3.429 | 1.926 / 2.983 / 3.896 |

Observed stage completion throughput:

| Scenario | C1 turns/s | C5 turns/s | C10 turns/s |
| --- | ---: | ---: | ---: |
| Fresh | 0.086 | 0.468 | 0.895 |
| Immediate continuation | 0.314 | 1.917 | 3.652 |

This indicates useful bounded scaling through C10 for this workload. It is not
a maximum-capacity result because the test did not drive a controlled arrival
rate to saturation.

## What dominates latency

### Fresh requests

On reused roles, Function duration was approximately **9.6-9.7 s p50**, while:

- Responses create to hosted terminal was approximately **2.1-2.2 s p50**.
- Hosted ingress to terminal was approximately **1.9-2.0 s p50**.
- Model dependency duration was approximately **0.9-1.0 s p50**.

The largest fresh-turn opportunity is therefore the approximately **7.4-7.8 s
before the measured create-to-hosted chain**, not model inference.

### Immediate continuations

Immediate continuation avoided most of that setup:

- Reused-role Function duration: approximately **2.1-2.4 s p50**.
- Hosted ingress to terminal: approximately **1.4-1.6 s p50**.
- Model dependency: approximately **0.8-1.0 s p50**.

This is the primary evidence that durable session reuse is valuable.

### Idle continuations

All 15 idle requests reused initialized Function roles, but median TTFT returned
to fresh-like latency at **9.491 s**. For the eight fully traced idle requests:

- Responses create to hosted ingress: **7.125 s p50**.
- Hosted ingress to terminal: **1.911 s p50**.
- Model dependency: **0.903 s p50**.

The idle penalty is provider/session reactivation before hosted-agent ingress,
not Function startup or model execution.

### Flex role allocation

The 455-attempt matrix allocated 21 Function roles:

- 20 requests directly overlapped startup.
- 21 requests were first on a role.
- 434 requests reused initialized roles.
- Startup overlap occurred on **4.4%** of matrix requests.

Startup overlap added about **8.0 s** to median TTFT compared with a reused
fresh request. Always Ready capacity can reduce this tail, but it will not
remove the larger fresh/idle provider-session setup cost.

## Reliability and session behavior

- Corrected client requests: **495/495 successful**.
- Matrix Function requests: **455/455 successful**.
- Throttled requests: **0**.
- Application/server errors: **0**.
- Server/model retries: **0**.
- Client retries: **1**, a recovered DNS resolution failure.
- Fresh/immediate session pairs: **220/220 completed both turns**.
- Idle continuations: **15/15 successful** with existing provider sessions.

The benchmark proves successful runtime/provider-session reuse. The fixed `OK`
prompt does not prove semantic history fidelity; a separate conversational
assertion workload is needed for that claim.

## Traceability

| Trace surface | Completeness |
| --- | ---: |
| Hosted invoke + MAF + model chain | 455/455 |
| Choice event | 455/455 |
| Fresh `fha.responses.create` span | 220/220 |
| Immediate continuation create span | 80/220 |
| Idle continuation create span | 8/15 |
| Function AppRequest to worker shared OperationId | 0/455 |
| Exact worker-role classification | 451/455 |

The most important observability gap is the missing common key between the
Function host request and worker/FHA trace. Add the Functions invocation ID to
worker/root/create spans and establish a valid W3C parent before creating the
worker span.

## LT-ready claims

The data supports these statements:

1. **Reliability:** The corrected spike workload completed 495/495 requests
   without throttling or server errors.
2. **Session reuse:** Immediate continuation reduced median TTFT by 79% and
   median total latency by 75% relative to fresh requests on initialized roles.
3. **Bounded scale:** Reused-role p95 did not degrade monotonically through
   concurrency 10; observed completion throughput increased approximately 10x.
4. **Flex behavior:** With zero Always Ready instances, 4.4% of matrix requests
   overlapped natural role allocation, producing a 17.5 s median TTFT cohort.
5. **Optimization target:** Fresh and idle latency is dominated by pre-hosted
   provider/session setup rather than model execution.

Do not claim:

- Stable-warm p95/p99 under the current AlwaysReady=0 configuration.
- A controlled cold-start percentile.
- Stable per-cell p99 from this sample size.
- Semantic conversation-history fidelity.
- Maximum throughput or saturation capacity.

## Recommended next actions

1. **Instrument the 7.4-7.8 s fresh path.** Add spans for runtime admission,
   session lookup/creation, durable reads/writes, connection acquisition,
   request construction, and first provider acknowledgement.
2. **Instrument idle reactivation.** Split the 7.1 s create-to-hosted-ingress
   delay into provider-session lookup, rehydration, connection, queue, and
   acknowledgement phases.
3. **Fix trace correlation.** Propagate one invocation/trace key across client
   headers, Function AppRequests, worker spans, Responses create, and FHA spans.
4. **Run an Always Ready comparison.** Configure sufficient Always Ready HTTP
   capacity, repeat the C5 canary, then rerun a smaller matched matrix. Treat it
   as a separate configuration cohort.
5. **Measure realistic streaming.** Use fixed-size 100/500-token responses to
   capture output rate, first-token latency, inter-delta-gap p95, and stream
   duration. The two-character workload cannot measure streaming quality.
6. **Measure cost efficiency.** Capture model tokens, Function GB-seconds,
   Foundry usage, and cost per successful fresh/continued/idle turn.
7. **Validate history fidelity.** Use deterministic multi-turn assertions
   across immediate, idle, and post-version continuations.
8. **Strengthen tails.** Run thousands of turns per homogeneous cohort across
   multiple time windows before presenting p99 as stable.

## Preserved artifacts

- Client report and raw data: `files/fha-metrics/benchmark/`
- Final telemetry analysis and KQL: `files/fha-metrics/telemetry/`
- LT scorecard and methodology: `files/fha-metrics/scorecard/`
- Corrected pilot and trace map: `files/fha-metrics/corrected-pilot/`
