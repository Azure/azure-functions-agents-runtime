# Live ACA harness smoke coverage

The E2E template has one nonblocking, 30-minute Linux Python 3.13
current-checkout ACA/model smoke for trusted pull requests. It materializes one
Function-app root, captures it through the production package path, verifies
the Sandbox Group's sole guest UAMI, and always reaps current-run sandboxes and
snapshots.

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

Deployed cold-start, lifecycle, loss, and load suites remain direct/manual test
assets pending issue #166. They have no pipeline wiring, target metadata, or
queue-time parameters in this repository.
