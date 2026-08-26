# Isolate Dynamic Workflow retry internals without changing behavior

This ExecPlan is a living document. The sections `Progress`, `Surprises &
Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to
date as work proceeds. Maintain this file in accordance with `PLANS.md`.

## Purpose / Big Picture

Dynamic Workflow retry behavior in PR #170 is correct and tested, but its
policy validation, Activity outcome interpretation, attempt transitions, and
Durable timer coordination are spread across `workflows/schema.py` and several
parts of `workflows/engine.py`. This makes the current implementation difficult
to maintain and makes a future migration to stable Durable Python 2.x harder to
evaluate.

After this refactor, users observe exactly the same retry behavior: the same
attempt context, exponential delays, status fields, error codes,
`continue_on_error`, cancellation behavior, telemetry, and independent
`for_each` retries. Internally, policy/outcome decisions and the runtime retry
state machine have explicit boundaries. The top-level Durable generator still
owns every `yield` and all Durable side effects, so replay behavior stays
visible and deterministic.

## Progress

- [x] (2026-08-25 22:18Z) Reviewed the current policy, Activity, scheduler, tests,
  FRD, and the preserved Durable 2.x spike.
- [x] (2026-08-25 22:23Z) Completed an independent architecture review and
  incorporated PR review comment `discussion_r3848942627`.
- [x] (2026-08-25 22:30Z) Added characterization tests for the Activity boundary and focused tests for
  the extracted retry-decision boundary.
- [x] (2026-08-25 22:33Z) Extracted behavior-preserving retry policy and outcome helpers.
- [x] (2026-08-25 22:33Z) Replaced the Activity boundary's compound validation clauses with strict
  internal Pydantic models.
- [x] (2026-08-25 22:34Z) Consolidated runtime attempt/state transitions while retaining Durable
  side effects and timer coordination in the top-level orchestrator.
- [x] (2026-08-25 22:37Z) Updated the FRD and architecture description.
- [x] (2026-08-25 22:39Z) Ran targeted tests, mypy, and the full CI-equivalent pytest gate. Ruff found
  one import-order issue; corrected it and re-ran Ruff successfully.
- [x] (2026-08-26) Moved the complete policy-aware Activity boundary into
  `workflows/policy.py` in response to PR review, leaving Durable scheduling and
  replay-sensitive side effects in `workflows/engine.py`.
- [x] (2026-08-26) Reworked the retry sample to persist a simulated inventory
  incident in Azure Blob Storage instead of branching on Activity attempt
  metadata.
- [x] (2026-08-26) Re-ran focused tests (`118 passed`), Ruff, strict mypy, and
  the CI-equivalent suite (`1181 passed, 53 deselected`).

## Surprises & Discoveries

- Observation: A fully replaceable `schedule_node(...) -> Durable task` adapter
  cannot be introduced on stable 1.x without adding a sub-orchestration.
  Evidence: custom retry requires multiple `yield` points, while the parent wave
  expects Durable tasks. The 2.x spike showed that a per-node
  sub-orchestration adds history, latency, and weaker cancellation.
- Observation: `retry_delays_ms` is also consumed inside the Activity for
  telemetry, not only by the orchestrator timer loop.
  Evidence: `invoke_policy_handler.finish()` selects the current delay for the
  `selected_delay_ms` telemetry field.
- Observation: `_policy_activity_context()` runs only in Activity functions, not
  in the replay-sensitive orchestrator.
  Evidence: its call sites validate policy-aware tool and Sub Agent Activity
  inputs and construct request-local handler context.
- Observation: Pydantic `ValidationError` can include rejected input values,
  including task arguments, if logged with traceback details.
  Evidence: independent diff review reproduced an identifier mismatch whose
  error text contained a secret placed in `args`.
- Observation: A customer-facing transient-failure sample should model state in
  the dependency boundary rather than inspect scheduler metadata.
  Evidence: the revised order tool reads and decrements Blob-backed
  `failures_remaining`, while engine tests continue to verify attempt three and
  decorator precedence independently.

## Decision Log

- Decision: Refactor Option A internally but preserve every observable behavior.
  Rationale: Preview 2.x cannot preserve attempt metadata, retry-wait status,
  structured exhaustion details, or cooperative cancellation.
  Date/Author: 2026-08-25, Human (TsuyoshiUshio) and Copilot.
- Decision: Do not add a per-node sub-orchestration now.
  Rationale: It adds production complexity before stable 2.x is available and
  does not preserve current behavior.
  Date/Author: 2026-08-25, Copilot.
- Decision: Keep every Durable `yield` and side effect in
  `_run_dynamic_workflow`.
  Rationale: The FRD explicitly requires replay-sensitive control flow to remain
  visible rather than hidden behind opaque abstractions.
  Date/Author: 2026-08-25, Copilot.
- Decision: Treat the current persisted effective execution shape as a stable
  internal history contract for this refactor.
  Rationale: Changing `retry_delays_ms` is unnecessary to establish useful
  seams and risks changing Activity validation, telemetry, and replay.
  Date/Author: 2026-08-25, Copilot.
- Decision: Use strict Pydantic models to validate the policy-aware Activity
  boundary, but retain TypedDict/plain JSON contracts inside the orchestrator.
  Rationale: Pydantic substantially simplifies untrusted Activity-input
  validation without affecting replay. Reconstructing models in the generator
  would add replay overhead and couple history to validator implementation.
  Date/Author: 2026-08-25, Copilot and independent architecture review.
- Decision: Do not introduce a retry-timer coordinator in this refactor.
  Rationale: Hiding timer identity and ordering has high replay/cancellation
  risk and little migration value. Timer creation, lookup, cancellation, and
  `task_any` races remain visible in `_run_dynamic_workflow`.
  Date/Author: 2026-08-25, Copilot and independent architecture review.
- Decision: Hide Pydantic input values and log only a fixed boundary-validation
  message.
  Rationale: malformed Activity inputs are untrusted and may contain sensitive
  handler arguments; validation diagnostics must not echo them.
  Date/Author: 2026-08-25, Copilot and independent diff review.
- Decision: Put policy-aware Activity execution, boundary validation, sanitized
  outcomes, telemetry, and retry disposition in `workflows/policy.py`.
  Rationale: this makes the policy contract reviewable as one unit while keeping
  every Durable call, timer, cancellation race, and yield in the orchestrator.
  Date/Author: 2026-08-26, Human (TsuyoshiUshio) and Copilot.
- Decision: Demonstrate transient dependency recovery with Azure Blob Storage.
  Rationale: customer tools should react to dependency state, not to
  `WorkflowTaskContext.attempt`; runtime attempt semantics remain independently
  covered by engine and E2E status assertions.
  Date/Author: 2026-08-26, Human (TsuyoshiUshio) and Copilot.

## Outcomes & Retrospective

The refactor preserves Option A's user-visible behavior while giving
policy-aware Activity execution, validation, outcomes, telemetry, and retry
disposition one internal module.
Activity telemetry and orchestration now share the same pure decision function.
Strict Pydantic validation replaces the compound boundary clauses requested in
PR review without entering replay-sensitive orchestration. Attempt preparation
and retry/continued-failure transitions have named helpers, while all Durable
timer coordination remains in the top-level generator.

After the PR feedback updates, focused policy, engine, and sample tests passed
(`118 passed`). The CI-equivalent suite passed (`1181 passed, 53 deselected`);
strict mypy and full Ruff passed. Independent diff
review found that Pydantic validation details could expose Activity inputs in
logs; input values are now hidden, validation logs use a fixed message, and a
regression test protects the boundary. The current implementation remains
runtime-managed; no preview dependency or sub-orchestration was introduced.
The sample now models two transient failures through Blob-backed incident state,
and no longer uses the workflow attempt number to manufacture failures.

## Context and Orientation

`src/azure_functions_agents/workflows/schema.py` defines the authored
`WorkflowRetryPolicy` and resolves it into the JSON-safe
`EffectiveWorkflowTaskExecution`. The resolved object contains
`max_attempts` and precomputed `retry_delays_ms`, which are persisted before
orchestration starts.

`src/azure_functions_agents/workflows/engine.py` registers the tool and Sub
Agent Activities plus one Durable orchestrator. Policy-aware Activities return
a structured success or failure outcome. The dynamic scheduler materializes
logical DAG nodes into executable instances, dispatches a wave of Activities,
and currently decides retry/continue/fail while applying each wave result.
Retry waits are Durable timers that race Activity tasks and the cooperative
cancel event.

`tests/test_workflow_engine.py` contains the behavioral contract for attempts,
delays, status, cancellation, fan-out, failures, and telemetry.
`docs/frds/0004-dynamic-workflows.md` is the durable design record.

The preserved 2.x spike is on branch
`tsuyoshiushio-durable-retry-2x-spike`, commit
`2f71a233fe3314385f25a7051654dbd9574d11b9`. It demonstrates that a future
native retry driver is mechanically possible but not behavior-equivalent.

## Plan of Work

First add characterization tests for the Activity boundary. They must prove that
malformed retry-delay length and values, elapsed-time overflow, invalid policy
source values, mismatched Activity/node ids, mismatched maximum attempts,
out-of-range attempts, and forged idempotency keys all become a sanitized,
non-retryable, non-continuable handler-contract outcome. Then add focused unit
tests around a pure retry-decision function. Given an effective execution
policy, current attempt, and validated Activity failure, it must return retry
with the selected delay, continue, or fail. Include boundary attempts,
infrastructure failures, and non-retryable failures.

Then extract the retry outcome types, decision logic, and strict Activity
boundary validation models into a small internal module under
`src/azure_functions_agents/workflows/`. This module must not import Azure
Functions or perform Durable side effects. Nested execution models use
`ConfigDict(strict=True, extra="forbid")`; the outer Activity envelope permits
the task-type-specific payload fields but validates every policy field. Model
validators preserve identifier, attempt, schedule-length, elapsed-time, and
idempotency invariants. Both Activity telemetry and the orchestrator must call
the same decision function so their retry classification cannot drift.

In `workflows/engine.py`, replace `_policy_activity_context()`'s compound
validation with the strict Activity model, then construct the existing
`WorkflowTaskContext`. Consolidate attempt preparation and retry-state
transitions into named synchronous helpers. Keep the in-memory timer registry,
`task_any`, result reads, Activity calls, timer creation, timer identity lookup,
cancellation, and every `yield` visibly in `_run_dynamic_workflow`.

Update `docs/frds/0004-dynamic-workflows.md` with an append-only decision that
records this behavior-preserving internal boundary and the conditions for a
future stable 2.x reassessment. Update `docs/architecture.md` only if its module
map or execution-stage description needs the new internal module.

Do not modify or restore the user's existing uncommitted DTS sample files.

## Concrete Steps

From the repository root:

    python -m pytest tests/test_workflow_policy.py tests/test_workflow_engine.py -q
    python -m ruff check src tests
    python -m mypy src
    python -m pytest --cache-clear --cov=./src/azure_functions_agents \
      --cov-report=xml --cov-branch tests

Update this plan after each milestone with observed results.

## Validation and Acceptance

The focused retry tests must prove retry, continue, and fail decisions use the
same attempt and delay semantics as before. Existing workflow engine tests must
continue to prove:

- attempts are numbered 1 through `max_attempts`;
- exact precomputed Durable timer deadlines are unchanged;
- `retry_wait`, next retry time, last failure, and attempt status are unchanged;
- terminal and malformed outcomes are not retried;
- cooperative cancellation cancels explicit retry timers;
- `for_each` instances retry independently;
- infrastructure failures remain retryable;
- telemetry reports the same decision and selected delay.
- malformed Activity boundary data always returns a handler-contract outcome
  rather than raising or being repaired;
- the effective execution value remains a plain JSON dictionary inside the
  orchestrator.

Ruff, strict mypy, and the full CI-equivalent test command must pass.

## Idempotence and Recovery

All edits are source, tests, plans, and documentation. Commands are safe to
repeat. If the refactor changes behavior, retain the new focused tests, revert
only the refactor edits made by this plan, and restore the previous in-file
helpers. Never revert the existing user-owned sample DTS changes.

## Artifacts and Notes

The 2.x spike report is available from:

    git show origin/tsuyoshiushio-durable-retry-2x-spike:plans/durable-retry-2x-spike-report.md

Its key conclusion is that the future driver seam is realistic, but stable 2.x
must be re-evaluated for attempt context, retry status, structured exhaustion,
and child cancellation before replacing Option A.

## Interfaces and Dependencies

The new internal retry module must expose typed, Azure-independent contracts for
Activity failure and retry disposition. It must accept
`EffectiveWorkflowTaskExecution`, an attempt number, and a failure
classification, and return a discriminated retry/continue/fail result.

Its strict Pydantic Activity models must preserve these invariants:
`timeout_ms` is 1,000 through 600,000; `max_attempts` is 1 through 5;
`retry_delays_ms` contains exactly `max_attempts - 1` strict integers from zero
through 900,000; attempt deadlines plus delays do not exceed 3,600,000 ms;
policy sources use only their three existing literals; Activity id equals node
instance id; attempt is in range; outer and effective maximum attempts match;
all identifiers are non-empty strings; and the idempotency key equals the
versioned key derived from workflow and node-instance ids. Validation errors
remain `ValueError`-compatible so Activities convert them to the existing
handler-contract outcome.

No dependency versions change. No public Python exports, authored schema,
persisted effective execution fields, HTTP contracts, or UI status schema
change.

Revision note (2026-08-25): Initial plan created after the Durable 2.x spike and
the decision to preserve Option A behavior while isolating its internals.

Revision note (2026-08-25): Added strict Activity-boundary Pydantic validation,
characterization tests, and explicit timer non-abstraction after independent
architecture review and PR comment `discussion_r3848942627`.

Revision note (2026-08-25): Recorded completed implementation, documentation,
and validation results.

Revision note (2026-08-25): Added sensitive-input-safe validation logging and a
regression test after independent diff review.
