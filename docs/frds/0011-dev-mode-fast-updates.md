---
frd: 0011
title: Dev Mode for fast app updates
status: In review
author: tsuyoshiushio
created: 2026-10-14
updated: 2026-10-14
issues: []
pull_requests: []
branch: tsuyoshiushio-dev-mode-frd
---

# FRD 0011 - Dev Mode for fast app updates

## 1. Summary

Add a development mode to Azure Functions Flex Consumption to reduce the time
between an app change and its execution. Dev Mode limits the whole app to one
active platform worker and disables per-trigger scaling. When that worker is
running, an update changes the app content and restarts the runtime inside the
same worker. It does not replace the worker or change the front-end routing
endpoint. Hosted Skills is the first use case. The update contract must also
support other languages.

This is a feature proposal, not a description of an available feature. Setting
and API names are provisional.

## 2. Motivation / problem

Developers need to test frequent changes to skills, prompts, tools, functions,
and triggers. A normal deployment does more than put new code in place:

| Operation | Work that can increase update time |
| --- | --- |
| Build and package storage | Build the app, create a ZIP, and upload it to Blob storage. |
| Worker preparation | Specialize a worker, put or mount the content in place, and start the runtime. |
| SyncTriggers propagation | Publish trigger and app metadata to the services that use it. |
| Old worker drain | Stop new invocations and wait for active invocations before removing old workers. |
| Front-end synchronization | Stop routing to old workers and distribute the new routing state. |

These operations support normal deployment and production operation. With many
workers, propagation, drain, and routing synchronization can add more delay.
Development updates need a different tradeoff: fast feedback instead of scale
and continuous availability.

Dev Mode removes worker replacement and routing changes from an ordinary update
on a running worker. It does not remove the need to build dependencies or stop
active invocations. The time saved has not been measured.

## 3. Goals / non-goals

### Goals

- Make Dev Mode available after an initial normal deployment.
- Limit the whole app to one active platform worker, not one worker per trigger.
- Run all supported trigger listeners in that worker.
- Keep the platform worker and routing endpoint during an ordinary runtime update.
- Support changes to code, skills, function definitions, timer schedules, and
  trigger definitions.
- Do not require normal SyncTriggers propagation before the new revision can run.
- Report update progress, failures, and the revision that is running.
- Keep normal deployment and scaling behavior when Dev Mode is off.
- Define a common update contract with language-specific restart methods.

### Non-goals

- Production high availability or updates without an interruption.
- Scale-out while Dev Mode is on.
- Transfer process memory or active invocations to the new runtime.
- Remove build and dependency preparation.
- Keep every language process alive when its code changes.

Trigger-definition changes are in scope. The scope of automatic changes to
external resources and event subscriptions is not yet decided.

## 4. Proposed design

### 4.1 Terms and mode setting

A **platform worker** is the execution instance that the platform assigns to
the app. A **language worker** is a process, such as Python, that runs app code.
The platform worker stays in place during an update. The ScriptHost and language
workers can be stopped and created again.

The proposed setting is:

```text
AZURE_FUNCTIONS_DEV_MODE=true
```

Use an app setting or a platform app configuration field as the shared source of
truth. Do not use `host.json` as the only source: allocation, routing, and health
services must also know the mode. The final setting location is open.

The first deployment uses the normal path. Enabling or disabling Dev Mode is a
separate operation from a fast content update. This proposal adds no Dev Mode
field to agent front matter or `agents.config.yaml`.

### 4.2 Worker lifetime

The app has at most one active platform worker. It does not get separate workers
for different trigger groups. Stop, delete, maintenance, health checks, and
failure recovery remain platform operations.

Whether the worker stays running or can scale to zero is open:

| Option | Metadata use |
| --- | --- |
| Keep one worker running | The Host reads new metadata and creates new listeners. Scaling does not need a Blob metadata watcher. |
| Allow zero or one worker | The Scale Controller reads current metadata to decide when to start the worker. This must work when no Host is running. |

The final allocation path must enforce the app-wide limit for HTTP, event,
Always Ready, and recovery requests. A per-group maximum is not sufficient.
Failure recovery must also respect the active-worker limit.

### 4.3 Package and metadata contract

Store each ZIP and its trigger metadata as a new revision. Do not overwrite live
content. Publish a revision only after both objects are complete.

| Field | Purpose |
| --- | --- |
| Revision identifier | Identify one update. |
| Package reference and integrity data | Locate and check the ZIP. |
| Trigger metadata reference | Configure listeners and, if needed, wake-up monitoring. |
| Update state | Distinguish the requested revision from the revision successfully applied. |

Code and metadata must refer to the same revision. Never start an app with code
from one revision and trigger metadata from another.

How metadata is created and validated is open. A client can supply it, or a Host
can derive it from the package. A scale-to-zero design needs the metadata before
an app worker starts. The metadata contract must account for that requirement.

Management data, such as function inventory and keys, must still be available to
the services that need it. Do not assume that all SyncTriggers data is unnecessary.
Metadata that contains secrets needs access controls and must not appear in logs.

### 4.4 Update operation

For a running worker:

1. Upload the runnable ZIP and matching metadata.
2. Submit an authenticated update request for that revision.
3. Download the package to a separate location and check it.
4. Stop accepting new invocations. Give active invocations a bounded time to finish.
5. Stop the old ScriptHost and the required language workers.
6. Switch the content to the new revision.
7. Read fresh metadata and start the runtime, language workers, and listeners.
8. Confirm startup and record the applied revision.

Serialize update operations. A repeated request for the same operation must not
cause inconsistent state or unnecessary repeated application.

Request acceptance is not update completion. The API must expose operation state,
the applied revision, and failures. A successful response from the existing
restart endpoint alone is not proof that new code is running.

When the app has zero workers, the platform must preserve the requested revision
and the operation state. The policy for starting a worker immediately or waiting
for an event is open. No operation may report that a revision is running before
startup is confirmed.

Rollback and recovery rules are open. They must distinguish a requested revision
from a successfully applied revision.

### 4.5 Trigger changes

| Change | Required behavior |
| --- | --- |
| Timer schedule | Create the new timer listener. If zero workers are allowed, also update the schedule used to start a worker. |
| Function or trigger addition/removal | Rebuild function metadata and listeners. Update wake-up monitoring where needed. |
| Reference to an existing queue or other event source | Update the listener and any required Scale Controller monitoring. |
| External event subscription | Identify and perform the required external registration, or report that a separate operation is needed. The automation scope is open. |

For zero-or-one operation, add Blob revision detection and re-create the affected
metadata and monitoring state. Reading an event source alone does not tell the
Scale Controller that its configuration has changed.

Do not require normal SyncTriggers propagation or app worker replacement for
these metadata updates.

### 4.6 Runtime, routing, and health

Use sequential stop and start for content updates. Confirm that old language
workers have stopped before starting new code. Refresh metadata and process
caches; a ScriptHost restart alone does not guarantee this for every runtime path.

Keep the routing endpoint while the runtime restarts. A short interruption is
permitted. Define whether requests wait, retry, or receive an unavailable response.
Long-running and streaming requests need explicit interruption rules.

An update state can temporarily affect health evaluation, but it must have a time
limit. Do not hide a persistent failure as an update in progress.

Control the Host's automatic SyncTriggers calls in Dev Mode. Required management
publication must not silently restore normal worker replacement.

### 4.7 Component changes

The following table identifies the changes indicated by the source review. Exact
interfaces remain subject to architecture review.

| Component | Required change |
| --- | --- |
| Websites / DataRole | Recognize Dev Mode. Separate fast content updates from changes that require the normal app lifecycle. |
| Scale Controller / Trigger Monitor | Disable per-trigger scale-out. If zero workers are allowed, detect Blob revisions and rebuild metadata and monitoring without replacing the app worker. |
| Legion allocation | Enforce the app-wide limit on every allocation path, including Always Ready and failure recovery. |
| Legion revision management | Track Dev revisions without starting normal content-version drain. Define which revision a replacement worker uses. |
| Legion content management | Add a safe content switch inside an existing worker. Reuse package download and placement functions where suitable. |
| Functions Host | Run all supported listeners in one worker. Combine content switching, sequential restart, and metadata refresh. |
| Language worker management | Stop old processes and connections and start fresh ones. Externally managed language workers need a corresponding operation in their owner. |
| Front end and health services | Keep the endpoint during updates. Define request behavior and bounded update health handling. |
| Update client | Upload matching content and metadata, submit an authenticated request, and check the result. |
| Hosted Skills / Agent Runtime | Rebuild discovery, configuration, registration, and execution state in the new language process. |

The Host already has an inner ScriptHost restart path. It also has a listener
path without function-group filtering. These are reuse candidates. Flex currently
treats deployed content as read-only, so a content switch needs a new platform
operation or a Dev-specific content arrangement.

Preserve this repository's pipeline boundaries:

| Pipeline stage | Modules | Intended behavior |
| --- | --- | --- |
| Discover | `discovery/*` | Read the new project tree in a fresh process. Do not add package download or content switching here. |
| Translate | `config/*`, `app.py` | Build and validate fresh configuration and catalogs. |
| Register | `registration/*` | Register functions and endpoints from the new configuration. |
| Execute | `runner.py`, selected `harness/*` modules | Run new code without stale process-local state. Do not transfer active invocations. |

### 4.8 Compatibility and open questions

Dev Mode is opt-in. Normal deployment, authoring formats, and the default agent
runtime remain unchanged. Define unsupported update cases explicitly rather than
silently using a different deployment path.

| Question | Decision needed |
| --- | --- |
| Worker lifetime | Keep one worker running, or allow zero and one? |
| Mode setting | Final name, location, and enable/disable conditions. |
| Update API | Endpoint owner, authorization, and operation-status contract. |
| Metadata | Creation, validation, Blob format, and detection interval. |
| Content switch | Staged local content with a reference switch, or remount. |
| Active invocations | Drain deadline, cancellation, and streaming behavior. |
| Failure and recovery | Rollback, zero-worker startup failure, and recovery revision. |
| External registration | Which resource and subscription changes are automatic? |
| App configuration | Which app setting, identity, and network changes can use the fast path? |
| Languages | Restart methods and support conditions beyond Hosted Skills on Python. |
| Performance | Measurement conditions and acceptance thresholds. |

Use a common update contract, but permit language-specific restart methods.
Split-runtime workers require coordination with the external process owner.
Some runtimes can require a full runtime-process restart rather than an inner
ScriptHost restart. Do not promise one restart method for every language.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| --- | --- | --- | --- | --- | --- |
| 1 | Primary purpose | Normal production deployment / development feedback | Fast app updates during development. | Human (Tsuyoshi Ushio) | 2026-10-14 |
| 2 | Allocation limit | One worker per trigger group / one for the whole app | At most one active worker for the whole app; disable per-trigger scaling. | Human (Tsuyoshi Ushio) | 2026-10-14 |
| 3 | Running-worker update | Replace the worker / restart the runtime in place | Keep the platform worker; switch content and restart the runtime. | Human (Tsuyoshi Ushio) | 2026-10-14 |
| 4 | Language scope | Hosted Skills only / a common design for other languages | Hosted Skills first, with a contract that supports other languages. | Human (Tsuyoshi Ushio) | 2026-10-14 |
| 5 | Trigger changes | Exclude trigger changes / include them | Include timer schedules and function/trigger changes. External registration automation remains open. | Human (Tsuyoshi Ushio) | 2026-10-14 |
| 6 | Code and metadata consistency | Independent updates / matched revisions | Use matching code and metadata revisions; report acceptance separately from successful application. | Human (Tsuyoshi Ushio) | 2026-10-14 |

## 6. Test plan

These are acceptance tests for the proposed feature, not tests already completed.

- [ ] Update skills, prompts, and tool code; confirm execution uses the new revision.
- [ ] Confirm a running-worker update keeps the platform worker and routing endpoint.
- [ ] Confirm old imports, skill inventories, catalogs, and metadata do not remain.
- [ ] Change a timer schedule and add/remove functions and triggers.
- [ ] Run HTTP and supported event listeners in the same worker.
- [ ] Reject incomplete, invalid, or mismatched package and metadata revisions.
- [ ] Test concurrent requests, retries, and duplicate operation identifiers.
- [ ] Test package validation failure, startup failure, and failure during content switching.
- [ ] Test bounded invocation drain, cancellation, and streaming interruption.
- [ ] Test revision selection after worker failure and replacement.
- [ ] If zero workers are supported, test new trigger metadata and wake-up behavior
  while no app worker is running.
- [ ] Verify the app-wide limit across allocation and recovery paths.
- [ ] Verify normal deployment and scaling when Dev Mode is off.
- [ ] Measure time from update start to confirmed execution of the new revision.
  Compare with normal deployment, both with and without build time.

Add platform tests in the affected services. Add Hosted Skills coverage under
`tests/` for fresh-process discovery, registration, and execution. Add config
scenario fixtures if authoring behavior changes.

## 7. Docs impact

- [ ] Platform documentation: mode setting, update API, revision contract, worker
  lifetime, health behavior, and failure recovery.
- [ ] `docs/architecture.md`: explain the platform boundary and fresh-process rebuild.
- [ ] `docs/triggers.md`: supported trigger changes and external registration limits.
- [ ] `README.md` and `docs/getting-started.md`: distinguish first deployment from
  Dev Mode updates when the feature is available.
- [ ] Hosted Skills examples: update workflow and supported runtime conditions.
- [ ] Authoring references: change only if this feature changes the authoring schema.

## 8. Status & sign-off

- **Status:** In review. This document describes a proposal, not implemented behavior.
- **Architecture review:** Pending. Cross-service contracts and open questions
  require review.
- **Human sign-off:** Tsuyoshi Ushio reviewed the Japanese draft and requested
  this English proposal. Final architecture and implementation approval are pending.
