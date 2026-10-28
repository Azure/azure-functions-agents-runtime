---
frd: 0012
title: Workflow fan-out groups
status: Finalized
author: TsuyoshiUshio
created: 2026-10-28
updated: 2026-10-28
issues: []
pull_requests: []
branch: tsuyoshiushio-dts-fan-out-analysis
---

# FRD 0012 - Workflow fan-out groups

## 1. Summary

Use a nested Durable Task composite to wait for parallel tasks in new Dynamic
Workflows. This structure lets Durable Task Scheduler (DTS) show a standard
Fan-out group. Keep the existing wait path for workflows whose stored input
does not select the new behavior. Keep per-task failure processing when a wave
permits continuation after failure.

## 2. Motivation / problem

The workflow engine waits for each task through repeated `when_any` calls.
This avoids a completion-notification defect in earlier Durable Task SDKs.
However, this structure does not give DTS the standard Fan-out group.

The corrected SDK supports `when_any([cancel, when_all(tasks)])`. The runtime
must use this structure to obtain the intended group. A dependency update alone
does not change the existing wait path.

The change also affects failure timing. The new group waits for all its children
before it reports a Durable task failure. The current path can report that
failure before other children finish.

## 3. Goals / Non-goals

**Goals**

- Show standard DTS Fan-out groups for eligible waves in new workflows.
- Use the same selection rule for static and data-driven workflows.
- Select execution behavior from stored orchestration input.
- Preserve the old wait path when the selection field is absent.
- Preserve result order, task identities, authorization, retries, and timer cleanup.
- Keep the current behavior of waves with continuation after failure.

**Non-goals**

- Change existing workflow authoring, management tools, or endpoints.
- Change Activity names or their existing display-name tags.
- Put all iterations of a large `for_each` node in one DTS group.
- Change the parallelism limit or the wave scheduler.
- Cancel running Activities when cooperative cancellation wins.
- Make the DTS graph a complete view of the authored dependency graph.

## 4. Proposed design

A wave is the bounded group of tasks that the scheduler dispatches and waits for
together. A group can contain tool Activities, Sub Agent Activities, and timers.

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| discover | `discovery/*` | None. Discovery remains read-only. |
| translate | `workflows/schema.py` | Add a typed internal wait-mode field to `WorkflowPayload`. Do not add a user configuration field. |
| register | `workflows/engine.py:register_workflows` | Keep the current orchestrator and Activity registrations. |
| execute | `workflows/tools.py` | Store the new wait mode when starting a workflow. |
| execute | `workflows/engine.py` | Select the wait path from stored input in both schedulers. |

### Authoring / API surface

There is no new front matter, tool argument, endpoint, or status schema.
The runtime writes `wave_wait_mode: "all"` into new orchestration input.
An absent field selects the existing per-task wait path.
An unsupported value must cause an explicit error before task dispatch.
Do not select the mode from an environment variable or the installed SDK version.

### Wait-path selection

When any task in the wave has stored `continue_on_error: true`, use the existing
`_await_continuation_wave` path, regardless of the stored wait mode. Preserve
immediate failure for failures that cannot continue, bounded continued results,
and cancellation restoration.

Otherwise, use the stored mode. For the new mode, create
`all_task = when_all(wave_tasks)` and wait for
`when_any([cancel_task, all_task])`.
Keep cancellation first in the candidate list.
If cancellation wins, use the existing cancellation result and timer cleanup.
If `all_task` wins, read its result explicitly. Preserve its input result order
and pass its failure through the existing failure decoder and diagnostics.
Reject an unexpected winner explicitly.

Only create a composite for a non-empty wave. Empty plans and skipped collections
must finish through the existing scheduler rules without waiting on
`when_all([])`.

### Failure and cancellation contract

For the new mode, a Durable child failure becomes visible only after all children
in the wave finish. The remaining children can include long-running Activities,
retries, or timers. Cancellation can win while those children remain pending,
even if one child has already failed.

Returned failure envelopes remain processed after a successful group wait.
Do not commit partial results from an unfinished ordinary wave.
Keep completed iteration instances from earlier waves. Restore instances in the
canceled dynamic wave to their existing pending states, even if an Activity in
that wave has finished.

The new mode intentionally changes failure timing only for eligible new
workflows. It does not change the failure contract of continuation waves.

### Compatibility

The stored input is the boundary between the old and new wait paths.
Do not add the new field to an existing instance during replay.
The old path keeps its wait, result-processing, and cancellation order.

Use an SDK release that supports nested completion and shared task observers.
Keep dependency declarations and the dependency lock consistent.
The SDK update also has its own replay behavior changes. The input field does
not isolate those changes. Test old runtime histories with the corrected SDK;
do not claim compatibility for arbitrary external orchestrators in the same app.

### DTS presentation

The target is one native Fan-out group per eligible parallel wave.
Keep the existing orchestration, tool, and Sub Agent display names.
Repeated iteration Activities can keep the same label; their input retains the
distinct task instance ID.
Continuation waves keep their current presentation.
Confirm the group in a real DTS dashboard rather than inferring it from a mock.

## 5. Decisions log

<!-- Never cite Decisions-log numbers in FRD prose or cross-references; describe the decision because numbers can shift. -->

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Existing workflow behavior | Replace all waits / select from stored input | Apply the new behavior only to new workflows; keep the old path when the field is absent | Human (TsuyoshiUshio) | 2026-10-28 |
| 2 | Continuation waves | Replace all waits / retain per-task processing | Retain per-task processing and its existing failure contract | Agent, proposed | 2026-10-28 |
| 3 | New ordinary-wave failure timing | Preserve immediate failure / use native all-task completion | Wait for every child before reporting a Durable task failure; allow cancellation to win before group completion | Agent, proposed | 2026-10-28 |
| 4 | Group labels | Add instance labels / keep existing tags | Keep existing labels and instance IDs | Agent, proposed | 2026-10-28 |
| 5 | Contract approval | Revise / approve | Approve the complete design; verify existing sample scenarios and add a scenario sample for missing cases | Human (TsuyoshiUshio) | 2026-10-28 |

## 6. Test plan

- [x] Regression: new stored input selects a nested composite; absent input keeps
  the old wait path. Unsupported modes fail before dispatch.
- [x] Success: static and dynamic waves preserve results when children finish
  out of order. Cover one task, multiple tasks, and multiple waves.
- [x] Boundaries: empty plans, skipped collections, mixed Activity/timer waves,
  and bounded `for_each` expansion keep their existing results.
- [x] Failure: a Durable child failure waits for siblings in the new mode.
  Returned failure envelopes retain their cause and decoding.
- [x] Cancellation: cover cancel-first, all-first, an already-completed cancel
  event, and cancellation after a child failure but before group completion.
  Verify timer cleanup and dynamic iteration restoration.
- [x] Continuation: both schedulers keep immediate disallowed failures,
  continued results, and cancellation behavior under either input format.
- [x] SDK integration: use real tasks and executor histories, not only fake
  pre-completed tasks. Reuse the cancel event across multiple waves.
- [x] Replay: recorded success, failure, and cancellation histories use the
  selected wait path with the corrected SDK. Both input formats replay with
  the same terminal result.
- [ ] Replay: old timer and native-retry histories keep their continuation
  with the corrected SDK.
- [x] Startup: workflow submission stores the mode without adding a public
  management-tool argument.
- [x] End to end: run tool fan-out/fan-in on DTS. Confirm the
  Fan-out group and readable labels in its dashboard. Also verify execution
  with the Azure Storage backend.
- [ ] End to end: run model-backed Sub Agent fan-out/fan-in on DTS.
- [x] Samples: use existing scenarios where possible. Add a scenario sample
  for deterministic fan-out, failure timing, and cancellation where existing
  samples do not provide those cases.

No configuration scenario fixture is required. The authored format is unchanged.

## 7. Docs impact

- [x] `docs/architecture.md`: describe stored wait-mode selection and the
  ordinary-wave failure timing.
- [x] `docs/workflows.md`: describe DTS groups, continuation-wave limits,
  and the failure/cancellation contract for new workflows.
- [x] `docs/frds/0004-dynamic-workflows.md`: link this contract where it changes
  the ordinary-wave timing of new workflows.
- [x] `docs/frds/README.md`: add this document to the index.

The configuration reference, front matter specification, trigger documentation,
and quickstart do not need changes. Their public contracts do not change.

## 8. Status & sign-off

- **Architecture review (phase 2):** Agent review completed on 2026-10-28.
  The change stays in workflow execution and its stored input contract.
  The review checked failure timing, continuation priority, shared cancel
  observers, empty waves, and SDK replay changes. It clarified that dynamic
  cancellation keeps completed iterations from earlier waves, not partial
  results from the canceled wave. No design blockers remain.
- **Human sign-off:** TsuyoshiUshio approved the complete design on 2026-10-28.
  The approval also requires sample-based checks and a scenario sample for
  missing cases.
- **Testing review (phase 4):** A separate direct review checked real pending
  SDK tasks, executor history replay, ordered results, shared cancel observers,
  failure timing, and continuation priority. Boundary tests exercise both
  stored input formats for empty and skipped collections, bounded expansion,
  timer cancellation, and retention of completed iterations.
- **Sample evidence:** The release evidence review sample passed 14 cases on
  Azure Storage and DTS. This includes all five scenarios and two legacy
  comparisons on each backend. The DTS Flow view showed **Fan-out (3 tasks)**
  for both the static and collection scenarios, with `check_release` labels.
  Four existing retry/timeout sample cases also passed on Azure Storage.
- **Gate evidence:** `python -m ruff check src tests` passed.
  `python -m mypy src` passed for 87 source files.
  `python -m pytest --cache-clear --cov=./src/azure_functions_agents --cov-report=xml --cov-branch tests -q --tb=short`
  passed: 2628 passed, 56 skipped, and 83 deselected.
  All commands used the worktree virtual environment and locked dependencies.
- **Remaining evidence:** Old timer and native-retry executor history replay
  have not been checked separately. Live retry/timeout cases do not prove that
  replay contract. The model-backed Sub Agent check could not run because
  `AZURE_OPENAI_DEPLOYMENT` and `AZURE_OPENAI_API_VERSION` were not configured.
  The model-free sample does not test model plan generation.
