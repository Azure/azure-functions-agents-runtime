# Source-aware whole-function recovery

Two ordinary queue Functions share [src/function_app.py](src/function_app.py),
both using `AzureWebJobsStorage`:

| Handler | Queue | Decorator | Behavior |
| --- | --- | --- | --- |
| `process_work_item` | `recovery-work-items` | `@app.smart_retry(max_retries=3)` | Logs a computed sum; after failure, allows up to three whole-handler correction attempts in the same invocation |
| `process_work_item2` | `bad-package` | `@app.smart_retry(audit_only=True)` | Deliberately imports missing `verybadpackage` inside the handler; logs advice after failure without replay |

Both use the bundled Smart Retry Advisor: no custom advisor file or `agent_name`
is required. Each decorator sits immediately above its `async def` handler and
below the unchanged SDK queue binding. The only handler argument is `message`;
indexing selects it from trigger metadata. No skill is injected and neither
handler calls an agent or implements repair rules. Valid normal-queue input
starts no agent.

The keyword-only `agent_name: str | None = None` selects the bundled advisor,
inheriting global model/`agent_configuration` and existing environment/provider
defaults, not another agent's settings. Selected provider configuration is
preflighted at decoration; no model client is acquired before the original
handler fails. Defaults remain `max_retries=1`, `timeout=60`, `request_arg=None`
and `audit_only=False`; there is no `arg_name` option. The normal handler's three
attempts share one 60-second recovery deadline, stop at first success and are
separate from host redelivery. The bundled advisor registers no trigger or
endpoint. The examples below describe expected behavior, not live validation
results for this revision.

## Run locally

Follow the [shared setup](../README.md#run-locally) from this sample's
[src](src/) directory, including Python dependencies and local settings.
Configure the Foundry endpoint and deployed model; keep
`AzureWebJobsStorage=UseDevelopmentStorage=true`. Reuse your existing Azurite
instance (default ports); if none is running, start
`azurite --skipApiVersionCheck` in a separate terminal. Start Core Tools:

```powershell
func start
```

## Queue examples

Use [Azure Storage Explorer](https://azure.microsoft.com/products/storage/storage-explorer/)
to avoid adding a sender dependency (`azure-storage-queue` is in this repo's dev
extra, not the sample's default install). Connect to the existing local emulator
on default ports, open **Queues**, and create `recovery-work-items` if absent.
Use **Add Message** for each body below, **encoding the text as Base64 once** to
match the Functions queue binding's default transport encoding. Send one at a
time and inspect the host logs before sending the next; retain the message IDs,
synthetic bodies and insertion times for cleanup.

| Scenario | Message body | Expected result |
| --- | --- | --- |
| Valid | `{"operation":"sum","values":[1,2,3]}` | Logs `Queue work item processed: total=6.0`; no agent run |
| Extra brace | `{"operation":"sum","values":[1,2,3]}}` | Agent may infer a syntax-only replacement and retry to log `total=6.0` |
| Trailing comma | `{"operation":"sum","values":[1,2,3],}` | Agent may infer a syntax-only replacement and retry to log `total=6.0` |
| Unfamiliar property | `{"operation":"sum","values":[1,2,3],"mystery":true}` | Requires human clarification/remediation; must not drop `mystery` to force success |

[src/queue_work_item.py](src/queue_work_item.py) is deterministic business logic:
strict JSON with no duplicate keys or extra properties, `operation: "sum"`, and
a nonempty list of finite numbers (not booleans), producing a finite sum. It
rejects invalid inputs without correcting them. The queue advisor inspects this
source; there are no example-specific correction tables or hardcoded brace fixes.
Syntax correction and preservation of supplied meaning, including unfamiliar
properties, are model-guided, not guaranteed. A successful replay confirms that
the handler accepted the replacement, not that the original intent was preserved.
Do not rely on smart retry to enforce a no-data-loss policy: a model may remove
an unsupported property despite guidance. Recovery may remain unresolved or time out.

The retry tool takes a complete replacement `body` (UTF-8, at most 32 KiB) and
preserves delivery metadata without updating or settling the stored message.
Success returns the handler's `None`, allowing the host to complete the delivery.
Unresolved recovery raises `HostedSkillRecoveryError` with remediation in host
exception logs. The host can redeliver the original body and eventually move it
to `recovery-work-items-poison`; each failed delivery may incur model usage.

## Audit-only import failure

In Storage Explorer, create `bad-package` if absent and send a synthetic body
such as `{"check":"missing-import"}`, Base64-encoded once as above. The shipped
`process_work_item2` handler imports `verybadpackage` before its success log.
Leave this deliberately missing dependency uninstalled for the example; the
expected failure is `ModuleNotFoundError`, not a malformed-body error.

Its `@app.smart_retry(audit_only=True)` starts one advisor after failure with
**only** `list_files`, `read_file`, and `search_source`. There is no
`retry_function`, replacement body or handler replay. Expect bounded remediation
advice once at WARNING level and `HostedSkillRecoveryError`, not a successful
delivery. The advisor cannot install a dependency or edit source. Advice wording
is model-dependent; provider failure, timeout or missing advice uses static
fallback guidance. The host may redeliver and eventually move the message to
`bad-package-poison`; each delivery may incur another audit/model charge.

`audit_only` requires a boolean and keeps the existing positive `max_retries`
validation even though its effective retry budget is zero: `max_retries=0` is
invalid. Advice is limited to 2,000 characters. Initial success would start no
agent or advice log. Analysis and source tools share one deadline; cancellation
propagates without advice. Logs contain untrusted model text, not a
confidentiality guarantee.

## Optional custom advisor

The separate [src/error-advisor.agent.md](src/error-advisor.agent.md) is retained
as an optional authored example, not required by either queue handler. Select
it by its filename-derived slug with
`@app.smart_retry(agent_name="error_advisor", max_retries=3)` or
`@app.smart_retry(agent_name="error_advisor", audit_only=True)` instead of the
corresponding existing decorator. Its resolved markdown instructions, model
and agent configuration replace bundled guidance; packaged instructions are
not appended. Runtime recovery/audit policy still controls tools, budgets and
failure behavior. In particular, custom guidance cannot enable retries in
audit mode. Invalid or unknown names fail rather than silently using the default.

## Limits and cleanup

Use replay-safe handlers only: no HTTP bodies, streaming, output bindings or
non-idempotent side effects. **Use reviewed source and synthetic non-sensitive
inputs:** queue text is shared with the model and cannot be reliably field-redacted;
source may contain secrets. Source/tool results can persist in harness history.
Cancellation still propagates; cleanup is bounded and cooperative. See the
[recovery guide](../../docs/front-matter-spec.md#opt-in-handler-recovery) for limits.

Stop Core Tools with Ctrl+C. In Storage Explorer, inspect `recovery-work-items`,
`recovery-work-items-poison`, `bad-package` and `bad-package-poison`;
delete **only messages created for this sample
run**. Poison copies may have different IDs; verify synthetic body and timing,
and leave messages untouched if ownership is uncertain. Do not clear or delete
shared queues, purge Azurite data, or stop an emulator used by other apps.
No Azure resources are provisioned; model usage can incur charges.