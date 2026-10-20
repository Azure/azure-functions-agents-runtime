# Hosted Skill error recovery

An HTTP Function catches an operation exception and asks a Hosted Skill whether
corrected inputs can fix it. The **agent decides**: return corrected inputs for
one retry, or return remediation steps for the user. There are no application
eligibility rules, alias allowlists, or tool-evidence checks in the recovery flow.

## Run locally

1. Prerequisites: Python 3.13+, Azure Functions Core Tools, Azurite, and credentials
   for a Microsoft Foundry project and deployed model supporting tool calling.
2. Follow the [shared setup](../README.md#run-locally) from this sample's `src`
   directory. Set `FOUNDRY_PROJECT_ENDPOINT` and `FOUNDRY_MODEL` in local settings.
3. Start Azurite with `azurite --skipApiVersionCheck`, then run `func start`.

## Test the three scenarios

Run the following commands from this sample's `src` directory while the host and
Azurite are running. Examples use Core Tools' default port, **7071**; change it
if the host uses another port. In PowerShell, use `curl.exe` rather than the
Windows PowerShell `curl` alias.

| Scenario | Setup and request | Expected result |
| --- | --- | --- |
| Normal execution | Template exists; GET `/api/reports/weekly` | HTTP 200; no agent call |
| Human remediation | Template missing; GET `/api/reports/weekly` | HTTP 500; agent explains how to restore the template |
| Automatic input correction | Template exists; GET `/api/reports/weekly?format=markdwon` | HTTP 200 if the agent corrects the input and the retry succeeds |

### 1. Normal execution: no exception

Supply the local report template, then request the default text report:

```powershell
Copy-Item templates/weekly-report.example.txt templates/weekly-report.txt
curl.exe -i "http://localhost:7071/api/reports/weekly"
```

Expect **HTTP 200** with `report`, `auto_corrected: false`, and
`requires_user_action: false`. The report starts with `Weekly operations report`.
The Function completes normally without calling the agent.

### 2. Human remediation: the agent cannot supply a missing file

Remove only the demo template created above, then repeat the request:

```powershell
Remove-Item templates/weekly-report.txt
curl.exe -i "http://localhost:7071/api/reports/weekly"
```

The operation raises `FileNotFoundError`. The agent should recognize that changing
inputs cannot supply a missing deployment asset. Expect **HTTP 500** with
`requires_user_action: true`, `auto_corrected: false`, and `remediation` explaining
how to restore the template and verify the repair. The agent does not create files.

Copy the example template back and repeat the request to verify **HTTP 200**, as
in scenario 1. For a deployed app, include the template in the project and redeploy.

### 3. Automatic correction: the agent fixes an input and the app retries

Ensure the template exists, then send a misspelled format:

```powershell
Copy-Item templates/weekly-report.example.txt templates/weekly-report.txt
curl.exe -i "http://localhost:7071/api/reports/weekly?format=markdwon"
```

The operation raises `InvalidReportFormatError`. The agent receives the exception
type, operation context, and query inputs. It can optionally call
`correct_report_format`, but need not use a tool to suggest a correction. Its
response to the recovery helper is ordinary JSON, for example:

```json
{
  "corrected_input": {"format": "markdown"},
  "remediation": "The requested format appears to be a typo for markdown."
}
```

The application uses those inputs directly for **one** operation retry. On actual
success it returns HTTP 200 with `report`, `auto_corrected: true`,
`requires_user_action: false`, and `corrected_input: {"format": "markdown"}`.
The report starts with `# Weekly operations report`. The operation itself still
enforces its normal input rules; there is no separate recovery policy validator.
For unclear intent or non-input failures, the agent should return
`corrected_input: null` with meaningful remediation instead.

Agent decisions and wording are not guaranteed. A model failure, invalid response,
15-second deadline, or failed retry returns HTTP 500 with static fallback guidance.
The helper does not call the agent or retry again. A typo request with the template
missing can exercise a failed retry if the agent first proposes a format correction.
For an additional ambiguous-input case, try `?format=text&format=markdown`; the
agent should ask the caller to clarify rather than guess. All unresolved failures
use HTTP 500 in this deliberately generic demo.

### Cleanup

Delete only the demo template you created, then stop the host and Azurite with
**Ctrl+C** in their terminals:

```powershell
Remove-Item templates/weekly-report.txt -ErrorAction SilentlyContinue
```

## Reuse and limits

- [Handler](src/function_app.py): calls the business operation, then delegates
  failures to `recover_operation()`.
- [Recovery helper](src/report_recovery.py): accepts an operation callback and
  context, not a report-specific exception or input field. Supply another
  synchronous, read-only operation accepting `QueryParams` and returning a
  JSON-serializable string-valued dictionary. It makes one agent call
  (15-second deadline) and at most one retry; cancellation propagates.
- [Report operation](src/hosted_report.py): extracts/validates input and renders
  a fixed template. [Optional tool](src/tools/correct_report_format.py) normalizes
  a few known aliases but does not limit which corrections the agent can suggest.
- [Agent](src/error-advisor.agent.md): reviews different error types and chooses
  correction or human remediation. Bad JSON or unavailable agents use static
  fallback advice. Only basic response parsing is retained, not correction-policy
  or tool-evidence validation.

**Demo only:** the agent's corrected inputs are trusted. The helper sends all
query inputs to the model; use synthetic, non-sensitive values only. Raw exception
messages/traces are omitted, but query inputs can contain secrets or personal
data. This is not a production-safe generic autocorrect mechanism. Before using
with real data, add redaction, authorization, explicit correction policy, and
idempotency for any side effects. Prompt instructions are not a security boundary.
Do not swallow queue/timer failures that must drive host retry behavior.

This local-first sample creates no Azure resources. Deployed routes use default
Function-key authorization. Protect exception logs, review model advice, and
budget for model charges. For deployed template repairs, update the project and
redeploy rather than changing a running app's filesystem. Reset by deleting only
the git-ignored destination template; stop the host and Azurite when finished.