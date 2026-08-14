# Live ACA harness smoke coverage

The E2E template has one nonblocking, 30-minute Linux Python 3.13
current-checkout ACA/model smoke for every trusted pipeline invocation. It
materializes one Function-app root, captures it through the production package
path, verifies the Sandbox Group's sole guest UAMI, and always reaps current-run
sandboxes and snapshots.

The protected ACA service connection must be unavailable to fork builds. The
job's fork condition is defense in depth, not authorization. The smoke requires
one guest UAMI on the Sandbox Group. Protected infrastructure/IaC guarantees
that guest has model-only, no-state/no-group RBAC; CI cannot enumerate that
guest's model-scope assignments with its least-privilege controller connection.
The production composition verifies runtime egress permits only the configured
model host with hard denies, and a real model turn positively proves model
access. Its controller identity owns all ACA create, list, and cleanup actions.

`test_aca_harness_entrypoint_smoke.py` and
`test_aca_run_journal_acceptance.py` intentionally use low-level ACA transport
coverage. `test_aca_real_agent_turn.py` exercises only the production execution
backend.

Deployed cold-start, lifecycle, loss, load, and one-shot recovery suites remain
direct/manual test assets pending issue #166. They have no pipeline wiring,
target metadata, or queue-time parameters in this repository.

## Deployed one-shot recovery

`test_aca_one_shot_recovery.py` calls one deployed ACA-backed Functions endpoint
exactly once, discards its prompt and idempotency key, and uses only the first
response's management URLs to read status/result, open events, cancel, and
observe terminal settlement.

Enable it separately from the current-checkout smoke and point it at a
function-key-protected ACA-backed HTTP agent:

```bash
export AZURE_FUNCTIONS_AGENTS_RUN_ACA_SMOKE=1
export AZURE_FUNCTIONS_AGENTS_RUN_ACA_ENDPOINT_SMOKE=1
export AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_CHAT_URL="https://<app>.azurewebsites.net/agents/<slug>/chat"
export AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_FUNCTION_KEY="<function-key>"
python -m pytest -m live_aca tests/live/test_aca_one_shot_recovery.py -v
```

The fixture performs the only POST and reports endpoint authorization,
configuration, throttling, or capacity failures as `ACA-SMOKE-ENV` errors. The
test body never receives the prompt or idempotency key and never replays the
request. It proves caller-visible recovery against a deployed host. It does not
inject a deterministic provider delay; deterministic unit and Azurite tests
cover the linked timeout branches, while a naturally slow setup may return the
same linked recovery ticket as `504`.
