# Live ACA harness smoke coverage

`tests/live/test_aca_harness_entrypoint_smoke.py` is an opt-in, paid-service
smoke test for the ACA sandbox harness entrypoint. It intentionally exercises a
real `AcaSandboxAdapter.create()` call, a real `SandboxSessionHandle`, a direct
file upload, and synchronous process execution. It does not use an ACA fake.

## Run locally

Install the ACA transport extra, authenticate with the Azure CLI, and set the
following values for a CI-only Sandbox Group:

```powershell
python -m pip install -U -e ".[dev,aca_sandbox]"
az login
$env:AZURE_FUNCTIONS_AGENTS_RUN_ACA_SMOKE = "1"
$env:AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID = "/subscriptions/<subscription>/resourceGroups/<resource-group>/providers/Microsoft.App/sandboxGroups/<group>"
$env:AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_DISK = "<sandbox-disk-name>"
$env:AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_REGION = "<sandbox-group-region>"
python -m pytest -m live_aca tests/live/test_aca_harness_entrypoint_smoke.py -v -o log_cli=true -o log_cli_level=INFO
```

The test skips before collecting a live fixture unless
`AZURE_FUNCTIONS_AGENTS_RUN_ACA_SMOKE` is exactly `1`. Do not set that variable
in normal local development or ordinary unit-test jobs.

### Host ABI prerequisite

Run the live test only from Linux x86_64, including WSL, with CPython 3.13 or
3.14. The fixture builds the dependency closure with the host interpreter, and
the ACA sandbox is Linux x86_64. Windows and macOS hosts are unsupported for
local runs because they would upload incompatible native wheels instead of the
Linux binaries the sandbox needs. The fixture fails before building the closure
or creating a sandbox when the host ABI is unsupported.

## Result taxonomy

| Result | Meaning | Triage |
| --- | --- | --- |
| Skip | Live ACA was not explicitly enabled. | No action. |
| Error with `ACA-SMOKE-ENV:` | The opted-in subscription, group, credentials, quota, region, sandbox creation, closure delivery, or closure verification is unusable. | Call ops. |
| Failure | The sandbox was healthy and the synchronous harness probe returned a nonzero exit code. | Call the runtime owners. |

**Triage rule: errors = call ops; failures = call the runtime owners.**

The fixture builds and verifies the complete dependency closure before the
entrypoint probe. In particular, it runs `python -c "import agent_framework"`
inside the sandbox and treats a nonzero result as an environment error. This
prevents an incomplete closure from being misreported as an entrypoint defect.

## Dependency closure and budget

The live fixture uses local `pip install --target` to build the runtime's full
dependency closure, emits the archive size and ZIP entry count, creates a
deterministic `ZIP_STORED` archive, and sends that archive through one
`write_file` call. The sandbox extracts it into the `PYTHONPATH` directory
before the verification and probe commands run. No network package install is
attempted inside a sandbox.

The archive budget is 80 MiB. The measured closure contains 5,968 ZIP entries
and is 75.9 MiB. The budget is deliberately not expanded with compression:
`ZIP_STORED` keeps the payload digest-stable and the 80 MiB threshold is the
largest verified incompressible single write.

Measured direct-write timing was approximately:

| Payload | Time |
| --- | ---: |
| 1 MiB | 2.1 s |
| 16 MiB | 2.0 s |
| 64 MiB | 7.6 s |
| 80 MiB | 13.8 s |

There is roughly two seconds of fixed overhead per write, with throughput
leveling near 6–8 MiB/s. The archive is well below the service's 256 MiB file
cap and 65,535 ZIP-entry cap.

## Lifecycle and cleanup

Every smoke sandbox has dedicated CI owner and app hash labels, receives an
explicit per-sandbox lifecycle policy immediately after creation, and is
deleted in fixture teardown. The teardown first uses the live handle, then
falls back to direct ID deletion and finally an exact-label reconciliation.
The pipeline also runs a label-scoped reaper with `condition: always()` so a
failed job cannot leave an unbounded paid resource behind. The dedicated labels
must never be reused for non-CI sandboxes.

## CI prerequisites

The ACA smoke job is in `eng/templates/official/jobs/e2e-tests.yml`. It runs
only for scheduled and manually started builds and is currently
`continueOnError`, avoiding a permanently red pull-request check while the
entrypoint change lands.

Ops must provide:

1. A CI-dedicated ACA Sandbox Group in the intended region.
2. A federated Azure service connection, passed through the
   `acaServiceConnection` template parameter.
3. `SandboxGroup Data Owner` for that identity, scoped to the CI Sandbox Group.
4. Non-secret pipeline variables:
   `ACA_SANDBOX_GROUP_RESOURCE_ID`, `ACA_SANDBOX_DISK`, and
   `ACA_SANDBOX_REGION`.

The job first runs `Verify ACA sandbox group is reachable` through `AzureCLI@2`
with `AZURE_TOKEN_CREDENTIALS=dev`. That gives authentication, RBAC, and group
configuration failures an explicit operational diagnosis before pytest runs.
