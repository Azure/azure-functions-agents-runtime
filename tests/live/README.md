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

Deployed cold-start, lifecycle, loss, load, and one-shot recovery suites are
wired into the post-main ACA qualification stages of `eng/ci/e2e-tests.yml`.
See the [ACA qualification CI guide](../../eng/ci/docs/aca-qualification.md)
for targets, triage, and promotion criteria. They remain runnable by hand, and
still skip unless `AZURE_FUNCTIONS_AGENTS_RUN_DEPLOYED_ACA_SMOKE=1` is set
explicitly.

## Controlled deployed one-shot recovery

`test_aca_one_shot_recovery.py` targets the fixed
`deployed_setup_timeout` route in
`tests/fixtures/live_aca_setup_timeout_recovery`, a dedicated deployment that
does not contain the other qualification routes. Its fixture-local
`controlled_setup_timeout.py` temporarily injects a complete provider wrapper
through `compose_aca_application`; the wrapper delays `create` for 95 seconds
after Table reservation and before it delegates to ACA. It has no environment
toggle or production runtime surface. The initial delayed sleep is canceled
before an ACA sandbox is created; reconciliation-only creates delegate normally
if recovery is needed.

Deploy that exact fixture to the protected Easy-Auth ACA Function App before
running the test. Issue #166 still owns immutable package deployment and
external attestation, so this remains a direct/manual asset rather than a CI
deployment target.

```bash
export AZURE_FUNCTIONS_AGENTS_RUN_DEPLOYED_ACA_SMOKE=1
export AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_FUNCTION_BASE_URL="https://<app>.azurewebsites.net"
export AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_AGENT_SLUG="deployed_setup_timeout"
export AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EASY_AUTH_TOKEN_SCOPE="api://<app-id>/.default"
export AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EASY_AUTH_AUDIENCE="<app-id>"
export AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_TIMEOUT_SECONDS=180
python -m pytest -m live_aca tests/live/test_aca_one_shot_recovery.py -v
```

The admission fixture performs the only chat POST without the asynchronous
preference header (`Prefer: respond-async`), so the controlled
post-reservation timeout must return a linked `504` with
`admission=committed`. Configuration, authorization, and capacity problems are
`ACA-SMOKE-ENV` fixture errors. The test body verifies the recovery ticket and
headers, never replays the chat POST, then uses only its management URLs to
cancel and poll for a terminal outcome. A cancellation `202` honors
`Retry-After` before status polling. The terminal polling window is five
minutes: it covers the 120-second operation lease plus a 60-second dedicated
fixture reconciler cadence and scheduling jitter.

Deployed cold-start, lifecycle, loss, and load suites are wired into the
post-main ACA qualification stages of `eng/ci/e2e-tests.yml`. See the
[ACA qualification CI guide](../../eng/ci/docs/aca-qualification.md)
for targets, triage, and promotion criteria. They remain runnable by hand, and
still skip unless `AZURE_FUNCTIONS_AGENTS_RUN_DEPLOYED_ACA_SMOKE=1` is set
explicitly.
