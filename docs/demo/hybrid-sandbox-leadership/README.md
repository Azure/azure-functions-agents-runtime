# Hybrid sandbox leadership demo

This package explains and demonstrates the experimental hybrid execution path:

- Azure Functions hosts the agent endpoint, Hosted Skill, and Microsoft Agent
  Framework model loop.
- Azure API Management governs Foundry model traffic and remote MCP traffic
  through separate APIs.
- One fresh, customer-owned Azure Container Apps Sandbox executes every local
  customer tool call for the top-level invocation.
- Application Insights correlates sandbox creation, package delivery, executor
  readiness, tool execution, and cleanup without recording prompts, tool
  arguments, results, credentials, or sandbox identifiers.

The implementation remains experimental and private. The demo should support a
productization decision, not imply that the current environment is a supported
production service.

## Presentation package

`hybrid-sandbox-leadership-demo.pptx` is a six-slide executive deck:

1. **Customer-owned sandboxes for every tool call** - the outcome in one
   sentence.
2. **Powerful agents need a safer place to act** - why worker-local customer
   tools create an avoidable blast radius.
3. **One request. Two governed execution planes.** - the end-to-end visual
   architecture.
4. **One fresh sandbox, one bounded invocation** - the measured lifecycle and
   trust controls.
5. **The live proof is visible across Azure** - four actual views from the same
   correlated invocation.
6. **The optimization cut runtime latency in half** - measured impact and the
   proposed leadership decision.

Speaker notes contain a concise narration for every slide.

## Five-minute live sequence

| Time | View | What to show |
| --- | --- | --- |
| 0:00-0:30 | Slides 1-2 | State the problem and the split-plane bet. |
| 0:30-1:10 | Slide 3 | Trace the request through Functions, APIM, Foundry/MCP, and ACA Sandbox. |
| 1:10-1:40 | Hosted Skill endpoint | Submit one bounded deterministic prompt and show the successful streamed result. |
| 1:40-2:15 | Function monitoring | Show the real invocation, duration, success, and correlation window. |
| 2:15-2:50 | APIM telemetry | Show model and MCP requests through their protected APIs with body capture disabled. |
| 2:50-3:25 | ACA Sandbox Group | Show the invocation-created sandbox and the group returning to zero inventory. |
| 3:25-4:10 | Application Insights | Show the correlated `hybrid.progress` phases and component durations. |
| 4:10-5:00 | Slides 4-6 | Close on the security controls, measured latency improvement, and productization decision. |

Use a prerecorded run as the primary leadership presentation. Keep the live
environment available for questions and use a fresh live request only as a
drill-down. This avoids spending the meeting on portal latency or transient
telemetry ingestion delay while keeping every recorded view grounded in the
real retained resources.

## Demonstration safety

- Use only the retained nonproduction spike resources.
- Start from typed Sandbox Group inventory zero and verify eventual zero after
  the invocation.
- Never record credential entry, subscription keys, tokens, connection strings,
  managed-identity token values, request headers, or environment settings.
- Keep APIM request and response body capture at zero bytes.
- Use the deterministic `customer_probe` prompt so the result is repeatable and
  contains no customer data.
- Prefer resource names and a short request time window over full resource IDs.
- Crop or blur tenant, subscription, user, and machine-specific identifiers
  that are not needed to prove the flow.

## Measured results used in the deck

The optimized qualification ran the same deterministic prompt as the canonical
baseline:

| Measure | Baseline | Optimized | Change |
| --- | ---: | ---: | ---: |
| Runtime request average | 20.493 s | 10.649 s | 48.03% lower |
| Cold request | 27.706 s | 17.743 s | 35.96% lower |
| c1 p50 | 19.721 s | 9.108 s | 53.81% lower |
| c10 p50 | 27.560 s | 18.468 s | 32.99% lower |
| Package upload average | 5.510 s | 0.135 s | 97.55% lower |
| Executor readiness average | 2.145 s | 0.333 s | 84.47% lower |

All 21 optimized benchmark requests succeeded. The clean window recorded 21
sandbox creates, package uploads, package verifications, local tool calls,
lifecycle handoffs, and accepted delete requests; 42 model calls; zero hybrid
failures; and final typed sandbox inventory zero. APIM model p50 gateway
overhead was approximately 1.3 ms.

The durable source of truth is
`docs/decisions/0009-hybrid-sandbox-tool-execution-results.json` on the spike
branch.

