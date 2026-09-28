# Local Copilot harness preview

This is the small, **default-off**, local-only foundation for
Azure/azure-functions-bucees-planning#1351, not full MAF parity or a production
deployment sample. It uses the normal markdown discovery, typed composition,
Azure Functions registration and `run_agent` entry points. It is not an
SDK-only script.

The pinned pair is **`github-copilot-sdk==1.0.14`, native runtime `1.0.85`,
protocol 3, external Rust stdio**. Python 3.13/3.14 and Functions Core Tools 4
are required. The initial live target is a **Microsoft Foundry project,
`gpt-5.4`, Entra ID**, using its project `/openai/v1/responses` API. The adapter
also accepts explicit `openai` BYOK at `https://api.openai.com/v1` (Chat
Completions); that path has native/synthetic-provider coverage, not a separate
live-provider qualification.

No Azure resources, role assignments, storage accounts, Copilot login, or
GitHub token are created/required. Use an **existing approved test deployment**
and an existing Azure sign-in with project inference access. Provider calls
incur the deployment's normal token charges.

## Supported subset and hard limits

| Surface | This foundation |
| --- | --- |
| Invocation | Non-streaming built-in chat and authored HTTP trigger; explicit model |
| Tools | Plain discovered Python functions and simple `@tool` functions; text/JSON results |
| Identity | Agent slug plus public session ID; no-ID means create, supplied ID means strict resume |
| History | Native local files, completed turns only, including a cold runtime restart |
| Provider state | Native history is authoritative; Responses requests use `store:false`; no MAF transcript import |
| Ownership | One process/event loop per app, one OS-locked writer per local state namespace |
| Cancellation | Abort/detach only the affected session; do not stop the shared runtime |
| Output limit | Existing `agent_configuration.max_output_tokens` enforced on outbound provider requests |
| Diagnostics | Harness, pinned versions, transport, provider/model, custom-tool count; no credential values |

The sample **explicitly disables** the default-on `web_request` tool, ACA
`execute_python`, MCP, skills, debug UI, delegation and Dynamic Workflows.
These and non-HTTP triggers, delegated/Workflow Sub Agent roles, MAF compaction
configuration, reasoning overrides, special tool approval/invocation-limit/
context policies, and custom tool subclasses are rejected, not ignored.
`chat_api` also registers the existing stream/history routes, but those return
**501** in this preview instead of invoking MAF or returning an empty transcript.
There is no automatic fallback.

Native compaction is disabled in this baseline. Compaction, Blob-backed native
storage, distributed ownership, native history projection, Azure hosting,
provider parity and recovery of interrupted turns are later lanes. There is
no host summarizer. The live flag is deliberately rejected on Azure-hosted
workers and with `FUNCTIONS_WORKER_PROCESS_COUNT` other than `1`.

## Exact setup (PowerShell, from the repository root)

Check out the foundation PR's implementation commit before running these
commands. Record `git rev-parse HEAD` with your evidence; do not mix a wheel
from a different branch with this sample.

```powershell
git rev-parse HEAD
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,copilot]"
$env:PATH = "$PWD\.venv\Scripts;$env:PATH"
$env:VIRTUAL_ENV = "$PWD\.venv"

# Public configuration only. Do not put a token in local.settings.json.
$env:AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT = "true"
$env:AZURE_FUNCTIONS_AGENTS_PROVIDER = "foundry"
$env:FOUNDRY_PROJECT_ENDPOINT = "https://<resource>.services.ai.azure.com/api/projects/<project>"
$env:FOUNDRY_MODEL = "gpt-5.4"
$env:FUNCTIONS_WORKER_PROCESS_COUNT = "1"
$env:AZURE_FUNCTIONS_AGENTS_SESSION_DIR = "$PWD\.preview-state"

# Use a dedicated native-asset cache, not an interactive Copilot installation.
$env:COPILOT_CLI_EXTRACT_DIR = "$PWD\.tmp-validation\runtime-1.0.85"
.\.venv\Scripts\python.exe -m azure_functions_agents._copilot --setup
.\.venv\Scripts\python.exe -m azure_functions_agents._copilot

Copy-Item samples\copilot-preview\src\local.settings.template.json `
  samples\copilot-preview\src\local.settings.json
Push-Location samples\copilot-preview\src
func start --port 7071
```

If `py` is not installed but `uv` is available, the equivalent first two setup
commands are `uv venv .venv --python 3.13` and
`uv pip install --python .venv\Scripts\python.exe -e ".[dev,copilot]"`.

`--setup` is the **only** explicit native download path. It uses the pinned
SDK's checksum-verified release bundle. Indexing, importing the package, and
requests do not download native assets. Diagnostics print version constants
and check local assets; they **do not authenticate or prove inference access**.
The first actual request creates the shared client lazily.

For OpenAI BYOK instead, set `AZURE_FUNCTIONS_AGENTS_PROVIDER=openai`,
`AZURE_FUNCTIONS_AGENTS_MODEL` to an available model and supply
`OPENAI_API_KEY` through your existing secret environment. The key is fetched
in a host callback, not copied into native session configuration or command
arguments. Foundry uses the shared `DefaultAzureCredential` builder and the
`https://ai.azure.com/.default` scope; its credentials remain host-owned too.
No interactive approval or auto-login is delegated to the native runtime.

## Reproducible live check

Obtain approval for the exact provider/deployment before running the successful
phases. The configured timeout is 60 seconds per HTTP turn and maximum output
is 256 tokens per model response. The normal first/follow-up flow makes
**three model calls**: tool selection, reply after the tool, then recall.
Model/provider retries can occur within the timeout; this is not a fixed-price
or exactly-once guarantee. The verifier does not automatically retry failed
phases. Only the local harmless `make_receipt` tool can run.

In a second terminal, from the repository root:

```powershell
# First turn: real model, exactly one tool call, receipt and public ID saved.
.\.venv\Scripts\python.exe samples\copilot-preview\verify.py `
  --phase first --evidence .preview-evidence.json
```

Stop the Functions host with Ctrl+C, then start it again with the **same**
working directory, environment and state directory. The next phase deliberately
does not contain the tag or expected receipt in its prompt:

```powershell
.\.venv\Scripts\python.exe samples\copilot-preview\verify.py `
  --phase followup --evidence .preview-evidence.json
.\.venv\Scripts\python.exe samples\copilot-preview\verify.py --phase negative
```

Alternatively, in the **terminal where you ran the environment setup**, with
**no sample host already running**, this
single checked-in command owns both host starts, the restart, both requests,
the negative checks and host cleanup (using the repository's existing
Functions E2E helper):

```powershell
.\.venv\Scripts\python.exe samples\copilot-preview\verify.py `
  --restart-host --evidence .preview-evidence.json
```

Expected results: `PASS first`, then `PASS followup`; the second turn uses the
same public ID, recalls the exact first tool result and makes no tool call.
The verifier checks the actual `tool_calls` output, not merely assistant prose.
Negative checks require an unknown supplied session ID to fail, plus 501 for
stream/history; none sends a prompt to a model.

Equivalent manual requests:

```powershell
$first = Invoke-RestMethod http://127.0.0.1:7071/agents/main/chat `
  -Method Post -ContentType application/json `
  -Body '{"prompt":"Call make_receipt exactly once with tag ''harmless-demo''. Reply only with its result."}'
$first.tool_calls
Invoke-RestMethod http://127.0.0.1:7071/agents/main/chat `
  -Method Post -ContentType application/json `
  -Headers @{"x-ms-session-id"=$first.session_id} `
  -Body '{"prompt":"Recall the previous receipt without calling any tool."}'
```

The authored trigger is also registered at `POST /preview`, with the same
session-header convention and existing request/response handling.

## Failure behavior and rollback

`AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT` is resolved once per app root, also for
standalone calls: unset/`false`/`0` select MAF; `true`/`1` select Copilot.
Boolean text is case-insensitive and trimmed; **present-empty, whitespace-only
or any other value is an error**. `runtime:` front matter remains ignored.
Each app construction captures a fresh immutable binding; existing handlers
retain theirs. Standalone calls independently capture a first-use default for
their resolved app root. Constructing another app does not change that default.

Missing optional dependencies, wrong SDK/native versions, unsupported
capabilities, missing model/provider settings, an unavailable native bundle,
or invalid native tool catalogs are explicit failures. A missing/corrupt native
file or completion marker never silently starts a new conversation. Any turn
that does not commit completely leaves its session blocked. Start a new
conversation **without** a session header after resolving the underlying fault;
do not reuse or repair an unfinished native journal by hand.

Stop the host, then:

```powershell
$env:AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT = "false"
# Restart the same func start command. No Copilot process/auth/download occurs.
```

The original MAF `ClientManager`, streaming, tool, role and history behavior
remain in effect. Existing MAF history is not mutated by Copilot. For manual
flag-off inference use a **new** conversation with no header and fresh evidence;
do not use a Copilot ID as a MAF ID or expect cross-harness continuation.

Standalone code should await `shutdown_client_manager()` before closing its
event loop. The native owner also has a bounded process-handle exit cleanup.
Request cleanup never invokes that shared-client shutdown.

## Offline and native qualification

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_harness.py `
  tests\test_copilot_state.py -q

# Explicitly opt into the installed real native runtime, with a synthetic
# intercepted provider. No real credentials, network inference or billing.
$env:AZURE_FUNCTIONS_AGENTS_TEST_NATIVE_COPILOT = "1"
.\.venv\Scripts\python.exe -m pytest tests\test_copilot_native.py -q
Remove-Item Env:\AZURE_FUNCTIONS_AGENTS_TEST_NATIVE_COPILOT
```

Native tests exercise real SDK/stdio sessions, exact custom-only catalogs,
both wire protocols, token-callback sentinel handling, provider `store:false`,
tool execution, cold native process restart/resume, and cancellation without
killing the shared process. They scan persisted native files for the sentinel.
These are **synthetic-provider results**, not proof of real model, Blob,
compaction or production hosting behavior.

At this pinned native version, the SDK's advertised provider/model output-token
override is not emitted as an API generation cap. The adapter therefore uses
the SDK's supported HTTP request-handler seam to enforce the configured
`max_output_tokens`/`max_completion_tokens` and `store:false` immediately before
sending. It only forwards requests for the active session's exact configured
provider URL, never follows redirects, and scopes the policy per concurrent
turn. Tests assert the actual outgoing body, not just SDK configuration.

## Targeted cleanup

Stop only the sample host you started. Its `.preview-state` contains native
conversation content; treat it as sensitive local development data. Inspect
the resolved path before removing it:

```powershell
Get-Item .preview-state
Remove-Item .preview-state -Recurse
Remove-Item .preview-evidence.json
Remove-Item samples\copilot-preview\src\local.settings.json
# Optional: remove this sample's isolated native asset cache, not shared caches.
Get-Item .tmp-validation\runtime-1.0.85
Remove-Item .tmp-validation\runtime-1.0.85 -Recurse
Remove-Item Env:\AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT
Remove-Item Env:\COPILOT_CLI_EXTRACT_DIR
```

Do not delete `~\.copilot`, shared SDK caches, MAF `agent-sessions`, or another
worktree's state. If the state directory already existed before this run,
remove only the app-hash subdirectory shown by your locally inspected tree.

## Ownership and compatibility ledger

| Contract | Current owner / handoff |
| --- | --- |
| `_harness.py`, app/capability/runner entry wiring | Foundation; one app-level selection, not a provider `ClientManager` replacement |
| `InferenceTarget`, ephemeral provider auth | Existing neutral target reused; provider lane extends the two explicit preview mappings |
| `HarnessRequest`, `AgentResult`, tool schemas | Internal adapter inputs and existing outputs; runner lane extends roles/events without a public plugin API |
| Native create/resume/detach + completed-turn marker | Foundation local baseline; session lane owns Blob, native compaction and production ownership |
| SDK setup, version pin, client/process shutdown | One foundation lifecycle owner; hosting lane qualifies platform/process lifecycle |
| MCP, skills, delegate tools, Workflow Sub Agents, Dynamic Workflows, web/ACA tools | Explicitly unsupported here; their owners extend this baseline, never fall back to MAF |

The foundation owns shared source edits until its PR merges; later lanes
should agree a handoff before editing these shared seams. The feature-only FRD
is developed independently, not a stacked implementation dependency.

## Recorded foundation evidence

On **2026-09-28**, implementation commit
`13d05700e92b6fb429ad990ea2eaf7b69566346d` passed the checked-in
`verify.py --restart-host` flow on **Windows, Python 3.13.15, Core Tools 4.13.0,
SDK 1.0.14, native 1.0.85/protocol 3**, against an explicitly approved existing
**Foundry project / `gpt-5.4` deployment with Entra ID**. No project endpoint,
credential, raw native journal or customer resource identifier is committed.

| Observed check | Result |
| --- | --- |
| First HTTP request through discovered markdown and registered route | Real model reply; exactly one `make_receipt` call and verified result |
| Second HTTP request after full Functions host/worker/native restart | Same public session ID; value-free recall matched the prior receipt; no new tool call |
| Persisted native event evidence | Two user messages, three assistant API-call IDs, one tool start/completion, one resume |
| Unknown supplied session / streaming / history | Explicit error / 501 / 501, no inference |
| Flag off and host restarted | Original MAF HTTP/history behavior, no Copilot process, no additional inference |
| Local state/cleanup | Completed marker `ready`; active Entra token absent from persisted files; zero owned native processes left |

The separate native synthetic-provider tests passed for OpenAI Completions and
Foundry Responses, including real custom schema/description and API output-cap
checks, cold native resume, recoverable startup failure, and cancellation.
The final lint, strict type-check and full coverage gates passed on both
Python **3.13.15** and **3.14.7**: **1,345 passed**, 10 intentionally skipped,
62 E2E cases deselected in the ordinary suite. All **six** explicitly enabled
native qualification cases also passed on each Python version.
The first output-cap check failed when relying only on SDK metadata; the
committed HTTP-boundary enforcement above fixes that actual failure.
An initial setup in the shared Windows SDK cache failed a directory rename;
the documented dedicated cache succeeded without changing shared files.

This is one bounded live flow, **not** production qualification, an exactly-once
guarantee, proof of all provider/model combinations, or compaction/Blob recovery
evidence. No production app flag, cloud resource or permission was changed.
