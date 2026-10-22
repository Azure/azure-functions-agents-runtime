# Source-aware whole-function recovery

Two ordinary Functions share [src/function_app.py](src/function_app.py): an HTTP
handler adds two numbers; `process_work_item` consumes `recovery-work-items` via
`AzureWebJobsStorage` and logs a computed sum. Valid inputs need no agent. After
an uncaught exception, the decorator starts one source-aware agent run with
read-only Python source tools and `retry_function` to replay the **entire original
handler in the same invocation**. The standalone `@app.smart_retry` decorator
injects no skill or other parameter; neither handler calls an agent or implements
repair rules.

HTTP uses `@app.smart_retry(agent_name="error_advisor")` below its unchanged
`@app.route(route="add/{a}/{b}", methods=["GET"])` binding. Queue processing uses
`@app.smart_retry(agent_name="queue_advisor", max_retries=3)` below its unchanged
`@app.queue_trigger(arg_name="message", queue_name="recovery-work-items", connection="AzureWebJobsStorage")`
binding. Each smart-retry decorator is immediately above its `async def` handler.
The handlers keep only their real inputs, `req` and `message`; indexing selects
these from their SDK trigger metadata without a `request_arg` override.

The decorator accepts plain keyword options, with unchanged defaults
`max_retries=1`, `timeout=60`, and `request_arg=None`, plus additive
`audit_only=False`, and has no `arg_name` option.
Queue processing allows up to three sequential correction attempts under one
60-second recovery deadline, stopping at first success. This budget is separate
from the host's queue redelivery policy. Combining skill injection and recovery
on one handler, or applying smart retry twice, is unsupported and rejected.

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

## HTTP examples

| Scenario | Request | Expected result |
| --- | --- | --- |
| Normal execution | `/api/add/1/2` | HTTP 200, `{"sum":3.0}`, no agent run |
| Agent-driven retry | `/api/add/%221%22/2` | If the agent infers the numeric correction and retries successfully, HTTP 200, `{"sum":3.0}` |
| Human remediation | `/api/add/unknown/2` | No clear number can be inferred; HTTP 500 with `requires_user_action` and remediation |

Using port 7071 and PowerShell's `curl.exe`:

```powershell
curl.exe -i "http://localhost:7071/api/add/1/2"
curl.exe -i "http://localhost:7071/api/add/%221%22/2"
curl.exe -i "http://localhost:7071/api/add/unknown/2"
```

The handler uses `float()`; `%221%22` contains literal quotes around `1` and
raises `ValueError`. A successful retry returns the actual handler response
unchanged; the model does not manufacture the sum or resend HTTP.

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
Live validation has also observed the advisor removing an unsupported property
despite that instruction. Do not rely on smart retry to enforce a no-data-loss
policy. Recovery may remain unresolved or time out.

The retry tool takes a complete replacement `body` (UTF-8, at most 32 KiB) and
preserves delivery metadata without updating or settling the stored message.
Success returns the handler's `None`, allowing the host to complete the delivery.
Unresolved recovery raises `HostedSkillRecoveryError` with remediation in host
exception logs. The host can redeliver the original body and eventually move it
to `recovery-work-items-poison`; each failed delivery may incur model usage.

## Optional audit-only mode

The shipped HTTP and queue handlers remain in **normal retry mode**. This is an
opt-in example only, not enabled in [src/function_app.py](src/function_app.py):
use this instead of the existing smart-retry decorator in your own app, below
its SDK trigger binding and immediately above the unchanged handler:

```python
@app.smart_retry(agent_name="queue_advisor", max_retries=3, audit_only=True)
```

`audit_only` requires a boolean. It overrides any valid positive `max_retries`
with an effective zero budget; explicit `max_retries=0` is still invalid.
Success starts no agent and emits no advice log. On failure, one advisor uses
separate audit system instructions and **only** `list_files`, `read_file`, and
`search_source`. With `retry_function` absent, the model cannot invoke a retry,
even if it ignores instructions: no replacement input or handler replay occurs.
Advice is bounded to 2,000 characters and logged once at WARNING level through
the shared logger; missing advice, provider errors or timeout use static fallback
guidance. HTTP still returns 500 (HEAD bodyless); queues still raise
`HostedSkillRecoveryError`, leaving redelivery/poison handling intact. Each host
delivery may run another audit. Analysis and source tools share one deadline;
cancellation propagates without an advice log. Logs contain untrusted model text,
not a confidentiality guarantee; the source/input warnings below still apply.

## Limits and cleanup

Use replay-safe handlers only: no HTTP bodies, streaming, output bindings or
non-idempotent side effects. **Use reviewed source and synthetic non-sensitive
inputs:** queue text is shared with the model and cannot be reliably field-redacted;
source may contain secrets. Source/tool results can persist in harness history.
Cancellation still propagates; cleanup is bounded and cooperative. See the
[recovery guide](../../docs/front-matter-spec.md#opt-in-handler-recovery) for limits.

Stop Core Tools with Ctrl+C. In Storage Explorer, inspect `recovery-work-items`
and `recovery-work-items-poison`; delete **only messages created for this sample
run**. Poison copies may have different IDs; verify synthetic body and timing,
and leave messages untouched if ownership is uncertain. Do not clear or delete
shared queues, purge Azurite data, or stop an emulator used by other apps.
No Azure resources are provisioned; model usage can incur charges.