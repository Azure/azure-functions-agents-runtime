# Engineering Scripts

This directory contains automation scripts for the azure-functions-agents-runtime repository.

## Available Scripts

### `generate_config_reference.py`

Auto-generates `docs/front-matter-reference.md` from Pydantic schema models.

**Purpose:** Keeps the API reference documentation in sync with the source code (`src/azure_functions_agents/config/schema.py`).

**Usage:**
```bash
# Generate/update the documentation
python eng/scripts/generate_config_reference.py

# Check if docs are up to date (CI mode)
python eng/scripts/generate_config_reference.py --check
```

**When to run:**
- After modifying `src/azure_functions_agents/config/schema.py`
- Automatically via pre-commit hook (if configured)
- During CI builds (verification mode)

**Integration:**
- **Pre-commit hook:** Configured in `.pre-commit-config.yaml`
- **CI pipeline:** Runs in `eng/templates/jobs/ci-tests.yml`

### `reap_aca_smoke_sandboxes.py`

Deletes leftover ACA smoke sandboxes created by the current Function App smoke
identity and its low-level CI companion tests.

**Purpose:** Cleanup safety net for the live ACA smoke job. It is label-scoped to
the BuildId-derived Function App owner/app hashes and the low-level CI
owner/app hashes, so it only ever deletes sandboxes this pipeline created. The
label selectors are imported from the test-support module
(`tests/live/aca_smoke_support.py`) rather than duplicated here.

**Usage:**
```bash
# Reap the current Function App and CI smoke sandbox families in the configured Sandbox Group
python eng/scripts/reap_aca_smoke_sandboxes.py
```

The Sandbox Group is read from the
`AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID` environment variable,
and its required data-plane region is read from
`AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_REGION`.

**When to run:**
- As an `always()` cleanup step after the ACA smoke job

**Integration:** The current-checkout E2E ACA job runs it with `always()`. It
deletes only the BuildId-derived Function App family and current-run low-level
family, including their snapshots, then fails if either remains.

### `aca_pr_smoke.py`

Runs the protected current-checkout ACA smoke preflight. It uses the controller
service connection to require exactly one guest UAMI with no system-assigned
identity and validates the protected group, disk, endpoint, and deployment
inputs. Guest model-only, no-state/no-group RBAC is an IaC/operations
prerequisite; the real model turn is positive access proof, not a negative
role-assignment attestation.

The retained `aca_deployed_qualification.py` and deployed suite helpers are
manual/local assets only pending the separate post-main qualification work.

### `aca_qualification_pipeline.py`

Packages, deploys, and verifies the deployed ACA qualification fixture
(`tests/live/apps/aca-qualification/`). The official E2E pipeline invokes these
commands, and they remain runnable by hand.

| Command | Purpose |
| --- | --- |
| `install-tooling` | Install the shared Python dependencies used by qualification runs |
| `stamp` | Write `BUILD_INFO.json` into the fixture app before packaging |
| `assemble` | Build the deployable upload: fixture source, the runtime wheel, the marker, and pinned requirements |
| `deploy` | Preflight deployment rights, configure the authored region, package and deploy the staged fixture, and add best-effort portal metadata |
| `check-build` | Verify lightweight in-package build ID, commit SHA, and Python-minor provenance |
| `sweep` | Report and delete resources older than six hours from the CI-dedicated Sandbox Group; never blocks qualification |

`assemble` requires exactly one runtime wheel in the build output; ambiguity is
a hard error rather than a silent "newest wins", because deploying the wrong
wheel is precisely the failure `check-build` exists to catch. Fixture
dependencies come from the single Oryx-compatible
`eng/constraints/aca-fixture-requirements.txt` export of `uv.lock`, valid for
both supported interpreter minors.

`check-build` is meaningful only because the marker is a *file inside the
deployed package*: a file can be served only if that package is genuinely on
disk, so a stale app cannot claim a build it is not running. An app setting or
resource tag could be changed without deploying anything.

The provenance is deliberately narrow. It does not cover the wheel digest, the
installed package version, a deploy-input manifest, the deployment-storage
chain, or rollback; those remain open under issue #166.

The canonical qualification uses N=5 with provisioning concurrency 1 and the
fixture's 120-second reclaim policy. Manual diagnostics retain load values 1–99
and provisioning values 1, 2, or 4; their operator owns shared-group quota and
cost. `aca_deployed_qualification.py` rejects N=100 before authentication or
provider work with `formal_n100_unsupported_by_qualification_fixture`. Formal
N=100 remains future human-only acceptance requiring a purpose-built workflow.

`sweep` uses the configured group resource ID and authored region, lists the
whole group, and requires
`--dedicated-group-scope exclusive-ci-qualification`. That literal acknowledges
an external infrastructure invariant; the data-plane API cannot verify that the
group is exclusive to CI, so using a shared group is unsafe. Unknown-age and
recent resources are retained. Inspection, unknown-age, and delete failures
emit Azure DevOps warnings, and the summary exposes incomplete and
delete-failure counts while remaining nonblocking. A stale sandbox that ACA
idle-delete removes between inventory and deletion is reported as
`already_absent`, without a failure warning.

The sweep is pre-run rather than a destructive post-run reaper. Current-run
qualification suites already assert their own cleanup; deleting immediately
afterward would mask idle-delete or controller-reconciliation failures. The next
run reports accumulated leftovers. A final report-only group audit is also
omitted because intentionally retained sessions could create false positives.
