---
frd: 0013
title: Hosted Skill Storage Queue recovery
status: Finalized
author: hallvictoria
created: 2026-10-21
updated: 2026-10-21
---

# FRD 0013 — Hosted Skill Storage Queue recovery

## 1. Summary

Extend opt-in handler recovery to Azure Storage Queue messages. An agent can
inspect project source and replace a malformed textual message body before
retrying the original handler in the same invocation. Ordinary valid messages
execute without an agent. Unresolved failures surface remediation while remaining
failed queue invocations, preserving the host's redelivery and poison-queue policy.

## 2. Motivation / problem

HTTP-only recovery cannot demonstrate deterministic queue processing with
source-aware input correction. Application authors should not write agent calls,
repair rules or retry callbacks in their queue handler.

## 3. Goals / Non-goals

Goals: minimal decorator-based authoring; generic bounded textual body correction;
unchanged HTTP behavior; preserved queue metadata and message settlement;
source-grounded remediation for unfamiliar properties; existing retry budget,
deadline, cancellation, source-access and tool isolation guarantees.

Non-goals: Service Bus, Event Hubs, binary payload correction, queue updates,
message settlement tools, HTTP body recovery, rollback, guaranteed model inference,
or a deterministic malformed-JSON repair algorithm.

## 4. Proposed design

| Pipeline stage | Responsibility |
| --- | --- |
| discover / translate | No configuration-schema changes |
| register | Existing trigger argument detection selects the queue parameter |
| execute | A shared recovery-input interface selects the HTTP or queue adapter and supplies its retry argument schema |
| execute | Existing recovery engine owns source tools, sequential retry budget, result retention and cancellation |
| execute | Queue adapter validates textual input and reconstructs a queue message with a replaced body |

### Queue input and retry contract

Support `azure.functions.QueueMessage` with strict UTF-8 bodies of at most 32 KiB
encoded bytes. Bound both the original diagnostic and replacement size; never
silently truncate a message for repair. The initial handler receives the original
message. Recovery starts only after that handler raises.

The queue-specific `retry_function` accepts one required `body` string; it does
not expose HTTP route/query arguments. The runtime imposes no JSON parser or
business schema on the replacement. The deterministic handler remains the source
of validation truth. Existing `max_retries` counts executions, not invalid tool
arguments, and each retry is independent of earlier corrections.

Reconstruct an SDK-compatible message with the replacement body. Preserve the
original public `id`, `pop_receipt`, `dequeue_count`, `insertion_time`,
`expiration_time` and `time_next_visible`. Use an SDK message subclass with
explicit typed property overrides delegating to the original message, not
`__getattr__` forwarding or private worker-state copying. The new body belongs
to the subclass's native SDK constructor and body/JSON methods. Never update or
delete the real queue message; settlement belongs solely to the Functions host.
The agent receives the bounded textual body, handler identity and existing safe
error/source diagnostics, not pop receipts, credentials or delivery metadata.

One recovery run may make sequential retries under the existing shared deadline.
Stop execution after the first normal handler return, including `None`. Return
that actual result to the worker. A successful retry completes the queue invocation.
External cancellation during either the agent or retry always propagates as
`CancelledError`, not remediation. Recovery deadline expiry uses the existing
bounded cooperative shutdown; absent a retained successful result it raises the
queue recovery error with static fallback advice. Retained success survives
provider failure or recovery timeout, but never external cancellation.

### Failure and remediation

Export `HostedSkillRecoveryError`, a typed exception with bounded `remediation`.
When no retry succeeds, the queue adapter raises it rather than returning an HTTP
response or a success value. The host therefore retains its normal failure,
redelivery and eventual poison-queue behavior. Advice is visible in host exception
logs and to callers testing the handler directly. Advice is untrusted model text;
it is not a security or confidentiality guarantee. Do not include the original
exception text or queue body automatically in the raised exception. Suppress the
original exception context in the queue failure surface using
`raise HostedSkillRecoveryError(...) from None`; test formatted exception output
and `__suppress_context__` rather than assuming Python removes `__context__`.
No automatic terminal
classification, queue acknowledgement, or dead-letter handling is added.

### Sample

Keep the existing HTTP function in the same sample main module. Add a small
async queue handler using `hosted_skill(..., recovery=HostedSkillRecovery())`.
It delegates deterministic processing to a separate Python helper and logs a
successful computed result. No agent calls or recovery branches appear in the
handler. Processing has no external side effects.

The helper parses JSON and strictly validates a small work-item contract, such
as `{"operation":"sum","values":[1,2,3]}`, rejecting unknown fields, duplicate
keys, unsupported operations and non-finite numbers. This is the sample's
ordinary business contract, not a runtime repair contract. Tests assert that an
unknown property is not silently discarded to manufacture success.

Retain the existing error-advisor agent and add a queue advisor with generic
source-grounded instructions: repair representation only when intent is clear;
do not fabricate values, drop unfamiliar properties or reinterpret business
meaning. No example-specific brace fixes, field aliases or regex correction
tables. The runtime remains domain-neutral.

### Compatibility and safety

Queue-body sharing is an explicit extension of the data-sharing boundary: use
only reviewed source and synthetic non-sensitive messages. Malformed textual
bodies cannot be reliably field-redacted. Document this rather than promise
redaction. Unsupported or oversized input fails qualification before execution
or agent acquisition. Existing HTTP constraints and failure responses remain
unchanged. Output bindings and non-idempotent processing remain unsupported.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Recovery owner | Sample callbacks / generic decorator support | Generic queue adapter; agent invokes original handler through the existing retry tool | Human | 2026-10-21 |
| 2 | Unresolved queue outcome | Return advice and acknowledge / raise with advice | Raise a typed recovery error, preserving host redelivery and poison handling | Agent | 2026-10-21 |
| 3 | Correction surface | JSON-specific patches / textual body replacement | Bounded UTF-8 body replacement; handler owns business validation | Agent | 2026-10-21 |

## 6. Test plan

- Queue adapter: accepted types/encodings, empty and oversized bodies, replacement
  validation, metadata preservation, unchanged original message and native SDK API.
- Valid input: handler executes once; no agent call or recovery-tool acquisition.
- Agent fake uses real source and retry tools; corrected input replays the original
  handler under the same skill and invocation, returning its actual result.
- Extra brace and other malformed JSON examples; unfamiliar property produces
  advice without deleting the property or succeeding.
- Failure advice raises rather than acknowledging; safe exception context,
  bounded advice, provider fallback and existing host-failure semantics.
- Multiple sequential attempts, rejected overlap, exhaustion, successful `None`,
  deadline/cancellation, inactive tools and concurrent invocation isolation.
- HTTP regression coverage and queue-specific tool schemas without cross-kind fields.
- Core Tools with Azurite: valid, correctable and unfamiliar-property messages;
  verify delivery outcomes and same-invocation retry from host/tool evidence.
- Independent testing review and full lint, type-check and pytest gates.

## 7. Docs impact

Update architecture module map, recovery guide, trigger guide, public exports and
sample README with payloads, observed outcomes and poison-queue cleanup. Document
body sharing and the typed failure contract. No frontmatter-schema changes.

## 8. Status & sign-off

Finalized. Independent architecture review conditionally approved the design;
exception-context suppression, cancellation precedence and explicit SDK metadata
delegation have been clarified. Human approved the design, including the queue
failure and body-sharing contracts, on 2026-10-21.